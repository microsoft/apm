"""Centralized authentication resolution for APM CLI.

Every APM operation that touches a remote host MUST use AuthResolver.
Resolution is per-(host, org) pair, thread-safe, and cached per-process.

All token-bearing requests use HTTPS -- that is the transport security
boundary. Token environment variables are chosen by host class (GitHub-class,
GitLab, generic, or ADO); when a resolved token fails against the target host,
``try_with_fallback`` retries with git credential helpers where applicable.

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

import base64
import logging
import os
import sys
import threading
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple, TypeVar
from urllib.parse import urlsplit

from apm_cli.core.auth_chain import (  # AdoAuthChainExhaustedError: re-exported
    STEP_BEARER,
    AdoAuthChainExhaustedError,  # noqa: F401
    AuthAttemptLog,
    HelperCredentialCache,
    authorization_headers,
    run_ado_chain,
)
from apm_cli.core.auth_outcome import (
    SECRET_RE,
    AuthAttemptFailed,
    AuthOutcomeKind,
    classify_auth_outcome,
    redact_secrets,
    sanitize_diagnostic_detail,
)
from apm_cli.core.host_providers import (
    HOST_PROVIDERS,
    classify_host_provider,
    git_transport_cache_key,
    git_transport_policy,
)
from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.models.dependency.host_virtual import (
    dependency_repository_owner,
    repository_owner_from_reference_text,
)
from apm_cli.utils.github_host import (
    default_host,
    is_gitlab_hostname,
)

if TYPE_CHECKING:
    from apm_cli.models.dependency.reference import DependencyReference

T = TypeVar("T")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Secret redaction -- applied by SecretRedactionFilter to all debug records
# ---------------------------------------------------------------------------

# The redaction pattern is owned by ``core/auth_outcome.py`` so the auth chain,
# diagnostics, and this logging filter share one definition.
_SECRET_RE = SECRET_RE


def _redact_secrets(text: str) -> str:
    """Replace obvious credential patterns in ``text`` with ``[REDACTED]``.

    Called by :class:`SecretRedactionFilter` before log records are emitted.
    Also safe to call directly in tests.  Preserves text that contains no
    secret patterns so non-sensitive messages are returned verbatim.
    """
    return redact_secrets(text)


class SecretRedactionFilter(logging.Filter):
    """Logging filter that redacts secret patterns from all emitted log records.

    Install on the ``apm_cli`` logger (or a sub-logger) when debug logging is
    enabled so that exception messages carrying HTTP client error strings --
    which some libraries embed auth headers or token values in -- do not leak
    credentials into the debug stream.

    Usage::

        logging.getLogger("apm_cli").addFilter(SecretRedactionFilter())
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = _redact_secrets(msg)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
            if record.exc_info is not None:
                formatted = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = _redact_secrets(formatted)
                record.exc_info = None
        except Exception:
            pass
        return True


_PORT_CREDENTIAL_DOCS_URL = (
    "https://microsoft.github.io/apm/getting-started/authentication/"
    "#custom-port-hosts-and-per-port-credentials"
)
# Git localises its diagnostics through gettext, but APM classifies clone
# failures by matching English signal strings (see
# ``AuthResolver.is_public_github_auth_failure``). A translated stderr makes an
# authentication failure unrecognisable, which suppresses the token retry. The
# ``C`` locale has no message catalogue, so git emits its untranslated source
# strings. ``LANGUAGE`` takes precedence over ``LC_ALL`` for translations and
# must be neutralised too. GitPython already does this for every command it
# runs; this keeps APM's own subprocess calls consistent with it.
_GIT_MESSAGE_LOCALE_ENV: Mapping[str, str] = MappingProxyType({"LC_ALL": "C", "LANGUAGE": "C"})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostInfo:
    """Immutable description of a remote Git host."""

    host: str
    kind: str  # "github" | "ghe_cloud" | "ghes" | "ado" | "gitlab" | "generic"
    has_public_repos: bool
    api_base: str
    port: int | None = None  # Non-standard git port (e.g. 7999 for Bitbucket DC)
    credential_purpose: str | None = None

    @property
    def display_name(self) -> str:
        """``host:port`` when a non-default port is set, else bare ``host``.

        Well-known default ports (443, 80, 22) are suppressed even if
        stored explicitly, as defence-in-depth against callers that
        construct a ``HostInfo`` without prior normalisation.

        Use this wherever user-facing text identifies the host -- errors, log
        lines, diagnostic output.
        """
        _well_known_default_ports = {443, 80, 22}
        if self.port is not None and self.port not in _well_known_default_ports:
            return f"{self.host}:{self.port}"
        return self.host


