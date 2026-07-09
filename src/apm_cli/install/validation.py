"""Manifest validation: package existence checks, dependency syntax canonicalisation.

This module contains the leaf validation helpers extracted from
``apm_cli.commands.install``.  They are pure functions of their arguments
with zero coupling to the install pipeline, which is why they could be
relocated verbatim.

The orchestrator ``_validate_and_add_packages_to_apm_yml`` remains in
``commands/install.py`` because dozens of tests patch
``apm_cli.commands.install._validate_package_exists`` and rely on
module-level name resolution inside the orchestrator to intercept the call.
Keeping the orchestrator co-located with the re-exported name preserves
``@patch`` compatibility without any test modifications.

Functions
---------
_validate_package_exists
    Probe GitHub API / git-ls-remote / local FS to confirm a package ref
    is accessible.
_local_path_failure_reason
    Return a human-readable reason when a local-path dep fails validation.
_local_path_no_markers_hint
    Scan a local directory for nested installable packages and hint the user.
_generic_host_ambiguous_subpath_hint
    Return a GITLAB_HOST/APM_GITLAB_HOSTS hint when an unrecognised FQDN
    swallowed a subpath into a single (non-existent) repo path.
"""

import re
from pathlib import Path

import requests

from ..utils.console import _rich_echo, _rich_info, _rich_warning
from ..utils.github_host import (
    default_host,
    is_ado_auth_failure_signal,
    is_github_hostname,
    is_gitlab_hostname,
)
from .errors import AuthenticationError

# ---------------------------------------------------------------------------
# TLS failure helpers
# ---------------------------------------------------------------------------

# Marker prefix used on RuntimeError messages raised when the underlying
# network probe fails TLS verification. Lets the caller distinguish trust
# failures from auth / 404 / network errors so the user is not pushed down
# the PAT troubleshooting path for a CA-trust problem.
_TLS_ERROR_PREFIX = "TLS verification failed"


def _is_tls_failure(exc: BaseException) -> bool:
    """Return True if exc (or any cause in its chain) is a TLS verification failure."""
    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 8:
        msg = str(cur)
        if _TLS_ERROR_PREFIX in msg or "CERTIFICATE_VERIFY_FAILED" in msg:
            return True
        if isinstance(cur, requests.exceptions.SSLError):
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False


# Marker prefix used on RuntimeError messages raised when the GitHub REST
# probe is throttled (primary 60/hr or secondary concurrency rate limit).
# A throttled response is NOT evidence the repo is missing, so the marker
# lets the caller proceed to the download step (the real source of truth)
# instead of surfacing a false "package not accessible" error.
_RATE_LIMIT_PREFIX = "GitHub API rate limit"


def _is_rate_limit_failure(exc: BaseException) -> bool:
    """Return True if exc (or any cause in its chain) is a rate-limit throttle."""
    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 8:
        if _RATE_LIMIT_PREFIX in str(cur):
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False


def _raise_if_rate_limited(resp, host_display: str) -> None:
    """Raise a marked RuntimeError when *resp* is a GitHub rate-limit throttle.

    GitHub signals primary exhaustion with HTTP 403 + ``X-RateLimit-Remaining: 0``
    and secondary (concurrency) limits with 403/429 + a ``Retry-After`` header.
    Either way the repo's existence is unknown, so the marker lets the caller
    proceed rather than report a false negative. Plain 403s (SSO / permission)
    carry neither signal and fall through to the normal not-accessible path.
    """
    if resp.status_code not in (403, 429):
        return
    remaining = resp.headers.get("X-RateLimit-Remaining")
    retry_after = resp.headers.get("Retry-After")
    if resp.status_code == 429 or remaining == "0" or retry_after:
        raise RuntimeError(f"{_RATE_LIMIT_PREFIX} hit for {host_display} ({resp.status_code})")


def _log_rate_limit_allow(host_display: str, verbose_log, logger) -> None:
    """Note that a throttled probe is allowed through to the download step."""
    if logger:
        logger.info(
            f"GitHub API rate limit hit while checking {host_display}; skipping the "
            "pre-flight accessibility probe and letting the download step confirm the package"
        )
    if verbose_log:
        verbose_log(f"rate limit reached for {host_display}; proceeding to download")


