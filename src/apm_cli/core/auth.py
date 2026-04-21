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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional, TypeVar

from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.utils.github_host import (
    default_host,
    is_azure_devops_hostname,
    is_github_hostname,
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
    port: Optional[int] = None  # Non-standard git port (e.g. 7999 for Bitbucket DC)

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

    token: Optional[str]
    source: str  # e.g. "GITHUB_APM_PAT_ORGNAME", "GITHUB_TOKEN", "none"
    token_type: str  # "fine-grained", "classic", "oauth", "github-app", "unknown"
    host_info: HostInfo
    git_env: dict = field(compare=False, repr=False)


# ---------------------------------------------------------------------------
# AuthResolver
# ---------------------------------------------------------------------------

class AuthResolver:
    """Single source of truth for auth resolution.

    Every APM operation that touches a remote host MUST use this class.
    Resolution is per-(host, org) pair, thread-safe, cached per-process.
    """

    def __init__(self, token_manager: Optional[GitHubTokenManager] = None):
        self._token_manager = token_manager or GitHubTokenManager()
        self._cache: dict[tuple, AuthContext] = {}
        self._lock = threading.Lock()

    # -- host classification ------------------------------------------------

    @staticmethod
    def classify_host(host: str, port: Optional[int] = None) -> HostInfo:
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
        if ghes_host and ghes_host == h and ghes_host != "github.com" and not ghes_host.endswith(".ghe.com"):
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
        org: Optional[str] = None,
        *,
        port: Optional[int] = None,
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
            token, source = self._resolve_token(host_info, org)
            token_type = self.detect_token_type(token) if token else "unknown"
            git_env = self._build_git_env(token)

            ctx = AuthContext(
                token=token,
                source=source,
                token_type=token_type,
                host_info=host_info,
                git_env=git_env,
            )
            self._cache[key] = ctx
            return ctx

    def resolve_for_dep(self, dep_ref: "DependencyReference") -> AuthContext:
        """Resolve auth from a ``DependencyReference``.

        Threads ``dep_ref.port`` through so the resolver (and any downstream
        git credential helper) can discriminate same-host multi-port setups.
        """
        host = dep_ref.host or default_host()
        org: Optional[str] = None
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
        org: Optional[str] = None,
        port: Optional[int] = None,
        unauth_first: bool = False,
        verbose_callback: Optional[Callable[[str], None]] = None,
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

        # Hosts that never have public repos → auth-only
        if host_info.kind in ("ghe_cloud", "ado"):
            _log(f"Auth-only attempt for {host_info.kind} host {host_info.display_name}")
            try:
                return operation(auth_ctx.token, git_env)
            except Exception as exc:
                return _try_credential_fallback(exc)

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
        else:
            # Download path: auth-first for higher rate limits
            if auth_ctx.token:
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
        org: Optional[str] = None,
        *,
        port: Optional[int] = None,
    ) -> str:
        """Build an actionable error message for auth failures."""
        auth_ctx = self.resolve(host, org, port=port)
        host_info = auth_ctx.host_info
        display = host_info.display_name
        lines: list[str] = [f"Authentication failed for {operation} on {display}."]

        if auth_ctx.token:
            lines.append(f"Token was provided (source: {auth_ctx.source}, type: {auth_ctx.token_type}).")
            if host_info.kind == "ghe_cloud":
                lines.append(
                    "GHE Cloud Data Residency hosts (*.ghe.com) require "
                    "enterprise-scoped tokens. Ensure your PAT is authorized "
                    "for this enterprise."
                )
            elif host_info.kind == "ado":
                lines.append(
                    "Verify your ADO_APM_PAT is valid and has Code (Read) scope."
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
            if host_info.kind == "ado":
                lines.append("Azure DevOps authentication required.")
                lines.append(
                    "Set the ADO_APM_PAT environment variable with a PAT that has Code (Read) scope."
                )
            else:
                lines.append("No token available.")
                lines.append(
                    "Set GITHUB_APM_PAT or GITHUB_TOKEN, or run 'gh auth login'."
                )

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

    def _resolve_token(
        self, host_info: HostInfo, org: Optional[str]
    ) -> tuple[Optional[str], str]:
        """Walk the token resolution chain.  Returns (token, source).

        Resolution order:
        1. Per-org env var ``GITHUB_APM_PAT_{ORG}`` (any host)
        2. Global env vars ``GITHUB_APM_PAT`` → ``GITHUB_TOKEN`` → ``GH_TOKEN``
           (any host — if the token is wrong for the target host,
           ``try_with_fallback`` retries with git credentials)
        3. Git credential helper (any host except ADO)

        All token-bearing requests use HTTPS, which is the transport
        security boundary.  Host-gating global env vars is unnecessary
        and creates DX friction for multi-host setups.
        """
        # 1. Per-org env var (GitHub-like hosts only — ADO uses ADO_APM_PAT)
        if org and host_info.kind not in ("ado",):
            env_name = f"GITHUB_APM_PAT_{_org_to_env_suffix(org)}"
            token = os.environ.get(env_name)
            if token:
                return token, env_name

        # 2. Global env var chain (any host)
        purpose = self._purpose_for_host(host_info)
        token = self._token_manager.get_token_for_purpose(purpose)
        if token:
            source = self._identify_env_source(purpose)
            return token, source

        # 3. Git credential helper (not for ADO — uses its own PAT)
        if host_info.kind not in ("ado",):
            credential = self._token_manager.resolve_credential_from_git(
                host_info.host, port=host_info.port
            )
            if credential:
                return credential, "git-credential-fill"

        return None, "none"

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
    def _build_git_env(token: Optional[str] = None) -> dict:
        """Pre-built env dict for subprocess git calls."""
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        # On Windows, GIT_ASKPASS='' can cause issues; use 'echo' instead
        env["GIT_ASKPASS"] = "" if sys.platform != "win32" else "echo"
        if token:
            env["GIT_TOKEN"] = token
        return env


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _org_to_env_suffix(org: str) -> str:
    """Convert an org name to an env-var suffix (upper-case, hyphens → underscores)."""
    return org.upper().replace("-", "_")
