"""APM prune command."""

import shutil
import sys
from pathlib import Path

import click

from ..constants import APM_LOCK_FILENAME, APM_MODULES_DIR, APM_YML_FILENAME  # noqa: F401
from ..core.command_logger import CommandLogger

# APM Dependencies
from ..deps.lockfile import LockFile, get_lockfile_path
from ..models.apm_package import APMPackage
from ..utils.path_security import PathTraversalError, safe_rmtree  # noqa: F401
from ._helpers import _build_expected_install_paths, _scan_installed_packages


@click.command(help="Remove APM packages not listed in apm.yml")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without removing")
@click.pass_context
def prune(ctx, dry_run):
    """Remove installed APM packages that are not listed in apm.yml (like npm prune).

    This command cleans up the apm_modules/ directory by removing packages that
    were previously installed but are no longer declared as dependencies in apm.yml.

    Examples:
        apm prune           # Remove orphaned packages
        apm prune --dry-run # Show what would be removed
    """
    logger = CommandLogger("prune", dry_run=dry_run)
    try:
        # Check if apm.yml exists
        if not Path(APM_YML_FILENAME).exists():
            logger.error("No apm.yml found. Run 'apm init' first.")
            sys.exit(1)

        # Check if apm_modules exists
        apm_modules_dir = Path(APM_MODULES_DIR)
        if not apm_modules_dir.exists():
            logger.progress("No apm_modules/ directory found. Nothing to prune.")
            return

        logger.start("Analyzing installed packages vs apm.yml...")

        # Build expected vs installed using shared helpers
        try:
            apm_package = APMPackage.from_apm_yml(Path(APM_YML_FILENAME))
            declared_deps = apm_package.get_apm_dependencies()
            lockfile = LockFile.read(get_lockfile_path(Path.cwd()))
            expected_installed = _build_expected_install_paths(
                declared_deps, lockfile, apm_modules_dir
            )
        except Exception as e:
            logger.error(f"Failed to parse {APM_YML_FILENAME}: {e}")
            sys.exit(1)

        installed_packages = _scan_installed_packages(apm_modules_dir)
        orphaned_packages = [p for p in installed_packages if p not in expected_installed]

        if not orphaned_packages:
            logger.success("No orphaned packages found. apm_modules/ is clean.", symbol="check")
            return

        # Show what will be removed
        logger.progress(f"Found {len(orphaned_packages)} orphaned package(s):")
        for pkg_name in orphaned_packages:
            if dry_run:
                logger.progress(f"  - {pkg_name} (would be removed)")
            else:
                logger.progress(f"  - {pkg_name}")

        if dry_run:
            logger.success("Dry run complete - no changes made")
            return

        # Remove orphaned packages
        removed_count = 0
        pruned_keys = []
        deleted_pkg_paths: list = []
        for org_repo_name in orphaned_packages:
            path_parts = org_repo_name.split("/")
            pkg_path = apm_modules_dir.joinpath(*path_parts)
            try:
                safe_rmtree(pkg_path, apm_modules_dir)
                logger.progress(f"+ Removed {org_repo_name}")
                removed_count += 1
                pruned_keys.append(org_repo_name)
                deleted_pkg_paths.append(pkg_path)
            except Exception as e:
                logger.error(f"x Failed to remove {org_repo_name}: {e}")

        # Batch parent cleanup  -- single bottom-up pass
        from ..integration.base_integrator import BaseIntegrator

        BaseIntegrator.cleanup_empty_parents(deleted_pkg_paths, stop_at=apm_modules_dir)

        # Clean deployed files for pruned packages and update lockfile
        if pruned_keys:
            lockfile_path = get_lockfile_path(Path("."))
            lockfile = LockFile.read(lockfile_path)
            project_root = Path(".")
            if lockfile:
                deployed_cleaned = 0
                deleted_targets: list = []
                for dep_key in pruned_keys:
                    dep = lockfile.get_dependency(dep_key)
                    if not dep or not dep.deployed_files:
                        # No deployed files to clean — just remove lockfile entry
                        if dep_key in lockfile.dependencies:
                            del lockfile.dependencies[dep_key]
                        continue
                    for rel_path in dep.deployed_files:
                        if not BaseIntegrator.validate_deploy_path(rel_path, project_root):
                            continue
                        target = project_root / rel_path
                        if target.is_file():
                            target.unlink()
                            deployed_cleaned += 1
                            deleted_targets.append(target)
                        elif target.is_dir():
                            shutil.rmtree(target)
                            deployed_cleaned += 1
                            deleted_targets.append(target)
                    # Remove from lockfile
                    if dep_key in lockfile.dependencies:
                        del lockfile.dependencies[dep_key]
                # Batch parent cleanup  -- single bottom-up pass
                BaseIntegrator.cleanup_empty_parents(deleted_targets, stop_at=project_root)
                if deployed_cleaned > 0:
                    logger.progress(f"+ Cleaned {deployed_cleaned} deployed integration file(s)")
                # Write updated lockfile (or remove if empty)
                try:
                    if lockfile.dependencies:
                        lockfile.write(lockfile_path)
                    else:
                        lockfile_path.unlink(missing_ok=True)
                except Exception:
                    pass

        # Final summary
        if removed_count > 0:
            logger.success(f"Pruned {removed_count} orphaned package(s)")
        else:
            logger.warning("No packages were removed")

    except Exception as e:
        logger.error(f"Error pruning packages: {e}")
        sys.exit(1)
