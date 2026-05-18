"""Helpers for the ``apm prune`` command."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ..constants import APM_YML_FILENAME
from ..deps.lockfile import LockFile, get_lockfile_path
from ..models.apm_package import APMPackage
from ..utils.path_security import safe_rmtree
from ._helpers import (
    _build_expected_install_paths,
    _expand_with_ancestors,
    _scan_installed_packages,
    _standalone_installed_packages,
)


def _load_prune_state(apm_modules_dir: Path, logger):
    """Load expected and installed package state for prune."""
    try:
        apm_package = APMPackage.from_apm_yml(Path(APM_YML_FILENAME))
        declared_deps = apm_package.get_apm_dependencies()
        lockfile = LockFile.read(get_lockfile_path(Path.cwd()))
        expected_installed = _build_expected_install_paths(declared_deps, lockfile, apm_modules_dir)
    except Exception as exc:
        logger.error(f"Failed to parse {APM_YML_FILENAME}: {exc}")
        sys.exit(1)

    installed_packages = _scan_installed_packages(apm_modules_dir)
    standalone_installed = _standalone_installed_packages(
        installed_packages,
        apm_modules_dir,
        lockfile=lockfile,
    )
    expected_with_ancestors = _expand_with_ancestors(expected_installed, standalone_installed)
    orphaned_packages = sorted(
        package for package in installed_packages if package not in expected_with_ancestors
    )
    return lockfile, orphaned_packages


def _render_orphaned_packages(orphaned_packages, dry_run: bool, logger) -> None:
    """Render the orphan summary before prune removes anything."""
    logger.warning(f"Found {len(orphaned_packages)} orphaned package(s):")
    for package_name in orphaned_packages:
        suffix = " (would be removed)" if dry_run else ""
        logger.warning(f"  - {package_name}{suffix}")


def _remove_orphaned_packages(orphaned_packages, apm_modules_dir: Path, logger):
    """Delete orphaned packages from disk and return bookkeeping data."""
    removed_count = 0
    pruned_keys: list[str] = []
    deleted_pkg_paths: list[Path] = []
    for package_name in orphaned_packages:
        pkg_path = apm_modules_dir.joinpath(*package_name.split("/"))
        try:
            safe_rmtree(pkg_path, apm_modules_dir)
            logger.progress(f"+ Removed {package_name}")
            removed_count += 1
            pruned_keys.append(package_name)
            deleted_pkg_paths.append(pkg_path)
        except Exception as exc:
            logger.error(f"x Failed to remove {package_name}: {exc}")
    return removed_count, pruned_keys, deleted_pkg_paths


def _delete_deployed_targets(dep, project_root: Path) -> list[Path]:
    from ..integration.base_integrator import BaseIntegrator

    deleted_targets: list[Path] = []
    for rel_path in dep.deployed_files:
        if not BaseIntegrator.validate_deploy_path(rel_path, project_root):
            continue
        target = project_root / rel_path
        if target.is_file():
            target.unlink()
            deleted_targets.append(target)
        elif target.is_dir():
            shutil.rmtree(target)
            deleted_targets.append(target)
    return deleted_targets


def _persist_pruned_lockfile(lockfile, lockfile_path: Path) -> None:
    try:
        if lockfile.dependencies:
            lockfile.write(lockfile_path)
        else:
            lockfile_path.unlink(missing_ok=True)
    except Exception:
        pass


def _cleanup_pruned_lockfile(pruned_keys, logger) -> None:
    """Remove lockfile entries and deployed files for pruned packages."""
    if not pruned_keys:
        return

    from ..integration.base_integrator import BaseIntegrator

    lockfile_path = get_lockfile_path(Path("."))
    lockfile = LockFile.read(lockfile_path)
    project_root = Path(".")
    if not lockfile:
        return

    deleted_targets: list[Path] = []
    for dep_key in pruned_keys:
        dep = lockfile.get_dependency(dep_key)
        if dep and dep.deployed_files:
            deleted_targets.extend(_delete_deployed_targets(dep, project_root))
        lockfile.dependencies.pop(dep_key, None)

    BaseIntegrator.cleanup_empty_parents(deleted_targets, stop_at=project_root)
    if deleted_targets:
        logger.progress(f"+ Cleaned {len(deleted_targets)} deployed integration file(s)")
    _persist_pruned_lockfile(lockfile, lockfile_path)
