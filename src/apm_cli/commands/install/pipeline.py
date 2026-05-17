"""APM install command and dependency installation engine."""

import builtins
import contextlib
import dataclasses
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from apm_cli.install.errors import (
    AuthenticationError,
    DirectDependencyError,
    FrozenInstallError,
    PolicyViolationError,
)
from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand

if TYPE_CHECKING:
    from apm_cli.install.plan import UpdatePlan

# Re-export the pre-deploy security scan so that bare-name call sites inside
# this module and ``tests/unit/test_install_scanning.py``'s direct import
# (``from apm_cli.commands.install import _pre_deploy_security_scan``) keep
# working without modification.
from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan  # noqa: F401
from apm_cli.install.insecure_policy import (
    InsecureDependencyPolicyError,
    _allow_insecure_host_callback,
    _check_insecure_dependencies,
    _collect_insecure_dependency_infos,  # noqa: F401
    _format_insecure_dependency_requirements,
    _format_insecure_dependency_warning,  # noqa: F401
    _get_insecure_dependency_url,
    _guard_transitive_insecure_dependencies,  # noqa: F401
    _InsecureDependencyInfo,  # noqa: F401
)

# Re-export MCP add/build helpers under their underscore-prefixed legacy
# names. Aliases live in mcp/writer.py and mcp/entry.py respectively.
from apm_cli.install.mcp.entry import _build_mcp_entry  # noqa: F401
from apm_cli.install.mcp.writer import _add_mcp_to_apm_yml  # noqa: F401
from apm_cli.install.package_resolution import (
    GIT_PARENT_USER_SCOPE_ERROR,
    dependency_reference_to_yaml_entry,
    merge_structured_entry_into_current_deps,
    persist_dependency_list_if_changed,
    resolve_parsed_dependency_reference,
    user_scope_rejection_reason,
)

# Re-export local-content leaf helpers so that callers inside this module
# (e.g. _install_apm_dependencies) and any future test patches against
# "apm_cli.commands.install._copy_local_package" keep working.
from apm_cli.install.phases.local_content import (
    _copy_local_package,  # noqa: F401
    _has_local_apm_content,  # noqa: F401
    _project_has_root_primitives,
)

# Re-export lockfile hash helper so existing call sites and the regression
# test pinned in #762 (test_hash_deployed_is_module_level_and_works) keep
# working via "apm_cli.commands.install._hash_deployed".
from apm_cli.install.phases.lockfile import compute_deployed_hashes as _hash_deployed  # noqa: F401

# Re-export DI-seam helpers from the install services module so that test
# patches against ``apm_cli.commands.install._integrate_*`` keep working.
from apm_cli.install.services import (
    _integrate_local_content,  # noqa: F401
    _integrate_package_primitives,  # noqa: F401
)

# Re-export validation leaf helpers so that existing test patches like
# @patch("apm_cli.commands.install._validate_package_exists") keep working.
# _validate_and_add_packages_to_apm_yml stays here (not moved) because it
# calls _validate_package_exists and _local_path_failure_reason via module-
# level name lookup -- keeping it co-located means @patch on this module
# intercepts those calls without test changes.
from apm_cli.install.validation import (
    _local_path_failure_reason,
    _local_path_no_markers_hint,  # noqa: F401
    _validate_package_exists,
)
from apm_cli.utils.diagnostics import DiagnosticCollector  # noqa: F401

from ...constants import (
    APM_YML_FILENAME,
    InstallMode,
)
from ...core.auth import AuthResolver
from ...core.command_logger import InstallLogger, _ValidationOutcome
from ...core.target_detection import TargetParamType

