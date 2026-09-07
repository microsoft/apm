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

from ..deps.github_rate_limit import GitHubThrottleError, raise_for_github_throttle
from ..models.dependency.host_virtual import (
    dependency_repository_owner,
    repository_owner,
    repository_path_segments,
)
from ..utils.console import _rich_echo, _rich_info, _rich_warning
from ..utils.git_env import (
    GitUrlRewriteError,
    GitUrlRewriteProbeError,
    redact_git_diagnostic,
)
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


class _GitHubRestStatusError(RuntimeError):
    """Preserve a failed GitHub REST response status across auth fallback."""

    def __init__(self, status_code: int, reason: str) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"GitHub API returned HTTP {status_code}: {reason}")


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


def _inconclusive_github_probe_detail(exc: BaseException) -> str | None:
    """Describe failures that leave repository accessibility unproven."""
    if _is_tls_failure(exc):
        return None
    if isinstance(exc, _GitHubRestStatusError):
        if exc.status_code == 408 or 500 <= exc.status_code < 600:
            return f"HTTP {exc.status_code}"
        return None
    if isinstance(exc, requests.exceptions.Timeout):
        return "request timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection failure"
    return None


def _allow_download_after_inconclusive_github_probe(
    exc: BaseException,
    host_display: str,
    verbose_log,
) -> bool:
    """Let the downloader decide only when the REST probe was inconclusive."""
    detail = _inconclusive_github_probe_detail(exc)
    if detail is None:
        return False
    if verbose_log:
        verbose_log(
            f"GitHub API pre-flight for {host_display} was inconclusive ({detail}); "
            "continuing because the download step will verify repository access"
        )
    return True


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
        "TLS verification failed -- APM uses the system trust store by default. "
        "If you're behind a corporate proxy or firewall, make sure your "
        "organisation's CA is installed in the OS trust store, or set "
        "REQUESTS_CA_BUNDLE to a readable PEM bundle and retry. "
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
    segments = repository_path_segments(dep_ref.repo_url)
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

    host = dep_ref.host or default_host()
    org = dependency_repository_owner(dep_ref)
    if verbose_log:
        if (
            auth_resolver.uses_public_github_anonymous_first(
                host,
                port=dep_ref.port,
                host_type=dep_ref.host_type,
            )
            is True
        ):
            verbose_log(
                f"Auth deferred: host={host}, org={org} -- "
                "anonymous probe before credential resolution"
            )
        else:
            ctx = auth_resolver.resolve_for_dep(dep_ref)
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
    public_github = auth_resolver.uses_public_github_anonymous_first(
        host,
        port=dep_ref.port,
        host_type=dep_ref.host_type,
    )
    auth_was_resolved = auth_resolver.has_cached_resolution(
        host,
        org,
        port=dep_ref.port,
        host_type=dep_ref.host_type,
        path=dep_ref.repo_url,
    )
    if not result and verbose_log and (public_github is not True or auth_was_resolved):
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
    *,
    protocol_pref=None,
    allow_protocol_fallback: bool | None = None,
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

    from apm_cli.deps.github_downloader import GitHubPackageDownloader
    from apm_cli.deps.transport_selection import (
        ProtocolPreference,
        is_fallback_allowed,
        protocol_pref_from_env,
    )
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

    # Determine host type before selecting each transport attempt's canonical
    # AuthResolver environment. GitLab uses managed header authentication;
    # generic hosts retain only the helper policy selected for that transport.
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

    ado_downloader = GitHubPackageDownloader(auth_resolver=auth_resolver)
    if dep_ref.host:
        ado_downloader.github_host = dep_ref.host

    resolved_pref = (
        protocol_pref if isinstance(protocol_pref, ProtocolPreference) else protocol_pref_from_env()
    )
    resolved_fallback = (
        is_fallback_allowed() if allow_protocol_fallback is None else allow_protocol_fallback
    )
    explicit_scheme = (getattr(dep_ref, "explicit_scheme", None) or "").lower() or None
    candidate_uses_ssh = explicit_scheme == "ssh" or (
        explicit_scheme is None and resolved_pref == ProtocolPreference.SSH
    )
    candidate_url = ado_downloader._build_repo_url(
        dep_ref.repo_url,
        use_ssh=candidate_uses_ssh,
        dep_ref=dep_ref,
        token="",
    )
    dep_ctx = None if is_generic else auth_resolver.resolve_for_dep(dep_ref)
    auth_scheme = getattr(dep_ctx, "auth_scheme", "basic") or "basic"
    transport_plan = ado_downloader._transport_selector.select(
        dep_ref=dep_ref,
        cli_pref=resolved_pref,
        allow_fallback=resolved_fallback,
        has_token=bool(dep_ctx and getattr(dep_ctx, "token", None)),
        candidate_url=candidate_url,
    )
    attempts: list[tuple[object, str, dict[str, str]]] = []
    org = dependency_repository_owner(dep_ref)
    for attempt in transport_plan.attempts:
        if attempt.requested_url is not None:
            probe_url = attempt.requested_url
        else:
            probe_url = ado_downloader._build_repo_url(
                dep_ref.repo_url,
                use_ssh=attempt.scheme == "ssh",
                dep_ref=dep_ref,
                token="",
                auth_scheme=auth_scheme if attempt.use_token else "basic",
            )
        policy_url = attempt.effective_url or probe_url
        attempt_ctx = dep_ctx
        if is_generic:
            attempt_ctx = auth_resolver.resolve_for_remote(
                dep_ref.host,
                policy_url,
                org,
                port=dep_ref.port,
                host_type=dep_ref.host_type,
            )
        if attempt_ctx is None:
            raise RuntimeError("Git validation could not resolve host authentication policy")
        probe_env = auth_resolver.git_env_for_remote(attempt_ctx, policy_url)
        attempts.append((attempt, probe_url, probe_env))

    if verbose_log:
        attempt_word = "attempt" if len(attempts) == 1 else "attempts"
        verbose_log(f"Trying git ls-remote for {dep_ref.host} ({len(attempts)} {attempt_word})")

    def _scheme_of(url: str) -> str:
        return url.split("://", 1)[0] if "://" in url else "ssh"

    def _log_attempt_result(
        scheme: str,
        probe_env: dict[str, str],
        run_result,
    ) -> None:
        """Per-attempt sanitized verbose logging.

        The previous implementation only logged the final attempt's
        result, which masked the actual failure (typically the HTTPS
        leg) behind the SSH-fallback timeout. Logging each attempt
        gives users the diagnostic data they need to act.
        """
        if not verbose_log:
            return
        if run_result.returncode == 0:
            verbose_log(f"git ls-remote ({scheme}) rc=0 for {package}")
            return
        raw_stderr = (run_result.stderr or "").strip()[:200]
        stderr_snippet = redact_git_diagnostic(raw_stderr)
        for env_var in ("GIT_ASKPASS", "GIT_CONFIG_GLOBAL"):
            env_val = probe_env.get(env_var, "")
            if env_val:
                stderr_snippet = stderr_snippet.replace(env_val, "***")
        verbose_log(f"git ls-remote ({scheme}) rc={run_result.returncode}: {stderr_snippet}")

    def _probe_attempts(
        probe_attempts: list[tuple[object, str, dict[str, str]]],
    ) -> tuple[object | None, object | None, str | None, dict[str, str] | None]:
        run_result = None
        final_attempt = None
        final_url = None
        final_env = None
        for attempt, probe_url, probe_env in probe_attempts:
            from ..utils.git_env import git_remote_refs

            run_result = git_remote_refs(
                probe_url,
                timeout=30,
                env=probe_env,
                options=("--heads", "--exit-code"),
            )
            final_attempt = attempt
            final_url = probe_url
            final_env = probe_env
            _log_attempt_result(
                getattr(attempt, "scheme", _scheme_of(probe_url)),
                probe_env,
                run_result,
            )
            if run_result.returncode == 0:
                break
        return run_result, final_attempt, final_url, final_env

    bearer_attempted = False
    primary_outcome = None

    def _primary_probe():
        nonlocal primary_outcome
        primary_outcome = _probe_attempts(attempts)
        return primary_outcome

    def _bearer_probe(bearer):
        if dep_ctx is None or primary_outcome is None or primary_outcome[2] is None:
            return primary_outcome
        primary_attempt = primary_outcome[1]
        primary_url = primary_outcome[2]
        bearer_url = ado_downloader._build_repo_url(
            dep_ref.repo_url,
            use_ssh=False,
            dep_ref=dep_ref,
            token=None,
            auth_scheme="bearer",
        )
        if getattr(primary_attempt, "requested_url", None) is not None:
            bearer_url = primary_url
        bearer_env = auth_resolver.build_ado_bearer_git_env(
            dep_ctx,
            bearer,
            getattr(primary_attempt, "effective_url", None) or bearer_url,
            base_env=ado_downloader.git_env,
        )
        return _probe_attempts([(primary_attempt, bearer_url, bearer_env)])

    def _outcome_is_ado_auth_failure(outcome) -> bool:
        if outcome is None or outcome[0] is None or outcome[1] is None:
            return False
        run_result, attempt = outcome[0], outcome[1]
        return bool(
            getattr(dep_ctx, "source", "") == "ADO_APM_PAT"
            and getattr(attempt, "scheme", None) == "https"
            and getattr(attempt, "use_token", False)
            and run_result.returncode != 0
            and is_ado_auth_failure_signal(run_result.stderr or "")
        )

    if dep_ref.is_azure_devops():
        fallback = auth_resolver.execute_with_bearer_fallback(
            dep_ref,
            _primary_probe,
            _bearer_probe,
            _outcome_is_ado_auth_failure,
        )
        result, _failed_attempt, _failed_url, _failed_env = fallback.outcome
        bearer_attempted = fallback.bearer_attempted
    else:
        result, _failed_attempt, _failed_url, _failed_env = _primary_probe()

    if result is None:
        return False

    # ADO PAT-to-bearer fallback is owned by AuthResolver. ADO validation
    # never constructs or invokes a native Git credential-helper attempt.

    # Per-attempt verbose logging is emitted inside the probe loop
    # (and by the bearer-fallback branch above), so the result is
    # already on screen by the time we get here. Git diagnostics are
    # redacted by the canonical helper before verbose rendering.

    # #1015: distinguish auth failures from non-auth failures (DNS,
    # timeout, repo-truly-not-found 404). Auth failures get a typed
    # exception with actionable diagnostics; non-auth failures keep
    # the legacy False return so the caller can word its own message.
    if result.returncode != 0 and not is_generic:
        if is_ado_auth_failure_signal(result.stderr or ""):
            _host = dep_ref.host or "dev.azure.com"
            _org = dependency_repository_owner(dep_ref)
            _diag = auth_resolver.build_error_context(
                _host,
                "validate",
                org=_org,
                dep_url=dep_ref.repo_url,
                bearer_also_failed=bearer_attempted,
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
    org = dependency_repository_owner(dep_ref)
    host_info = auth_resolver.classify_host(host, port=port)

    if is_enforce_only():
        # PROXY_REGISTRY_ONLY=1: skip the GitHub API probe.
        # Marketplace/lockfile resolution already ran through the proxy;
        # the download step will surface a proxy 404 if absent.
        if logger:
            logger.info(f"Skipping direct GitHub API probe for {host}: proxy-only mode is active")
        return True

    if verbose_log:
        if auth_resolver.uses_public_github_anonymous_first(host, port=port) is True:
            verbose_log(
                f"Auth deferred: host={host_info.display_name}, org={org} -- "
                "anonymous probe before credential resolution"
            )
        else:
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
        raise_for_github_throttle(resp, host_info.display_name)
        raise _GitHubRestStatusError(resp.status_code, resp.reason)

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
            host_type=dep_ref.host_type,
            unauth_first=True,
            verbose_callback=verbose_log,
        )
    except GitHubThrottleError:
        _log_rate_limit_allow(host_info.display_name, verbose_log, logger)
        return True
    except Exception as exc:
        if _is_tls_failure(exc):
            _log_tls_failure(host_info.display_name, exc, verbose_log, logger)
            return False
        if _allow_download_after_inconclusive_github_probe(
            exc, host_info.display_name, verbose_log
        ):
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
    org = repository_owner(package)
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
        raise_for_github_throttle(resp, host_info.display_name)
        raise _GitHubRestStatusError(resp.status_code, resp.reason)

    try:
        return auth_resolver.try_with_fallback(
            host,
            _check_repo_fallback,
            org=org,
            path=repo_path,
            unauth_first=True,
            verbose_callback=verbose_log,
        )
    except GitHubThrottleError:
        _log_rate_limit_allow(host, verbose_log, logger)
        return True
    except Exception as exc:
        if _is_tls_failure(exc):
            # See note above: logged once here, skip auth context render.
            _log_tls_failure(host, exc, verbose_log, logger)
            return False
        if _allow_download_after_inconclusive_github_probe(exc, host, verbose_log):
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


def _validate_package_exists(
    package,
    verbose=False,
    auth_resolver=None,
    logger=None,
    dep_ref=None,
    *,
    protocol_pref=None,
    allow_protocol_fallback: bool | None = None,
):
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
            return _validate_ado_git_package(
                dep_ref,
                auth_resolver,
                verbose_log,
                package,
                logger,
                protocol_pref=protocol_pref,
                allow_protocol_fallback=allow_protocol_fallback,
            )

        # For GitHub.com, use AuthResolver with unauth-first fallback
        return _validate_github_package(
            dep_ref, auth_resolver, verbose, verbose_log, package, logger
        )

    except AuthenticationError:
        # #1015: let auth failures propagate to the caller for proper
        # rendering -- the outer try/except is only for parse failures.
        raise
    except (GitUrlRewriteError, GitUrlRewriteProbeError):
        raise
    except GitHubThrottleError:
        # A virtual-file probe carries a typed throttle through its local
        # validation helpers so it cannot become a generic false negative.
        _log_rate_limit_allow(dep_ref.host or default_host(), verbose_log, logger)
        return True
    except Exception:
        # If parsing fails, assume it's a regular GitHub package
        return _validate_parse_failure_fallback(package, auth_resolver, verbose_log, logger)
