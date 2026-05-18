"""Git ls-remote validation for ADO / GHES / generic hosts.

Extracted from ``apm_cli.install.validation._validate_package_exists`` so the
git-based probe branch lives in a focused, independently testable module.
The entry point is :func:`_validate_via_git_ls_remote`; it covers:

* Azure DevOps (PAT + az-cli bearer fallback)
* GitHub Enterprise Server (GHES)
* Generic non-GitHub / non-ADO hosts (Bitbucket, GitLab, etc.)
* Virtual-subdirectory packages on non-GitHub hosts

Callers that patch ``subprocess.run`` globally (the standard pattern in
APM's validation unit tests) are unaffected because ``subprocess`` is looked
up in this module's own namespace rather than through the package
``__init__``.
"""

from __future__ import annotations

import os
import subprocess

from apm_cli.install.errors import AuthenticationError
from apm_cli.utils.github_host import (
    is_ado_auth_failure_signal,
    is_azure_devops_hostname,
    is_github_hostname,
)

__all__: list[str] = []


def _build_validate_env(is_generic: bool, is_insecure: bool, ado_downloader, dep_ctx) -> dict:
    """Build the subprocess environment for ``git ls-remote`` validation.

    Generic hosts get a relaxed env (native credential helpers active);
    managed hosts (GHES/ADO) merge the resolved dep-context git overrides.
    """
    if is_generic:
        return ado_downloader._build_noninteractive_git_env(
            preserve_config_isolation=is_insecure,
            suppress_credential_helpers=is_insecure,
        )
    _ctx_git_env = getattr(dep_ctx, "git_env", {}) if dep_ctx else {}
    return {**os.environ, **ado_downloader.git_env, **_ctx_git_env}


def _try_ado_bearer_fallback(
    dep_ref,
    ado_downloader,
    auth_resolver,
    verbose_log,
    package: str,
) -> bool:
    """Attempt an ADO az-cli bearer-token retry after a PAT rejection.

    Returns ``True`` when the bearer retry succeeds (rc == 0); ``False``
    (or silently) on any other outcome.  The ``ImportError`` guard allows
    environments without ``apm_cli.core.azure_cli`` to skip silently.
    """
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
                bearer_env = auth_resolver._build_git_env(bearer, scheme="bearer", host_kind="ado")
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
                        verbose_log(f"git ls-remote rc=0 for {package} (via AAD bearer fallback)")
                    return True
            except AzureCliBearerError:
                pass
    except ImportError:
        pass
    return False


def _validate_via_git_ls_remote(
    dep_ref,
    package: str,
    auth_resolver,
    verbose_log,
    virtual_subdir_repo_probe: bool,
) -> bool:
    """Validate *dep_ref* via ``git ls-remote`` for ADO / GHES / generic hosts.

    Returns ``True`` when the remote is reachable and the repo exists,
    ``False`` when it is not, and raises :exc:`AuthenticationError` when an
    auth failure (not a DNS / timeout error) is detected on a managed host.

    Parameters
    ----------
    dep_ref:
        Parsed dependency reference (``DependencyReference`` instance).
    package:
        Original package string, used only for log messages.
    auth_resolver:
        Active ``AuthResolver`` instance for credential look-ups.
    verbose_log:
        Callable ``(str) -> None`` for verbose-mode diagnostics, or ``None``.
    virtual_subdir_repo_probe:
        ``True`` when the caller determined that this virtual subdirectory
        should be validated by probing the clone root via git rather than
        through the virtual-package API path.
    """
    from apm_cli.deps.github_downloader import GitHubPackageDownloader
    from apm_cli.deps.transport_selection import is_fallback_allowed

    # Determine host type before building the URL so we know whether to
    # embed a token.  Generic (non-GitHub, non-ADO) hosts are excluded
    # from APM-managed auth; they rely on git credential helpers via the
    # relaxed validate_env below. GitLab hosts are managed when classified
    # as GitLab because they need oauth2 HTTPS token formatting.
    is_gitlab = auth_resolver.classify_host(dep_ref.host).kind == "gitlab"
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

    validate_env = _build_validate_env(is_generic, is_insecure, ado_downloader, _dep_ctx)

    # Build the probe order. Non-generic hosts (GHES/ADO) always probe
    # a single authenticated URL. Generic hosts:
    #   - explicit https/http  -> web URL only (strict)
    #   - explicit ssh         -> SSH URL only (strict)
    #   - shorthand (no scheme) -> legacy [SSH, HTTPS] chain
    # ``APM_ALLOW_PROTOCOL_FALLBACK=1`` re-appends the opposite scheme
    # for the explicit cases to match clone semantics exactly.
    urls_to_try: list[str] = []
    if is_generic:
        ssh_url = ado_downloader._build_repo_url(dep_ref.repo_url, use_ssh=True, dep_ref=dep_ref)
        if explicit_scheme in ("http", "https"):
            urls_to_try = [package_url] if not allow_fallback_env else [package_url, ssh_url]
        elif explicit_scheme == "ssh":
            urls_to_try = [ssh_url] if not allow_fallback_env else [ssh_url, package_url]
        else:
            # Shorthand has no user-stated transport; keep the legacy
            # SSH-first chain so existing flows (e.g. SSH-key users on
            # corporate hosts) keep validating successfully.
            urls_to_try = [ssh_url, package_url]
    else:
        urls_to_try = [package_url]

    if verbose_log:
        attempt_word = "attempt" if len(urls_to_try) == 1 else "attempts"
        verbose_log(f"Trying git ls-remote for {dep_ref.host} ({len(urls_to_try)} {attempt_word})")

    def _scheme_of(url: str) -> str:
        return url.split("://", 1)[0] if "://" in url else "ssh"

    def _log_attempt_result(probe_url: str, run_result) -> None:
        """Per-attempt sanitized verbose logging.

        The previous implementation only logged the final attempt's
        result, which masked the actual failure (typically the HTTPS
        leg) behind the SSH-fallback timeout. Logging each attempt
        gives users the diagnostic data they need to act.
        """
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
        if _try_ado_bearer_fallback(dep_ref, ado_downloader, auth_resolver, verbose_log, package):
            return True

    # Per-attempt verbose logging is emitted inside the probe loop
    # (and by the bearer-fallback branch above), so the result is
    # already on screen by the time we get here. Stderr is sanitized
    # via ``GitHubPackageDownloader._sanitize_git_error`` to scrub
    # any token-bearing URLs / env values before logging.

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