def _log_tls_failure(host_display: str, exc: BaseException, verbose_log, logger) -> None:
    """Surface a TLS verification failure with an actionable CA-trust hint.

    Default verbosity: a single one-liner via ``logger.warning`` so users behind
    a corporate proxy see the right next step without re-running with --verbose.
    Verbose: also include the host name and the underlying exception text.
    """
    logger.warning(
        "TLS verification failed -- if you're behind a corporate proxy or "
        "firewall, set the REQUESTS_CA_BUNDLE environment variable to the "
        "path of your organisation's CA bundle (a PEM file) and retry. "
        "See: https://microsoft.github.io/apm/troubleshooting/ssl-issues/"
    )
    if verbose_log:
        verbose_log(f"underlying error from {host_display}: {exc}")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _local_path_failure_reason(dep_ref):
    """Return a specific failure reason for local path deps, or None for remote."""
    if not (dep_ref.is_local and dep_ref.local_path):
        return None
    local = Path(dep_ref.local_path).expanduser()
    if not local.is_absolute():
        local = Path.cwd() / local
    local = local.resolve()
    if not local.exists():
        return "path does not exist"
    if not local.is_dir():
        return "path is not a directory"
    # Directory exists but has no package markers
    return "no apm.yml, SKILL.md, or plugin.json found"


def _generic_host_ambiguous_subpath_hint(dep_ref) -> str | None:
    """Return an actionable hint when an unrecognised FQDN swallowed a subpath.

    Mirrors GHES (``GITHUB_HOST``): a self-hosted GitLab instance is only
    classified as GitLab-class -- which enables the repo/subpath boundary
    probe for nested groups -- once the user points ``GITLAB_HOST`` or
    ``APM_GITLAB_HOSTS`` at it (issue #2066). Without that, parse-time
    detection has no way to know where the repo ends and the subpath
    begins, so it folds the whole remaining path into ``repo_url``, which
    then fails validation as a single (non-existent) repository. Surface
    *why* instead of a bare "not accessible" so the user knows to
    configure the host rather than suspect a permissions problem.
    """
    host = dep_ref.host
    if not host or dep_ref.is_local or dep_ref.is_virtual:
        return None
    if is_github_hostname(host) or dep_ref.is_azure_devops() or is_gitlab_hostname(host):
        return None
    segments = [seg for seg in dep_ref.repo_url.split("/") if seg]
    if len(segments) <= 2:
        return None
    return (
        f"'{host}' was treated as a single repository path "
        f"('{dep_ref.repo_url}') because it isn't recognised as GitHub, "
        f"Azure DevOps, or GitLab. If this is a self-hosted GitLab instance, "
        f"set GITLAB_HOST={host} (or add it to the comma-separated "
        f"APM_GITLAB_HOSTS) and re-run to enable repo/subpath resolution for "
        f"nested groups. Otherwise, use an explicit 'git:' + 'path:' entry "
        f"in apm.yml."
    )


def _local_path_no_markers_hint(local_dir, logger=None):
    """Scan two levels for sub-packages and print a hint if any are found."""
    from apm_cli.utils.helpers import find_plugin_json

    markers = ("apm.yml", "SKILL.md")
    found = []
    for child in sorted(local_dir.iterdir()):
        if not child.is_dir():
            continue
        if any((child / m).exists() for m in markers) or find_plugin_json(child) is not None:
            found.append(child)
        # Also check one more level (e.g. skills/<name>/)
        for grandchild in sorted(child.iterdir()) if child.is_dir() else []:
            if not grandchild.is_dir():
                continue
            if (
                any((grandchild / m).exists() for m in markers)
                or find_plugin_json(grandchild) is not None
            ):
                found.append(grandchild)

    if not found:
        return

    if logger:
        logger.progress("  [i] Found installable package(s) inside this directory:")
        for p in found[:5]:
            logger.verbose_detail(f"      apm install {p}")
        if len(found) > 5:
            logger.verbose_detail(f"      ... and {len(found) - 5} more")
    else:
        _rich_info("  [i] Found installable package(s) inside this directory:")
        for p in found[:5]:
            _rich_echo(f"      apm install {p}", color="dim")
        if len(found) > 5:
            _rich_echo(f"      ... and {len(found) - 5} more", color="dim")


