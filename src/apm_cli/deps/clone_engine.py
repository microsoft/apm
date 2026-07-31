"""Transport-plan-driven clone execution.

Drives a :class:`TransportPlan` to completion by executing a sequence
of :class:`TransportAttempt` "commands". Each attempt is a self-contained
recipe (URL scheme, auth scheme, label) that the engine renders into
a concrete URL + git env, hands to the caller-provided ``clone_action``,
and -- on auth/transport failure -- rolls forward to the next attempt
or applies an in-attempt ADO bearer fallback.

Design pattern: **Command** (each TransportAttempt is a command),
chained together with a small **Chain-of-Responsibility** for failure
recovery (per-attempt ADO bearer retry, then plan-level next attempt).

The engine collaborates with the surrounding downloader via a
duck-typed :class:`_DownloaderContext` Protocol so it does not need
to import :class:`GitHubPackageDownloader`. This keeps the engine
unit-testable in isolation and mirrors the existing
``DownloadDelegate`` / ``ArtifactoryOrchestrator`` patterns.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from git.exc import GitCommandError

from ..models.apm_package import DependencyReference
from ..utils.github_host import (
    default_host,
    is_ado_auth_failure_signal,
    is_github_hostname,
)
from .bare_cache import build_clone_failure_message
from .transport_selection import TransportAttempt, TransportPlan

if TYPE_CHECKING:
    from ..core.auth import AuthResolver

_PROTOCOL_FALLBACK_DOCS_URL = (
    "https://microsoft.github.io/apm/guides/dependencies/#restoring-the-legacy-permissive-chain"
)


def _debug(msg: str) -> None:
    """Indirect to the downloader's module-level _debug to keep behaviour parity."""
    from .github_downloader import _debug as _gd_debug

    _gd_debug(msg)


def _rich_warning(message: str, *, symbol: str = "warning") -> None:
    """Indirect to ``github_downloader._rich_warning``.

    Routes through the github_downloader module's namespace so that
    existing ``mock.patch('apm_cli.deps.github_downloader._rich_warning')``
    test interception sites still see warning emissions made from this
    extracted module.
    """
    from . import github_downloader as _gd

    _gd._rich_warning(message, symbol=symbol)


class _DownloaderContext(Protocol):
    """The slice of the downloader the clone engine needs."""

    auth_resolver: AuthResolver
    git_env: dict
    has_ado_token: bool

    def _resolve_dep_token(self, dep_ref: DependencyReference | None = ...) -> str | None: ...
    def _resolve_dep_auth_ctx(self, dep_ref: DependencyReference | None = ...): ...
    def _build_noninteractive_git_env(
        self,
        *,
        preserve_config_isolation: bool = ...,
        suppress_credential_helpers: bool = ...,
    ) -> dict: ...
    def _build_repo_url(
        self,
        repo_url_base: str,
        *,
        use_ssh: bool = ...,
        dep_ref: DependencyReference | None = ...,
        token: str | None = ...,
        auth_scheme: str = ...,
    ) -> str: ...
    def _sanitize_git_error(self, error_message: str) -> str: ...


