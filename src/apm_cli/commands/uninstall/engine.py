"""APM uninstall engine  -- validation, removal, and cleanup helpers."""

import builtins
from pathlib import Path

from ...constants import APM_MODULES_DIR, APM_YML_FILENAME
from ...core.command_logger import CommandLogger
from ...utils.path_security import PathTraversalError, safe_rmtree
from ...utils.paths import portable_relpath

from ...deps.lockfile import LockFile
from ...models.apm_package import APMPackage, DependencyReference
from ...integration.mcp_integrator import MCPIntegrator


def _parse_dependency_entry(dep_entry):
    """Parse a dependency entry from apm.yml into a DependencyReference."""
    if isinstance(dep_entry, DependencyReference):
        return dep_entry
    if isinstance(dep_entry, str):
        return DependencyReference.parse(dep_entry)
    if isinstance(dep_entry, builtins.dict):
        return DependencyReference.parse_from_dict(dep_entry)
    raise ValueError(f"Unsupported dependency entry type: {type(dep_entry).__name__}")


def _validate_uninstall_packages(packages, current_deps, logger):
    """Validate which packages can be removed and return matched/unmatched lists."""
    packages_to_remove = []
    packages_not_found = []

    for package in packages:
        if "/" not in package:
            logger.error(f"Invalid package format: {package}. Use 'owner/repo' format.")
            continue

        matched_dep = None
        try:
            pkg_ref = DependencyReference.parse(package)
            pkg_identity = pkg_ref.get_identity()
        except Exception:
            pkg_identity = package

        for dep_entry in current_deps:
            try:
                dep_ref = _parse_dependency_entry(dep_entry)
                if dep_ref.get_identity() == pkg_identity:
                    matched_dep = dep_entry
                    break
            except (ValueError, TypeError, AttributeError, KeyError):
                dep_str = dep_entry if isinstance(dep_entry, str) else str(dep_entry)
                if dep_str == package:
                    matched_dep = dep_entry
                    break

        if matched_dep is not None:
            packages_to_remove.append(matched_dep)
            logger.progress(f"{package} - found in apm.yml", symbol="check")
        else:
            packages_not_found.append(package)
            logger.warning(f"{package} - not found in apm.yml")

    return packages_to_remove, packages_not_found


def _dry_run_uninstall(packages_to_remove, apm_modules_dir, logger):
    """Show what would be removed without making changes."""
    logger.progress(f"Dry run: Would remove {len(packages_to_remove)} package(s):")
    for pkg in packages_to_remove:
        logger.progress(f"  - {pkg} from apm.yml")
        try:
            dep_ref = _parse_dependency_entry(pkg)
            package_path = dep_ref.get_install_path(apm_modules_dir)
        except (ValueError, TypeError, AttributeError, KeyError):
            pkg_str = pkg if isinstance(pkg, str) else str(pkg)
            package_path = apm_modules_dir / pkg_str.split("/")[-1]
        if apm_modules_dir.exists() and package_path.exists():
            logger.progress(f"  - {pkg} from apm_modules/")

    from ...deps.lockfile import LockFile, get_lockfile_path
    lockfile_path = get_lockfile_path(Path("."))
    lockfile = LockFile.read(lockfile_path)
    if lockfile:
        removed_repo_urls = builtins.set()
        for pkg in packages_to_remove:
            try:
                ref = _parse_dependency_entry(pkg)
                removed_repo_urls.add(ref.repo_url)
            except (ValueError, TypeError, AttributeError, KeyError):
                removed_repo_urls.add(pkg)
        queue = builtins.list(removed_repo_urls)
        potential_orphans = builtins.set()
        while queue:
            parent_url = queue.pop()
            for dep in lockfile.get_package_dependencies():
                key = dep.get_unique_key()
                if key in potential_orphans:
                    continue
                if dep.resolved_by and dep.resolved_by == parent_url:
                    potential_orphans.add(key)
                    queue.append(dep.repo_url)
        if potential_orphans:
            logger.progress(f"  Transitive dependencies that would be removed:")
            for orphan_key in sorted(potential_orphans):
                logger.progress(f"    - {orphan_key}")

    logger.success("Dry run complete - no changes made")