def _validate_local_package(dep_ref, logger) -> bool:
    """Validate a local-path package: directory must exist and contain package markers.

    Returns True if the directory exists and has ``apm.yml``, ``SKILL.md``, or
    a ``plugin.json`` file.  Returns False and optionally surfaces a sub-package
    hint when markers are absent.
    """
    local = Path(dep_ref.local_path).expanduser()
    if not local.is_absolute():
        local = Path.cwd() / local
    local = local.resolve()
    if not local.is_dir():
        return False
    # Must contain apm.yml, SKILL.md, or plugin.json
    if (local / "apm.yml").exists() or (local / "SKILL.md").exists():
        return True
    from apm_cli.utils.helpers import find_plugin_json

    if find_plugin_json(local) is not None:
        return True
    # Directory exists but lacks package markers -- surface a hint
    _local_path_no_markers_hint(local, logger=logger)
    return False


def _validate_virtual_package(
    dep_ref,
    auth_resolver,
    verbose: bool,
    verbose_log,
    package: str,
    logger,
) -> bool:
    """Validate a virtual package using ``GitHubPackageDownloader``.

    Returns True when ``PROXY_REGISTRY_ONLY=1`` (proxy handles the 404 case),
    or delegates to the downloader's ``validate_virtual_package_exists`` and
    surfaces a verbose auth context on failure.
    """
    from apm_cli.deps.github_downloader import GitHubPackageDownloader

    from ..deps.registry_proxy import is_enforce_only

    if is_enforce_only():
        # PROXY_REGISTRY_ONLY=1: skip virtual package validation probe.
        # The download step will surface a proxy 404 if the package is absent.
        if logger:
            logger.info(
                "Skipping virtual package validation for"
                f" {dep_ref.host or 'remote'}: proxy-only mode is active"
            )
        return True

    ctx = auth_resolver.resolve_for_dep(dep_ref)
    host = dep_ref.host or default_host()
    org = dep_ref.repo_url.split("/")[0] if dep_ref.repo_url and "/" in dep_ref.repo_url else None
    if verbose_log:
        verbose_log(
            f"Auth resolved: host={host}, org={org}, source={ctx.source}, type={ctx.token_type}"
        )
    virtual_downloader = GitHubPackageDownloader(auth_resolver=auth_resolver)

    def _warn(msg: str) -> None:
        # Round-4 panel fix (cli-logging + devx-ux converge):
        #   * Yellow warnings MUST reach the user in BOTH
        #     verbose and non-verbose modes -- the git-fallback
        #     signal is security-relevant (a scoped PAT may
        #     have correctly rejected the package on the API
        #     surface and the broader git-credential chain
        #     accepted it). Operators must see this in default
        #     CI logs.
        #   * Strip the "Run with --verbose for details."
        #     suffix only when --verbose is already set; the
        #     suffix is meaningful only when it tells the user
        #     a follow-up is available.
        #   * Fall back to ``_rich_warning`` when ``logger`` is
        #     None so production callers without a
        #     CommandLogger still emit the yellow signal --
        #     comments are not enforcement.
        display = msg
        verbose_suffix = " Run with --verbose for details."
        if verbose and msg.endswith(verbose_suffix):
            display = msg[: -len(verbose_suffix)]
        if logger:
            logger.warning(display)
        else:
            _rich_warning(display)

    result = virtual_downloader.validate_virtual_package_exists(
        dep_ref,
        verbose_callback=verbose_log,
        warn_callback=_warn,
    )
    if not result and verbose_log:
        try:
            err_ctx = auth_resolver.build_error_context(
                host,
                f"accessing {package}",
                org=org,
                port=dep_ref.port,
                dep_url=dep_ref.repo_url,
            )
            for line in err_ctx.splitlines():
                verbose_log(line)
        except Exception:
            pass
    return result


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
                        # Emit deferred stale-PAT warning via resolver
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


