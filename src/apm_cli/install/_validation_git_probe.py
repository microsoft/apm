"""git ls-remote-based validation for ADO, GHES, GitLab, and generic git hosts.

Extracted from ``validation.py`` (Stage-2 line-budget split).
Public name ``_validate_ado_git_package`` is re-exported from ``validation.py``
to preserve all ``apm_cli.install.validation.*`` patch targets.
"""

from ..utils.github_host import is_ado_auth_failure_signal
from .errors import AuthenticationError


def _validate_ado_git_package(
    dep_ref,
    auth_resolver,
    verbose_log,
    package: str,
    logger,
) -> bool:
    """Validate an ADO, GHES, or generic-git-host package via ``git ls-remote``.

    Handles:
    - Proxy-only short-circuit (``PROXY_REGISTRY_ONLY=1``)
    - Host classification (GitLab, generic, ADO/GHES)
    - Authenticated URL construction with the correct auth scheme
    - Strict vs. fallback protocol ordering (``APM_ALLOW_PROTOCOL_FALLBACK``)
    - ADO bearer-token fallback when a PAT is rejected
    - Typed ``AuthenticationError`` for auth failures on managed hosts

    Returns True when the repo is reachable, False otherwise.
    Raises ``AuthenticationError`` for auth failures on non-generic managed hosts.
    """
    import os
    import subprocess

    from apm_cli.deps.github_downloader import GitHubPackageDownloader
    from apm_cli.deps.transport_selection import is_fallback_allowed
    from apm_cli.utils.github_host import is_azure_devops_hostname, is_github_hostname

    from ..deps.registry_proxy import is_enforce_only

    if is_enforce_only():
        # PROXY_REGISTRY_ONLY=1: skip direct git ls-remote probe for ADO/GHES.
        # The download step will surface a proxy 404 if the package is absent.
        if logger:
            logger.info(
                "Skipping direct git ls-remote for"
                f" {dep_ref.host or 'remote'}: proxy-only mode is active"
            )
        return True

    # Determine host type before building the URL so we know whether to
    # embed a token.  Generic (non-GitHub, non-ADO) hosts are excluded
    # from APM-managed auth; they rely on git credential helpers via the
    # relaxed validate_env below. GitLab hosts are managed when classified
    # as GitLab because they need oauth2 HTTPS token formatting.
    is_gitlab = (
        auth_resolver.classify_host(
            dep_ref.host,
            port=dep_ref.port,
            host_type=dep_ref.host_type,
        ).kind
        == "gitlab"
    )
    is_generic = (
        not is_github_hostname(dep_ref.host)
        and not is_azure_devops_hostname(dep_ref.host)
        and not is_gitlab
    )

    # For GHES / ADO: resolve per-dependency auth up front so the URL
    # carries an embedded token and avoids triggering OS credential
    # helper popups during git ls-remote validation.
    _url_token = None
    _dep_ctx = None
    _auth_scheme = "basic"
    if not is_generic:
        _dep_ctx = auth_resolver.resolve_for_dep(dep_ref)
        _url_token = _dep_ctx.token
        _auth_scheme = getattr(_dep_ctx, "auth_scheme", "basic") or "basic"

    ado_downloader = GitHubPackageDownloader(auth_resolver=auth_resolver)
    # Set the host
    if dep_ref.host:
        ado_downloader.github_host = dep_ref.host

    # Build authenticated URL using the resolved per-dep token.
    # #1015: pass auth_scheme so bearer tokens use extraheader
    # injection instead of embedding a ~1.5KB JWT in the userinfo.
    package_url = ado_downloader._build_repo_url(
        dep_ref.repo_url,
        use_ssh=False,
        dep_ref=dep_ref,
        token=_url_token,
        auth_scheme=_auth_scheme,
    )

    explicit_scheme = (getattr(dep_ref, "explicit_scheme", None) or "").lower() or None
    is_insecure = bool(getattr(dep_ref, "is_insecure", False))

    # Strict-by-default cross-protocol policy (issue microsoft/apm#992):
    # an explicit ``http://`` / ``https://`` / ``ssh://`` URL is honored
    # exactly and does NOT silently fall back to a different protocol.
    # This mirrors the strict default of ``_clone_with_fallback`` /
    # :class:`TransportSelector` and prevents the foot-gun where a user
    # types ``https://corp-bitbucket.example/...`` and the validation
    # pre-check silently retries SSH on port 22, masking the real HTTPS
    # failure (auth/redirect/etc.) behind a 30s SSH timeout. The
    # ``APM_ALLOW_PROTOCOL_FALLBACK=1`` env var (the same escape-hatch
    # the clone path honors) restores the legacy permissive chain.
    allow_fallback_env = is_fallback_allowed()

    # For generic hosts (not GitHub, not ADO), relax the env so native
    # credential helpers (macOS Keychain, credential-store,
    # manager-core, SSH agent, etc.) can work.  Config isolation
    # (GIT_CONFIG_GLOBAL=/dev/null, GIT_CONFIG_NOSYSTEM=1) is only
    # enforced for insecure plaintext HTTP connections where
    # credential leakage is a real risk; HTTPS connections need
    # access to user-configured helpers in ~/.gitconfig.  This
    # matches _clone_with_fallback() and git_reference_resolver.
    if is_generic:
        validate_env = ado_downloader._build_noninteractive_git_env(
            preserve_config_isolation=is_insecure,
            suppress_credential_helpers=is_insecure,
        )
    else:
        # #1015: merge _dep_ctx.git_env (bearer-aware GIT_CONFIG_*
        # overrides) into the subprocess env so `git ls-remote`
        # actually sends the Authorization header for AAD tokens.
        _ctx_git_env = getattr(_dep_ctx, "git_env", {}) if _dep_ctx else {}
        validate_env = {**os.environ, **ado_downloader.git_env, **_ctx_git_env}

    # Build the probe order. Non-generic hosts (GHES/ADO) always probe
    # a single authenticated URL. Generic hosts:
    #   - explicit https/http  -> web URL only (strict)
    #   - explicit ssh         -> SSH URL only (strict)
    #   - shorthand (no scheme) -> legacy [SSH, HTTPS] chain
    # ``APM_ALLOW_PROTOCOL_FALLBACK=1`` re-appends the opposite scheme
    # for the explicit cases to match clone semantics exactly.
    if is_generic:
        ssh_url = ado_downloader._build_repo_url(dep_ref.repo_url, use_ssh=True, dep_ref=dep_ref)
        if explicit_scheme in ("http", "https"):
            urls_to_try: list[str] = (
                [package_url] if not allow_fallback_env else [package_url, ssh_url]
            )
        elif explicit_scheme == "ssh":
            urls_to_try = [ssh_url] if not allow_fallback_env else [ssh_url, package_url]
        else:
            # Shorthand has no user-stated transport; keep the legacy
            # SSH-first chain so existing flows (e.g. SSH-key users on
            # corporate hosts) keep validating successfully.
            urls_to_try = [ssh_url, package_url]
    elif is_gitlab and explicit_scheme == "ssh":
        # Issue #1501: mirror the generic-host explicit-ssh arm so
        # GitLab refs typed as ``git@gitlab.com:...`` or ``ssh://...``
        # probe SSH first instead of demanding GITLAB_APM_PAT for an
        # HTTPS probe. ``APM_ALLOW_PROTOCOL_FALLBACK=1`` mirrors
        # ``_clone_with_fallback`` (SSH-first, HTTPS-second). The
        # ``package_url`` fallback is built earlier with token=None
        # when no GitLab PAT is resolved, so it embeds no credential
        # (no token leak via git ls-remote trace output).
        ssh_url = ado_downloader._build_repo_url(dep_ref.repo_url, use_ssh=True, dep_ref=dep_ref)
        urls_to_try = [ssh_url] if not allow_fallback_env else [ssh_url, package_url]
    else:
        urls_to_try = [package_url]

    if verbose_log:
        attempt_word = "attempt" if len(urls_to_try) == 1 else "attempts"
        verbose_log(f"Trying git ls-remote for {dep_ref.host} ({len(urls_to_try)} {attempt_word})")

    def _scheme_of(url: str) -> str:
        return url.split("://", 1)[0] if "://" in url else "ssh"

    def _log_attempt_result(probe_url: str, run_result) -> None:
        """Per-attempt sanitized verbose logging."""
        if not verbose_log:
            return
        scheme = _scheme_of(probe_url)
        if run_result.returncode == 0:
            verbose_log(f"git ls-remote ({scheme}) rc=0 for {package}")
            return
        raw_stderr = (run_result.stderr or "").strip()[:200]
        stderr_snippet = ado_downloader._sanitize_git_error(raw_stderr)
        for env_var in ("GIT_ASKPASS", "GIT_CONFIG_GLOBAL"):
            env_val = validate_env.get(env_var, "")
            if env_val:
                stderr_snippet = stderr_snippet.replace(env_val, "***")
        verbose_log(f"git ls-remote ({scheme}) rc={run_result.returncode}: {stderr_snippet}")

    result = None
    for probe_url in urls_to_try:
        cmd = ["git", "ls-remote", "--heads", "--exit-code", probe_url]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            env=validate_env,
        )
        _log_attempt_result(probe_url, result)
        if result.returncode == 0:
            break

    # ADO bearer fallback: if PAT was rejected (rc != 0 with auth-failure
    # signal) AND the dep is on Azure DevOps AND we resolved a PAT,
    # silently retry with az-cli bearer token.
    if (
        result is not None
        and result.returncode != 0
        and dep_ref.is_azure_devops()
        and _url_token is not None  # we had a PAT
        and is_ado_auth_failure_signal(result.stderr or "")
    ):
        try:
            from apm_cli.core.azure_cli import AzureCliBearerError, get_bearer_provider

            provider = get_bearer_provider()
            if provider.is_available():
                try:
                    bearer = provider.get_bearer_token()
                    bearer_url = ado_downloader._build_repo_url(
                        dep_ref.repo_url,
                        use_ssh=False,
                        dep_ref=dep_ref,
                        token=None,
                        auth_scheme="bearer",
                    )
                    # SECURITY: build a CLEAN env via _build_git_env(scheme="bearer")
                    # rather than {**validate_env, **build_ado_bearer_git_env(bearer)}.
                    # validate_env still carries the PAT-context GIT_CONFIG_*
                    # entries from _ctx_git_env; merging the bearer env on top
                    # would keep the rejected PAT visible in the child-process
                    # env (visible in /proc/<pid>/environ on Linux). _build_git_env
                    # explicitly skips GIT_TOKEN for scheme="bearer" and emits
                    # only the bearer-specific GIT_CONFIG_* injection.
                    bearer_env = auth_resolver._build_git_env(
                        bearer, scheme="bearer", host_kind="ado"
                    )
                    cmd = ["git", "ls-remote", "--heads", "--exit-code", bearer_url]
                    bearer_result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        timeout=30,
                        env=bearer_env,
                    )
                    if bearer_result.returncode == 0:
                        auth_resolver.emit_stale_pat_diagnostic(dep_ref.host or "dev.azure.com")
                        if verbose_log:
                            verbose_log(
                                f"git ls-remote rc=0 for {package} (via AAD bearer fallback)"
                            )
                        return True
                except AzureCliBearerError:
                    pass
        except ImportError:
            pass

    # #1015: distinguish auth failures from non-auth failures (DNS,
    # timeout, repo-truly-not-found 404). Auth failures get a typed
    # exception with actionable diagnostics; non-auth failures keep
    # the legacy False return so the caller can word its own message.
    if result.returncode != 0 and not is_generic:
        if is_ado_auth_failure_signal(result.stderr or ""):
            _host = dep_ref.host or "dev.azure.com"
            _org = (
                dep_ref.repo_url.split("/")[0]
                if dep_ref.repo_url and "/" in dep_ref.repo_url
                else None
            )
            _diag = auth_resolver.build_error_context(
                _host,
                "validate",
                org=_org,
                dep_url=dep_ref.repo_url,
            )
            raise AuthenticationError(
                f"Authentication failed for {_host}",
                diagnostic_context=_diag,
            )

    return result.returncode == 0
