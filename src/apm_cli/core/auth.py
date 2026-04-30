"""Centralized authentication resolution for APM CLI.

Every APM operation that touches a remote host MUST use AuthResolver.
Resolution is per-(host, org) pair, thread-safe, and cached per-process.

All token-bearing requests use HTTPS — that is the transport security
boundary.  Global env vars are tried for every host; if the token is
wrong for the target host, ``try_with_fallback`` retries with git
credential helpers automatically.

Usage::

    resolver = AuthResolver()
    ctx = resolver.resolve("github.com", org="microsoft")
    # ctx.token, ctx.source, ctx.token_type, ctx.host_info, ctx.git_env

For dependencies::

    ctx = resolver.resolve_for_dep(dep_ref)

For operations with automatic auth/unauth fallback::

    result = resolver.try_with_fallback(
        "github.com", lambda token, env: download(token, env),
        org="microsoft",
    )
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, TypeVar  # noqa: F401

from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.utils.github_host import (
    default_host,
    is_azure_devops_hostname,
    is_github_hostname,  # noqa: F401
    is_valid_fqdn,
)

if TYPE_CHECKING:
    from apm_cli.models.dependency.reference import DependencyReference

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostInfo:
    """Immutable description of a remote Git host."""

    host: str
    kind: str  # "github" | "ghe_cloud" | "ghes" | "ado" | "generic"
    has_public_repos: bool
    api_base: str
    port: int | None = None  # Non-standard git port (e.g. 7999 for Bitbucket DC)

    @property
    def display_name(self) -> str:
        """``host:port`` when a custom port is set, else bare ``host``.

        Use this wherever user-facing text identifies the host — errors, log
        lines, diagnostic output. Bare ``host`` in those places misleads
        users when port is what actually differentiates the target.

        Uses ``is not None`` (not truthy) for symmetry with the
        ``host_info.port is not None`` checks elsewhere in the resolver and
        to avoid silently dropping any non-default integer ports.
        """
        return f"{self.host}:{self.port}" if self.port is not None else self.host


@dataclass
class AuthContext:
    """Resolved authentication for a single (host, org) pair.

    Treat as immutable after construction — fields are never mutated.
    Not frozen because ``git_env`` is a dict (unhashable).
    """

    token: str | None = field(repr=False)  # B1 #852: never expose JWT/PAT via repr()
    source: str  # e.g. "GITHUB_APM_PAT_ORGNAME", "GITHUB_TOKEN", "none"
    token_type: str  # "fine-grained", "classic", "oauth", "github-app", "unknown"
    host_info: HostInfo
    git_env: dict = field(compare=False, repr=False)
    auth_scheme: str = (
        "basic"  # "basic" | "bearer". Determines how _build_git_env injects credentials.
    )


# ---------------------------------------------------------------------------
# AuthResolver
# ---------------------------------------------------------------------------


class AuthResolver:
    """Single source of truth for auth resolution.

    Every APM operation that touches a remote host MUST use this class.
    Resolution is per-(host, org) pair, thread-safe, cached per-process.
    """

    def __init__(
        self,
        token_manager: GitHubTokenManager | None = None,
        logger: object | None = None,
    ):
        self._token_manager = token_manager or GitHubTokenManager()
        self._cache: dict[tuple, AuthContext] = {}
        self._lock = threading.Lock()
        # F2/F3 #852: optional logger lets the install command route the
        # verbose auth-source line through CommandLogger and the deferred
        # stale-PAT warning through DiagnosticCollector. When unset (CLI
        # paths that do not construct an InstallLogger), behaviour falls
        # back to the previous direct-write paths.
        self._logger = logger
        # F5 #852: pre-init the per-host dedup set so callers do not need
        # the prior hasattr() guard.
        self._verbose_auth_logged_hosts: set = set()

    def set_logger(self, logger: object) -> None:
        """Wire a CommandLogger (or InstallLogger) into the resolver after
        construction. Idempotent. Used by the install command, which builds
        the logger before it knows it needs an AuthResolver elsewhere."""
        self._logger = logger

    # -- host classification ------------------------------------------------

    @staticmethod
    def classify_host(host: str, port: int | None = None) -> HostInfo:
        """Return a ``HostInfo`` describing *host*.

        ``port`` is carried through onto the returned ``HostInfo`` so that
        downstream code (cache keys, credential-helper input, error text)
        can discriminate between the same hostname on different ports.
        Host-kind classification itself is transport-agnostic -- the port
        never influences whether a host is GitHub/GHES/ADO/generic.
        """
        h = host.lower()

        if h == "github.com":
            return HostInfo(
                host=host,
                kind="github",
                has_public_repos=True,
                api_base="https://api.github.com",
                port=port,
            )

        if h.endswith(".ghe.com"):
            return HostInfo(
                host=host,
                kind="ghe_cloud",
                has_public_repos=False,
                api_base=f"https://{host}/api/v3",
                port=port,
            )

        if is_azure_devops_hostname(host):
            return HostInfo(
                host=host,
                kind="ado",
                has_public_repos=True,
                api_base="https://dev.azure.com",
                port=port,
            )

        # GHES: GITHUB_HOST is set to a non-github.com, non-ghe.com FQDN
        ghes_host = os.environ.get("GITHUB_HOST", "").lower()
        if (
            ghes_host
            and ghes_host == h
            and ghes_host != "github.com"
            and not ghes_host.endswith(".ghe.com")
        ):
            if is_valid_fqdn(ghes_host):
                return HostInfo(
                    host=host,
                    kind="ghes",
                    has_public_repos=True,
                    api_base=f"https://{host}/api/v3",
                    port=port,
                )

        # Generic FQDN (GitLab, Bitbucket, self-hosted, etc.)
        return HostInfo(
            host=host,
            kind="generic",
            has_public_repos=True,
            api_base=f"https://{host}/api/v3",
            port=port,
        )

    # -- token type detection -----------------------------------------------

    @staticmethod
    def detect_token_type(token: str) -> str:
        """Classify a token string by its prefix.

        Note: EMU (Enterprise Managed Users) tokens use standard PAT
        prefixes (``ghp_`` or ``github_pat_``).  There is no prefix that
        identifies a token as EMU-scoped — that's a property of the
        account, not the token format.

        Prefix reference (docs.github.com):
        - ``github_pat_`` → fine-grained PAT
        - ``ghp_``        → classic PAT
        - ``ghu_``        → OAuth user-to-server (e.g. ``gh auth login``)
        - ``gho_``        → OAuth app token
        - ``ghs_``        → GitHub App installation (server-to-server)
        - ``ghr_``        → GitHub App refresh token
        """
        if token.startswith("github_pat_"):
            return "fine-grained"
        if token.startswith("ghp_"):
            return "classic"
        if token.startswith("ghu_"):
            return "oauth"
        if token.startswith("gho_"):
            return "oauth"
        if token.startswith("ghs_"):
            return "github-app"
        if token.startswith("ghr_"):
            return "github-app"
        return "unknown"

    # -- core resolution ----------------------------------------------------

    def resolve(
        self,
        host: str,
        org: str | None = None,
        *,
        port: int | None = None,
    ) -> AuthContext:
        """Resolve auth for *(host, port, org)*.  Cached & thread-safe.

        ``port`` discriminates the cache key so that the same hostname on
        different ports (e.g. Bitbucket Datacenter with SSH on 7999 and a
        second HTTPS instance on 7990) never collapses to a single
        ``AuthContext``. Also flows into ``git credential fill`` so git's
        helpers can return port-specific credentials.
        """
        key = (
            host.lower() if host else host,
            port,
            org.lower() if org else "",
        )
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

            # Hold lock during entire credential resolution to prevent duplicate
            # credential-helper popups when parallel downloads resolve the same
            # (host, port, org) concurrently.  The first caller fills the cache;
            # all subsequent callers for the same key become O(1) cache hits.
            # Bounded by APM_GIT_CREDENTIAL_TIMEOUT (default 60s). No deadlock
            # risk: single lock, never nested.
            host_info = self.classify_host(host, port=port)
            token, source, scheme = self._resolve_token(host_info, org)
            token_type = self.detect_token_type(token) if token else "unknown"
            git_env = self._build_git_env(token, scheme=scheme, host_kind=host_info.kind)

            ctx = AuthContext(
                token=token,
                source=source,
                token_type=token_type,
                host_info=host_info,
                git_env=git_env,
                auth_scheme=scheme,
            )
            self._cache[key] = ctx
            return ctx

    def resolve_for_dep(self, dep_ref: DependencyReference) -> AuthContext:
        """Resolve auth from a ``DependencyReference``.

        Threads ``dep_ref.port`` through so the resolver (and any downstream
        git credential helper) can discriminate same-host multi-port setups.
        """
        host = dep_ref.host or default_host()
        org: str | None = None
        if dep_ref.repo_url:
            parts = dep_ref.repo_url.split("/")
            if parts:
                org = parts[0]
        return self.resolve(host, org, port=dep_ref.port)

    # -- fallback strategy --------------------------------------------------

    def try_with_fallback(
        self,
        host: str,
        operation: Callable[..., T],
        *,
        org: str | None = None,
        port: int | None = None,
        unauth_first: bool = False,
        verbose_callback: Callable[[str], None] | None = None,
    ) -> T:
        """Execute *operation* with automatic auth/unauth fallback.

        Parameters
        ----------
        host:
            Target git host.
        operation:
            ``operation(token, git_env) -> T`` — the work to do.
        org:
            Optional organisation for per-org token lookup.
        unauth_first:
            If *True*, try unauthenticated first (saves rate limits, EMU-safe).
        verbose_callback:
            Called with a human-readable step description at each attempt.

        When the resolved token comes from a global env var and fails
        (e.g. a github.com PAT tried on ``*.ghe.com``), the method
        retries with ``git credential fill`` before giving up.
        """
        auth_ctx = self.resolve(host, org, port=port)
        host_info = auth_ctx.host_info
        git_env = auth_ctx.git_env

        def _log(msg: str) -> None:
            if verbose_callback:
                verbose_callback(msg)

        def _try_credential_fallback(exc: Exception) -> T:
            """Retry with git-credential-fill when an env-var token fails."""
            if auth_ctx.source in ("git-credential-fill", "none"):
                raise exc
            # ADO uses ADO_APM_PAT + AAD bearer fallback; credential fill is out of scope.
            if host_info.kind == "ado":
                raise exc
            _log(
                f"Token from {auth_ctx.source} failed, trying git credential fill "
                f"for {host_info.display_name}"
            )
            cred = self._token_manager.resolve_credential_from_git(
                host_info.host, port=host_info.port
            )
            if cred:
                return operation(cred, self._build_git_env(cred))
            raise exc

        # ADO bearer fallback machinery (PAT was tried first; bearer is the safety net)
        ado_bearer_fallback_available = (
            auth_ctx.host_info.kind == "ado" and auth_ctx.source == "ADO_APM_PAT"
        )

        def _try_ado_bearer_fallback(exc: Exception) -> T:
            """Retry ADO operation with AAD bearer when PAT fails with 401."""
            if not ado_bearer_fallback_available:
                raise exc
            exc_msg = str(exc)
            if (
                "401" not in exc_msg
                and "Unauthorized" not in exc_msg
                and "Authentication failed" not in exc_msg
            ):
                raise exc
            from apm_cli.core.azure_cli import AzureCliBearerError, get_bearer_provider

            provider = get_bearer_provider()
            if not provider.is_available():
                raise exc
            try:
                bearer = provider.get_bearer_token()
                bearer_env = self._build_git_env(bearer, scheme="bearer", host_kind="ado")
                result = operation(bearer, bearer_env)
                # Success on fallback -- emit deferred diagnostic warning
                self.emit_stale_pat_diagnostic(auth_ctx.host_info.display_name)
                return result
            except AzureCliBearerError:
                pass  # Bearer acquisition itself failed; fall through to original error
            except Exception:
                # Bearer also failed (Case 4). Re-raise the ORIGINAL PAT exception.
                pass
            raise exc

        # Hosts that never have public repos -> auth-only
        if host_info.kind == "ghe_cloud":
            _log(f"Auth-only attempt for {host_info.kind} host {host_info.display_name}")
            try:
                return operation(auth_ctx.token, git_env)
            except Exception as exc:
                return _try_credential_fallback(exc)

        # ADO: auth-first with bearer fallback when PAT fails
        if host_info.kind == "ado":
            _log(f"Auth-only attempt for {host_info.kind} host {host_info.display_name}")
            try:
                return operation(auth_ctx.token, git_env)
            except Exception as exc:
                return _try_ado_bearer_fallback(exc)

        if unauth_first:
            # Validation path: save rate limits, EMU-safe
            try:
                _log(f"Trying unauthenticated access to {host_info.display_name}")
                return operation(None, git_env)
            except Exception:
                if auth_ctx.token:
                    _log(f"Unauthenticated failed, retrying with token (source: {auth_ctx.source})")
                    try:
                        return operation(auth_ctx.token, git_env)
                    except Exception as exc:
                        return _try_credential_fallback(exc)
                raise
        # Download path: auth-first for higher rate limits
        elif auth_ctx.token:
            try:
                _log(
                    f"Trying authenticated access to {host_info.display_name} "
                    f"(source: {auth_ctx.source})"
                )
                return operation(auth_ctx.token, git_env)
            except Exception as exc:
                if host_info.has_public_repos:
                    _log("Authenticated failed, retrying without token")
                    try:
                        return operation(None, git_env)
                    except Exception:
                        return _try_credential_fallback(exc)
                return _try_credential_fallback(exc)
        else:
            _log(f"No token available, trying unauthenticated access to {host_info.display_name}")
            return operation(None, git_env)

    # -- error context ------------------------------------------------------

    def build_error_context(
        self,
        host: str,
        operation: str,
        org: str | None = None,
        *,
        port: int | None = None,
        dep_url: str | None = None,
    ) -> str:
        """Build an actionable error message for auth failures."""
        auth_ctx = self.resolve(host, org, port=port)
        host_info = auth_ctx.host_info
        display = host_info.display_name

        # --- ADO-specific error cases ---
        if host_info.kind == "ado":
            from apm_cli.core.azure_cli import get_bearer_provider

            provider = get_bearer_provider()
            az_available = provider.is_available()
            pat_set = bool(os.environ.get("ADO_APM_PAT"))

            org_part = org or ""
            if not org_part:
                source_url = dep_url or ""
                if source_url:
                    parts = source_url.replace("https://", "").split("/")
                    if len(parts) >= 2 and (
                        parts[0] in ("dev.azure.com",) or parts[0].endswith(".visualstudio.com")
                    ):
                        org_part = parts[1] if len(parts) > 1 else ""

            token_url = (
                f"https://dev.azure.com/{org_part}/_usersSettings/tokens"
                if org_part
                else "https://dev.azure.com/<org>/_usersSettings/tokens"
            )

            if pat_set:
                if az_available:
                    # Case 4: PAT and bearer were both available; both attempts
                    # failed. We may not have observed an explicit 401 (could be
                    # a 404, a network error, etc.) so the wording stays
                    # tentative -- see #856 review C6.
                    return (
                        f"\n    ADO_APM_PAT is set, and Azure CLI credentials may also be available,\n"  # noqa: F541
                        f"    but the Azure DevOps request still failed.\n\n"  # noqa: F541
                        f"    If this is an authentication failure, the PAT may be expired, revoked,\n"  # noqa: F541
                        f"    or scoped to a different org, and Azure CLI credentials may need to\n"  # noqa: F541
                        f"    be refreshed.\n\n"  # noqa: F541
                        f"    To fix:\n"  # noqa: F541
                        f"      1. Unset the PAT to test Azure CLI auth only:  unset ADO_APM_PAT\n"  # noqa: F541
                        f"      2. Re-authenticate Azure CLI if needed:        az login\n"  # noqa: F541
                        f"      3. Retry:                                       apm install\n\n"  # noqa: F541
                        f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"  # noqa: F541
                    )
                # PAT set but rejected, no az -> bare PAT failure
                return (
                    f"\n    ADO_APM_PAT is set, but the Azure DevOps request failed.\n"
                    f"    If this is an authentication failure, the token may be expired,\n"
                    f"    revoked, or scoped to a different org.\n\n"
                    f"    Generate a new PAT at {token_url}\n"
                    f"    with Code (Read) scope.\n\n"
                    f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
                )

            # No PAT set
            if not az_available:
                # Case 1: no az, no PAT
                return (
                    f"\n    Azure DevOps requires authentication. You have two options:\n\n"
                    f"    1. Install Azure CLI and sign in (recommended for Entra ID users):\n"
                    f"         https://aka.ms/installazurecliwindows  (or 'brew install azure-cli')\n"
                    f"         az login\n"
                    f"         apm install                   # retry -- no env var needed\n\n"
                    f"    2. Use a Personal Access Token:\n"
                    f"         export ADO_APM_PAT=your_token\n"
                    f"         (Create one at {token_url} with Code (Read) scope.)\n\n"
                    f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
                )

            # az is available; check if logged in by trying to get tenant
            tenant = provider.get_current_tenant_id()
            if tenant is None:
                # Case 3: az present, not logged in
                return (
                    f"\n    Azure DevOps requires authentication. You have two options:\n\n"  # noqa: F541
                    f"    1. Sign in with Azure CLI (recommended for Entra ID users):\n"  # noqa: F541
                    f"         az login\n"  # noqa: F541
                    f"         apm install                   # retry -- no env var needed\n\n"  # noqa: F541
                    f"    2. Use a Personal Access Token:\n"  # noqa: F541
                    f"         export ADO_APM_PAT=your_token\n\n"  # noqa: F541
                    f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"  # noqa: F541
                )

            # Case 2: az returned token (tenant known) but ADO rejected it
            return (
                f"\n    Your az cli session (tenant: {tenant}) returned a bearer token,\n"
                f"    but Azure DevOps rejected it (HTTP 401).\n\n"
                f"    Check that you are signed into the correct tenant:\n"
                f"      az account show\n"
                f"      az login --tenant <correct-tenant-id>\n\n"
                f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
            )

        # --- Non-ADO error paths (unchanged) ---
        lines: list[str] = [f"Authentication failed for {operation} on {display}."]

        if auth_ctx.token:
            lines.append(
                f"Token was provided (source: {auth_ctx.source}, type: {auth_ctx.token_type})."
            )
            if host_info.kind == "ghe_cloud":
                lines.append(
                    "GHE Cloud Data Residency hosts (*.ghe.com) require "
                    "enterprise-scoped tokens. Ensure your PAT is authorized "
                    "for this enterprise."
                )
            elif host.lower() == "github.com":
                lines.append(
                    "If your organization uses SAML SSO or is an EMU org, "
                    "ensure your PAT is authorized at "
                    "https://github.com/settings/tokens"
                )
            else:
                lines.append(
                    "If your organization uses SAML SSO, you may need to "
                    "authorize your token at https://github.com/settings/tokens"
                )
        else:
            lines.append("No token available.")
            lines.append("Set GITHUB_APM_PAT or GITHUB_TOKEN, or run 'gh auth login'.")

        if org and host_info.kind != "ado":
            lines.append(
                f"If packages span multiple organizations, set per-org tokens: "
                f"GITHUB_APM_PAT_{_org_to_env_suffix(org)}"
            )

        # When a custom port is in play, helpers that key by hostname alone
        # (some `gh` integrations, older keychain backends) can silently
        # return the wrong credential. Point the user at the concrete fix.
        if host_info.port is not None:
            lines.append(
                f"[i] Host '{display}' -- verify your credential helper stores per-port entries "
                f"(some helpers key by host only)."
            )

        lines.append("Run with --verbose for detailed auth diagnostics.")
        return "\n".join(lines)

    # -- internals ----------------------------------------------------------

    def _resolve_token(self, host_info: HostInfo, org: str | None) -> tuple[str | None, str, str]:
        """Walk the token resolution chain.  Returns (token, source, scheme).

        Resolution order (GitHub-like hosts):
        1. Per-org env var ``GITHUB_APM_PAT_{ORG}`` (any host)
        2. Global env vars ``GITHUB_APM_PAT`` -> ``GITHUB_TOKEN`` -> ``GH_TOKEN``
           (any host -- if the token is wrong for the target host,
           ``try_with_fallback`` retries with git credentials)
        3. Git credential helper (any host except ADO)

        Resolution order (ADO):
        1. ``ADO_APM_PAT`` env var -> scheme ``"basic"``
        2. AAD bearer via ``az cli`` -> scheme ``"bearer"``
        3. None -> source ``"none"``

        All token-bearing requests use HTTPS, which is the transport
        security boundary.  Host-gating global env vars is unnecessary
        and creates DX friction for multi-host setups.
        """
        if host_info.kind == "ado":
            # ADO resolution chain: PAT env -> AAD bearer -> none
            pat = os.environ.get("ADO_APM_PAT")
            if pat:
                return pat, "ADO_APM_PAT", "basic"
            # Try AAD bearer via az cli (lazy import to avoid module-load cost on non-ADO paths)
            from apm_cli.core.azure_cli import AzureCliBearerError, get_bearer_provider

            provider = get_bearer_provider()
            if provider.is_available():
                try:
                    bearer = provider.get_bearer_token()
                    return bearer, GitHubTokenManager.ADO_BEARER_SOURCE, "bearer"
                except AzureCliBearerError:
                    # az is on PATH but token acquisition failed (e.g., not logged in).
                    # Fall through to token=None; build_error_context will render Case 3.
                    pass
            return None, "none", "basic"

        # ADO uses ADO_APM_PAT (single var) + AAD bearer fallback;
        # per-org vars and credential fill are out of scope.

        # 1. Per-org env var (GitHub-like hosts only)
        if org and host_info.kind not in ("ado",):
            env_name = f"GITHUB_APM_PAT_{_org_to_env_suffix(org)}"
            token = os.environ.get(env_name)
            if token:
                return token, env_name, "basic"

        # 2. Global env var chain (any host)
        purpose = self._purpose_for_host(host_info)
        token = self._token_manager.get_token_for_purpose(purpose)
        if token:
            source = self._identify_env_source(purpose)
            return token, source, "basic"

        # 3. Git credential helper (not for ADO)
        if host_info.kind not in ("ado",):
            credential = self._token_manager.resolve_credential_from_git(
                host_info.host, port=host_info.port
            )
            if credential:
                return credential, "git-credential-fill", "basic"

        return None, "none", "basic"

    @staticmethod
    def _purpose_for_host(host_info: HostInfo) -> str:
        if host_info.kind == "ado":
            return "ado_modules"
        return "modules"

    def _identify_env_source(self, purpose: str) -> str:
        """Return the name of the first env var that matched for *purpose*."""
        for var in self._token_manager.TOKEN_PRECEDENCE.get(purpose, []):
            if os.environ.get(var):
                return var
        return "env"

    @staticmethod
    def _build_git_env(
        token: str | None = None,
        *,
        scheme: str = "basic",
        host_kind: str = "github",
    ) -> dict:
        """Pre-built env dict for subprocess git calls.

        For ADO bearer tokens (scheme='bearer'), injects an Authorization header
        via GIT_CONFIG_COUNT/KEY/VALUE env vars (see github_host.build_ado_bearer_git_env).
        For all other cases, behavior is unchanged.
        """
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        # On Windows, GIT_ASKPASS='' can cause issues; use 'echo' instead
        env["GIT_ASKPASS"] = "" if sys.platform != "win32" else "echo"
        if scheme == "bearer" and token and host_kind == "ado":
            # B2 #852: skip GIT_TOKEN for bearer scheme -- the JWT is injected via
            # GIT_CONFIG_VALUE_0 only; GIT_TOKEN here would leak it into every
            # child-process env (visible in /proc/<pid>/environ, ps eww).
            from apm_cli.utils.github_host import build_ado_bearer_git_env

            env.update(build_ado_bearer_git_env(token))
        elif token:
            env["GIT_TOKEN"] = token
        return env

    def emit_stale_pat_diagnostic(self, host_display: str) -> None:
        """Emit a [!] warning when PAT was rejected but bearer succeeded.

        F3 #852: when an InstallLogger is wired via :meth:`set_logger`, the
        warning is collected by its DiagnosticCollector so it appears in the
        install summary. Without a logger (e.g. unit tests) we fall back to
        the inline ``_rich_warning`` emission for backwards compatibility.

        Naming: previously ``_emit_stale_pat_diagnostic`` (private). Public
        now (#856 follow-up C9) so external modules (validation.py,
        github_downloader.py) do not reach into the underscore API.
        """
        msg = f"ADO_APM_PAT was rejected for {host_display} (HTTP 401); fell back to az cli bearer."
        detail = "Consider unsetting the stale variable."
        diagnostics = self._diagnostics_or_none()
        if diagnostics is not None:
            diagnostics.warn(msg, detail=detail)
            return
        try:
            from apm_cli.utils.console import _rich_warning

            _rich_warning(msg, symbol="warning")
            _rich_warning(f"    {detail}", symbol="warning")
        except ImportError:
            pass  # console module not importable in some test contexts

    # Backwards-compat alias for any in-tree caller still importing the
    # private name. Safe to remove once all callers move to the public name.
    _emit_stale_pat_diagnostic = emit_stale_pat_diagnostic

    def _diagnostics_or_none(self):
        """Return the wired logger's DiagnosticCollector, or None."""
        if self._logger is None:
            return None
        try:
            return self._logger.diagnostics
        except AttributeError:
            return None

    def notify_auth_source(self, host_display: str, ctx) -> None:
        """Emit the verbose auth-source line for ``host_display`` exactly once.

        F2 #852: routes through CommandLogger when wired (so the line obeys
        the same verbose channel as every other diagnostic), and falls back
        to a direct stderr write when no logger is set so the existing
        bearer e2e tests keep working.
        """
        host_key = (host_display or "").lower()
        if not host_key or host_key in self._verbose_auth_logged_hosts:
            return
        self._verbose_auth_logged_hosts.add(host_key)
        if ctx is None or getattr(ctx, "source", "none") == "none":
            return
        if getattr(ctx, "auth_scheme", None) == "bearer":
            line = f"  [i] {host_key} -- using bearer from az cli (source: {ctx.source})"
        else:
            line = f"  [i] {host_key} -- token from {ctx.source}"
        if self._logger is not None and getattr(self._logger, "verbose", False):
            try:
                from apm_cli.utils.console import _rich_echo

                _rich_echo(line, color="dim")
                return
            except ImportError:
                pass
        # No logger wired -- the install path always wires one in the
        # bearer branch, so this fallback only fires in unit-test contexts
        # that opt-in via APM_VERBOSE=1.
        sys.stderr.write(line + "\n")

    def execute_with_bearer_fallback(
        self,
        dep_ref,
        primary_op,
        bearer_op,
        is_auth_failure,
    ):
        """Run ``primary_op``; on a confirmed auth failure for ADO, retry
        via AAD bearer using ``bearer_op(bearer_token)``.

        F1 #852: collapses the duplicated PAT->bearer fallback that used to
        live in both :meth:`try_with_fallback` (clone path) and
        ``install/validation.py::_validate_package_exists`` (ls-remote path).

        Args:
            dep_ref: DependencyReference -- only used to detect ADO and to
                supply the host display string for the deferred [!] warning.
            primary_op: Callable returning the primary outcome (typically a
                ``subprocess.CompletedProcess`` or any object). Whatever it
                returns is returned as-is on the no-fallback paths.
            bearer_op: Callable[[str], object] taking the freshly-acquired
                bearer JWT and returning the same outcome shape as
                ``primary_op``. Only invoked on a confirmed auth failure.
            is_auth_failure: Callable[[outcome], bool]. Receives whatever
                ``primary_op`` returned and decides whether the failure
                signature matches an ADO auth rejection (HTTP 401, "Authentication
                failed", etc.). Caller knows the outcome shape; resolver does not.

        Returns:
            The outcome of ``bearer_op`` on successful fallback, otherwise
            the outcome of ``primary_op``. Never raises (exceptions from
            ``bearer_op`` are swallowed and the primary outcome is returned
            so the caller's existing error rendering still runs).
        """
        primary = primary_op()
        if dep_ref is None or not getattr(dep_ref, "is_azure_devops", lambda: False)():
            return primary
        if not is_auth_failure(primary):
            return primary
        try:
            from apm_cli.core.azure_cli import AzureCliBearerError, get_bearer_provider
        except ImportError:
            return primary
        provider = get_bearer_provider()
        if not provider.is_available():
            return primary
        try:
            bearer = provider.get_bearer_token()
        except AzureCliBearerError:
            return primary
        try:
            fallback = bearer_op(bearer)
        except Exception:
            return primary
        if fallback is None or is_auth_failure(fallback):
            return primary
        host_display = getattr(dep_ref, "host", None) or "dev.azure.com"
        self.emit_stale_pat_diagnostic(host_display)
        return fallback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _org_to_env_suffix(org: str) -> str:
    """Convert an org name to an env-var suffix (upper-case, hyphens → underscores)."""
    return org.upper().replace("-", "_")
