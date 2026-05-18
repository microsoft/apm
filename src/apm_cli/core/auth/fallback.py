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

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from .class_ import BearerFallbackOutcome, _FallbackRequest

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _CredentialFallbackContext:
    """State required to retry auth via secondary credential sources."""

    auth_ctx: object
    host_info: object
    operation: object
    path: str | None
    log_callback: object


@dataclass(frozen=True, slots=True)
class _FallbackExecutionContext:
    """Common execution state for auth retry flows."""

    auth_ctx: object
    git_env: object
    host_info: object
    operation: object
    log_callback: object
    retry_auth: object


def _try_credential_fallback_impl(
    exc: Exception,
    self_: object,
    context: _CredentialFallbackContext,
) -> T:
    """Inner logic for the ``_try_credential_fallback`` closure.

    Extracted from :func:`try_with_fallback` to reduce its McCabe complexity
    within the configured Ruff thresholds.
    """
    auth_ctx = context.auth_ctx
    host_info = context.host_info
    operation = context.operation
    path = context.path
    log_callback = context.log_callback
    if auth_ctx.source in ("gh-auth-token", "git-credential-fill", "none"):
        raise exc
    # ADO uses ADO_APM_PAT + AAD bearer fallback; credential fill is out of scope.
    if host_info.kind == "ado":
        raise exc
    log_callback(
        f"Token from {auth_ctx.source} failed for {host_info.display_name}; "
        "trying secondary credential sources"
    )
    log_callback(f"trying gh auth token for {host_info.display_name}")
    gh_token = self_._token_manager.resolve_credential_from_gh_cli(host_info.host)
    if gh_token:
        log_callback(f"gh auth token resolved a credential for {host_info.display_name}")
        return operation(
            gh_token,
            self_._build_git_env(gh_token, scheme="basic", host_kind=host_info.kind),
        )
    path_suffix = f" (path={path})" if path else ""
    log_callback(f"trying git credential fill for {host_info.display_name}{path_suffix}")
    cred = self_._token_manager.resolve_credential_from_git(
        host_info.host, port=host_info.port, path=path
    )
    if cred:
        log_callback(f"git credential fill resolved a credential for {host_info.display_name}")
        return operation(
            cred,
            self_._build_git_env(cred, scheme="basic", host_kind=host_info.kind),
        )
    raise exc


def _try_ado_bearer_fallback_impl(
    exc: Exception,
    self_: object,
    operation: object,
    ado_bearer_fallback_available: bool,
    auth_ctx: object,
) -> T:
    """Inner logic for the ``_try_ado_bearer_fallback`` closure.

    Extracted from :func:`try_with_fallback` to reduce its McCabe complexity
    within the configured Ruff thresholds.
    """
    if not ado_bearer_fallback_available:
        raise exc
    from apm_cli.utils.github_host import is_ado_auth_failure_signal

    if not is_ado_auth_failure_signal(str(exc)):
        raise exc
    from apm_cli.core.azure_cli import AzureCliBearerError, get_bearer_provider

    provider = get_bearer_provider()
    if not provider.is_available():
        raise exc
    try:
        bearer = provider.get_bearer_token()
        bearer_env = self_._build_git_env(bearer, scheme="bearer", host_kind="ado")
        result = operation(bearer, bearer_env)
        # Success on fallback -- emit deferred diagnostic warning
        self_.emit_stale_pat_diagnostic(auth_ctx.host_info.display_name)
        return result
    except AzureCliBearerError:
        pass  # Bearer acquisition itself failed; fall through to original error
    except Exception:
        # Bearer also failed (Case 4). Re-raise the ORIGINAL PAT exception.
        pass
    raise exc


def _execute_auth_only_flow(host_info, auth_ctx, git_env, operation, on_failure):
    """Run the auth-only paths used by GHE Cloud and Azure DevOps."""
    try:
        return operation(auth_ctx.token, git_env)
    except Exception as exc:
        return on_failure(exc)


def _execute_unauth_first_flow(context: _FallbackExecutionContext):
    """Run the unauthenticated-first path used by validation flows."""
    try:
        context.log_callback(f"Trying unauthenticated access to {context.host_info.display_name}")
        return context.operation(None, context.git_env)
    except Exception:
        if not context.auth_ctx.token:
            raise
        context.log_callback(
            f"Unauthenticated failed, retrying with token (source: {context.auth_ctx.source})"
        )
        try:
            return context.operation(context.auth_ctx.token, context.git_env)
        except Exception as exc:
            return context.retry_auth(exc)


def _execute_auth_first_flow(context: _FallbackExecutionContext):
    """Run the authenticated-first path for hosts with public repos."""
    if context.auth_ctx.token:
        try:
            context.log_callback(
                f"Trying authenticated access to {context.host_info.display_name} "
                f"(source: {context.auth_ctx.source})"
            )
            return context.operation(context.auth_ctx.token, context.git_env)
        except Exception as exc:
            if context.host_info.has_public_repos:
                context.log_callback("Authenticated failed, retrying without token")
                try:
                    return context.operation(None, context.git_env)
                except Exception:
                    return context.retry_auth(exc)
            return context.retry_auth(exc)
    context.log_callback(
        f"No token available, trying unauthenticated access to {context.host_info.display_name}"
    )
    return context.operation(None, context.git_env)


