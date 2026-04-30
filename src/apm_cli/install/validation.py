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
"""

from pathlib import Path

import requests

from ..utils.console import _rich_echo, _rich_info
from ..utils.github_host import default_host
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
            if any((grandchild / m).exists() for m in markers) or find_plugin_json(grandchild) is not None:
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


def _validate_package_exists(package, verbose=False, auth_resolver=None, logger=None):
    """Validate that a package exists and is accessible on GitHub, Azure DevOps, or locally."""
    import os
    import subprocess
    import tempfile
    from apm_cli.core.auth import AuthResolver

    if logger:
        verbose_log = (lambda msg: logger.verbose_detail(f"  {msg}")) if verbose else None
    else:
        verbose_log = (lambda msg: _rich_echo(f"  {msg}", color="dim")) if verbose else None
    # Use provided resolver or create new one if not in a CLI session context
    if auth_resolver is None:
        auth_resolver = AuthResolver()

    try:
        # Parse the package to check if it's a virtual package or ADO
        from apm_cli.models.apm_package import DependencyReference
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        dep_ref = DependencyReference.parse(package)

        # For local packages, validate directory exists and has valid package content
        if dep_ref.is_local and dep_ref.local_path:
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

        # For virtual packages, use the downloader's validation method
        if dep_ref.is_virtual:
            ctx = auth_resolver.resolve_for_dep(dep_ref)
            host = dep_ref.host or default_host()
            org = dep_ref.repo_url.split('/')[0] if dep_ref.repo_url and '/' in dep_ref.repo_url else None
            if verbose_log:
                verbose_log(f"Auth resolved: host={host}, org={org}, source={ctx.source}, type={ctx.token_type}")
            virtual_downloader = GitHubPackageDownloader(auth_resolver=auth_resolver)
            result = virtual_downloader.validate_virtual_package_exists(dep_ref)
            if not result and verbose_log:
                try:
                    err_ctx = auth_resolver.build_error_context(
                        host, f"accessing {package}", org=org, port=dep_ref.port,
                        dep_url=dep_ref.repo_url,
                    )
                    for line in err_ctx.splitlines():
                        verbose_log(line)
                except Exception:
                    pass
            return result

        # For Azure DevOps or GitHub Enterprise (non-github.com hosts),
        # use the downloader which handles authentication properly
        if dep_ref.is_azure_devops() or (dep_ref.host and dep_ref.host != "github.com"):
            from apm_cli.utils.github_host import is_github_hostname, is_azure_devops_hostname

            # Determine host type before building the URL so we know whether to
            # embed a token.  Generic (non-GitHub, non-ADO) hosts are excluded
            # from APM-managed auth; they rely on git credential helpers via the
            # relaxed validate_env below.
            is_generic = not is_github_hostname(dep_ref.host) and not is_azure_devops_hostname(dep_ref.host)

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
                dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref,
                token=_url_token, auth_scheme=_auth_scheme,
            )

            explicit_scheme = (getattr(dep_ref, "explicit_scheme", None) or "").lower() or None
            is_insecure = bool(getattr(dep_ref, "is_insecure", False))
            prefer_web_probe_first = explicit_scheme in ("http", "https") or is_insecure

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
            from apm_cli.deps.transport_selection import is_fallback_allowed
            allow_fallback_env = is_fallback_allowed()

            # For generic hosts (not GitHub, not ADO), relax the env so native
            # credential helpers (SSH keys, macOS Keychain, etc.) can work.
            # This mirrors _clone_with_fallback() which does the same relaxation.
            if is_generic:
                validate_env = ado_downloader._build_noninteractive_git_env(
                    preserve_config_isolation=prefer_web_probe_first,
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
            urls_to_try = []
            if is_generic:
                ssh_url = ado_downloader._build_repo_url(
                    dep_ref.repo_url, use_ssh=True, dep_ref=dep_ref
                )
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
                verbose_log(
                    f"Trying git ls-remote for {dep_ref.host} "
                    f"({len(urls_to_try)} {attempt_word})"
                )

            def _scheme_of(url: str) -> str:
                return url.split("://", 1)[0] if "://" in url else "ssh"

            def _log_attempt_result(probe_url: str, run_result):
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
                verbose_log(
                    f"git ls-remote ({scheme}) rc={run_result.returncode}: "
                    f"{stderr_snippet}"
                )

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
                and (
                    "401" in (result.stderr or "")
                    or "Authentication failed" in (result.stderr or "")
                    or "Unauthorized" in (result.stderr or "")
                )
            ):
                try:
                    from apm_cli.core.azure_cli import AzureCliBearerError, get_bearer_provider
                    from apm_cli.utils.github_host import build_ado_bearer_git_env
                    provider = get_bearer_provider()
                    if provider.is_available():
                        try:
                            bearer = provider.get_bearer_token()
                            bearer_url = ado_downloader._build_repo_url(
                                dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref,
                                token=None, auth_scheme="bearer",
                            )
                            bearer_env = {**validate_env, **build_ado_bearer_git_env(bearer)}
                            cmd = ["git", "ls-remote", "--heads", "--exit-code", bearer_url]
                            bearer_result = subprocess.run(
                                cmd, capture_output=True, text=True,
                                encoding="utf-8", timeout=30, env=bearer_env,
                            )
                            if bearer_result.returncode == 0:
                                # Emit deferred stale-PAT warning via resolver
                                auth_resolver.emit_stale_pat_diagnostic(
                                    dep_ref.host or "dev.azure.com"
                                )
                                if verbose_log:
                                    verbose_log(
                                        f"git ls-remote rc=0 for {package} "
                                        f"(via AAD bearer fallback)"
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
                _stderr = (result.stderr or "").lower()
                _auth_signals = (
                    "401" in _stderr
                    or "403" in _stderr
                    or "authentication failed" in _stderr
                    or "unauthorized" in _stderr
                    or "could not read username" in _stderr
                )
                if _auth_signals:
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

        # For GitHub.com, use AuthResolver with unauth-first fallback
        host = dep_ref.host or default_host()
        port = dep_ref.port
        org = dep_ref.repo_url.split('/')[0] if dep_ref.repo_url and '/' in dep_ref.repo_url else None
        host_info = auth_resolver.classify_host(host, port=port)

        if verbose_log:
            ctx = auth_resolver.resolve(host, org=org, port=port)
            verbose_log(
                f"Auth resolved: host={host_info.display_name}, org={org}, "
                f"source={ctx.source}, type={ctx.token_type}"
            )

        def _check_repo(token, git_env):
            """Check repo accessibility via GitHub API (or git ls-remote for non-GitHub)."""
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
                raise RuntimeError(
                    f"TLS verification failed for {host_info.display_name}"
                ) from e
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
            raise RuntimeError(f"API returned {resp.status_code}: {resp.reason}")

        try:
            return auth_resolver.try_with_fallback(
                host, _check_repo,
                org=org,
                port=port,
                unauth_first=True,
                verbose_callback=verbose_log,
            )
        except Exception as exc:
            if _is_tls_failure(exc):
                _log_tls_failure(host_info.display_name, exc, verbose_log, logger)
                return False
            if verbose_log:
                try:
                    ctx = auth_resolver.build_error_context(
                        host, f"accessing {package}", org=org, port=port,
                        dep_url=getattr(dep_ref, "repo_url", None),
                    )
                    for line in ctx.splitlines():
                        verbose_log(line)
                except Exception:
                    pass
            return False

    except AuthenticationError:
        # #1015: let auth failures propagate to the caller for proper
        # rendering -- the outer try/except is only for parse failures.
        raise
    except Exception:
        # If parsing fails, assume it's a regular GitHub package
        host = default_host()
        org = package.split('/')[0] if '/' in package else None
        repo_path = package  # owner/repo format

        def _check_repo_fallback(token, git_env):
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
                raise RuntimeError(
                    f"TLS verification failed for {host_info.display_name}"
                ) from e
            except requests.exceptions.RequestException as e:
                if verbose_log:
                    verbose_log(f"API fallback failed: {e}")
                raise

            if resp.ok:
                return True
            if verbose_log:
                verbose_log(f"API fallback -> {resp.status_code} {resp.reason}")
            raise RuntimeError(f"API returned {resp.status_code}")

        try:
            return auth_resolver.try_with_fallback(
                host, _check_repo_fallback,
                org=org,
                unauth_first=True,
                verbose_callback=verbose_log,
            )
        except Exception as exc:
            if _is_tls_failure(exc):
                # See note above: logged once here, skip auth context render.
                _log_tls_failure(host, exc, verbose_log, logger)
                return False
            if verbose_log:
                try:
                    ctx = auth_resolver.build_error_context(host, f"accessing {package}", org=org, dep_url=package)
                    for line in ctx.splitlines():
                        verbose_log(line)
                except Exception:
                    pass
            return False