def _validate_github_package(
    dep_ref,
    auth_resolver,
    verbose: bool,
    verbose_log,
    package: str,
    logger,
) -> bool:
    """Validate a GitHub.com (or GHES) package via the GitHub REST API.

    Uses ``AuthResolver.try_with_fallback`` with ``unauth_first=True`` so
    public repos are probed anonymously before burning a rate-limited token.
    Returns True/False; surfaces verbose auth context on failure.
    """
    from ..deps.registry_proxy import is_enforce_only

    host = dep_ref.host or default_host()
    port = dep_ref.port
    org = dep_ref.repo_url.split("/")[0] if dep_ref.repo_url and "/" in dep_ref.repo_url else None
    host_info = auth_resolver.classify_host(host, port=port)

    if is_enforce_only():
        # PROXY_REGISTRY_ONLY=1: skip the GitHub API probe.
        # Marketplace/lockfile resolution already ran through the proxy;
        # the download step will surface a proxy 404 if absent.
        if logger:
            logger.info(f"Skipping direct GitHub API probe for {host}: proxy-only mode is active")
        return True

    if verbose_log:
        ctx = auth_resolver.resolve(host, org=org, port=port)
        verbose_log(
            f"Auth resolved: host={host_info.display_name}, org={org}, "
            f"source={ctx.source}, type={ctx.token_type}"
        )

    def _check_repo(token, git_env) -> bool:
        """Check repo accessibility via GitHub API."""
        api_base = host_info.api_base
        api_url = f"{api_base}/repos/{dep_ref.repo_url}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "apm-cli",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = requests.get(api_url, headers=headers, timeout=15)
        except requests.exceptions.SSLError as e:
            raise RuntimeError(f"TLS verification failed for {host_info.display_name}") from e
        except requests.exceptions.RequestException as e:
            if verbose_log:
                verbose_log(f"API request failed: {e}")
            raise

        if verbose_log:
            verbose_log(f"API {api_url} -> {resp.status_code}")
        if resp.ok:
            return True
        if resp.status_code == 404 and token:
            # 404 with token could mean no access -- raise to trigger fallback
            raise RuntimeError(f"API returned {resp.status_code}")
        _raise_if_rate_limited(resp, host_info.display_name)
        raise RuntimeError(f"API returned {resp.status_code}: {resp.reason}")

    try:
        return auth_resolver.try_with_fallback(
            host,
            _check_repo,
            org=org,
            port=port,
            # dep_ref.repo_url is owner/repo (never a full URL per the
            # DependencyReference invariant); forwarded as path= so GCM
            # multi-account users get per-URL credential matching.
            path=dep_ref.repo_url,
            unauth_first=True,
            verbose_callback=verbose_log,
        )
    except Exception as exc:
        if _is_tls_failure(exc):
            _log_tls_failure(host_info.display_name, exc, verbose_log, logger)
            return False
        if _is_rate_limit_failure(exc):
            _log_rate_limit_allow(host_info.display_name, verbose_log, logger)
            return True
        if verbose_log:
            try:
                ctx = auth_resolver.build_error_context(
                    host,
                    f"accessing {package}",
                    org=org,
                    port=port,
                    dep_url=getattr(dep_ref, "repo_url", None),
                )
                for line in ctx.splitlines():
                    verbose_log(line)
            except Exception:
                pass
        return False


def _validate_parse_failure_fallback(
    package: str,
    auth_resolver,
    verbose_log,
    logger,
) -> bool:
    """Fallback validation used when ``DependencyReference.parse`` raises.

    Treats *package* as a raw ``owner/repo`` slug and probes the GitHub.com
    API.  Rejects anything that doesn't match the strict slug pattern so
    path-confusion sequences cannot reach the API URL or git credential fill.
    """
    from ..deps.registry_proxy import is_enforce_only

    host = default_host()
    org = package.split("/")[0] if "/" in package else None
    repo_path = package  # owner/repo format
    # Defensive owner/repo guard: when DependencyReference.parse raises,
    # we fall back to embedding `repo_path` directly into an API URL and
    # forwarding it as `path=` to git credential fill. Reject anything
    # that isn't a strict <owner>/<repo> slug so path-confusion sequences
    # (`../`, embedded slashes, control bytes) cannot reach either sink.
    # Allows GitHub's documented owner/repo characters: alphanumeric,
    # dot, underscore, hyphen.
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo_path):
        return False

    if is_enforce_only():
        # PROXY_REGISTRY_ONLY=1: skip the GitHub API fallback probe.
        # The download step will surface a proxy 404 if the package is absent.
        if logger:
            logger.info(
                f"Skipping direct GitHub API fallback probe for {host}: proxy-only mode is active"
            )
        return True

    def _check_repo_fallback(token, git_env) -> bool:
        host_info = auth_resolver.classify_host(host)
        api_url = f"{host_info.api_base}/repos/{repo_path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "apm-cli",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = requests.get(api_url, headers=headers, timeout=15)
        except requests.exceptions.SSLError as e:
            raise RuntimeError(f"TLS verification failed for {host_info.display_name}") from e
        except requests.exceptions.RequestException as e:
            if verbose_log:
                verbose_log(f"API fallback failed: {e}")
            raise

        if resp.ok:
            return True
        if verbose_log:
            verbose_log(f"API fallback -> {resp.status_code} {resp.reason}")
        _raise_if_rate_limited(resp, host_info.display_name)
        raise RuntimeError(f"API returned {resp.status_code}")

    try:
        return auth_resolver.try_with_fallback(
            host,
            _check_repo_fallback,
            org=org,
            path=repo_path,
            unauth_first=True,
            verbose_callback=verbose_log,
        )
    except Exception as exc:
        if _is_tls_failure(exc):
            # See note above: logged once here, skip auth context render.
            _log_tls_failure(host, exc, verbose_log, logger)
            return False
        if _is_rate_limit_failure(exc):
            _log_rate_limit_allow(host, verbose_log, logger)
            return True
        if verbose_log:
            try:
                ctx = auth_resolver.build_error_context(
                    host, f"accessing {package}", org=org, dep_url=package
                )
                for line in ctx.splitlines():
                    verbose_log(line)
            except Exception:
                pass
        return False


