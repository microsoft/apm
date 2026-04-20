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

from ..utils.console import _rich_echo, _rich_info
from ..utils.github_host import default_host


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
                    err_ctx = auth_resolver.build_error_context(host, f"accessing {package}", org=org)
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
            if not is_generic:
                _dep_ctx = auth_resolver.resolve_for_dep(dep_ref)
                _url_token = _dep_ctx.token

            ado_downloader = GitHubPackageDownloader(auth_resolver=auth_resolver)
            # Set the host
            if dep_ref.host:
                ado_downloader.github_host = dep_ref.host

            # Build authenticated URL using the resolved per-dep token.
            package_url = ado_downloader._build_repo_url(
                dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref, token=_url_token
            )

            # For generic hosts (not GitHub, not ADO), relax the env so native
            # credential helpers (SSH keys, macOS Keychain, etc.) can work.
            # This mirrors _clone_with_fallback() which does the same relaxation.
            if is_generic:
                validate_env = {k: v for k, v in ado_downloader.git_env.items()
                                if k not in ('GIT_ASKPASS', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM')}
                validate_env['GIT_TERMINAL_PROMPT'] = '0'
            else:
                validate_env = {**os.environ, **ado_downloader.git_env}

            if verbose_log:
                verbose_log(f"Trying git ls-remote for {dep_ref.host}")

            # For generic hosts, try SSH first (no credentials needed when SSH
            # keys are configured) before falling back to HTTPS.
            urls_to_try = []
            if is_generic:
                ssh_url = ado_downloader._build_repo_url(
                    dep_ref.repo_url, use_ssh=True, dep_ref=dep_ref
                )
                urls_to_try = [ssh_url, package_url]
            else:
                urls_to_try = [package_url]

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
                if result.returncode == 0:
                    break

            if verbose_log:
                if result.returncode == 0:
                    verbose_log(f"git ls-remote rc=0 for {package}")
                else:
                    # Sanitize stderr to avoid leaking tokens.  Two layers:
                    # 1) scrub PAT-bearing URLs (git often echoes the URL
                    #    in error messages -- the URL we built above
                    #    embeds _url_token).  Use the same sanitizer the
                    #    downloader uses for clone errors.
                    # 2) belt-and-suspenders: also redact any literal env
                    #    values that may have leaked through unrelated
                    #    diagnostics paths.
                    raw_stderr = (result.stderr or "").strip()[:200]
                    stderr_snippet = ado_downloader._sanitize_git_error(raw_stderr)
                    for env_var in ("GIT_ASKPASS", "GIT_CONFIG_GLOBAL"):
                        env_val = validate_env.get(env_var, "")
                        if env_val:
                            stderr_snippet = stderr_snippet.replace(env_val, "***")
                    verbose_log(f"git ls-remote rc={result.returncode}: {stderr_snippet}")

            return result.returncode == 0

        # For GitHub.com, use AuthResolver with unauth-first fallback
        host = dep_ref.host or default_host()
        org = dep_ref.repo_url.split('/')[0] if dep_ref.repo_url and '/' in dep_ref.repo_url else None
        host_info = auth_resolver.classify_host(host)

        if verbose_log:
            ctx = auth_resolver.resolve(host, org=org)
            verbose_log(f"Auth resolved: host={host}, org={org}, source={ctx.source}, type={ctx.token_type}")

        def _check_repo(token, git_env):
            """Check repo accessibility via GitHub API (or git ls-remote for non-GitHub)."""
            import urllib.request
            import urllib.error

            api_base = host_info.api_base
            api_url = f"{api_base}/repos/{dep_ref.repo_url}"
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": "apm-cli",
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"

            req = urllib.request.Request(api_url, headers=headers)
            try:
                resp = urllib.request.urlopen(req, timeout=15)
                if verbose_log:
                    verbose_log(f"API {api_url} -> {resp.status}")
                return True
            except urllib.error.HTTPError as e:
                if verbose_log:
                    verbose_log(f"API {api_url} -> {e.code} {e.reason}")
                if e.code == 404 and token:
                    # 404 with token could mean no access -- raise to trigger fallback
                    raise RuntimeError(f"API returned {e.code}")
                raise RuntimeError(f"API returned {e.code}: {e.reason}")
            except Exception as e:
                if verbose_log:
                    verbose_log(f"API request failed: {e}")
                raise

        try:
            return auth_resolver.try_with_fallback(
                host, _check_repo,
                org=org,
                unauth_first=True,
                verbose_callback=verbose_log,
            )
        except Exception:
            if verbose_log:
                try:
                    ctx = auth_resolver.build_error_context(host, f"accessing {package}", org=org)
                    for line in ctx.splitlines():
                        verbose_log(line)
                except Exception:
                    pass
            return False

    except Exception:
        # If parsing fails, assume it's a regular GitHub package
        host = default_host()
        org = package.split('/')[0] if '/' in package else None
        repo_path = package  # owner/repo format

        def _check_repo_fallback(token, git_env):
            import urllib.request
            import urllib.error

            host_info = auth_resolver.classify_host(host)
            api_url = f"{host_info.api_base}/repos/{repo_path}"
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": "apm-cli",
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"

            req = urllib.request.Request(api_url, headers=headers)
            try:
                resp = urllib.request.urlopen(req, timeout=15)
                return True
            except urllib.error.HTTPError as e:
                if verbose_log:
                    verbose_log(f"API fallback -> {e.code} {e.reason}")
                raise RuntimeError(f"API returned {e.code}")
            except Exception as e:
                if verbose_log:
                    verbose_log(f"API fallback failed: {e}")
                raise

        try:
            return auth_resolver.try_with_fallback(
                host, _check_repo_fallback,
                org=org,
                unauth_first=True,
                verbose_callback=verbose_log,
            )
        except Exception:
            if verbose_log:
                try:
                    ctx = auth_resolver.build_error_context(host, f"accessing {package}", org=org)
                    for line in ctx.splitlines():
                        verbose_log(line)
                except Exception:
                    pass
            return False
