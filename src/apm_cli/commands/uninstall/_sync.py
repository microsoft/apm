# pylint: disable=duplicate-code
"""Sync / re-integration helpers extracted from engine.py."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...constants import APM_MODULES_DIR
from ...integration.mcp_integrator import MCPIntegrator
from ...integration.skill_integrator.opts import SkillOpts as _SkillOpts


def _compute_skill_dirs_exist(resolved_targets: list, project_root: Path) -> bool:
    """Return True when any resolved target has a skills directory on disk."""
    for t in resolved_targets:
        if t.supports("skills"):
            sm = t.primitives["skills"]
            er = sm.deploy_root or t.root_dir
            if (project_root / er / "skills").exists():
                return True
    return False


def _build_managed_buckets(all_deployed_files, user_scope: bool, resolved_targets) -> dict | None:
    """Partition deployed files into per-integrator buckets.

    When *user_scope* is ``True``, also partitions against *resolved_targets*
    so both ``.github/`` (legacy) and ``.copilot/`` prefixes are recognised.
    Returns the bucket dict, or ``None`` when there are no deployed files.
    """
    from ...integration.base_integrator import BaseIntegrator

    sync_managed = all_deployed_files or None
    if sync_managed is None:
        return None

    buckets = BaseIntegrator.partition_managed_files(sync_managed)
    if user_scope and resolved_targets:
        scope_buckets = BaseIntegrator.partition_managed_files(
            sync_managed, targets=resolved_targets
        )
        for bname, bpaths in scope_buckets.items():
            existing = buckets.get(bname)
            if existing is not None:
                existing.update(bpaths)
            else:
                buckets[bname] = bpaths
    return buckets


@dataclass(frozen=True, slots=True)
class _ReintegrationContext:
    """Inputs for re-integrating remaining packages after uninstall."""

    apm_package: object
    project_root: Path
    resolved_targets: list
    integrators: dict
    dispatch: dict
    logger: object


@dataclass(frozen=True, slots=True)
class _McpCleanupContext:
    """Inputs for stale MCP cleanup after uninstall."""

    apm_package: object
    lockfile: object
    lockfile_path: Path
    old_mcp_servers: set
    modules_dir: Path | None = None
    project_root: Path | None = None
    user_scope: bool = False
    scope: object | None = None


def _phase2_reintegrate_packages(context: _ReintegrationContext):
    """Re-integrate primitives from each remaining installed package (Phase 2).

    Iterates over the APM dependencies still present in *apm_package*, loads
    their manifests, and runs each integrator's integrate method so primitives
    from surviving packages are re-deployed after the uninstalled packages have
    been cleaned up.
    """
    from ...models.apm_package import PackageInfo, validate_apm_package

    apm_package = context.apm_package
    project_root = context.project_root
    resolved_targets = context.resolved_targets
    integrators = context.integrators
    dispatch = context.dispatch
    logger = context.logger

    for dep in apm_package.get_apm_dependencies():
        dep_ref = dep if hasattr(dep, "repo_url") else None
        if not dep_ref:
            continue
        install_path = dep_ref.get_install_path(Path(APM_MODULES_DIR))
        if not install_path.exists():
            continue

        result = validate_apm_package(install_path)
        pkg = result.package if result and result.package else None
        if not pkg:
            continue
        pkg_info = PackageInfo(
            package=pkg,
            install_path=install_path,
            dependency_ref=dep_ref,
            package_type=result.package_type if result else None,
        )

        try:
            for _target in resolved_targets:
                for _prim_name in _target.primitives:
                    _entry = dispatch.get(_prim_name)
                    if not _entry or _entry.multi_target:
                        continue
                    getattr(integrators[_prim_name], _entry.integrate_method)(
                        _target,
                        pkg_info,
                        project_root,
                    )
            integrators["skills"].integrate_package_skill(
                pkg_info,
                project_root,
                _SkillOpts(targets=resolved_targets),
            )
        except Exception:
            pkg_id = dep_ref.get_identity() if hasattr(dep_ref, "get_identity") else str(dep_ref)
            logger.warning(f"Best-effort re-integration skipped for {pkg_id}")


def _sync_integrations_after_uninstall(
    apm_package, project_root, all_deployed_files, logger, user_scope=False
):
    """Remove deployed files and re-integrate from remaining packages.

    When *user_scope* is ``True``, targets are resolved for user-level
    deployment so cleanup and re-integration use the correct paths.
    """
    from ...integration.base_integrator import BaseIntegrator
    from ...integration.dispatch import get_dispatch_table
    from ...integration.targets import resolve_targets

    _dispatch = get_dispatch_table()
    _integrators = {name: entry.integrator_class() for name, entry in _dispatch.items()}

    # Resolve targets once -- used for both Phase 1 removal and Phase 2 re-integration.
    config_target = apm_package.target
    _explicit = config_target or None
    _resolved_targets = resolve_targets(
        project_root, user_scope=user_scope, explicit_target=_explicit
    )

    _buckets = _build_managed_buckets(all_deployed_files, user_scope, _resolved_targets)

    counts = {entry.counter_key: 0 for entry in _dispatch.values()}

    # Phase 1: Remove all APM-deployed files
    # Per-target sync for primitives with sync_for_target
    for _target in _resolved_targets:
        for _prim_name, _mapping in _target.primitives.items():
            _entry = _dispatch.get(_prim_name)
            if not _entry or _entry.sync_method != "sync_for_target":
                continue
            _effective_root = _mapping.deploy_root or _target.root_dir
            _deploy_dir = project_root / _effective_root / _mapping.subdir
            if not _deploy_dir.exists():
                continue
            _managed_subset = None
            if _buckets is not None:
                _bucket_key = BaseIntegrator.partition_bucket_key(_prim_name, _target.name)
                _managed_subset = _buckets.get(_bucket_key, set())
            result = _integrators[_prim_name].sync_for_target(
                _target,
                apm_package,
                project_root,
                managed_files=_managed_subset,
            )
            counts[_entry.counter_key] += result.get("files_removed", 0)

    # Skills (multi-target, handled by SkillIntegrator)
    _skill_dirs_exist = _compute_skill_dirs_exist(_resolved_targets, project_root)

    # Scan sync_managed DIRECTLY for cowork:// entries.
    # partition_managed_files() uses resolved_deploy_root to detect
    # dynamic-root targets, but the static KNOWN_TARGETS["copilot-cowork"]
    # always has resolved_deploy_root=None (it is only set after for_scope()
    # resolves the OneDrive path at install time).  As a result, cowork://
    # paths are never routed into _buckets["skills"] by the partition, so
    # the bucket-based _has_cowork_skills check in the previous fix always
    # returned False.  Bypassing the bucket and scanning sync_managed
    # directly is the correct approach: no partition logic is involved.
    _cowork_skill_files: set = set()
    sync_managed = all_deployed_files or None
    if sync_managed:
        from ...integration.copilot_cowork_paths import COWORK_URI_SCHEME

        _cowork_skill_files = {p for p in sync_managed if p.startswith(COWORK_URI_SCHEME)}
    _has_cowork_skills = bool(_cowork_skill_files)

    if _skill_dirs_exist or _has_cowork_skills:
        # Merge cowork entries into the skills bucket so sync_integration
        # receives them via managed_files.
        if _has_cowork_skills and _buckets is not None:
            _buckets.setdefault("skills", set()).update(_cowork_skill_files)
        elif _has_cowork_skills:
            _buckets = {"skills": _cowork_skill_files, "hooks": set()}

        # When cowork entries are present, pass targets=None so
        # sync_integration builds skill_prefix_tuple from KNOWN_TARGETS
        # (which includes the copilot-cowork target with user_root_resolver
        # set).  Using _resolved_targets alone would yield only the local
        # prefix (.copilot/skills/) and cowork:// paths would be silently
        # skipped by the startswith() guard inside sync_integration.
        _sync_targets = None if _has_cowork_skills else _resolved_targets
        result = _integrators["skills"].sync_integration(
            apm_package,
            project_root,
            managed_files=_buckets["skills"] if _buckets else None,
            targets=_sync_targets,
        )
        counts["skills"] = result.get("files_removed", 0)

    # Hooks (multi-target sync_integration handles all targets)
    result = _integrators["hooks"].sync_integration(
        apm_package,
        project_root,
        managed_files=_buckets["hooks"] if _buckets else None,
    )
    counts["hooks"] = result.get("files_removed", 0)

    # Phase 2: Re-integrate from remaining installed packages
    _phase2_reintegrate_packages(
        _ReintegrationContext(
            apm_package=apm_package,
            project_root=project_root,
            resolved_targets=_resolved_targets,
            integrators=_integrators,
            dispatch=_dispatch,
            logger=logger,
        )
    )

    return counts


def _cleanup_stale_mcp(
    context_or_apm_package: _McpCleanupContext | object,
    lockfile=None,
    lockfile_path=None,
    old_mcp_servers=None,
    **kwargs,
):
    """Remove MCP servers that are no longer needed after uninstall."""
    if not isinstance(context_or_apm_package, _McpCleanupContext):
        # Backward-compat: called with positional args (apm_package, lockfile, lockfile_path, set())
        _valid = _McpCleanupContext.__dataclass_fields__
        _extra = {k: v for k, v in kwargs.items() if k in _valid}
        context = _McpCleanupContext(
            apm_package=context_or_apm_package,
            lockfile=lockfile,
            lockfile_path=lockfile_path,
            old_mcp_servers=old_mcp_servers or set(),
            **_extra,
        )
    else:
        context = context_or_apm_package
    if not context.old_mcp_servers:
        return
    apm_modules_path = (
        context.modules_dir if context.modules_dir is not None else Path.cwd() / APM_MODULES_DIR
    )
    remaining_mcp = MCPIntegrator.collect_transitive(
        apm_modules_path, context.lockfile_path, trust_private=True
    )
    try:
        remaining_root_mcp = context.apm_package.get_mcp_dependencies()
    except Exception:
        remaining_root_mcp = []
    all_remaining_mcp = MCPIntegrator.deduplicate(remaining_root_mcp + remaining_mcp)
    new_mcp_servers = MCPIntegrator.get_server_names(all_remaining_mcp)
    stale_servers = context.old_mcp_servers - new_mcp_servers
    if stale_servers:
        MCPIntegrator.remove_stale(
            stale_servers,
            project_root=context.project_root,
            user_scope=context.user_scope,
            scope=context.scope,
        )
    MCPIntegrator.update_lockfile(new_mcp_servers, context.lockfile_path)