def _validate_package_exists(package, verbose=False, auth_resolver=None, logger=None, dep_ref=None):
    """Validate that a package exists and is accessible on GitHub, Azure DevOps, or locally.

    When *dep_ref* is provided (for example, marketplace GitLab monorepo
    resolution), use it instead of reparsing *package* so explicit ``git`` +
    ``path`` semantics are preserved.

    Dispatches to per-backend helpers:

    - ``_validate_local_package`` -- local filesystem paths
    - ``_validate_virtual_package`` -- virtual monorepo packages
    - ``_validate_ado_git_package`` -- ADO / GHES / generic git hosts
    - ``_validate_github_package`` -- GitHub.com REST API
    - ``_validate_parse_failure_fallback`` -- raw slug fallback
    """
    from apm_cli.core.auth import AuthResolver

    if logger:
        verbose_log = (lambda msg: logger.verbose_detail(f"  {msg}")) if verbose else None
    else:
        verbose_log = (lambda msg: _rich_echo(f"  {msg}", color="dim")) if verbose else None
    # Use provided resolver or create new one if not in a CLI session context
    if auth_resolver is None:
        auth_resolver = AuthResolver()

    try:
        from apm_cli.models.apm_package import DependencyReference
        from apm_cli.utils.github_host import is_github_hostname

        if dep_ref is None:
            dep_ref = DependencyReference.parse(package)

        # For local packages, validate directory exists and has valid package content
        if dep_ref.is_local and dep_ref.local_path:
            return _validate_local_package(dep_ref, logger)

        # ``virtual_subdir_repo_probe``: a virtual subdirectory on a non-GitHub,
        # non-ADO host must validate the clone root via git ls-remote rather than
        # the virtual downloader so SSH/credential-helper flows are preserved.
        virtual_subdir_repo_probe = (
            dep_ref.is_virtual
            and dep_ref.is_virtual_subdirectory()
            and not is_github_hostname(dep_ref.host or default_host())
            and not dep_ref.is_azure_devops()
        )

        # For virtual packages, use the downloader's validation method unless
        # the virtual path is a subdirectory on a non-GitHub host. Those should
        # validate the clone root with git, preserving SSH/credential-helper flows.
        if dep_ref.is_virtual and not virtual_subdir_repo_probe:
            return _validate_virtual_package(
                dep_ref, auth_resolver, verbose, verbose_log, package, logger
            )

        # For Azure DevOps or GitHub Enterprise (non-github.com hosts),
        # use git ls-remote which handles authentication properly.
        if (
            virtual_subdir_repo_probe
            or dep_ref.is_azure_devops()
            or (dep_ref.host and dep_ref.host != "github.com")
        ):
            return _validate_ado_git_package(dep_ref, auth_resolver, verbose_log, package, logger)

        # For GitHub.com, use AuthResolver with unauth-first fallback
        return _validate_github_package(
            dep_ref, auth_resolver, verbose, verbose_log, package, logger
        )

    except AuthenticationError:
        # #1015: let auth failures propagate to the caller for proper
        # rendering -- the outer try/except is only for parse failures.
        raise
    except Exception:
        # If parsing fails, assume it's a regular GitHub package
        return _validate_parse_failure_fallback(package, auth_resolver, verbose_log, logger)
