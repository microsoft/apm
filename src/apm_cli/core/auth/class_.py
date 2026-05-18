"""Centralized authentication resolution for APM CLI.

Every APM operation that touches a remote host MUST use AuthResolver.
Resolution is per-(host, org) pair, thread-safe, and cached per-process.

All token-bearing requests use HTTPS — that is the transport security
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

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple, TypeVar

from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.utils.github_host import (
    default_host,
)

if TYPE_CHECKING:
    from apm_cli.models.dependency.reference import DependencyReference

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _FallbackRequest:
    """Options for auth fallback execution."""

    org: str | None = None
    port: int | None = None
    path: str | None = None
    unauth_first: bool = False
    verbose_callback: Callable[[str], None] | None = None


@dataclass(frozen=True, slots=True)
class _ErrorContextRequest:
    """Options for auth error-context construction."""

    port: int | None = None
    dep_url: str | None = None
    bearer_also_failed: bool = False


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


class BearerFallbackOutcome(NamedTuple):
    """Result of :meth:`AuthResolver.execute_with_bearer_fallback`.

    ``bearer_attempted`` is True iff ``bearer_op`` was actually invoked.
    Callers use it to distinguish "PAT rejected, bearer also rejected"
    (both halves failed) from "PAT rejected, bearer never tried" (early
    return: non-ADO, az unavailable, JWT acquisition failed) so the user
    diagnostic does not falsely claim an attempt that never happened.
    """

    outcome: object
    bearer_attempted: bool


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
        # #1212 follow-up: with preflight + list_remote_refs + clone all
        # routing through execute_with_bearer_fallback, a single ADO host
        # in an install plan can trigger emit_stale_pat_diagnostic up to
        # 3x per dependency. Dedup per host so the user sees ONE warning.
        self._stale_pat_warned_hosts: set = set()

    def set_logger(self, logger: object) -> None:
        """Wire a CommandLogger (or InstallLogger) into the resolver after
        construction. Idempotent. Used by the install command, which builds
        the logger before it knows it needs an AuthResolver elsewhere."""
        self._logger = logger

    # -- host classification ------------------------------------------------

    @staticmethod
    @staticmethod
    def classify_host(host: str, port: int | None = None) -> HostInfo:
        return _classify.classify_host(host, port)

    # -- token type detection -----------------------------------------------

    @staticmethod
    @staticmethod
    def detect_token_type(token: str) -> str:
        return _classify.detect_token_type(token)

    @staticmethod
    @staticmethod
    def gitlab_rest_headers(token: str | None, *, oauth_bearer: bool = False) -> dict[str, str]:
        return _classify.gitlab_rest_headers(token, oauth_bearer=oauth_bearer)

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
        request: _FallbackRequest | None = None,
        **legacy_kwargs,
    ) -> T:
        request = request or _FallbackRequest(**legacy_kwargs)
        return _fallback.try_with_fallback(self, host, operation, request)

    # -- error context ------------------------------------------------------

    def build_error_context(
        self,
        host: str,
        operation: str,
        org: str | None = None,
        request: _ErrorContextRequest | None = None,
        **legacy_kwargs,
    ) -> str:
        request = request or _ErrorContextRequest(**legacy_kwargs)
        return _errors.build_error_context(self, host, operation, org, request)

    # -- internals ----------------------------------------------------------

    def _resolve_token(self, host_info: HostInfo, org: str | None) -> tuple[str | None, str, str]:
        return _tokens._resolve_token(self, host_info, org)

    @staticmethod
    @staticmethod
    def _purpose_for_host(host_info: HostInfo) -> str:
        return _tokens._purpose_for_host(host_info)

    def _identify_env_source(self, purpose: str) -> str:
        return _tokens._identify_env_source(self, purpose)

    @staticmethod
    @staticmethod
    def _build_git_env(
        token: str | None = None, *, scheme: str = "basic", host_kind: str = "github"
    ) -> dict:
        return _tokens._build_git_env(token, scheme=scheme, host_kind=host_kind)

    def emit_stale_pat_diagnostic(self, host_display: str) -> None:
        return _errors.emit_stale_pat_diagnostic(self, host_display)

    # Backwards-compat alias for any in-tree caller still importing the
    # private name. Safe to remove once all callers move to the public name.
    _emit_stale_pat_diagnostic = emit_stale_pat_diagnostic

    def _diagnostics_or_none(self):
        return _tokens._diagnostics_or_none(self)

    def notify_auth_source(self, host_display: str, ctx) -> None:
        return _errors.notify_auth_source(self, host_display, ctx)

    def execute_with_bearer_fallback(
        self, dep_ref, primary_op, bearer_op, is_auth_failure
    ) -> BearerFallbackOutcome:
        return _fallback.execute_with_bearer_fallback(
            self, dep_ref, primary_op, bearer_op, is_auth_failure
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _org_to_env_suffix(org: str) -> str:
    """Convert an org name to an env-var suffix (upper-case, hyphens → underscores)."""
    return org.upper().replace("-", "_")


from . import classify as _classify
from . import errors as _errors
from . import fallback as _fallback
from . import tokens as _tokens
