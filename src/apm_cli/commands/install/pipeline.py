# pylint: disable=duplicate-code
"""APM install command and dependency installation engine."""

from __future__ import annotations

import builtins
import sys
from typing import TYPE_CHECKING

from apm_cli.install.errors import (
    AuthenticationError,
    FrozenInstallError,
    PolicyViolationError,
)

if TYPE_CHECKING:
    pass

# Re-export the pre-deploy security scan so that bare-name call sites inside
# this module and ``tests/unit/test_install_scanning.py``'s direct import
# (``from apm_cli.commands.install import _pre_deploy_security_scan``) keep
# working without modification.
from apm_cli.install.insecure_policy import (
    InsecureDependencyPolicyError,  # noqa: F401
)

# Re-export MCP add/build helpers under their underscore-prefixed legacy
# names. Aliases live in mcp/writer.py and mcp/entry.py respectively.
from apm_cli.install.package_resolution import (
    GIT_PARENT_USER_SCOPE_ERROR,
)

# Re-export local-content leaf helpers so that callers inside this module
# (e.g. _install_apm_dependencies) and any future test patches against
# "apm_cli.commands.install._copy_local_package" keep working.
from apm_cli.install.phases.local_content import (
    _project_has_root_primitives,
)

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
from ...constants import (
    APM_YML_FILENAME,
    InstallMode,
)

# MCP --mcp helpers (module-level re-exports for test patches); must stay at
# import time per comments in the original mid-file block.
from ...utils.console import _rich_echo, _rich_error  # noqa: F401
from .pipeline_helpers import (
    _APMInstallRunCtx,
    _capture_existing_mcp_state,
    _collect_transitive_mcp,
    _DryRunPreflightCtx,
    _install_mcp_dependencies,
    _MCPDependencyInstallCtx,
    _MCPInstallCtx,
    _parse_install_manifest,
    _preflight_transitive_mcp,
    _run_apm_install,
    _run_dry_run_preflight,
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


def _install_apm_packages(ctx, outcome):
    """Execute the APM + transitive MCP installation pipeline."""
    logger = ctx.logger
    logger.resolution_start(
        to_install_count=len(ctx.only_packages or []) if ctx.packages else 0,
        lockfile_count=0,
    )

    apm_package, apm_deps, dev_apm_deps, mcp_deps, has_any_apm_deps = _parse_install_manifest(
        ctx,
        logger,
    )
    sys.modules[__package__]._check_insecure_dependencies(
        list(apm_deps) + list(dev_apm_deps),
        ctx.allow_insecure,
        logger,
    )
    should_install_apm = ctx.install_mode != InstallMode.MCP
    should_install_mcp = ctx.install_mode != InstallMode.APM
    if ctx.dry_run:
        return _run_dry_run_preflight(
            ctx,
            _DryRunPreflightCtx(
                logger=logger,
                apm_deps=apm_deps,
                dev_apm_deps=dev_apm_deps,
                mcp_deps=mcp_deps,
                should_install_apm=should_install_apm,
                should_install_mcp=should_install_mcp,
            ),
        )

    lock_path, existing_lock, old_mcp_servers, old_mcp_configs = _capture_existing_mcp_state(
        ctx.apm_dir
    )
    apm_count, apm_diagnostics = _run_apm_install(
        ctx,
        _APMInstallRunCtx(
            outcome=outcome,
            logger=logger,
            apm_package=apm_package,
            has_any_apm_deps=has_any_apm_deps,
            should_install_apm=should_install_apm,
            existing_lock=existing_lock,
        ),
    )
    if ctx.update:
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()

    mcp_deps = _collect_transitive_mcp(
        ctx,
        logger,
        apm_diagnostics,
        mcp_deps,
        should_install_mcp,
    )
    _preflight_transitive_mcp(ctx, logger, should_install_mcp, mcp_deps)
    mcp_count = _install_mcp_dependencies(
        ctx,
        _MCPDependencyInstallCtx(
            logger=logger,
            apm_package=apm_package,
            mcp_deps=mcp_deps,
            should_install_mcp=should_install_mcp,
            mcp_state=_MCPInstallCtx(
                old_mcp_servers=old_mcp_servers,
                old_mcp_configs=old_mcp_configs,
                lock_path=lock_path,
                apm_diagnostics=apm_diagnostics,
            ),
        ),
    )
    return apm_count, mcp_count, apm_diagnostics


def _install_apm_dependencies(apm_package: APMPackage, **params: object):
    """Thin wrapper -- builds an :class:`InstallRequest` and delegates to
    :class:`apm_cli.install.service.InstallService`.

    Kept here so that ``@patch("apm_cli.commands.install._install_apm_dependencies")``
    continues to intercept calls from the Click handler.  The service
    itself is the typed Application Service entry point for any future
    programmatic callers.
    """

    update_refs = params.get("update_refs", False)
    verbose = params.get("verbose", False)
    only_packages = params.get("only_packages")
    force = params.get("force", False)
    parallel_downloads = params.get("parallel_downloads", 4)
    logger = params.get("logger")
    scope = params.get("scope")
    auth_resolver = params.get("auth_resolver")
    target = params.get("target")
    allow_insecure = params.get("allow_insecure", False)
    allow_insecure_hosts = params.get("allow_insecure_hosts", ())
    marketplace_provenance = params.get("marketplace_provenance")
    protocol_pref = params.get("protocol_pref")
    allow_protocol_fallback = params.get("allow_protocol_fallback")
    no_policy = params.get("no_policy", False)
    skill_subset = params.get("skill_subset")
    skill_subset_from_cli = params.get("skill_subset_from_cli", False)
    legacy_skill_paths = params.get("legacy_skill_paths", False)
    frozen = params.get("frozen", False)
    plan_callback = params.get("plan_callback")
    if not sys.modules[__package__].APM_DEPS_AVAILABLE:
        raise RuntimeError("APM dependency system not available")

    from apm_cli.install.request import InstallRequest
    from apm_cli.install.service import InstallService

    request = InstallRequest(
        apm_package=apm_package,
        update_refs=update_refs,
        verbose=verbose,
        only_packages=only_packages,
        force=force,
        parallel_downloads=parallel_downloads,
        logger=logger,
        scope=scope,
        auth_resolver=auth_resolver,
        target=target,
        allow_insecure=allow_insecure,
        allow_insecure_hosts=allow_insecure_hosts,
        marketplace_provenance=marketplace_provenance,
        protocol_pref=protocol_pref,
        allow_protocol_fallback=allow_protocol_fallback,
        no_policy=no_policy,
        skill_subset=skill_subset,
        skill_subset_from_cli=skill_subset_from_cli,
        legacy_skill_paths=legacy_skill_paths,
        frozen=frozen,
        plan_callback=plan_callback,
    )
    return InstallService().run(request)