class CloneEngine:
    """Execute a TransportPlan with full fallback semantics.

    Owns:

    * TransportPlan resolution + per-dep transport warnings.
    * Per-attempt URL + env construction.
    * ADO bearer in-attempt retry on PAT 401.
    * Cross-protocol fallback warning on protocol switch.
    * Aggregate error construction on plan exhaustion.
    """

    def __init__(
        self,
        host: _DownloaderContext,
    ) -> None:
        self._host = host

    # The transport selector, protocol pref, allow-fallback flag, and
    # fallback-port-warned dedup set are all read dynamically from the
    # host on each invocation. Snapshotting them at engine-construction
    # time would break tests (and any caller) that mutates these
    # attributes on the downloader after construction.
    @property
    def _transport_selector(self):
        return self._host._transport_selector

    @property
    def _protocol_pref(self):
        return self._host._protocol_pref

    @property
    def _allow_fallback(self) -> bool:
        return self._host._allow_fallback

    @property
    def _fallback_port_warned(self) -> set[tuple]:
        return self._host._fallback_port_warned

    @property
    def _fallback_port_warned_lock(self):
        return self._host._fallback_port_warned_lock

    def execute(
        self,
        repo_url_base: str,
        target_path: Path,
        *,
        dep_ref: DependencyReference | None = None,
        clone_action: Callable[[str, dict[str, str], Path], None],
        verbose_callback=None,
    ) -> None:
        """Run the plan; raise :class:`RuntimeError` if all attempts fail."""
        host = self._host
        last_error: Exception | None = None
        is_ado = bool(dep_ref and dep_ref.is_azure_devops())

        dep_host = dep_ref.host if dep_ref else None
        is_github = is_github_hostname(dep_host) if dep_host else True
        is_generic = not is_ado and not is_github

        anonymous_plan = self._transport_selector.select(
            dep_ref=dep_ref,
            cli_pref=self._protocol_pref,
            allow_fallback=self._allow_fallback,
            has_token=False,
        )
        public_github_https_first = bool(
            dep_ref is not None
            and not dep_ref.is_insecure
            and anonymous_plan.attempts
            and anonymous_plan.attempts[0].scheme == "https"
            and host.auth_resolver.uses_public_github_anonymous_first(
                dep_host or default_host(),
                port=dep_ref.port,
                host_type=dep_ref.host_type,
            )
            is True
        )

        if public_github_https_first:
            dep_token = None
            has_token = False
            dep_auth_ctx = None
            dep_auth_scheme = "basic"
        else:
            dep_token = host._resolve_dep_token(dep_ref)
            has_token = dep_token is not None
            dep_auth_ctx = host._resolve_dep_auth_ctx(dep_ref)
            dep_auth_scheme = dep_auth_ctx.auth_scheme if dep_auth_ctx else "basic"

        _debug(
            f"_clone_with_fallback: repo={repo_url_base}, is_ado={is_ado}, "
            f"is_generic={is_generic}, has_token={has_token}, "
            f"auth_scheme={dep_auth_scheme}, "
            f"protocol_pref={self._protocol_pref.value}, "
            f"allow_fallback={self._allow_fallback}"
        )

        def _env_for(attempt: TransportAttempt) -> dict[str, str]:
            if attempt.use_token:
                if dep_auth_ctx is not None:
                    return host.auth_resolver.git_env_for_context(
                        dep_auth_ctx,
                        base_env=host.git_env,
                    )
                return host.git_env
            if attempt.scheme == "http":
                return host._build_noninteractive_git_env(
                    preserve_config_isolation=True,
                    suppress_credential_helpers=True,
                )
            if is_ado:
                return host.auth_resolver.build_noninteractive_git_env(
                    base_env=host.git_env,
                    host_kind="ado",
                    preserve_config_isolation=True,
                )
            return host._build_noninteractive_git_env()

        plan: TransportPlan = (
            anonymous_plan
            if public_github_https_first
            else self._transport_selector.select(
                dep_ref=dep_ref,
                cli_pref=self._protocol_pref,
                allow_fallback=self._allow_fallback,
                has_token=has_token,
            )
        )
        _debug(
            "transport plan: "
            f"strict={plan.strict}, "
            f"attempts={[(a.scheme, a.use_token, a.label) for a in plan.attempts]}"
        )

        # Cross-protocol fallback custom-port warning (#786).
        dep_port = getattr(dep_ref, "port", None) if dep_ref else None
        if (
            not plan.strict
            and dep_port is not None
            and any(a.scheme == "ssh" for a in plan.attempts)
            and any(a.scheme == "https" for a in plan.attempts)
        ):
            warn_key = (
                dep_host.lower() if dep_host else dep_host,
                repo_url_base,
                dep_port,
            )
            # Guard the check-then-add under the lock so two threads
            # racing on the same warn_key cannot both pass the
            # membership check before either calls add().
            _should_warn = False
            with self._fallback_port_warned_lock:
                if warn_key not in self._fallback_port_warned:
                    self._fallback_port_warned.add(warn_key)
                    _should_warn = True
            if _should_warn:
                initial_scheme = plan.attempts[0].scheme.upper()
                fallback_scheme = next(
                    a.scheme.upper() for a in plan.attempts if a.scheme != plan.attempts[0].scheme
                )
                host_display = dep_host or "host"
                _rich_warning(
                    f"Custom port {dep_port} on {host_display}/{repo_url_base}: "
                    f"if {initial_scheme} fails, APM will retry over "
                    f"{fallback_scheme} on the same port.\n"
                    f"    Pin the URL scheme, or drop "
                    f"--allow-protocol-fallback to fail fast.\n"
                    f"    See: {_PROTOCOL_FALLBACK_DOCS_URL}",
                    symbol="warning",
                )

        def _run_public_github_attempt() -> tuple[str, bool]:
            if dep_ref is None:
                raise RuntimeError("Public GitHub attempt requires a dependency reference")
            winning_url = ""
            winning_token = False

            def _clone_public_github(
                token: str | None,
                git_env: dict[str, str],
            ) -> None:
                nonlocal winning_token, winning_url
                winning_token = token is not None
                winning_url = host._build_repo_url(
                    repo_url_base,
                    use_ssh=False,
                    dep_ref=dep_ref,
                    token=token or "",
                    auth_scheme="basic",
                )
                clone_action(winning_url, git_env, target_path)

            org = repo_url_base.split("/", 1)[0] if "/" in repo_url_base else None
            host.auth_resolver.try_with_fallback(
                dep_host or default_host(),
                _clone_public_github,
                org=org,
                port=dep_ref.port,
                path=dep_ref.repo_url,
                host_type=dep_ref.host_type,
                unauth_first=True,
                verbose_callback=verbose_callback,
            )
            return winning_url, winning_token

        prev_label: str | None = None
        prev_scheme: str | None = None
        authenticated_fallback_used = False
        for attempt in plan.attempts:
            if attempt.use_token and not has_token:
                continue

            use_ssh = attempt.scheme == "ssh"
            lazy_public_attempt = (
                public_github_https_first
                and attempt.scheme == "https"
                and not attempt.use_token
                and dep_ref is not None
            )
            url = ""
            if not lazy_public_attempt:
                try:
                    url = host._build_repo_url(
                        repo_url_base,
                        use_ssh=use_ssh,
                        dep_ref=dep_ref,
                        token=dep_token if attempt.use_token else "",
                        auth_scheme=dep_auth_scheme if attempt.use_token else "basic",
                    )
                except Exception as e:
                    last_error = e
                    continue

            if not plan.strict and prev_label and prev_scheme and prev_scheme != attempt.scheme:
                _rich_warning(
                    f"Protocol fallback: {prev_label} clone of {repo_url_base} "
                    f"failed; retrying with {attempt.label}.",
                    symbol="warning",
                )

            try:
                _debug(f"Attempting clone with {attempt.label} (URL sanitized)")
                if lazy_public_attempt:
                    url, authenticated_fallback_used = _run_public_github_attempt()
                else:
                    clone_action(url, _env_for(attempt), target_path)
                if verbose_callback:
                    display = (
                        host._sanitize_git_error(url)
                        if attempt.use_token or authenticated_fallback_used
                        else url
                    )
                    verbose_callback(f"Cloned from: {display}")
                return
            except (GitCommandError, subprocess.CalledProcessError) as e:
                err_msg = str(e)
                stderr_attr = getattr(e, "stderr", None)
                if stderr_attr:
                    if isinstance(stderr_attr, bytes):
                        with contextlib.suppress(Exception):
                            err_msg += " " + stderr_attr.decode("utf-8", errors="replace")
                    else:
                        err_msg += " " + str(stderr_attr)
                if (
                    is_ado
                    and attempt.use_token
                    and dep_auth_scheme == "basic"
                    and has_token
                    and dep_auth_ctx is not None
                    and host.auth_resolver._supports_ado_bearer(dep_auth_ctx.host_info.host)
                    and is_ado_auth_failure_signal(err_msg)
                ):
                    try:
                        from apm_cli.core.azure_cli import (
                            AzureCliBearerError,
                            get_bearer_provider,
                        )

                        provider = get_bearer_provider()
                        if provider.is_available():
                            try:
                                bearer = provider.get_bearer_token()
                                bearer_url = host._build_repo_url(
                                    repo_url_base,
                                    use_ssh=False,
                                    dep_ref=dep_ref,
                                    token=None,
                                    auth_scheme="bearer",
                                )
                                bearer_env = host.auth_resolver._build_git_env(
                                    bearer,
                                    scheme="bearer",
                                    host_kind="ado",
                                    base_env=host.git_env,
                                )
                                clone_action(bearer_url, bearer_env, target_path)
                                host.auth_resolver.emit_stale_pat_diagnostic(
                                    dep_host or "dev.azure.com"
                                )
                                if verbose_callback:
                                    verbose_callback(
                                        "Cloned from: (sanitized) via AAD bearer fallback"
                                    )
                                return
                            except (
                                AzureCliBearerError,
                                GitCommandError,
                                subprocess.CalledProcessError,
                            ):
                                pass
                    except ImportError:
                        pass
                last_error = e
                prev_label = attempt.label
                prev_scheme = attempt.scheme
                if plan.strict:
                    break

        error_msg = build_clone_failure_message(
            repo_url_base=repo_url_base,
            plan=plan,
            dep_ref=dep_ref,
            dep_host=dep_host,
            is_ado=bool(is_ado),
            is_generic=is_generic,
            has_ado_token=host.has_ado_token,
            has_token=has_token or authenticated_fallback_used,
            auth_resolver=host.auth_resolver,
            configured_github_host=os.environ.get("GITHUB_HOST", ""),
            default_host_fn=default_host,
            last_error=last_error,
            last_attempt_scheme=prev_scheme,
            sanitize_git_error=host._sanitize_git_error,
            public_github_non_auth_failure=bool(
                public_github_https_first
                and last_error is not None
                and not host.auth_resolver.is_public_github_auth_failure(last_error)
            ),
        )

        raise RuntimeError(error_msg)