def _remove_packages_from_disk(packages_to_remove, apm_modules_dir, logger):
    """Remove direct packages from apm_modules/ and return removal count."""
    removed = 0
    if not apm_modules_dir.exists():
        return removed

    deleted_pkg_paths = []
    for package in packages_to_remove:
        try:
            dep_ref = _parse_dependency_entry(package)
            package_path = dep_ref.get_install_path(apm_modules_dir)
        except (PathTraversalError,) as e:
            logger.error(f"Refusing to remove {package}: {e}")
            continue
        except (ValueError, TypeError, AttributeError, KeyError):
            package_str = package if isinstance(package, str) else str(package)
            repo_parts = package_str.split("/")
            if len(repo_parts) >= 2:
                package_path = apm_modules_dir.joinpath(*repo_parts)
            else:
                package_path = apm_modules_dir / package_str

        if package_path.exists():
            try:
                safe_rmtree(package_path, apm_modules_dir)
                logger.progress(f"Removed {package} from apm_modules/")
                logger.verbose_detail(f"    Path: {portable_relpath(package_path, apm_modules_dir)}")
                removed += 1
                deleted_pkg_paths.append(package_path)
            except Exception as e:
                logger.error(f"Failed to remove {package} from apm_modules/: {e}")
        else:
            logger.warning(f"Package {package} not found in apm_modules/")

    from ...integration.base_integrator import BaseIntegrator as _BI2
    _BI2.cleanup_empty_parents(deleted_pkg_paths, stop_at=apm_modules_dir)
    return removed


def _cleanup_transitive_orphans(lockfile, packages_to_remove, apm_modules_dir, apm_yml_path, logger):
    """Remove orphaned transitive deps and return (removed_count, actual_orphan_keys)."""

    if not lockfile or not apm_modules_dir.exists():
        return 0, builtins.set()

    removed_repo_urls = builtins.set()
    for pkg in packages_to_remove:
        try:
            ref = _parse_dependency_entry(pkg)
            removed_repo_urls.add(ref.repo_url)
        except (ValueError, TypeError, AttributeError, KeyError):
            removed_repo_urls.add(pkg)

    # Find transitive orphans recursively
    orphans = builtins.set()
    queue = builtins.list(removed_repo_urls)
    while queue:
        parent_url = queue.pop()
        for dep in lockfile.get_package_dependencies():
            key = dep.get_unique_key()
            if key in orphans:
                continue
            if dep.resolved_by and dep.resolved_by == parent_url:
                orphans.add(key)
                queue.append(dep.repo_url)

    if not orphans:
        return 0, builtins.set()

    # Determine remaining deps to avoid removing still-needed packages
    remaining_deps = builtins.set()
    try:
        from ...utils.yaml_io import load_yaml
        updated_data = load_yaml(apm_yml_path) or {}
        for dep_str in updated_data.get("dependencies", {}).get("apm", []) or []:
            try:
                ref = _parse_dependency_entry(dep_str)
                remaining_deps.add(ref.get_unique_key())
            except (ValueError, TypeError, AttributeError, KeyError):
                remaining_deps.add(dep_str)
    except Exception:
        pass

    for dep in lockfile.get_package_dependencies():
        key = dep.get_unique_key()
        if key not in orphans and dep.repo_url not in removed_repo_urls:
            remaining_deps.add(key)

    actual_orphans = orphans - remaining_deps
    removed = 0
    deleted_orphan_paths = []
    for orphan_key in actual_orphans:
        orphan_dep = lockfile.get_dependency(orphan_key)
        if not orphan_dep:
            continue
        try:
            orphan_ref = DependencyReference.parse(orphan_key)
            orphan_path = orphan_ref.get_install_path(apm_modules_dir)
        except ValueError:
            parts = orphan_key.split("/")
            orphan_path = apm_modules_dir.joinpath(*parts) if len(parts) >= 2 else apm_modules_dir / orphan_key

        if orphan_path.exists():
            try:
                safe_rmtree(orphan_path, apm_modules_dir)
                logger.progress(f"Removed transitive dependency {orphan_key} from apm_modules/")
                logger.verbose_detail(f"    Path: {portable_relpath(orphan_path, apm_modules_dir)}")
                removed += 1
                deleted_orphan_paths.append(orphan_path)
            except Exception as e:
                logger.error(f"Failed to remove transitive dep {orphan_key}: {e}")

    from ...integration.base_integrator import BaseIntegrator as _BI
    _BI.cleanup_empty_parents(deleted_orphan_paths, stop_at=apm_modules_dir)
    return removed, actual_orphans


