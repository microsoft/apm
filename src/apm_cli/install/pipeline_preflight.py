"""Auth pre-flight probe for ``apm install --update`` (split from pipeline.py).

Verifies credentials for every distinct (host, org) cluster before the
write phases touch ``apm.yml`` / ``apm.lock.yaml`` / ``apm_modules/``.
Kept in its own module so ``pipeline.py`` stays within the file-length
budget; re-exported from ``apm_cli.install.pipeline`` so existing
``from apm_cli.install.pipeline import _preflight_auth_check`` imports and
the install-pipeline call site keep working unchanged.
"""

from __future__ import annotations

import builtins
import contextlib

from .errors import AuthenticationError


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

    For generic hosts, the probe uses the same transport the real clone
    would use, mirroring :meth:`TransportSelector.select`: SSH only when
    the dep carries an explicit ``ssh://`` scheme; otherwise HTTPS (token
    embedded when available, plain HTTPS for anonymous public deps).
    SSH failures are detected via :func:`is_ssh_auth_failure_signal`;
    HTTPS failures via :func:`is_ado_auth_failure_signal`.

    Raises :class:`AuthenticationError` (with ``build_error_context``
    payload) on the first auth failure that survives the fallback.
    """
    import os
    import subprocess as _sp

    from ..utils.github_host import (
        is_ado_auth_failure_signal,
        is_azure_devops_hostname,
        is_github_hostname,
        is_ssh_auth_failure_signal,
    )

    logger = getattr(ctx, "logger", None)

    def _trace(line: str) -> None:
        """Emit a verbose tracing line; best-effort, never raises."""
        if not verbose or logger is None:
            return
        with contextlib.suppress(Exception):
            logger.verbose_detail(line)

    seen: builtins.set = builtins.set()
    for dep in ctx.deps_to_install:
        host = dep.host
        if not host or is_github_hostname(host):
            continue  # github.com uses API probe with unauth fallback
        org = dep.repo_url.split("/")[0] if dep.repo_url and "/" in dep.repo_url else None
        key = (host, org)
        if key in seen:
            continue
        seen.add(key)

        dep_ctx = auth_resolver.resolve_for_dep(dep)
        _auth_scheme = getattr(dep_ctx, "auth_scheme", "basic") or "basic"

        from ..deps.github_downloader import GitHubPackageDownloader

        _dl = GitHubPackageDownloader(auth_resolver=auth_resolver)
        _dl.github_host = host
        is_generic = not is_github_hostname(host) and not is_azure_devops_hostname(host)

        # For generic hosts, mirror TransportSelector.select() when picking
        # the probe transport: SSH only when the dep carries an explicit
        # ssh:// scheme. Shorthand deps (no explicit scheme) default to
        # HTTPS regardless of token presence -- TransportSelector's default
        # is plain HTTPS without a token and authenticated HTTPS with one.
        # Forcing SSH on tokenless generic hosts would break anonymous
        # access to public Gitea/Forgejo deps that have neither an HTTPS
        # token nor a configured SSH key.
        _explicit_scheme = (getattr(dep, "explicit_scheme", None) or "").lower()
        _use_ssh = is_generic and _explicit_scheme == "ssh"

        probe_url = _dl._build_repo_url(
            dep.repo_url,
            use_ssh=_use_ssh,
            dep_ref=dep,
            token=dep_ctx.token,
            auth_scheme=_auth_scheme,
        )
        _ctx_env = getattr(dep_ctx, "git_env", {}) or {}
        probe_env = {**os.environ, **_dl.git_env, **_ctx_env}
        # GIT_CONFIG_GLOBAL / GIT_CONFIG_NOSYSTEM carve-out: GitAuthEnvBuilder
        # forces an empty global gitconfig for ALL hosts to prevent a user's
        # ~/.gitconfig insteadOf rewrites or credential helpers from leaking
        # tokens during a clone. But for preflight probes (a single ls-remote
        # against the same host the dep targets), the redirection surface is
        # nil and killing the user's global config kills Git Credential
        # Manager along with it -- the helper most Windows ADO users rely on
        # for Entra-cached credentials. For ADO specifically that matters
        # because bearer acquisition can fail for reasons unrelated to login
        # state (sandbox, proxy, microsoft/apm#1430-style PATH quirks), and
        # GCM is the only remaining channel that can save us. Generic hosts
        # have the same logic; widening the carve-out to ADO keeps the
        # actual clone path isolated (it builds its own clean env) while
        # giving the preflight probe the best chance to succeed.
        if is_generic or is_azure_devops_hostname(host):
            for _key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_ASKPASS"):
                probe_env.pop(_key, None)

        host_display = host if not org else f"{host}/{org}"

        def _run_ls_remote(url, env):
            # auth-delegated: invoked via _primary_op/_bearer_op below, both
            # routed through auth_resolver.execute_with_bearer_fallback.
            try:
                return _sp.run(
                    ["git", "ls-remote", "--heads", "--exit-code", url],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                    env=env,
                )
            except _sp.TimeoutExpired:
                return None  # network timeout sentinel; treated as non-auth

        def _primary_op(url=probe_url, env=probe_env):
            return _run_ls_remote(url, env)

        def _bearer_op(
            bearer, dep=dep, dep_ctx=dep_ctx, host=host, host_display=host_display, _dl=_dl
        ):
            # SECURITY: build a CLEAN env via _build_git_env(scheme="bearer")
            # rather than {**probe_env, **build_ado_bearer_git_env(bearer)}.
            # probe_env carries GIT_TOKEN=<stale-PAT> from dep_ctx.git_env;
            # leaving it set during the bearer attempt would leak the
            # rejected PAT into the child-process env table even though the
            # GIT_CONFIG_VALUE_0 header carries the bearer. _build_git_env
            # explicitly skips GIT_TOKEN for scheme="bearer".
            bearer_env = auth_resolver._build_git_env(bearer, scheme="bearer", host_kind="ado")
            bearer_url = _dl._build_repo_url(
                dep.repo_url,
                use_ssh=False,
                dep_ref=dep,
                token=None,
                auth_scheme="bearer",
            )
            _trace(f"Preflight: {host_display} -- retrying with az cli bearer")
            return _run_ls_remote(bearer_url, bearer_env)

        def _is_auth_failure(outcome):
            if outcome is None:
                return False  # timeout: not an auth failure
            if outcome.returncode == 0:
                return False
            return is_ado_auth_failure_signal(outcome.stderr or "")

        ado_eligible = (
            dep.is_azure_devops()
            and _auth_scheme == "basic"
            and getattr(dep_ctx, "source", None) == "ADO_APM_PAT"
        )

        if ado_eligible:
            fallback_result = auth_resolver.execute_with_bearer_fallback(
                dep,
                _primary_op,
                _bearer_op,
                _is_auth_failure,
            )
            result = fallback_result.outcome
            # bearer_also_failed is True only when the bearer leg actually
            # ran AND its outcome still matched the auth-failure signature.
            # Early returns from execute_with_bearer_fallback (az
            # unavailable, JWT acquisition failed) leave bearer_attempted
            # False so the diagnostic does not falsely claim an attempt.
            bearer_also_failed = (
                fallback_result.bearer_attempted
                and result is not None
                and result.returncode != 0
                and is_ado_auth_failure_signal(result.stderr or "")
            )
        else:
            result = _primary_op()
            bearer_also_failed = False

        if result is None:
            continue  # timeout fallthrough -- handled by the real phase

        if result.returncode != 0:
            stderr_text = result.stderr or ""
            if _use_ssh:
                # Generic SSH transport: check SSH-specific failure signals.
                if not is_ssh_auth_failure_signal(stderr_text):
                    continue  # non-auth SSH failure (network, unknown host key) -- defer
                _trace(f"Preflight: {host_display} -- SSH auth rejected")
                raise AuthenticationError(
                    f"SSH authentication failed for {host}",
                    diagnostic_context=(
                        f"    SSH authentication was rejected by {host_display}.\n"
                        f"    Ensure your SSH key is loaded in ssh-agent "
                        f"(ssh-add -l) and that the\n"
                        f"    public key is authorised on the server.\n\n"
                        f"    git output: {stderr_text.strip()}\n\n"
                        f"    No files were modified.\n"
                        f"    apm.yml, apm.lock.yaml, and apm_modules/ are unchanged."
                    ),
                )
            else:
                if not is_ado_auth_failure_signal(stderr_text):
                    continue  # non-auth git failure (network, ref-not-found) -- defer
                _trace(f"Preflight: {host_display} -- auth rejected")
                _diag = auth_resolver.build_error_context(
                    host,
                    "install --update",
                    org=org,
                    dep_url=dep.repo_url,
                    bearer_also_failed=bearer_also_failed,
                )
                raise AuthenticationError(
                    f"Authentication failed for {host}",
                    diagnostic_context=(
                        _diag
                        + "\n\n    No files were modified."
                        + "\n    apm.yml, apm.lock.yaml, and apm_modules/ are unchanged."
                    ),
                )
        else:
            _trace(f"Preflight: {host_display} -- accepted")
