"""Pre-flight authentication check for ``apm install --update`` (#1015).

Private helper extracted from :mod:`apm_cli.install.pipeline` to keep
``pipeline.py`` under 500 lines.  Import ``_preflight_auth_check`` from
``apm_cli.install.pipeline`` (it is re-exported there) rather than
importing from this module directly.
"""

from __future__ import annotations

import builtins
import contextlib
from dataclasses import dataclass
from typing import Any

from .errors import AuthenticationError


@dataclass(frozen=True, slots=True)
class _ProbeArgs:
    """Bundled arguments for :func:`_probe_and_fallback`."""

    probe_url: Any
    probe_env: Any
    dep: Any
    dep_ctx: Any
    auth_scheme: str
    host_display: str
    downloader: Any
    auth_resolver: Any
    trace: Any


def _probe_and_fallback(args: _ProbeArgs) -> tuple:
    """Run git ls-remote with optional ADO bearer fallback.

    Returns ``(result, bearer_also_failed)`` where *result* is either a
    ``subprocess.CompletedProcess`` or ``None`` on timeout.
    """
    import subprocess as sp

    from ..utils.github_host import is_ado_auth_failure_signal

    def run_ls_remote(url, env):
        try:
            return sp.run(
                ["git", "ls-remote", "--heads", "--exit-code", url],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                env=env,
            )
        except sp.TimeoutExpired:
            return None

    def primary_op(url=args.probe_url, env=args.probe_env):
        return run_ls_remote(url, env)

    def bearer_op(
        bearer,
        dep=args.dep,
        dep_ctx=args.dep_ctx,
        host_display=args.host_display,
        downloader=args.downloader,
    ):
        del dep_ctx
        bearer_env = args.auth_resolver._build_git_env(bearer, scheme="bearer", host_kind="ado")
        bearer_url = downloader._build_repo_url(
            dep.repo_url,
            use_ssh=False,
            dep_ref=dep,
            token=None,
            auth_scheme="bearer",
        )
        args.trace(f"Preflight: {host_display} -- retrying with az cli bearer")
        return run_ls_remote(bearer_url, bearer_env)

    def is_auth_failure(outcome):
        if outcome is None or outcome.returncode == 0:
            return False
        return is_ado_auth_failure_signal(outcome.stderr or "")

    ado_eligible = (
        args.dep.is_azure_devops()
        and args.auth_scheme == "basic"
        and getattr(args.dep_ctx, "source", None) == "ADO_APM_PAT"
    )
    if ado_eligible:
        fallback_result = args.auth_resolver.execute_with_bearer_fallback(
            args.dep,
            primary_op,
            bearer_op,
            is_auth_failure,
        )
        result = fallback_result.outcome
        bearer_also_failed = (
            fallback_result.bearer_attempted
            and result is not None
            and result.returncode != 0
            and is_ado_auth_failure_signal(result.stderr or "")
        )
    else:
        result = primary_op()
        bearer_also_failed = False
    return result, bearer_also_failed


def _probe_single_cluster(ctx, auth_resolver, dep, seen, trace) -> None:
    import os

    from ..utils.github_host import (
        is_ado_auth_failure_signal,
        is_azure_devops_hostname,
        is_github_hostname,
    )

    host = dep.host
    if not host or is_github_hostname(host):
        return
    org = dep.repo_url.split("/")[0] if dep.repo_url and "/" in dep.repo_url else None
    key = (host, org)
    if key in seen:
        return
    seen.add(key)

    dep_ctx = auth_resolver.resolve_for_dep(dep)
    auth_scheme = getattr(dep_ctx, "auth_scheme", "basic") or "basic"

    from ..deps.github_downloader import GitHubPackageDownloader

    downloader = GitHubPackageDownloader(auth_resolver=auth_resolver)
    downloader.github_host = host
    probe_url = downloader._build_repo_url(
        dep.repo_url,
        use_ssh=False,
        dep_ref=dep,
        token=dep_ctx.token,
        auth_scheme=auth_scheme,
    )
    ctx_env = getattr(dep_ctx, "git_env", {}) or {}
    probe_env = {**os.environ, **downloader.git_env, **ctx_env}
    if not is_azure_devops_hostname(host):
        for key_name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_ASKPASS"):
            probe_env.pop(key_name, None)

    host_display = host if not org else f"{host}/{org}"

    result, bearer_also_failed = _probe_and_fallback(
        _ProbeArgs(
            probe_url=probe_url,
            probe_env=probe_env,
            dep=dep,
            dep_ctx=dep_ctx,
            auth_scheme=auth_scheme,
            host_display=host_display,
            downloader=downloader,
            auth_resolver=auth_resolver,
            trace=trace,
        )
    )

    if result is None:
        return
    if result.returncode == 0:
        trace(f"Preflight: {host_display} -- accepted")
        return
    if not is_ado_auth_failure_signal(result.stderr or ""):
        return

    trace(f"Preflight: {host_display} -- auth rejected")
    diagnostic = auth_resolver.build_error_context(
        host,
        "install --update",
        org=org,
        dep_url=dep.repo_url,
        bearer_also_failed=bearer_also_failed,
    )
    raise AuthenticationError(
        f"Authentication failed for {host}",
        diagnostic_context=(
            diagnostic
            + "\n\n    No files were modified."
            + "\n    apm.yml, apm.lock.yaml, and apm_modules/ are unchanged."
        ),
    )


def _preflight_auth_check(ctx, auth_resolver, verbose: bool) -> None:
    """Verify auth for every distinct (host, org) before write phases.

    Called only when ``update_refs`` is set, so we know the pipeline is
    about to overwrite ``apm.yml``, ``apm.lock.yaml``, and
    ``apm_modules/``.  A single ``git ls-remote`` per cluster catches
    stale tokens before any file is touched.

    For ADO clusters, a stale ``ADO_APM_PAT`` automatically falls back
    to an ``az cli`` AAD bearer via :meth:`AuthResolver.execute_with_bearer_fallback`
    -- matching the protocol used by the actual clone path. Without this,
    ``apm install -g`` (which skipped preflight) would succeed but
    ``apm install -g --update`` would fail on the same machine with the
    same creds. See #1212.

    Raises :class:`AuthenticationError` (with ``build_error_context``
    payload) on the first auth failure that survives the fallback.
    """
    logger = getattr(ctx, "logger", None)

    def _trace(line: str) -> None:
        """Emit a verbose tracing line; best-effort, never raises."""
        if not verbose or logger is None:
            return
        with contextlib.suppress(Exception):
            logger.verbose_detail(line)

    seen: builtins.set = builtins.set()
    for dep in ctx.deps_to_install:
        _probe_single_cluster(ctx, auth_resolver, dep, seen, _trace)