# MCP --mcp helpers (module-level re-exports for test patches); must stay at
# import time per comments in the original mid-file block.
from ...install.mcp.command import run_mcp_install as _run_mcp_install
from ...install.mcp.conflicts import (
    validate_mcp_conflicts as _validate_mcp_conflicts,
)
from ...install.mcp.registry import (
    resolve_registry_url as _resolve_registry_url,
)
from ...install.mcp.registry import (
    validate_mcp_dry_run_entry as _validate_mcp_dry_run_entry,
)
from ...install.mcp.registry import (
    validate_registry_url as _validate_registry_url,
)
from ...utils.console import _rich_echo, _rich_error, _rich_info, _rich_success  # noqa: F401
from .._helpers import (
    _create_minimal_apm_yml,
    _get_default_config,
    _update_gitignore_for_apm_modules,  # noqa: F401
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
    from ...deps.github_downloader import GitHubPackageDownloader  # noqa: F401
    from ...deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed
    from ...integration import AgentIntegrator, PromptIntegrator  # noqa: F401
    from ...integration.mcp_integrator import MCPIntegrator
    from ...models.apm_package import APMPackage, DependencyReference

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

from ._command_context import InstallContext


def _install_apm_packages(ctx, outcome):
    """Execute the APM + transitive MCP installation pipeline.

    Parses ``apm.yml``, installs APM dependencies, collects and installs
    transitive MCP servers, and handles lockfile updates.

    Args:
        ctx: :class:`InstallContext` with configuration and environment.
        outcome: ``_ValidationOutcome`` from package validation (may be
            ``None`` when no explicit packages were passed).

    Returns:
        Tuple of ``(apm_count, mcp_count, apm_diagnostics)``.
    """
    logger = ctx.logger

    logger.resolution_start(
        to_install_count=len(ctx.only_packages or []) if ctx.packages else 0,
        lockfile_count=0,  # Refined later inside _install_apm_dependencies
    )

    # Parse apm.yml to get both APM and MCP dependencies
    try:
        apm_package = APMPackage.from_apm_yml(ctx.manifest_path)
    except Exception as e:
        logger.error(f"Failed to parse {ctx.manifest_display}: {e}")
        sys.exit(1)

    logger.verbose_detail(
        f"Parsed {APM_YML_FILENAME}: {len(apm_package.get_apm_dependencies())} APM deps, "
        f"{len(apm_package.get_mcp_dependencies())} MCP deps"
        + (
            f", {len(apm_package.get_dev_apm_dependencies())} dev deps"
            if apm_package.get_dev_apm_dependencies()
            else ""
        )
    )

    # Get APM and MCP dependencies
    apm_deps = apm_package.get_apm_dependencies()
    dev_apm_deps = apm_package.get_dev_apm_dependencies()
    has_any_apm_deps = bool(apm_deps) or bool(dev_apm_deps)
    mcp_deps = apm_package.get_mcp_dependencies()

    all_apm_deps = list(apm_deps) + list(dev_apm_deps)
    sys.modules[__package__]._check_insecure_dependencies(all_apm_deps, ctx.allow_insecure, logger)

    # Determine what to install based on install mode
    should_install_apm = ctx.install_mode != InstallMode.MCP
    should_install_mcp = ctx.install_mode != InstallMode.APM

    # Show what will be installed if dry run
    if ctx.dry_run:
        # -- W2-dry-run (#827): policy preflight in preview mode --
        # Runs discovery + checks against direct manifest deps (not
        # resolved/transitive -- dry-run does not run the resolver).
        # Block-severity violations render as "Would be blocked by
        # policy" without raising.  Documented limitation: transitive
        # deps are NOT evaluated since the resolver does not run.
        from apm_cli.policy.install_preflight import run_policy_preflight as _dr_preflight

        _dr_apm_deps = builtins.list(apm_deps) + builtins.list(dev_apm_deps)
        _dr_preflight(
            project_root=ctx.project_root,
            apm_deps=_dr_apm_deps,
            mcp_deps=mcp_deps if should_install_mcp else None,
            no_policy=ctx.no_policy,
            logger=logger,
            dry_run=True,
        )

        from apm_cli.install.presentation.dry_run import render_and_exit

        render_and_exit(
            logger=logger,
            should_install_apm=should_install_apm,
            apm_deps=apm_deps,
            mcp_deps=mcp_deps,
            dev_apm_deps=dev_apm_deps,
            should_install_mcp=should_install_mcp,
            update=ctx.update,
            only_packages=ctx.only_packages,
            apm_dir=ctx.apm_dir,
        )
        return 0, 0, None  # render_and_exit exits; this line is defensive

    # Install APM dependencies first (if requested)
    apm_count = 0

    # Migrate legacy apm.lock -> apm.lock.yaml if needed (one-time, transparent)
    migrate_lockfile_if_needed(ctx.apm_dir)

    # Capture old MCP servers and configs from lockfile BEFORE
    # _install_apm_dependencies regenerates it (which drops the fields).
    # We always read this -- even when --only=apm -- so we can restore the
    # field after the lockfile is regenerated by the APM install step.
    old_mcp_servers: builtins.set = builtins.set()
    old_mcp_configs: builtins.dict = {}
    _lock_path = get_lockfile_path(ctx.apm_dir)
    _existing_lock = LockFile.read(_lock_path)
    if _existing_lock:
        old_mcp_servers = builtins.set(_existing_lock.mcp_servers)
        old_mcp_configs = builtins.dict(_existing_lock.mcp_configs)

    # Enter the APM install path when there are deps, local .apm/ primitives
    # (#714), OR orphan deps in the lockfile to clean up (manifest emptied).
    from apm_cli.core.scope import InstallScope
    from apm_cli.core.scope import get_deploy_root as _get_deploy_root
    from apm_cli.deps.lockfile import _SELF_KEY as _LOCK_SELF_KEY

    _cli_project_root = _get_deploy_root(ctx.scope)
    _has_orphan_deps_in_lock = bool(
        _existing_lock
        and not has_any_apm_deps
        and any(k != _LOCK_SELF_KEY for k in _existing_lock.dependencies)
    )
    apm_diagnostics = None
    if should_install_apm and (
        has_any_apm_deps
        or _project_has_root_primitives(_cli_project_root)
        or _has_orphan_deps_in_lock
    ):
        if not sys.modules[__package__].APM_DEPS_AVAILABLE:
            logger.error("APM dependency system not available")
            logger.progress(f"Import error: {_APM_IMPORT_ERROR}")
            sys.exit(1)

        try:
            # If specific packages were requested, only install those
            # Otherwise install all from apm.yml.
            # `only_packages` was computed above so the dry-run preview
            # and the actual install share one canonical list.
            install_result = sys.modules[__package__]._install_apm_dependencies(
                apm_package,
                update_refs=ctx.update,
                verbose=ctx.verbose,
                only_packages=ctx.only_packages,
                force=ctx.force,
                parallel_downloads=ctx.parallel_downloads,
                logger=logger,
                scope=ctx.scope,
                auth_resolver=ctx.auth_resolver,
                target=ctx.target,
                allow_insecure=ctx.allow_insecure,
                allow_insecure_hosts=ctx.allow_insecure_hosts,
                marketplace_provenance=(
                    outcome.marketplace_provenance if ctx.packages and outcome else None
                ),
                protocol_pref=ctx.protocol_pref,
                allow_protocol_fallback=ctx.allow_protocol_fallback,
                no_policy=ctx.no_policy,
                legacy_skill_paths=ctx.legacy_skill_paths,
                frozen=ctx.frozen,
                plan_callback=ctx.plan_callback,
            )
            apm_count = install_result.installed_count
            apm_diagnostics = install_result.diagnostics
        except InsecureDependencyPolicyError:
            sys.modules[__package__]._maybe_rollback_manifest(
                ctx.snapshot_manifest_path, ctx.manifest_snapshot, logger
            )
            sys.exit(1)
        except AuthenticationError as e:
            # #1015: render auth diagnostics on the DEFAULT path (not --verbose).
            sys.modules[__package__]._maybe_rollback_manifest(
                ctx.snapshot_manifest_path, ctx.manifest_snapshot, logger
            )
            _rich_error(str(e))
            if e.diagnostic_context:
                _rich_echo(e.diagnostic_context)
            sys.exit(1)
        except FrozenInstallError as e:
            sys.modules[__package__]._maybe_rollback_manifest(
                ctx.snapshot_manifest_path, ctx.manifest_snapshot, logger
            )
            _rich_error(str(e))
            for reason in e.reasons:
                _rich_echo(reason)
            sys.exit(1)
        except Exception as e:
            sys.modules[__package__]._maybe_rollback_manifest(
                ctx.snapshot_manifest_path, ctx.manifest_snapshot, logger
            )
            # #832: surface PolicyViolationError verbatim (no double-nesting).
            msg = (
                str(e)
                if isinstance(e, PolicyViolationError)
                else f"Failed to install APM dependencies: {e}"
            )
            logger.error(msg)
            if not ctx.verbose:
                logger.progress("Run with --verbose for detailed diagnostics")
            sys.exit(1)
    elif should_install_apm and not has_any_apm_deps:
        logger.verbose_detail("No APM dependencies found in apm.yml")

    # When --update is used, package files on disk may have changed.
    # Clear the parse cache so transitive MCP collection reads fresh data.
    if ctx.update:
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()

    # Collect transitive MCP dependencies from resolved APM packages
    transitive_mcp = []
    from ...core.scope import get_modules_dir

    apm_modules_path = get_modules_dir(ctx.scope)
    if should_install_mcp and apm_modules_path.exists():
        lock_path = get_lockfile_path(ctx.apm_dir)
        transitive_mcp = MCPIntegrator.collect_transitive(
            apm_modules_path,
            lock_path,
            ctx.trust_transitive_mcp,
            diagnostics=apm_diagnostics,
        )
        if transitive_mcp:
            logger.verbose_detail(f"Collected {len(transitive_mcp)} transitive MCP dependency(ies)")
            mcp_deps = MCPIntegrator.deduplicate(mcp_deps + transitive_mcp)

    # -- S1/S2 fix (#827-C2/C3): enforce policy on ALL MCP deps ----
    # The pipeline gate phase (policy_gate.py) checks direct APM deps
    # and direct MCP deps from apm.yml.  However, transitive MCP
    # servers (discovered via collect_transitive above) are only known
    # after APM packages are installed.  Run a second preflight
    # against the *merged* MCP set (direct + transitive) BEFORE
    # MCPIntegrator writes runtime configs.  On PolicyBlockError we
    # abort the MCP write but leave already-installed APM packages
    # in place (they were approved by the gate phase).
    if should_install_mcp and mcp_deps:
        from apm_cli.policy.install_preflight import (
            PolicyBlockError as _TransitivePBE,
        )
        from apm_cli.policy.install_preflight import (
            run_policy_preflight as _transitive_preflight,
        )

        try:
            _transitive_preflight(
                project_root=ctx.project_root,
                mcp_deps=mcp_deps,
                no_policy=ctx.no_policy,
                logger=logger,
                dry_run=False,
            )
        except _TransitivePBE:
            logger.error(
                "MCP server(s) blocked by org policy. "
                "APM packages remain installed; MCP configs were NOT written."
            )
            logger.render_summary()
            sys.exit(1)

    # Continue with MCP installation (existing logic)
    mcp_count = 0
    new_mcp_servers: builtins.set = builtins.set()
    # Forward only the targets-key the user actually declared so parse_targets_field
    # in the gate sees the same dict shape it sees from raw apm.yml. Including a
    # `targets: None` placeholder when the user wrote `target:` (singular) would
    # falsely trip the conflict-mutex check (see core.apm_yml.parse_targets_field).
    # This restores parity with `apm install` for users on the modern `targets:`
    # plural form -- without this, `targets:` was silently dropped at the call
    # site and the gate fell back to permissive directory detection (#1335).
    mcp_apm_config: dict = {"scripts": apm_package.scripts or {}}
    if apm_package.targets is not None:
        mcp_apm_config["targets"] = apm_package.targets
    elif apm_package.target is not None:
        mcp_apm_config["target"] = apm_package.target
    if should_install_mcp and mcp_deps:
        mcp_count = MCPIntegrator.install(
            mcp_deps,
            ctx.runtime,
            ctx.exclude,
            ctx.verbose,
            stored_mcp_configs=old_mcp_configs,
            apm_config=mcp_apm_config,
            project_root=ctx.project_root,
            user_scope=(ctx.scope is InstallScope.USER),
            explicit_target=ctx.target,
            diagnostics=apm_diagnostics,
            scope=ctx.scope,
        )
        new_mcp_servers = MCPIntegrator.get_server_names(mcp_deps)
        new_mcp_configs = MCPIntegrator.get_server_configs(mcp_deps)

        # Remove stale MCP servers that are no longer needed
        stale_servers = old_mcp_servers - new_mcp_servers
        if stale_servers:
            MCPIntegrator.remove_stale(
                stale_servers,
                ctx.runtime,
                ctx.exclude,
                project_root=ctx.project_root,
                user_scope=(ctx.scope is InstallScope.USER),
                scope=ctx.scope,
            )

        # Persist the new MCP server set and configs in the lockfile
        MCPIntegrator.update_lockfile(new_mcp_servers, _lock_path, mcp_configs=new_mcp_configs)
    elif should_install_mcp and not mcp_deps:
        # No MCP deps at all -- remove any old APM-managed servers
        if old_mcp_servers:
            MCPIntegrator.remove_stale(
                old_mcp_servers,
                ctx.runtime,
                ctx.exclude,
                project_root=ctx.project_root,
                user_scope=(ctx.scope is InstallScope.USER),
                scope=ctx.scope,
            )
            MCPIntegrator.update_lockfile(builtins.set(), _lock_path, mcp_configs={})
        logger.verbose_detail("No MCP dependencies found in apm.yml")
    elif not should_install_mcp and old_mcp_servers:
        # --only=apm: APM install regenerated the lockfile and dropped
        # mcp_servers.  Restore the previous set so it is not lost.
        MCPIntegrator.update_lockfile(old_mcp_servers, _lock_path, mcp_configs=old_mcp_configs)

    # Local .apm/ content integration is now handled inside the
    # install pipeline (phases/integrate.py + phases/post_deps_local.py,
    # refactor F3).  The duplicate target resolution, integrator
    # initialization, and inline stale-cleanup block that lived here
    # have been removed.

    return apm_count, mcp_count, apm_diagnostics


def _install_apm_dependencies(apm_package: "APMPackage", **params: object):
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