def try_with_fallback(
    self,
    host: str,
    operation: Callable[..., T],
    request: _FallbackRequest,
) -> T:
    """Execute *operation* with automatic auth/unauth fallback.

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
    unauth_first:
        If *True*, try unauthenticated first (saves rate limits, EMU-safe).
    verbose_callback:
        Called with a human-readable step description at each attempt.

    When the resolved token comes from a global env var and fails
    (e.g. a github.com PAT tried on ``*.ghe.com``), the method
    retries with ``gh auth token`` and then ``git credential fill``
    before giving up.
    """
    auth_ctx = self.resolve(host, request.org, port=request.port)
    host_info = auth_ctx.host_info
    git_env = auth_ctx.git_env

    def _log(msg: str) -> None:
        if request.verbose_callback:
            request.verbose_callback(msg)

    def _try_credential_fallback(exc: Exception) -> T:
        """Retry the operation when the originally-resolved token fails.

        Walks the secondary chain in order: gh CLI (GitHub-like hosts;
        internal guard short-circuits unsupported hosts), then
        ``git credential fill`` (with ``path`` when known so
        helpers can disambiguate per-URL). Sources already obtained
        from a secondary chain (``gh-auth-token``,
        ``git-credential-fill``, ``none``) skip retry to avoid
        double-invocation.
        """
        return _try_credential_fallback_impl(
            exc,
            self,
            _CredentialFallbackContext(
                auth_ctx=auth_ctx,
                host_info=host_info,
                operation=operation,
                path=request.path,
                log_callback=_log,
            ),
        )

    # ADO bearer fallback machinery (PAT was tried first; bearer is the safety net)
    ado_bearer_fallback_available = (
        auth_ctx.host_info.kind == "ado" and auth_ctx.source == "ADO_APM_PAT"
    )

    def _try_ado_bearer_fallback(exc: Exception) -> T:
        """Retry ADO operation with AAD bearer when PAT fails with 401."""
        return _try_ado_bearer_fallback_impl(
            exc, self, operation, ado_bearer_fallback_available, auth_ctx
        )

    execution_context = _FallbackExecutionContext(
        auth_ctx=auth_ctx,
        git_env=git_env,
        host_info=host_info,
        operation=operation,
        log_callback=_log,
        retry_auth=_try_credential_fallback,
    )

    # Hosts that never have public repos -> auth-only
    if host_info.kind == "ghe_cloud":
        _log(f"Auth-only attempt for {host_info.kind} host {host_info.display_name}")
        return _execute_auth_only_flow(
            host_info,
            auth_ctx,
            git_env,
            operation,
            _try_credential_fallback,
        )

    # ADO: auth-first with bearer fallback when PAT fails
    if host_info.kind == "ado":
        _log(f"Auth-only attempt for {host_info.kind} host {host_info.display_name}")
        return _execute_auth_only_flow(
            host_info,
            auth_ctx,
            git_env,
            operation,
            _try_ado_bearer_fallback,
        )

    if request.unauth_first:
        return _execute_unauth_first_flow(execution_context)
    return _execute_auth_first_flow(execution_context)


def execute_with_bearer_fallback(
    self,
    dep_ref,
    primary_op,
    bearer_op,
    is_auth_failure,
) -> BearerFallbackOutcome:
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
        :class:`BearerFallbackOutcome` carrying the final ``outcome``
        plus a ``bearer_attempted`` flag. The flag is True iff
        ``bearer_op`` was actually invoked (ADO + auth-failure signature
        + az provider available + JWT acquired) and lets callers
        distinguish "PAT rejected, bearer also rejected" from "PAT
        rejected, bearer never tried" for accurate diagnostics. Never
        raises (exceptions from ``bearer_op`` are swallowed).
    """
    primary = primary_op()
    if dep_ref is None or not getattr(dep_ref, "is_azure_devops", lambda: False)():
        return BearerFallbackOutcome(primary, False)
    if not is_auth_failure(primary):
        return BearerFallbackOutcome(primary, False)
    try:
        from apm_cli.core.azure_cli import AzureCliBearerError, get_bearer_provider
    except ImportError:
        return BearerFallbackOutcome(primary, False)
    provider = get_bearer_provider()
    if not provider.is_available():
        return BearerFallbackOutcome(primary, False)
    try:
        bearer = provider.get_bearer_token()
    except AzureCliBearerError:
        return BearerFallbackOutcome(primary, False)
    try:
        fallback = bearer_op(bearer)
    except Exception:
        return BearerFallbackOutcome(primary, True)
    if fallback is None or is_auth_failure(fallback):
        return BearerFallbackOutcome(primary, True)
    host_display = getattr(dep_ref, "host", None) or "dev.azure.com"
    self.emit_stale_pat_diagnostic(host_display)
    return BearerFallbackOutcome(fallback, True)
