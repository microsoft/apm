"""Manifest validation: package existence checks, dependency syntax canonicalisation.

This module is the public surface of ``apm_cli.install.validation``.  Heavy
helpers have been extracted into focused sibling modules:

* :mod:`apm_cli.install.validation.tls` — TLS verification helpers
* :mod:`apm_cli.install.validation.local_path` — local-path probe helpers
* :mod:`apm_cli.install.validation._git_ls_remote` — ADO / GHES / generic
  git-ls-remote validation (private, imported lazily to avoid circular
  imports)

The public names re-exported here are:

* :func:`_validate_package_exists` — primary entry point kept here because
  test suites patch ``apm_cli.install.validation.requests.get`` and
  ``apm_cli.install.validation._rich_warning`` by fully-qualified name; both
  must resolve in this module's namespace for the patches to intercept calls.
* :func:`_local_path_failure_reason`, :func:`_local_path_no_markers_hint` —
  forwarded from :mod:`~.local_path`.
* :func:`_is_tls_failure`, :func:`_log_tls_failure` — forwarded from
  :mod:`~.tls`.
"""

from __future__ import annotations

import re
from pathlib import Path

import requests  # noqa: F401 – must stay: tests patch apm_cli.install.validation.requests.get

from apm_cli.install.errors import AuthenticationError
from apm_cli.utils.console import _rich_echo, _rich_warning  # _rich_warning patched in tests
from apm_cli.utils.github_host import default_host, is_ado_auth_failure_signal  # noqa: F401

from .local_path import _local_path_failure_reason, _local_path_no_markers_hint  # noqa: F401
from .tls import _TLS_ERROR_PREFIX, _is_tls_failure, _log_tls_failure  # noqa: F401

__all__ = [
    "_TLS_ERROR_PREFIX",
    "_is_tls_failure",
    "_local_path_failure_reason",
    "_local_path_no_markers_hint",
    "_log_tls_failure",
    "_validate_package_exists",
]


def _validate_package_exists(package, verbose=False, auth_resolver=None, logger=None, dep_ref=None):
    """Validate that a package exists and is accessible on GitHub, Azure DevOps, or locally.

    When *dep_ref* is provided (for example, marketplace GitLab monorepo
    resolution), use it instead of reparsing *package* so explicit ``git`` +
    ``path`` semantics are preserved.
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
        # Parse the package to check if it's a virtual package or ADO
        from apm_cli.deps.github_downloader import GitHubPackageDownloader
        from apm_cli.models.apm_package import DependencyReference

        if dep_ref is None:
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

        from apm_cli.utils.github_host import is_azure_devops_hostname, is_github_hostname

        from ...deps.registry_proxy import is_enforce_only

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
            org = (
                dep_ref.repo_url.split("/")[0]
                if dep_ref.repo_url and "/" in dep_ref.repo_url
                else None
            )
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

        # For Azure DevOps or GitHub Enterprise (non-github.com hosts),
        # delegate to the focused git-ls-remote validator in the sibling module.
        if (
            virtual_subdir_repo_probe
            or dep_ref.is_azure_devops()
            or (dep_ref.host and dep_ref.host != "github.com")
        ):
            from ._git_ls_remote import _validate_via_git_ls_remote

            return _validate_via_git_ls_remote(
                dep_ref,
                package,
                auth_resolver,
                verbose_log,
                virtual_subdir_repo_probe,
            )

        # For GitHub.com, use AuthResolver with unauth-first fallback
        host = dep_ref.host or default_host()
        port = dep_ref.port
        org = (
            dep_ref.repo_url.split("/")[0] if dep_ref.repo_url and "/" in dep_ref.repo_url else None
        )
        host_info = auth_resolver.classify_host(host, port=port)

        if is_enforce_only():
            # PROXY_REGISTRY_ONLY=1: skip the GitHub API probe.
            # Marketplace/lockfile resolution already ran through the proxy;
            # the download step will surface a proxy 404 if absent.
            if logger:
                logger.info(
                    f"Skipping direct GitHub API probe for {host}: proxy-only mode is active"
                )
            return True

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

    except AuthenticationError:
        # #1015: let auth failures propagate to the caller for proper
        # rendering -- the outer try/except is only for parse failures.
        raise
    except Exception:
        # If parsing fails, assume it's a regular GitHub package
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

        from ...deps.registry_proxy import is_enforce_only

        if is_enforce_only():
            # PROXY_REGISTRY_ONLY=1: skip the GitHub API fallback probe.
            # The download step will surface a proxy 404 if the package is absent.
            if logger:
                logger.info(
                    f"Skipping direct GitHub API fallback probe for {host}:"
                    " proxy-only mode is active"
                )
            return True

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
                raise RuntimeError(f"TLS verification failed for {host_info.display_name}") from e
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
