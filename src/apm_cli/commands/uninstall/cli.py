"""APM uninstall command CLI."""

import builtins
import sys
from pathlib import Path

import click

from ...constants import APM_MODULES_DIR, APM_YML_FILENAME
from ...core.command_logger import CommandLogger

from ...models.apm_package import APMPackage

from .engine import (
    _parse_dependency_entry,
    _validate_uninstall_packages,
    _dry_run_uninstall,
    _remove_packages_from_disk,
    _cleanup_transitive_orphans,
    _sync_integrations_after_uninstall,
    _cleanup_stale_mcp,
)


@click.command(help="Remove APM packages, their integrated files, and apm.yml entries")
@click.argument("packages", nargs=-1, required=True)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be removed without removing"
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed removal information")
@click.option(
    "--global", "-g", "global_",
    is_flag=True,
    default=False,
    help="Remove from user scope (~/.apm/) instead of the current project",
)
@click.pass_context
def uninstall(ctx, packages, dry_run, verbose, global_):
    """Remove APM packages from apm.yml and apm_modules (like npm uninstall).

    This command removes packages from both the apm.yml dependencies list
    and the apm_modules/ directory. It's the opposite of 'apm install <package>'.

    Examples:
        apm uninstall acme/my-package                # Remove one package
        apm uninstall org/pkg1 org/pkg2              # Remove multiple packages
        apm uninstall acme/my-package --dry-run      # Show what would be removed
        apm uninstall -g acme/my-package             # Remove from user scope
    """
    from ...core.scope import InstallScope, get_deploy_root, get_apm_dir, get_modules_dir, get_manifest_path
    scope = InstallScope.USER if global_ else InstallScope.PROJECT

    manifest_path = get_manifest_path(scope)
    apm_dir = get_apm_dir(scope)
    deploy_root = get_deploy_root(scope)
    manifest_display = str(manifest_path) if scope is InstallScope.USER else APM_YML_FILENAME

    logger = CommandLogger("uninstall", verbose=verbose, dry_run=dry_run)
    try:
        # Check if apm.yml exists
        if not manifest_path.exists():
            if scope is InstallScope.USER:
                logger.error(
                    f"No user manifest found at {manifest_display}. Install a package globally "
                    "first with 'apm install -g <package>' or create the file manually."
                )
            else:
                logger.error(f"No {manifest_display} found. Run 'apm init' in this project first.")
            sys.exit(1)

        if not packages:
            logger.error("No packages specified. Specify packages to uninstall.")
            sys.exit(1)

        if scope is InstallScope.USER:
            logger.progress("Uninstalling from user scope (~/.apm/)")

        logger.start(f"Uninstalling {len(packages)} package(s)...")

        # Read current apm.yml
        from ...utils.yaml_io import load_yaml, dump_yaml

        apm_yml_path = manifest_path
        try:
            data = load_yaml(apm_yml_path) or {}
        except Exception as e:
            logger.error(f"Failed to read {apm_yml_path}: {e}")
            sys.exit(1)

        if "dependencies" not in data:
            data["dependencies"] = {}
        if "apm" not in data["dependencies"]:
            data["dependencies"]["apm"] = []

        current_deps = data["dependencies"]["apm"] or []

        # Step 1: Validate packages
        packages_to_remove, packages_not_found = _validate_uninstall_packages(packages, current_deps, logger)
        if not packages_to_remove:
            logger.warning("No packages found in apm.yml to remove")
            return

        # Step 2: Dry run
        modules_dir = get_modules_dir(scope)
        if dry_run:
            _dry_run_uninstall(packages_to_remove, modules_dir, logger)
            return

        # Step 3: Remove from apm.yml
        for package in packages_to_remove:
            current_deps.remove(package)
            logger.progress(f"Removed {package} from apm.yml")
        data["dependencies"]["apm"] = current_deps
        try:
            dump_yaml(data, apm_yml_path)
            logger.success(f"Updated {apm_yml_path} (removed {len(packages_to_remove)} package(s))")
        except Exception as e:
            logger.error(f"Failed to write {apm_yml_path}: {e}")
            sys.exit(1)

        # Step 4: Load lockfile and capture pre-uninstall MCP state
        from ...deps.lockfile import LockFile, get_lockfile_path
        lockfile_path = get_lockfile_path(apm_dir)
        lockfile = LockFile.read(lockfile_path)
        _pre_uninstall_mcp_servers = builtins.set(lockfile.mcp_servers) if lockfile else builtins.set()

        # Step 5: Remove packages from disk
        removed_from_modules = _remove_packages_from_disk(packages_to_remove, modules_dir, logger)

        # Step 6: Cleanup transitive orphans
        orphan_removed, actual_orphans = _cleanup_transitive_orphans(
            lockfile, packages_to_remove, modules_dir, apm_yml_path, logger
        )
        removed_from_modules += orphan_removed

        # Step 7: Collect deployed files for removed packages (before lockfile mutation)
        from ...integration.base_integrator import BaseIntegrator
        removed_keys = builtins.set()
        for pkg in packages_to_remove:
            try:
                ref = _parse_dependency_entry(pkg)
                removed_keys.add(ref.get_unique_key())
            except (ValueError, TypeError, AttributeError, KeyError):
                removed_keys.add(pkg)
        removed_keys.update(actual_orphans)
        all_deployed_files = builtins.set()
        if lockfile:
            for dep_key, dep in lockfile.dependencies.items():
                if dep_key in removed_keys:
                    all_deployed_files.update(dep.deployed_files)
        all_deployed_files = BaseIntegrator.normalize_managed_files(all_deployed_files) or builtins.set()

        # Step 8: Update lockfile
        if lockfile:
            lockfile_updated = False
            for pkg in packages_to_remove:
                try:
                    ref = _parse_dependency_entry(pkg)
                    key = ref.get_unique_key()
                except (ValueError, TypeError, AttributeError, KeyError):
                    key = pkg
                if key in lockfile.dependencies:
                    del lockfile.dependencies[key]
                    lockfile_updated = True
            for orphan_key in actual_orphans:
                if orphan_key in lockfile.dependencies:
                    del lockfile.dependencies[orphan_key]
                    lockfile_updated = True
            if lockfile_updated:
                try:
                    if lockfile.dependencies:
                        lockfile.write(lockfile_path)
                    else:
                        lockfile_path.unlink(missing_ok=True)
                except Exception:
                    logger.warning("Failed to update lockfile -- it may be out of sync with uninstalled packages.")

        # Step 9: Sync integrations
        cleaned = {"prompts": 0, "agents": 0, "skills": 0, "commands": 0, "hooks": 0, "instructions": 0}
        try:
            apm_package = APMPackage.from_apm_yml(manifest_path)
            cleaned = _sync_integrations_after_uninstall(
                apm_package, deploy_root, all_deployed_files, logger,
                user_scope=scope is InstallScope.USER,
            )
        except Exception:
            pass  # Best effort cleanup

        for label, count in cleaned.items():
            if count > 0:
                logger.progress(f"Cleaned up {count} integrated {label}", symbol="check")
                logger.verbose_detail(f"    Removed {count} deployed {label} file(s)")

        # Step 10: MCP cleanup
        try:
            apm_package = APMPackage.from_apm_yml(manifest_path)
            _cleanup_stale_mcp(
                apm_package,
                lockfile,
                lockfile_path,
                _pre_uninstall_mcp_servers,
                modules_dir=get_modules_dir(scope),
                project_root=deploy_root,
                user_scope=scope is InstallScope.USER,
                scope=scope,
            )
        except Exception:
            logger.warning("MCP cleanup during uninstall failed")

        # Final summary
        summary_lines = [f"Removed {len(packages_to_remove)} package(s) from apm.yml"]
        if removed_from_modules > 0:
            summary_lines.append(f"Removed {removed_from_modules} package(s) from apm_modules/")
        logger.success("Uninstall complete: " + ", ".join(summary_lines))

        if packages_not_found:
            logger.warning(f"Note: {len(packages_not_found)} package(s) were not found in apm.yml")

    except Exception as e:
        logger.error(f"Error uninstalling packages: {e}")
        sys.exit(1)
