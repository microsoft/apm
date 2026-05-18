# pylint: disable=duplicate-code
"""APM install command and dependency installation engine."""

import builtins
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Re-export the pre-deploy security scan so that bare-name call sites inside
# this module and ``tests/unit/test_install_scanning.py``'s direct import
# (``from apm_cli.commands.install import _pre_deploy_security_scan``) keep
# working without modification.

# Re-export MCP add/build helpers under their underscore-prefixed legacy
# names. Aliases live in mcp/writer.py and mcp/entry.py respectively.
from apm_cli.install.package_resolution import (
    GIT_PARENT_USER_SCOPE_ERROR,
)

# Re-export local-content leaf helpers so that callers inside this module
# (e.g. _install_apm_dependencies) and any future test patches against
# "apm_cli.commands.install._copy_local_package" keep working.
# Re-export lockfile hash helper so existing call sites and the regression
# test pinned in #762 (test_hash_deployed_is_module_level_and_works) keep
# working via "apm_cli.commands.install._hash_deployed".
# Re-export DI-seam helpers from the install services module so that test
# patches against ``apm_cli.commands.install._integrate_*`` keep working.
# Re-export validation leaf helpers so that existing test patches like
# @patch("apm_cli.commands.install._validate_package_exists") keep working.
# _validate_and_add_packages_to_apm_yml stays here (not moved) because it
# calls _validate_package_exists and _local_path_failure_reason via module-
# level name lookup -- keeping it co-located means @patch on this module
# intercepts those calls without test changes.
# MCP --mcp helpers (module-level re-exports for test patches); must stay at
# import time per comments in the original mid-file block.
from ...install.mcp.command import run_mcp_install as _run_mcp_install
from ...install.mcp.registry import (
    resolve_registry_url as _resolve_registry_url,
)
from ...install.mcp.registry import (
    validate_mcp_dry_run_entry as _validate_mcp_dry_run_entry,
)

# ---------------------------------------------------------------------------
# Manifest snapshot + rollback (W2-pkg-rollback, #827)
# ---------------------------------------------------------------------------
# When the user runs ``apm install <pkg>``, ``_validate_and_add_packages_to_apm_yml``
# mutates ``apm.yml`` BEFORE the install pipeline runs.  If the pipeline fails
# (policy block, download error, etc.) the failed package would stay in
# ``apm.yml`` forever.  These helpers snapshot the raw bytes before mutation
# and atomically restore on failure.
# ---------------------------------------------------------------------------


# CRITICAL: Shadow Python builtins that share names with Click commands
set = builtins.set
list = builtins.list
dict = builtins.dict

# APM Dependencies (conditional import for graceful degradation)
APM_DEPS_AVAILABLE = False
_APM_IMPORT_ERROR = None
try:
    from ...deps.apm_resolver import APMDependencyResolver
    from ...deps.github_downloader import GitHubPackageDownloader as GitHubPackageDownloader
    from ...deps.lockfile import LockFile as LockFile
    from ...deps.lockfile import get_lockfile_path as get_lockfile_path
    from ...deps.lockfile import migrate_lockfile_if_needed as migrate_lockfile_if_needed
    from ...integration import AgentIntegrator as AgentIntegrator
    from ...integration import PromptIntegrator as PromptIntegrator
    from ...integration.mcp_integrator import MCPIntegrator as MCPIntegrator
    from ...models.apm_package import APMPackage as APMPackage
    from ...models.apm_package import DependencyReference as DependencyReference

    class _ScopedInstallDependencyResolver(APMDependencyResolver):
        """Install-time resolver; blocks ``git: parent`` expansion at user scope."""

        def __init__(self, *args, install_scope=None, **kwargs):
            super().__init__(*args, **kwargs)
            self._install_scope = install_scope

        def expand_parent_repo_decl(self, parent_dep, child_dep):
            from ...core.scope import InstallScope

            if self._install_scope is InstallScope.USER:
                raise ValueError(GIT_PARENT_USER_SCOPE_ERROR)
            return super().expand_parent_repo_decl(parent_dep, child_dep)

    APM_DEPS_AVAILABLE = True