@dataclass
class AuthContext:
    """Resolved authentication for a single (host, org) pair.

    Treat as immutable after construction -- fields are never mutated.
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
    # Helper-returned username; ADO Basic headers use ``username:password``.
    username: str | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# AuthResolver
# ---------------------------------------------------------------------------


class AuthCacheKey(NamedTuple):
    """Stable cache key for AuthResolver lookups."""

    host: str | None
    port: int | None
    host_type: str  # Empty string represents an absent or canonical host_type.
    org: str
    path: str  # Empty unless credential resolution is repository-scoped.
    transport: str  # Empty for callers without a concrete remote URL.


class AuthResolver:
    """Single source of truth for auth resolution.

    Every APM operation that touches a remote host MUST use this class.
    Resolution is per-(host, port, host_type, org, optional path), thread-safe,
    and cached per-process.
    """

    def __init__(
        self,
        token_manager: GitHubTokenManager | None = None,
        logger: object | None = None,
        *,
        allow_external_fallback: bool = True,
        github_auth_first: bool = False,
    ):
        self._token_manager = token_manager or GitHubTokenManager()
        self._allow_external_fallback = allow_external_fallback
        # issue #2545: CLI-scoped override for the anonymous-first opt-out.
        # ``False`` (default) defers to ``get_apm_github_auth_first()``
        # (env var > persisted config > False) inside
        # ``uses_public_github_anonymous_first``; ``True`` short-circuits it
        # for this resolver instance regardless of env/config.
        self._github_auth_first_cli = github_auth_first
        self._github_auth_first_warned = False
        self._cache: dict[AuthCacheKey, AuthContext] = {}
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
        # #1212 follow-up: with preflight + list_remote_refs + clone all
        # routing through the ADO auth chain, a single ADO host
        # in an install plan can trigger emit_stale_pat_diagnostic up to
        # 3x per dependency. Dedup per host so the user sees ONE warning.
        self._stale_pat_warned_hosts: set = set()
        # One path-scoped ``git credential fill`` per (host, port, path) for
        # this resolver, shared by parallel install workers.
        self._helper_cache = HelperCredentialCache()

    def set_logger(self, logger: object) -> None:
        """Wire a CommandLogger (or InstallLogger) into the resolver after
        construction. Idempotent. Used by the install command, which builds
        the logger before it knows it needs an AuthResolver elsewhere."""
        self._logger = logger

    def clear_cache(self) -> None:
        """Clear resolved auth contexts when a caller needs fresh env state."""
        with self._lock:
            self._cache.clear()
        self._helper_cache.clear()

    def _helper_credential(self, host_info: HostInfo, path: str | None):
        """Return the cached path-scoped ``git credential fill`` answer."""
        key = (host_info.host.lower(), host_info.port, path)
        return self._helper_cache.get(
            key,
            lambda: self._token_manager.resolve_git_helper_credential(
                host_info.host,
                port=host_info.port,
                path=path,
            ),
        )

    @staticmethod
    def http_authorization_headers(git_env: dict[str, str] | None) -> dict[str, str]:
        """Return the HTTP Authorization header for one chain attempt.

        HTTP operations run under :meth:`try_with_fallback` receive the
        attempt's Git environment; this returns the exact header the chain
        selected for it (Basic PAT, Bearer, or Basic helper
        ``username:password``), or an empty dict for an anonymous attempt.
        """
        return authorization_headers(git_env)

    def _emit_auth_trace(
        self,
        line: str,
        verbose_callback: Callable[[str], None] | None,
    ) -> None:
        """Send one auth-chain step to the caller's or the wired verbose channel."""
        safe = sanitize_diagnostic_detail(line) or line
        if verbose_callback is not None:
            verbose_callback(safe)
            return
        if self._logger is not None and getattr(self._logger, "verbose", False):
            try:
                from apm_cli.utils.console import _rich_echo

                _rich_echo(f"  [i] {safe}", color="dim")
            except ImportError:
                logger.debug("%s", safe)
            return
        logger.debug("%s", safe)

    def resolve_github_mcp_token(self, *, source_only: bool = False) -> str | None:
        """Select GitHub MCP auth without repository or external-helper fallback.

        Runtime-capable clients request only the selected variable name.
        Literal-only clients retain the historical Copilot token lookup.
        The caller must first admit the MCP server's name and HTTPS host.
        """
        selected = (
            self._token_manager.get_token_env_var_for_purpose("copilot")
            if source_only
            else self._token_manager.get_token_for_purpose("copilot")
        )
        if selected:
            return selected
        compatibility_name = "GITHUB_PERSONAL_ACCESS_TOKEN"
        compatibility_token = os.environ.get(compatibility_name)
        if source_only:
            return compatibility_name if compatibility_token else None
        return compatibility_token or None

    def has_cached_resolution(
        self,
        host: str,
        org: str | None = None,
        *,
        port: int | None = None,
        host_type: str | None = None,
        path: str | None = None,
        remote_url: str | None = None,
    ) -> bool:
        """Return whether credential resolution already ran for this scope."""
        key = AuthCacheKey(
            host.lower() if host else host,
            port,
            self._cache_host_type(host, host_type),
            org.lower() if org else "",
            path or "",
            git_transport_cache_key(remote_url),
        )
        with self._lock:
            return key in self._cache

    # -- host classification ------------------------------------------------

    @staticmethod
    def classify_host(
        host: str,
        port: int | None = None,
        host_type: str | None = None,
    ) -> HostInfo:
        """Return a ``HostInfo`` describing *host*.

        ``port`` is carried through onto the returned ``HostInfo`` so that
        downstream code (cache keys, credential-helper input, error text)
        can discriminate between the same hostname on different ports.
        Host-kind classification itself is transport-agnostic -- the port
        never influences whether a host is GitHub/GHES/ADO/generic.
        ``host_type`` is an explicit manifest hint for hosts whose names do
        not reveal the backing service.
        """
        provider = classify_host_provider(host, host_type=host_type)
        return HostInfo(
            host=host,
            kind=provider.kind,
            has_public_repos=provider.has_public_repos,
            api_base=provider.build_api_base(host.lower(), port),
            port=port,
            credential_purpose=provider.credential_purpose,
        )

    # -- token type detection -----------------------------------------------

    @staticmethod
    def detect_token_type(token: str) -> str:
        """Classify a token string by its prefix.

        Note: EMU (Enterprise Managed Users) tokens use standard PAT
        prefixes (``ghp_`` or ``github_pat_``).  There is no prefix that
        identifies a token as EMU-scoped -- that's a property of the
        account, not the token format.

        Prefix reference (docs.github.com):
        - ``github_pat_`` -> fine-grained PAT
        - ``ghp_``        -> classic PAT
        - ``ghu_``        -> OAuth user-to-server (e.g. ``gh auth login``)
        - ``gho_``        -> OAuth app token
        - ``ghs_``        -> GitHub App installation (server-to-server)
        - ``ghr_``        -> GitHub App refresh token
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

    @staticmethod
    def gitlab_rest_headers(
        token: str | None,
        *,
        oauth_bearer: bool = False,
    ) -> dict[str, str]:
        """Build HTTP headers for GitLab REST API v4 calls.

        Personal access tokens use ``PRIVATE-TOKEN``. OAuth2 access tokens
        typically use ``Authorization: Bearer <token>``; set *oauth_bearer*
        to use that style.

        Does not log or print *token*. Callers must not log the returned dict.
        """
        if not token:
            return {}
        if oauth_bearer:
            return AuthResolver.bearer_headers(token)
        return {"PRIVATE-TOKEN": token}

    @staticmethod
    def github_rest_headers(token: str | None) -> dict[str, str]:
        """Build the credential header for GitHub-family REST API calls.

        Returns an empty dict when *token* is empty. Callers must not log the
        returned dict.
        """
        if not token:
            return {}
        return {"Authorization": f"token {token}"}

    @staticmethod
    def bearer_headers(token: str | None) -> dict[str, str]:
        """Build an ``Authorization: Bearer`` header, or an empty dict."""
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    # -- core resolution ----------------------------------------------------

    @staticmethod
    def _cache_host_type(host: str, host_type: str | None) -> str:
        """Return the cache-discriminating host_type value for a host."""
        value = (host_type or "").strip().lower()
        if value == "gitlab" and is_gitlab_hostname(host):
            return ""
        return value

    def resolve(
        self,
        host: str,
        org: str | None = None,
        *,
        port: int | None = None,
        host_type: str | None = None,
        path: str | None = None,
        remote_url: str | None = None,
    ) -> AuthContext:
        """Resolve auth for a host/org and optional repository path.

        ``port`` discriminates the cache key so that the same hostname on
        different ports (e.g. Bitbucket Datacenter with SSH on 7999 and a
        second HTTPS instance on 7990) never collapses to a single
        ``AuthContext``. Also flows into ``git credential fill`` so git's
        helpers can return port-specific credentials.
        """
        key = AuthCacheKey(
            host.lower() if host else host,
            port,
            self._cache_host_type(host, host_type),
            org.lower() if org else "",
            path or "",
            git_transport_cache_key(remote_url),
        )
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

            # Hold lock during entire credential resolution to prevent duplicate
            # credential-helper popups when parallel downloads resolve the same
            # (host, port, org, path) concurrently. The first caller fills the cache;
            # all subsequent callers for the same key become O(1) cache hits.
            # Bounded by APM_GIT_CREDENTIAL_TIMEOUT (default 60s). No deadlock
            # risk: single lock, never nested.
            host_info = self.classify_host(host, port=port, host_type=host_type)
            transport_policy = (
                git_transport_policy(host_info.kind, remote_url) if remote_url is not None else None
            )
            if path is None:
                token, source, scheme = self._resolve_token(
                    host_info,
                    org,
                    allow_generic_credential_lookup=(
                        transport_policy is None or transport_policy.allow_native_credential_lookup
                    ),
                )
            else:
                token, source, scheme = self._resolve_token(
                    host_info,
                    org,
                    path=path,
                    allow_generic_credential_lookup=(
                        transport_policy is None or transport_policy.allow_native_credential_lookup
                    ),
                )
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

    def resolve_for_remote(
        self,
        host: str,
        remote_url: str,
        org: str | None = None,
        *,
        port: int | None = None,
        host_type: str | None = None,
    ) -> AuthContext:
        """Resolve one context after applying its canonical remote policy."""
        return self.resolve(
            host,
            org,
            port=port,
            host_type=host_type,
            remote_url=remote_url,
        )

    def resolve_for_dep(self, dep_ref: DependencyReference) -> AuthContext:
        """Resolve auth from a ``DependencyReference``.

        Threads ``dep_ref.port`` through so the resolver (and any downstream
        git credential helper) can discriminate same-host multi-port setups.
        """
        host = dep_ref.host or default_host()
        org = dependency_repository_owner(dep_ref)
        # Issue #2545: an opted-out github.com dependency resolves credentials
        # directly instead of after an anonymous probe; keep the same
        # path-scoped lookup the anonymous-first retry uses.
        path = (
            dep_ref.repo_url
            if self.classify_host(host, port=dep_ref.port, host_type=dep_ref.host_type).kind
            == "github"
            and self._github_auth_first_enabled()
            else None
        )
        return self.resolve(
            host,
            org,
            port=dep_ref.port,
            host_type=dep_ref.host_type,
            path=path,
        )

    def uses_public_github_anonymous_first(
        self,
        host: str,
        *,
        port: int | None = None,
        host_type: str | None = None,
    ) -> bool:
        """Return whether HTTPS repository access must start anonymously.

        This exact-host decision is owned here so validation, ref resolution,
        cache fetches, and clone execution cannot drift into separate host
        checks. GitHub Enterprise, ADO, GitLab, and generic hosts retain their
        existing authentication order.

        Issue #2545: exact-host github.com normally answers ``True``, but the
        ``--auth-first`` CLI flag / ``APM_GITHUB_AUTH_FIRST`` env var /
        ``github_auth_first`` config (``get_apm_github_auth_first()``, in
        that resolution order) opt out to ``False`` for users behind a
        TLS-inspecting proxy whose intercepting CA chain has no revocation
        info, where the isolated anonymous attempt cannot complete a TLS
        handshake even though a plain ``git clone`` (which inherits the real
        git/TLS config) succeeds.
        """
        if (
            self.classify_host(
                host,
                port=port,
                host_type=host_type,
            ).kind
            != "github"
        ):
            return False
        if self._github_auth_first_enabled():
            self.emit_github_auth_first_diagnostic()
            return False
        return True

    def _github_auth_first_enabled(self) -> bool:
        """Return whether the github.com auth-first opt-out is active.

        CLI override first, then ``get_apm_github_auth_first()`` (env var >
        persisted config > False). ``create_config=False``: a pure decision
        read must never create ``~/.apm/config.json`` as a side effect.
        """
        if self._github_auth_first_cli:
            return True
        from ..config import get_apm_github_auth_first

        return get_apm_github_auth_first(create_config=False)

    def emit_github_auth_first_diagnostic(self) -> None:
        """Emit a one-time [!] warning when the auth-first opt-out is active.

        Issue #2545: the opt-out trades the anonymous-first credential-
        isolation property for environment-inheriting behavior, so the
        tradeoff is surfaced once per resolver instance rather than silently.
        Mirrors :meth:`emit_stale_pat_diagnostic`'s logger-or-fallback shape.
        """
        with self._lock:
            if self._github_auth_first_warned:
                return
            self._github_auth_first_warned = True
        self._emit_warning_with_detail(
            "github-auth-first is enabled: git credentials will be forwarded "
            "on the first HTTPS attempt to github.com.",
            "To restore the default, run 'apm config unset github-auth-first', "
            "unset APM_GITHUB_AUTH_FIRST, or omit --auth-first.",
            fallback_context="auth-first",
            always_show_detail=True,
        )

    @staticmethod
    def is_public_github_auth_failure(exc: BaseException) -> bool:
        """Return whether an anonymous github.com failure may require auth.

        HTTP 401, 403, and 404 are deliberately eligible because GitHub hides
        private repositories behind 404. Git and libcurl equivalents are
        matched narrowly. Connectivity, TLS, timeout, and throttle failures
        are not auth signals and must surface without credential resolution.
        """
        return classify_auth_outcome(
            host_kind="github",
            error=exc,
            public_github_anonymous=True,
        ).is_auth_failure

    # -- fallback strategy --------------------------------------------------

    def try_with_fallback(
        self,
        host: str,
        operation: Callable[..., T],
        *,
        org: str | None = None,
        port: int | None = None,
        path: str | None = None,
        host_type: str | None = None,
        unauth_first: bool = False,
        verbose_callback: Callable[[str], None] | None = None,
        base_env: dict[str, str] | None = None,
        remote_url: str | None = None,
        attempts: AuthAttemptLog | None = None,
    ) -> T:
        """Execute *operation* with automatic auth/unauth fallback.

        This is the single entry point for authenticated host access. Every
        failed attempt is classified by
        :func:`apm_cli.core.auth_outcome.classify_auth_outcome`; operations
        report raw outcomes (raise :class:`AuthAttemptFailed` with the
        response or process result, or let Git/HTTP errors propagate) and
        never decide auth failure themselves.

        Parameters
        ----------
        host:
            Target git host.
        operation:
            ``operation(token, git_env) -> T`` -- the work to do.
        org:
            Optional organisation for per-org token lookup.
        path:
            Optional repository path (``org/repo``) included in the
            ``git credential fill`` request so helpers configured with
            ``credential.useHttpPath = true`` can disambiguate per-URL
            (notably Git Credential Manager for multi-account users).
            GitLab auth-first resolution also uses the path because private
            policy repositories may rely on per-URL credentials. Public
            github.com anonymous-first fallback uses it only after credentials
            may be required.
        host_type:
            Optional manifest provider hint. Forwarded through classification
            and credential resolution so the inner owner matches its caller.
        unauth_first:
            If *True*, try unauthenticated first (saves rate limits, EMU-safe).
        verbose_callback:
            Called with a human-readable step description at each attempt.
        base_env:
            Optional caller-owned Git environment. When provided, isolated
            config and transport policy are retained across every attempt.
        remote_url:
            Optional Git remote for this operation. When provided, each
            attempt's environment applies the canonical transport policy for
            that remote (:meth:`git_env_for_remote`).
        attempts:
            Optional :class:`AuthAttemptLog` the Azure DevOps chain records
            into, so callers can render what was tried.

        Azure DevOps walks ``ADO_APM_PAT -> az bearer -> path-scoped git
        credential fill`` (Server skips az); each step runs only after the
        previous one classified as an auth failure.

        When the resolved token comes from a global env var and fails
        (e.g. a github.com PAT tried on ``*.ghe.com``), the method
        retries with ``gh auth token`` and then ``git credential fill``
        before giving up.
        """
        host_info = self.classify_host(
            host,
            port=port,
            host_type=host_type,
        )
        if host_info.kind == "ado":
            ado_ctx = self.resolve(host, org, port=port, host_type=host_type)

            def _ado_env(attempt_ctx: AuthContext) -> dict[str, str]:
                if remote_url is not None:
                    return self.git_env_for_remote(attempt_ctx, remote_url, base_env=base_env)
                return self.git_env_for_context(attempt_ctx, base_env=base_env)

            return run_ado_chain(
                self,
                ado_ctx.host_info,
                operation,
                ctx=ado_ctx,
                path=path,
                env_for=_ado_env,
                log=lambda line: self._emit_auth_trace(line, verbose_callback),
                attempts=attempts if attempts is not None else AuthAttemptLog(),
            )
        # issue #2545: the opt-out only changes callers that would otherwise
        # try anonymously first, so it is consulted (and its one-time warning
        # emitted) only when unauth_first is set. For exact-host github.com
        # the opt-out skips the unauthenticated attempt entirely -- not merely
        # the hardened anonymous env -- so --auth-first never makes the
        # initial anonymous request when a credential resolves. Every other
        # host class keeps the historical unauth_first-only gate.
        lazy_public_github = unauth_first and self.uses_public_github_anonymous_first(
            host,
            port=port,
            host_type=host_type,
        )
        github_auth_first_opted_out = (
            unauth_first and host_info.kind == "github" and not lazy_public_github
        )
        attempt_unauthenticated = unauth_first and not github_auth_first_opted_out
        auth_ctx: AuthContext | None = None
        if not lazy_public_github:
            # Opted-out github.com keeps the path-scoped credential lookup the
            # anonymous-first retry uses, so credential.useHttpPath helpers
            # (e.g. GCM multi-account) still select the per-repository account.
            auth_ctx = self.resolve(
                host,
                org,
                port=port,
                host_type=host_type,
                path=(path if host_info.kind == "gitlab" or github_auth_first_opted_out else None),
            )
            host_info = auth_ctx.host_info
        if lazy_public_github:
            unauth_env = self.build_public_github_anonymous_git_env(base_env=base_env)
        else:
            unauth_env = self._build_git_env(
                None,
                host_kind=host_info.kind,
                base_env=base_env,
            )
            if github_auth_first_opted_out:
                # No-credential last resort for the opt-out: keep the caller's
                # TLS trust when base_env carries an isolated config.
                self._inherit_ambient_github_tls(unauth_env)

        def _auth_context() -> AuthContext:
            nonlocal auth_ctx
            if auth_ctx is None:
                auth_ctx = self.resolve(
                    host,
                    org,
                    port=port,
                    host_type=host_type,
                    path=path if lazy_public_github else None,
                )
            return auth_ctx

        def _git_env_for_context(ctx: AuthContext) -> dict[str, str]:
            if base_env is None:
                return ctx.git_env
            return self.git_env_for_context(ctx, base_env=base_env)

        def _log(msg: str) -> None:
            if verbose_callback:
                verbose_callback(msg)

        def _escalates(exc: BaseException, token: str | None) -> bool:
            """Outcome-reported failures escalate only on a classified auth failure.

            Arbitrary operation exceptions keep the historical escalate-on-any
            behavior for GitHub-class, GitLab, and generic hosts.
            """
            if not isinstance(exc, AuthAttemptFailed):
                return True
            return classify_auth_outcome(
                host_info=host_info,
                error=exc,
                credential_presented=bool(token),
                public_github_anonymous=lazy_public_github and not token,
            ).is_auth_failure

        def _try_credential_fallback(exc: Exception, ctx: AuthContext) -> T:
            """Retry the operation when the originally-resolved token fails.

            Walks the secondary chain in order: gh CLI (GitHub-like hosts;
            internal guard short-circuits unsupported hosts), then
            ``git credential fill`` (with ``path`` when known so
            helpers can disambiguate per-URL). Sources already obtained
            from a secondary chain (``gh-auth-token``,
            ``git-credential-fill``, ``none``) skip retry to avoid
            double-invocation.
            """
            if ctx.source in ("gh-auth-token", "git-credential-fill", "none"):
                raise exc
            if not _escalates(exc, ctx.token):
                raise exc
            _log(
                f"Token from {ctx.source} failed for {host_info.display_name}; "
                "trying secondary credential sources"
            )

            def _secondary_credential_env(token: str, source: str) -> dict[str, str]:
                """Build the env for a secondary (gh-cli / credential-fill)
                token via the same TLS-aware path as the primary token
                (issue #2545 follow-up: this fallback also reaches
                github.com and must not lose the ambient TLS config)."""
                return self.git_env_for_context(
                    AuthContext(
                        token=token,
                        source=source,
                        token_type="unknown",  # noqa: S106 - token kind label, not a secret
                        host_info=host_info,
                        git_env={},
                    ),
                    base_env=base_env,
                )

            _log(f"trying gh auth token for {host_info.display_name}")
            gh_token = self._token_manager.resolve_credential_from_gh_cli(host_info.host)
            if gh_token:
                _log(f"gh auth token resolved a credential for {host_info.display_name}")
                return operation(
                    gh_token,
                    _secondary_credential_env(gh_token, "gh-auth-token"),
                )
            path_suffix = f" (path={path})" if path else ""
            _log(f"trying git credential fill for {host_info.display_name}{path_suffix}")
            cred = self._token_manager.resolve_credential_from_git(
                host_info.host, port=host_info.port, path=path
            )
            if cred:
                _log(f"git credential fill resolved a credential for {host_info.display_name}")
                return operation(
                    cred,
                    _secondary_credential_env(cred, "git-credential-fill"),
                )
            raise exc

        # Hosts that never have public repos -> auth-only
        if host_info.kind == "ghe_cloud":
            ctx = _auth_context()
            _log(f"Auth-only attempt for {host_info.kind} host {host_info.display_name}")
            try:
                return operation(ctx.token, _git_env_for_context(ctx))
            except Exception as exc:
                # operation is caller-provided; broad catch required -- cannot narrow
                # without restricting the caller API.  Use %r so the type is visible.
                logger.debug(
                    "Auth-only operation failed for ghe_cloud host %s: %r",
                    host_info.display_name,
                    exc,
                )
                return _try_credential_fallback(exc, ctx)

        if attempt_unauthenticated:
            # Validation path: save rate limits, EMU-safe
            try:
                _log(f"Trying unauthenticated access to {host_info.display_name}")
                return operation(None, unauth_env)
            except Exception as exc:
                # operation is caller-provided; broad catch required -- cannot narrow
                # without restricting the caller API.  Use %r so the type is visible.
                logger.debug(
                    "Unauthenticated access failed for %s; will retry with token: %r",
                    host_info.display_name,
                    exc,
                )
                if (
                    lazy_public_github
                    and path is not None
                    and not self.is_public_github_auth_failure(exc)
                ):
                    raise
                if not _escalates(exc, None):
                    raise
                ctx = _auth_context()
                if ctx.token:
                    _log(f"Unauthenticated failed, retrying with token (source: {ctx.source})")
                    try:
                        return operation(ctx.token, _git_env_for_context(ctx))
                    except Exception as retry_exc:
                        # operation is caller-provided; broad catch required.
                        logger.debug(
                            "Authenticated retry also failed for %s: %r",
                            host_info.display_name,
                            retry_exc,
                        )
                        return _try_credential_fallback(retry_exc, ctx)
                raise
        # Download path: auth-first for higher rate limits
        ctx = _auth_context()
        if ctx.token:
            try:
                _log(
                    f"Trying authenticated access to {host_info.display_name} "
                    f"(source: {ctx.source})"
                )
                return operation(ctx.token, _git_env_for_context(ctx))
            except Exception as exc:
                # operation is caller-provided; broad catch required -- cannot narrow
                # without restricting the caller API.  Use %r so the type is visible.
                logger.debug(
                    "Authenticated access failed for %s; will retry unauthenticated: %r",
                    host_info.display_name,
                    exc,
                )
                if not _escalates(exc, ctx.token):
                    raise
                if host_info.has_public_repos:
                    _log("Authenticated failed, retrying without token")
                    try:
                        return operation(None, unauth_env)
                    except Exception as unauth_exc:
                        # operation is caller-provided; broad catch required.
                        logger.debug(
                            "Unauthenticated retry also failed for %s: %r",
                            host_info.display_name,
                            unauth_exc,
                        )
                        return _try_credential_fallback(exc, ctx)
                return _try_credential_fallback(exc, ctx)
        _log(f"No token available, trying unauthenticated access to {host_info.display_name}")
        return operation(None, unauth_env)

    # -- error context ------------------------------------------------------

    def build_error_context(
        self,
        host: str,
        operation: str,
        org: str | None = None,
        *,
        port: int | None = None,
        dep_url: str | None = None,
        bearer_also_failed: bool = False,
        attempts: AuthAttemptLog | None = None,
    ) -> str:
        """Build an actionable error message for auth failures.

        For Azure DevOps the message is derived from what the chain actually
        observed: the az CLI acquisition failure (kept on the bearer
        provider) or the chain's ``attempts`` log. "Your az cli session
        returned a bearer token, but Azure DevOps rejected it" is shown only
        when a bearer was really issued and rejected.

        ``bearer_also_failed=True`` prepends a single line to the Case 4
        block (PAT set, az available, both attempts failed) clarifying
        that ADO_APM_PAT was tried first and rejected before the bearer
        attempt -- so the user understands why both halves of the
        protocol failed without having to read the full diagnostic
        context. Callers MUST only set this when the bearer attempt
        actually ran (see :class:`BearerFallbackOutcome.bearer_attempted`).
        """
        # Exact github.com resolves path-scoped credentials whether or not the
        # auth-first opt-out (issue #2545) skipped the anonymous probe.
        path = dep_url if self.classify_host(host, port=port).kind == "github" else None
        auth_ctx = self.resolve(host, org, port=port, path=path)
        host_info = auth_ctx.host_info
        display = host_info.display_name

        # --- ADO-specific error cases ---
        if host_info.kind == "ado":
            from apm_cli.core.azure_cli import get_bearer_provider

            bearer_supported = self._supports_ado_bearer(host_info.host)
            provider = get_bearer_provider() if bearer_supported else None
            az_available = bool(provider and provider.is_available())
            pat_set = bool(os.environ.get("ADO_APM_PAT"))

            org_part = org or repository_owner_from_reference_text(dep_url) or ""

            token_url = (
                (
                    f"https://dev.azure.com/{org_part}/_usersSettings/tokens"
                    if bearer_supported
                    else f"https://{display}/{org_part}/_usersSettings/tokens"
                )
                if org_part
                else (
                    "https://dev.azure.com/<org>/_usersSettings/tokens"
                    if bearer_supported
                    else f"https://{display}/<collection>/_usersSettings/tokens"
                )
            )

            if pat_set:
                if az_available:
                    # Case 4: PAT and bearer were both available; both attempts
                    # failed. We may not have observed an explicit 401 (could be
                    # a 404, a network error, etc.) so the wording stays
                    # tentative -- see #856 review C6.
                    prefix = (
                        "    ADO_APM_PAT was rejected; az cli bearer was also rejected.\n\n"
                        if bearer_also_failed
                        else ""
                    )
                    return (
                        f"\n{prefix}"
                        f"    ADO_APM_PAT is set, and Azure CLI credentials may also be available,\n"
                        f"    but the Azure DevOps request still failed.\n\n"
                        f"    If this is an authentication failure, the PAT may be expired, revoked,\n"
                        f"    or scoped to a different org, and Azure CLI credentials may need to\n"
                        f"    be refreshed.\n\n"
                        f"    To fix:\n"
                        f"      1. Unset the PAT to test Azure CLI auth only:  unset ADO_APM_PAT\n"
                        f"      2. Re-authenticate Azure CLI if needed:        az login\n"
                        f"      3. Retry:                                       apm install\n\n"
                        f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
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
            if not bearer_supported:
                return (
                    "\n    Azure DevOps Server requires ADO_APM_PAT or a Git "
                    "credential helper.\n\n"
                    "    To fix:\n"
                    "      1. Create a PAT with Code (Read) scope in your "
                    "Azure DevOps Server collection.\n"
                    "      2. Set it for this process: export ADO_APM_PAT=your_token\n"
                    "      3. Or store a repository credential in Git Credential Manager\n"
                    "      4. Retry: apm install\n\n"
                    "    Azure CLI bearer authentication applies to Azure DevOps "
                    "Services, not Server.\n\n"
                    "    Docs: https://microsoft.github.io/apm/"
                    "getting-started/authentication/#on-prem-azure-devops-server"
                )
            if not az_available:
                # Case 1: no az, no PAT
                return (
                    f"\n    Azure DevOps requires authentication. You have two options:\n\n"
                    f"    1. Install Azure CLI and sign in (recommended for Entra ID users):\n"
                    f"         brew install azure-cli            # macOS\n"
                    f"         winget install Microsoft.AzureCLI # Windows\n"
                    f"         apt-get install azure-cli         # Debian/Ubuntu\n"
                    f"         dnf install azure-cli             # Fedora/RHEL\n"
                    f"         (full guide: https://aka.ms/InstallAzureCli)\n"
                    f"         az login\n"
                    f"         apm install                   # retry -- no env var needed\n\n"
                    f"    2. Use a Personal Access Token:\n"
                    f"         export ADO_APM_PAT=your_token\n"
                    f"         (Create one at {token_url} with Code (Read) scope.)\n\n"
                    f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
                )

            az_error = getattr(provider, "last_error", None)
            if isinstance(az_error, BaseException):
                return _az_token_failure_context(az_error)
            if attempts is not None and not attempts.has(
                STEP_BEARER, AuthOutcomeKind.CREDENTIAL_REJECTED
            ):
                return (
                    f"\n    Azure DevOps did not accept the request for {display}: "
                    f"{attempts.render() or 'no credential was available'}.\n\n"
                    "    To fix:\n"
                    "      1. Sign in with Azure CLI:            az login\n"
                    "      2. Or set a Personal Access Token:    export ADO_APM_PAT=your_token\n"
                    "      3. Or store a repository credential in Git Credential Manager\n\n"
                    "    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
                )

            # az issued a token earlier in this process; check the session tenant.
            tenant = provider.get_current_tenant_id()
            if tenant is None:
                # Case 3: az present, not logged in
                return (
                    "\n    Azure DevOps requires authentication. You have two options:\n\n"
                    "    1. Sign in with Azure CLI (recommended for Entra ID users):\n"
                    "         az login\n"
                    "         apm install                   # retry -- no env var needed\n\n"
                    "    2. Use a Personal Access Token:\n"
                    "         export ADO_APM_PAT=your_token\n\n"
                    "    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
                )

            # Case 2: az returned token (tenant known) but ADO rejected it.
            # Note: bearer_also_failed=True is structurally unreachable here --
            # callers only set it when source == "ADO_APM_PAT" (i.e. pat_set
            # is True), and Case 2 lives in the `not pat_set` branch. We do
            # not render a "PAT was also rejected" prefix in this case
            # because no PAT was tried.
            return (
                f"\n    Your az cli session (tenant: {tenant}) returned a bearer token,\n"
                f"    but Azure DevOps rejected it (HTTP 401).\n\n"
                f"    Check that you are signed into the correct tenant:\n"
                f"      az account show\n"
                f"      az login --tenant <correct-tenant-id>\n\n"
                f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
            )

        # --- Non-ADO error paths ---
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
            elif host_info.kind == "gitlab":
                lines.append(
                    "Ensure your GitLab personal or project access token meets the "
                    "API read requirements for your instance policy."
                )
            elif host.lower() == "github.com":
                lines.append(
                    "If your organization uses SAML SSO or is an EMU org, "
                    "ensure your PAT is authorized at "
                    "https://github.com/settings/tokens"
                )
            elif host_info.kind == "generic":
                lines.append("Verify credentials for this host in your git credential helper.")
            else:
                lines.append(
                    "If your organization uses SAML SSO, you may need to "
                    "authorize your token at https://github.com/settings/tokens"
                )
        else:
            lines.append("No token available.")
            if host_info.kind == "gitlab":
                lines.append(
                    "Set GITLAB_APM_PAT or GITLAB_TOKEN, or configure git credential fill "
                    f"for {display}."
                )
            elif host_info.kind == "generic":
                lines.append(
                    "APM does not apply GitHub PAT environment variables to generic git "
                    f"hosts; configure git credential fill for {display} or use a "
                    "public repository if available."
                )
            else:
                lines.append("Set GITHUB_APM_PAT or GITHUB_TOKEN, or run 'gh auth login'.")

        if org and host_info.kind not in ("ado", "gitlab", "generic"):
            lines.append(
                f"If packages span multiple organizations, set per-org tokens: "
                f"GITHUB_APM_PAT_{_org_to_env_suffix(org)}"
            )

        # When a custom port is in play, helpers that key by hostname alone
        # (some `gh` integrations, older keychain backends) can silently
        # return the wrong credential. Point the user at the concrete fix.
        if host_info.port is not None:
            lines.append(
                f"[i] Host '{display}' -- this helper may key by host only.\n"
                f"    Verify with: printf 'protocol=https\\nhost={display}\\n\\n'"
                f" | git credential fill\n"
                f"    Docs: {_PORT_CREDENTIAL_DOCS_URL}"
            )

        lines.append("Run with --verbose for detailed auth diagnostics.")
        return "\n".join(lines)

    # -- internals ----------------------------------------------------------

    def _resolve_token(
        self,
        host_info: HostInfo,
        org: str | None,
        *,
        path: str | None = None,
        allow_generic_credential_lookup: bool = True,
    ) -> tuple[str | None, str, str]:
        """Walk the token resolution chain.  Returns (token, source, scheme).

        Resolution order (GitHub-class: ``github``, ``ghe_cloud``, ``ghes``):
        1. Per-org ``GITHUB_APM_PAT_{ORG}`` when *org* is set
        2. ``GITHUB_APM_PAT`` -> ``GITHUB_TOKEN`` -> ``GH_TOKEN``
        3. ``gh auth token --hostname <host>`` (gh CLI active account)
        4. Host-specific git credential helper

        Resolution order (``gitlab``): ``GITLAB_APM_PAT`` -> ``GITLAB_TOKEN`` ->
        credential helper when the remote transport permits lookup.
        GitHub env vars are not consulted.

        Resolution order (``generic``): credential helper only (no GitHub or
        GitLab platform env vars).

        Resolution order (ADO Services, ``resolve``): ``ADO_APM_PAT`` ->
        AAD bearer -> ``none``. ``try_with_fallback`` then retries
        path-scoped ``git credential fill``.
        Resolution order (ADO Server, ``resolve``): ``ADO_APM_PAT`` ->
        ``none``. ``try_with_fallback`` then retries path-scoped
        ``git credential fill``.

        All token-bearing requests use HTTPS.
        """
        if host_info.kind == "ado":
            # ADO resolve() chain: PAT env -> cloud AAD bearer -> none.
            # Path-scoped git credential fill is owned by try_with_fallback.
            # Azure DevOps Server does not support Entra OAuth credentials.
            pat = os.environ.get("ADO_APM_PAT")
            if pat:
                return pat, "ADO_APM_PAT", "basic"
            if not self._supports_ado_bearer(host_info.host):
                return None, "none", "basic"
            # Try AAD bearer via az cli (lazy import to avoid module-load cost on non-ADO paths)
            from apm_cli.core.azure_cli import AzureCliBearerError, get_bearer_provider

            provider = get_bearer_provider()
            if provider.is_available():
                try:
                    bearer = provider.get_bearer_token()
                    return bearer, GitHubTokenManager.ADO_BEARER_SOURCE, "bearer"
                except AzureCliBearerError as exc:
                    # az is on PATH but token acquisition failed (e.g., not logged in).
                    # Fall through to token=None; build_error_context will render Case 3.
                    logger.debug(
                        "ADO bearer token acquisition failed for %s: %s",
                        host_info.display_name,
                        exc,
                    )
            return None, "none", "basic"

        # ADO uses ADO_APM_PAT (single var) + AAD bearer in resolve();
        # path-scoped git credential fill is owned by try_with_fallback.

        # 1. Per-org GitHub PAT (GitHub-class hosts only -- not GitLab / generic / ADO)
        if org and host_info.kind in ("github", "ghe_cloud", "ghes"):
            env_name = f"GITHUB_APM_PAT_{_org_to_env_suffix(org)}"
            token = os.environ.get(env_name)
            if token:
                return token, env_name, "basic"

        # 2. Global env vars by host class
        purpose = self._purpose_for_host(host_info)
        token = self._token_manager.get_token_for_purpose(purpose)
        if token:
            source = self._identify_env_source(purpose)
            return token, source, "basic"

        if not self._allow_external_fallback:
            return None, "none", "basic"

        # 3. gh CLI active account (eligibility gated inside the call;
        #    unsupported hosts return None instantly without a subprocess)
        gh_token = self._token_manager.resolve_credential_from_gh_cli(host_info.host)
        if gh_token:
            return gh_token, "gh-auth-token", "basic"

        # 4. Git credential helper (not for ADO)
        if host_info.kind not in ("ado",) and (
            host_info.kind not in ("generic", "gitlab") or allow_generic_credential_lookup
        ):
            # Most primary resolution calls remain host-scoped. The public
            # github.com anonymous-first fallback supplies path= after a
            # private-repo-shaped failure so GCM can choose the correct
            # account without an unscoped prompt.
            lookup_kwargs: dict[str, object] = {"port": host_info.port}
            if path is not None:
                lookup_kwargs["path"] = path
            if host_info.kind == "generic":
                lookup_kwargs["env"] = self._generic_credential_lookup_env()
            credential = self._token_manager.resolve_credential_from_git(
                host_info.host,
                **lookup_kwargs,
            )
            if credential:
                return credential, "git-credential-fill", "basic"

        return None, "none", "basic"

    @staticmethod
    def _supports_ado_bearer(host: str) -> bool:
        """Return whether Azure CLI bearer auth applies to this ADO host."""
        normalized = host.lower()
        return normalized in {"dev.azure.com", "ssh.dev.azure.com"} or normalized.endswith(
            ".visualstudio.com"
        )

    @staticmethod
    def _purpose_for_host(host_info: HostInfo) -> str:
        return host_info.credential_purpose or HOST_PROVIDERS[host_info.kind].credential_purpose

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
        base_env: dict | None = None,
        username: str | None = None,
    ) -> dict:
        """Pre-built env dict for subprocess git calls.

        ADO, GitLab, and GitHub-family subprocess credentials use an
        Authorization header. Other host classes retain GIT_TOKEN. An ADO
        helper credential carries its helper *username* so the header is
        ``Basic base64(username:password)``, exactly what Git would send.
        """
        env = dict(base_env) if base_env is not None else os.environ.copy()
        AuthResolver._clear_platform_token_env(env)
        AuthResolver._clear_git_auth_env(env)
        env["GIT_TERMINAL_PROMPT"] = "0"
        # ADO credentials arrive only as an APM-managed header. An echo
        # askpass would make Git answer a 401 with its own prompt text as
        # Basic credentials, so ADO children get no askpass (POSIX) and fail
        # with "terminal prompts disabled", which the auth chain classifies.
        env["GIT_ASKPASS"] = "" if host_kind == "ado" and sys.platform != "win32" else "echo"
        env.update(_GIT_MESSAGE_LOCALE_ENV)
        if host_kind == "ado" and not token:
            from ..deps.git_auth_env import GitAuthEnvBuilder

            env["GIT_CONFIG_NOSYSTEM"] = "1"
            env["GIT_CONFIG_GLOBAL"] = GitAuthEnvBuilder.isolated_global_config_path()
            AuthResolver._append_git_config(env, "credential.helper", "")
            AuthResolver._append_git_config(env, "http.extraheader", "")
        github_kinds = {"github", "ghe_cloud", "ghes"}
        header_auth = host_kind in {"ado", "gitlab", *github_kinds} or scheme == "github-basic"
        if token and header_auth and scheme in {"basic", "bearer", "github-basic"}:
            # ADO, GitLab, and explicit GitHub subprocess credentials use an
            # Authorization header, never argv or GIT_TOKEN.
            #
            # #2368: set (append-after-retained) rather than dict-merge, so the
            # non-auth entries _clear_git_auth_env just retained (e.g.
            # http.sslCAInfo) are not clobbered by a hardcoded COUNT=1 overlay.
            from apm_cli.utils.github_host import set_authorization_header_git_env

            if scheme == "bearer":
                credential = token
                header_scheme = "Bearer"
            elif scheme == "github-basic" or host_kind in github_kinds:
                credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
                header_scheme = "Basic"
            elif host_kind == "gitlab":
                credential = base64.b64encode(f"oauth2:{token}".encode()).decode()
                header_scheme = "Basic"
            else:
                user = username if isinstance(username, str) else ""
                credential = base64.b64encode(f"{user}:{token}".encode()).decode()
                header_scheme = "Basic"
            set_authorization_header_git_env(
                env,
                header_scheme,
                credential,
            )
        elif token:
            env["GIT_TOKEN"] = token
        return env

    @classmethod
    def build_noninteractive_git_env(
        cls,
        *,
        base_env: dict[str, str],
        host_kind: str = "generic",
        preserve_config_isolation: bool = False,
        suppress_credential_helpers: bool = False,
    ) -> dict[str, str]:
        """Build a credential-free Git environment from caller-owned config."""
        from ..deps.git_auth_env import GitAuthEnvBuilder

        if host_kind == "ado":
            preserve_config_isolation = True
            suppress_credential_helpers = True
        env = cls._build_git_env(
            None,
            host_kind=host_kind,
            base_env=base_env,
        )
        return GitAuthEnvBuilder.noninteractive_env(
            env,
            preserve_config_isolation=preserve_config_isolation,
            suppress_credential_helpers=suppress_credential_helpers,
        )

    @classmethod
    def build_public_github_anonymous_git_env(
        cls,
        *,
        base_env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build a credential-free Git env for public github.com attempts.

        Issue #2545: inherits the caller's Git TLS trust settings (see
        :meth:`_inherit_ambient_github_tls`) so a plain ``git clone``'s CA
        configuration also applies behind a TLS-inspecting proxy;
        ``credential.helper`` stays forced empty regardless.
        """
        caller_env = os.environ if base_env is None else base_env
        env = cls._build_git_env(
            None,
            host_kind="github",
            base_env=dict(caller_env),
        )
        for name in tuple(env):
            if name in {"GH_TOKEN", "GITHUB_APM_PAT", "GITHUB_TOKEN"} or name.startswith(
                "GITHUB_APM_PAT_"
            ):
                env.pop(name, None)
        from ..deps.git_auth_env import GitAuthEnvBuilder

        env["GIT_CONFIG_NOSYSTEM"] = "1"
        if base_env is None:
            env["GIT_CONFIG_GLOBAL"] = GitAuthEnvBuilder.isolated_global_config_path()
        else:
            env.setdefault(
                "GIT_CONFIG_GLOBAL",
                GitAuthEnvBuilder.isolated_global_config_path(),
            )
        cls._append_git_config(env, "credential.helper", "")
        cls._append_git_config(env, "http.extraheader", "")
        cls._inherit_ambient_github_tls(env)
        return env

    @classmethod
    def _inherit_ambient_github_tls(cls, env: dict[str, str]) -> None:
        """Restore the caller's Git TLS trust inside an isolated github.com env.

        Issue #2545: isolation (``GIT_CONFIG_NOSYSTEM`` / isolated
        ``GIT_CONFIG_GLOBAL``) discards ``http.sslBackend``,
        ``http.sslCAInfo`` and ``http.sslCAPath`` along with credential
        helpers. Only those TLS keys are re-added, and only when the env is
        isolated and does not already set them, so caller entries win and
        credential helpers stay disabled. Non-isolated envs already read the
        real config and are left unchanged.
        """
        if env.get("GIT_CONFIG_NOSYSTEM") != "1" and "GIT_CONFIG_GLOBAL" not in env:
            return
        from ..utils.git_env import real_git_tls_config

        for key, value in real_git_tls_config().items():
            if not cls._has_git_config_key(env, key):
                cls._append_git_config(env, key, value)

    @classmethod
    def build_public_github_authenticated_git_env(
        cls,
        token: str,
        *,
        base_env: dict[str, str],
    ) -> dict[str, str]:
        """Build a GitHub HTTPS env that keeps the credential out of argv."""
        return cls._build_git_env(
            token,
            scheme="github-basic",
            host_kind="github",
            base_env=base_env,
        )

    @staticmethod
    def _append_git_config(env: dict[str, str], key: str, value: str) -> None:
        """Append one process-scoped Git config entry without clobbering peers."""
        try:
            count = max(0, int(env.get("GIT_CONFIG_COUNT", "0") or "0"))
        except ValueError:
            count = 0
        env["GIT_CONFIG_COUNT"] = str(count + 1)
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value

    @staticmethod
    def _has_git_config_key(env: dict[str, str], key: str) -> bool:
        """Return whether ``env`` already has an indexed entry for ``key``.

        Case-insensitive, matching Git's own config-key normalization and
        ``real_git_tls_config()``'s lower-cased keys. Used to make ambient
        config inheritance (issue #2545) additive-only: it must fill a gap,
        never override a value the caller (or an earlier build step)
        already set explicitly -- indexed ``GIT_CONFIG_KEY_N`` entries are
        last-one-wins in Git, so a blind append would otherwise let the
        ambient value beat an explicit one appended earlier.
        """
        try:
            count = max(0, int(env.get("GIT_CONFIG_COUNT", "0") or "0"))
        except ValueError:
            count = 0
        normalized = key.lower()
        return any(env.get(f"GIT_CONFIG_KEY_{i}", "").lower() == normalized for i in range(count))

    @staticmethod
    def git_env_for_context(
        ctx: AuthContext,
        *,
        base_env: dict | None,
    ) -> dict:
        """Apply one resolved credential to a hardened Git base environment.

        Issue #2545: a credentialed exact-host github.com attempt (the
        ordinary 401/403/404-triggered retry, or the ``--auth-first`` bypass
        of the anonymous attempt) must not hit the same TLS-inspecting-proxy
        failure the isolated hardened base env would otherwise reproduce --
        mirrors :meth:`build_public_github_anonymous_git_env`. Scoped to
        exact-host github.com only; GHE Cloud/GHES/ADO/GitLab/generic are
        unaffected.
        """
        scheme = ctx.auth_scheme
        if ctx.token and ctx.host_info.kind in {"github", "ghe_cloud", "ghes"}:
            scheme = "github-basic"
        env = AuthResolver._build_git_env(
            ctx.token,
            scheme=scheme,
            host_kind=ctx.host_info.kind,
            base_env=base_env,
            username=getattr(ctx, "username", None),
        )
        if ctx.host_info.kind == "github":
            AuthResolver._inherit_ambient_github_tls(env)
        return env

    def hardened_git_base_env(self) -> dict:
        """Build the credential-free hardened base for Git subprocesses."""
        from apm_cli.deps.git_auth_env import GitAuthEnvBuilder

        return GitAuthEnvBuilder(self._token_manager).setup_environment()

    def hardened_git_env_for_context(self, ctx: AuthContext) -> dict:
        """Build a hardened Git environment for one resolved context."""
        return self.git_env_for_context(
            ctx,
            base_env=self.hardened_git_base_env(),
        )

    def git_env_for_remote(
        self,
        ctx: AuthContext,
        remote_url: str,
        *,
        base_env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build the canonical noninteractive Git environment for one remote.

        Host and transport policy belongs to the provider registry. This method
        only applies that selected policy to the resolved context, keeping
        marketplace and dependency consumers from branching on transport.
        """
        policy = git_transport_policy(ctx.host_info.kind, remote_url)
        base_env = self.hardened_git_base_env() if base_env is None else dict(base_env)
        if policy.use_resolved_credentials:
            env = self.git_env_for_context(ctx, base_env=base_env)
            if not ctx.token and ctx.host_info.kind != "ado":
                self._append_git_config(env, "credential.helper", "")
                self._append_git_config(env, "http.extraheader", "")
        else:
            env = self.build_noninteractive_git_env(
                base_env=base_env,
                host_kind=ctx.host_info.kind,
                preserve_config_isolation=policy.preserve_config_isolation,
                suppress_credential_helpers=policy.suppress_credential_helpers,
            )
        if ctx.host_info.kind == "generic" and urlsplit(remote_url).scheme.lower() != "http":
            from apm_cli.utils.git_env import git_subprocess_env

            caller_env = git_subprocess_env()
            for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"):
                if name in caller_env:
                    env[name] = caller_env[name]
        if not policy.use_resolved_credentials and not policy.suppress_credential_helpers:
            self._append_git_config(env, "http.extraheader", "")
        self._clear_platform_token_env(env, remove=True)
        if policy.reject_https_downgrade:
            from ..utils.git_env import validate_git_url_rewrite_safety

            validate_git_url_rewrite_safety(remote_url, env)
        return env

    def build_native_git_credential_env(
        self,
        host_info: HostInfo,
        remote_url: str,
    ) -> dict[str, str]:
        """Build a header-free Git environment that retains native helpers."""
        policy = git_transport_policy(host_info.kind, remote_url)
        env = self.build_noninteractive_git_env(
            base_env=self.hardened_git_base_env(),
            host_kind=host_info.kind,
            preserve_config_isolation=policy.preserve_config_isolation,
            suppress_credential_helpers=policy.suppress_credential_helpers,
        )
        if not policy.suppress_credential_helpers:
            from apm_cli.utils.git_env import git_subprocess_env

            caller_env = git_subprocess_env()
            for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"):
                if name in caller_env:
                    env[name] = caller_env[name]
            self._append_git_config(env, "http.extraheader", "")
        self._clear_platform_token_env(env, remove=True)
        if policy.reject_https_downgrade:
            from ..utils.git_env import validate_git_url_rewrite_safety

            validate_git_url_rewrite_safety(remote_url, env)
        return env

    def _generic_credential_lookup_env(self) -> dict[str, str]:
        """Build the token-free native-helper environment for generic hosts."""
        env = self.build_noninteractive_git_env(
            base_env=self.hardened_git_base_env(),
            host_kind="generic",
        )
        self._clear_platform_token_env(env, remove=True)
        return env

    @staticmethod
    def _clear_platform_token_env(env: dict, *, remove: bool = False) -> None:
        """Scrub raw platform token sources before spawning git.

        GitPython treats ``env`` as an overlay on the parent process, so
        deleting a key from the overlay leaves the ambient value intact.
        The default empty values mask those sources in GitPython and direct
        subprocesses. Complete subprocess environments may remove them.
        """
        from apm_cli.utils.git_env import clear_git_platform_token_env

        clear_git_platform_token_env(env, remove=remove)

    @staticmethod
    def _clear_git_auth_env(env: dict) -> None:
        """Remove inherited Git authorization channels before an attempt."""
        from apm_cli.utils.git_env import clear_git_auth_env

        clear_git_auth_env(env)

    def emit_stale_pat_diagnostic(self, host_display: str) -> None:
        """Emit a [!] warning when PAT was rejected but bearer succeeded.

        F3 #852: when an InstallLogger is wired via :meth:`set_logger`, the
        warning is collected by its DiagnosticCollector so it appears in the
        install summary. Without a logger (e.g. unit tests) we fall back to
        the inline ``_rich_warning`` emission for backwards compatibility.

        #1212 follow-up: dedup per host_display so the user sees ONE warning
        per ADO host even when preflight, list_remote_refs, and the clone
        path each trigger the bearer-fallback path against the same host.

        Naming: previously ``_emit_stale_pat_diagnostic`` (private). Public
        now (#856 follow-up C9) so external modules (validation.py,
        github_downloader.py) do not reach into the underscore API.

        #1214 follow-up: guard the check-then-add under self._lock so two
        threads (parallel install) racing on the same ADO host cannot both
        pass the membership check before either calls add(); without the
        lock the dedup set defeats its own purpose.
        """
        with self._lock:
            if host_display in self._stale_pat_warned_hosts:
                return
            self._stale_pat_warned_hosts.add(host_display)
        self._emit_warning_with_detail(
            f"ADO_APM_PAT was rejected for {host_display}; fell back to az cli bearer.",
            "Consider unsetting the stale variable.",
            fallback_context="stale-PAT",
        )

    def _emit_warning_with_detail(
        self,
        msg: str,
        detail: str,
        *,
        fallback_context: str,
        always_show_detail: bool = False,
    ) -> None:
        """Emit a msg+detail warning, preferring the wired DiagnosticCollector.

        Shared by every deferred one-time ``[!]`` warning on this resolver
        (stale-PAT fallback, github-auth-first opt-out, ...). Falls back to
        inline ``_rich_warning`` when no ``InstallLogger`` is wired (e.g.
        unit tests). Callers own their own dedup gate (e.g. a per-host set
        or a boolean flag) before calling this.

        ``DiagnosticCollector``'s warning renderer only prints ``detail``
        under ``--verbose`` (by design for supplementary context like the
        stale-PAT cleanup hint). When ``detail`` carries the *only*
        remediation for the warning (the "So What test"), pass
        ``always_show_detail=True`` to fold it into the always-rendered
        message instead -- the ``_rich_warning`` fallback already shows
        both unconditionally.
        """
        diagnostics = self._diagnostics_or_none()
        if diagnostics is not None:
            if always_show_detail:
                diagnostics.warn(f"{msg} {detail}")
            else:
                diagnostics.warn(msg, detail=detail)
            return
        try:
            from apm_cli.utils.console import _rich_warning

            if always_show_detail:
                _rich_warning(f"{msg} {detail}", symbol="warning")
            else:
                _rich_warning(msg, symbol="warning")
                _rich_warning(f"    {detail}", symbol="warning")
        except ImportError as exc:
            logger.debug(
                "Console module unavailable for %s warning; skipping: %s",
                fallback_context,
                exc,
            )

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
            except ImportError as exc:
                logger.debug(
                    "Console module unavailable for auth-source logging; skipping: %s", exc
                )
        # No logger wired -- the install path always wires one in the
        # bearer branch, so this fallback only fires in unit-test contexts
        # that opt-in via APM_VERBOSE=1.
        sys.stderr.write(line + "\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _az_token_failure_context(error: BaseException) -> str:
    """Render #3753: az is installed but did not issue an Azure DevOps token."""
    reason = sanitize_diagnostic_detail(getattr(error, "stderr", None) or str(error))
    login = "az login"
    if "tenant" in reason.lower():
        login = "az login --tenant <tenant-id>"
    return (
        "\n    Azure CLI is installed but could not get a token for Azure DevOps:\n"
        f"      {reason or 'no reason reported by az'}\n\n"
        "    To fix:\n"
        f"      1. Sign in again:                     {login}\n"
        "      2. Or set a Personal Access Token:    export ADO_APM_PAT=your_token\n"
        "      3. Or store a repository credential in Git Credential Manager\n\n"
        "    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
    )


def _org_to_env_suffix(org: str) -> str:
    """Convert an org name to an env-var suffix (upper-case, hyphens -> underscores)."""
    return org.upper().replace("-", "_")