def _sync_integrations_after_uninstall(apm_package, project_root, all_deployed_files, logger, user_scope=False):
    """Remove deployed files and re-integrate from remaining packages.

    When *user_scope* is ``True``, targets are resolved for user-level
    deployment so cleanup and re-integration use the correct paths.
    """
    from ...integration.base_integrator import BaseIntegrator
    from ...models.apm_package import PackageInfo, validate_apm_package
    from ...integration.dispatch import get_dispatch_table
    from ...integration.targets import resolve_targets

    _dispatch = get_dispatch_table()
    _integrators = {name: entry.integrator_class() for name, entry in _dispatch.items()}

    # Resolve targets once -- used for both Phase 1 removal and Phase 2 re-integration.
    config_target = apm_package.target
    _explicit = config_target or None
    _resolved_targets = resolve_targets(project_root, user_scope=user_scope, explicit_target=_explicit)

    sync_managed = all_deployed_files if all_deployed_files else None
    if sync_managed is not None:
        # Partition against default KNOWN_TARGETS for legacy/project-scope
        # paths, then merge with resolved targets for user-scope paths.
        # This ensures both .github/ (legacy) and .copilot/ (resolved)
        # prefixes are recognized during uninstall cleanup.
        _buckets = BaseIntegrator.partition_managed_files(sync_managed)
        if user_scope and _resolved_targets:
            _scope_buckets = BaseIntegrator.partition_managed_files(
                sync_managed, targets=_resolved_targets
            )
            for _bname, _bpaths in _scope_buckets.items():
                _existing = _buckets.get(_bname)
                if _existing is not None:
                    _existing.update(_bpaths)
                else:
                    _buckets[_bname] = _bpaths
    else:
        _buckets = None

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
                _bucket_key = BaseIntegrator.partition_bucket_key(
                    _prim_name, _target.name
                )
                _managed_subset = _buckets.get(_bucket_key, set())
            result = _integrators[_prim_name].sync_for_target(
                _target, apm_package, project_root,
                managed_files=_managed_subset,
            )
            counts[_entry.counter_key] += result.get("files_removed", 0)

    # Skills (multi-target, handled by SkillIntegrator)
    # Check both target root_dir and deploy_root for skill directories
    _skill_dirs_exist = False
    for t in _resolved_targets:
        if t.supports("skills"):
            sm = t.primitives["skills"]
            er = sm.deploy_root or t.root_dir
            if (project_root / er / "skills").exists():
                _skill_dirs_exist = True
                break
    if _skill_dirs_exist:
        result = _integrators["skills"].sync_integration(
            apm_package, project_root,
            managed_files=_buckets["skills"] if _buckets else None,
            targets=_resolved_targets,
        )
        counts["skills"] = result.get("files_removed", 0)

    # Hooks (multi-target sync_integration handles all targets)
    result = _integrators["hooks"].sync_integration(
        apm_package, project_root,
        managed_files=_buckets["hooks"] if _buckets else None,
    )
    counts["hooks"] = result.get("files_removed", 0)


    # Phase 2: Re-integrate from remaining installed packages
    _targets = _resolved_targets

    for dep in apm_package.get_apm_dependencies():
        dep_ref = dep if hasattr(dep, 'repo_url') else None
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
            package=pkg, install_path=install_path,
            dependency_ref=dep_ref,
            package_type=result.package_type if result else None,
        )

        try:
            for _target in _targets:
                for _prim_name in _target.primitives:
                    _entry = _dispatch.get(_prim_name)
                    if not _entry or _entry.multi_target:
                        continue
                    getattr(_integrators[_prim_name], _entry.integrate_method)(
                        _target, pkg_info, project_root,
                    )
            _integrators["skills"].integrate_package_skill(
                pkg_info, project_root, targets=_targets,
            )
        except Exception:
            pkg_id = dep_ref.get_identity() if hasattr(dep_ref, "get_identity") else str(dep_ref)
            logger.warning(f"Best-effort re-integration skipped for {pkg_id}")

    return counts


def _cleanup_stale_mcp(apm_package, lockfile, lockfile_path, old_mcp_servers, modules_dir=None, scope=None):
    """Remove MCP servers that are no longer needed after uninstall."""
    if not old_mcp_servers:
        return
    apm_modules_path = modules_dir if modules_dir is not None else Path.cwd() / APM_MODULES_DIR
    remaining_mcp = MCPIntegrator.collect_transitive(apm_modules_path, lockfile_path, trust_private=True)
    try:
        remaining_root_mcp = apm_package.get_mcp_dependencies()
    except Exception:
        remaining_root_mcp = []
    all_remaining_mcp = MCPIntegrator.deduplicate(remaining_root_mcp + remaining_mcp)
    new_mcp_servers = MCPIntegrator.get_server_names(all_remaining_mcp)
    stale_servers = old_mcp_servers - new_mcp_servers
    if stale_servers:
        MCPIntegrator.remove_stale(stale_servers, scope=scope)
    MCPIntegrator.update_lockfile(new_mcp_servers, lockfile_path)