except ImportError as e:
    _APM_IMPORT_ERROR = str(e)
    _ScopedInstallDependencyResolver = None  # type: ignore[misc,assignment]


# ---------------------------------------------------------------------------
# Package validation helpers (extracted from _validate_and_add_packages_to_apm_yml)


def _handle_mcp_install(**params: object):
    mcp_name = params["mcp_name"]
    transport = params["transport"]
    url = params["url"]
    env_pairs = params["env_pairs"]
    header_pairs = params["header_pairs"]
    mcp_version = params["mcp_version"]
    command_argv = params["command_argv"]
    dev = params["dev"]
    force = params["force"]
    runtime = params["runtime"]
    exclude = params["exclude"]
    verbose = params["verbose"]
    dry_run = params["dry_run"]
    logger = params["logger"]
    no_policy = params["no_policy"]
    validated_registry_url = params["validated_registry_url"]
    """Execute the ``--mcp`` install path (MCP server add).

    Resolves registry URL, runs policy preflight, handles dry-run,
    and delegates to :func:`_run_mcp_install` for the actual installation.
    Called from :func:`install` when ``--mcp`` is specified; the caller
    returns immediately after this function completes.
    """
    from ...core.scope import (
        InstallScope,
        get_apm_dir,
        get_manifest_path,
    )

    # Apply CLI > env > default precedence; emit override diagnostic.
    resolved_registry_url, _registry_source = _resolve_registry_url(
        validated_registry_url,
        logger=logger,
    )
    mcp_scope = InstallScope.PROJECT
    mcp_manifest_path = get_manifest_path(mcp_scope)
    mcp_apm_dir = get_apm_dir(mcp_scope)
    # -- W2-mcp-preflight: policy enforcement before MCP install --
    # Build a lightweight MCPDependency for policy evaluation.
    # This mirrors _build_mcp_entry routing but we only need the
    # fields that policy checks inspect (name, transport, registry).
    from ...models.dependency.mcp import MCPDependency as _MCPDep
    from ...policy.install_preflight import (
        PolicyBlockError,
        run_policy_preflight,
    )

    _is_self_defined = bool(url or command_argv)
    _preflight_transport = transport
    if _preflight_transport is None:
        if command_argv:
            _preflight_transport = "stdio"
        elif url:
            _preflight_transport = "http"
    _preflight_dep = _MCPDep(
        name=mcp_name,
        transport=_preflight_transport,
        registry=False if _is_self_defined else None,
        url=url,
    )

    try:
        _pf_result, _pf_active = run_policy_preflight(
            project_root=Path.cwd(),
            mcp_deps=[_preflight_dep],
            no_policy=no_policy,
            logger=logger,
            dry_run=dry_run,
        )
    except PolicyBlockError:
        # Diagnostics already emitted by the helper + logger.
        logger.render_summary()
        sys.exit(1)

    if dry_run:
        # C1: validate eagerly so dry-run rejects what real install would.
        _validate_mcp_dry_run_entry(
            mcp_name,
            transport=transport,
            url=url,
            env=env_pairs,
            headers=header_pairs,
            version=mcp_version,
            command_argv=command_argv,
            registry_url=resolved_registry_url,
        )
        logger.dry_run_notice(f"would add MCP server '{mcp_name}' to {mcp_manifest_path}")
        return
    _run_mcp_install(
        mcp_name=mcp_name,
        transport=transport,
        url=url,
        env_pairs=env_pairs,
        header_pairs=header_pairs,
        mcp_version=mcp_version,
        command_argv=command_argv,
        dev=dev,
        force=force,
        runtime=runtime,
        exclude=exclude,
        verbose=verbose,
        logger=logger,
        manifest_path=mcp_manifest_path,
        apm_dir=mcp_apm_dir,
        scope=mcp_scope,
        registry_url=validated_registry_url,
    )
