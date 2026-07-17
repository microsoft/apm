"""APM prune command."""

import sys
from pathlib import Path

import click

from ..constants import APM_MODULES_DIR, APM_YML_FILENAME
from ..core.command_logger import CommandLogger
from ..core.deployment_ledger import DeploymentLedgerCodec
from ..core.deployment_state import LocatorKind

# APM Dependencies
from ..deps.lockfile import LockFile, get_lockfile_path
from ..integration.base_integrator import BaseIntegrator
from ..integration.cleanup import remove_stale_deployed_files
from ..models.apm_package import APMPackage
from ..utils.path_security import safe_rmtree
from ._helpers import (
    _build_expected_install_paths,
    _expand_with_ancestors,
    _scan_installed_packages,
    _standalone_installed_packages,
)
from .uninstall.lockfile_state import lockfile_has_persisted_state


@click.command(
    help=(
        "Remove APM packages absent from the resolved dependency graph "
        "and repair stale deployment owners"
    )
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview package removal and ownership repair without mutating anything",
)
@click.pass_context
def prune(ctx, dry_run):
    """Remove orphaned packages and repair stale deployment ownership.

    This command cleans up the apm_modules/ directory by removing packages that are
    neither declared in apm.yml nor retained as transitive nodes in apm.lock.yaml.
    It also reconciles invalid canonical deployment owners in the lockfile.

    Examples:
        apm prune           # Remove orphaned packages
        apm prune --dry-run # Show what would be removed
    """
    logger = CommandLogger("prune", dry_run=dry_run)
    try:
        if not Path(APM_YML_FILENAME).exists():
            logger.error("No apm.yml found. Run 'apm init' first.")
            sys.exit(1)

        apm_modules_dir = Path(APM_MODULES_DIR)
        logger.start("Analyzing installed packages vs apm.yml...")

        try:
            apm_package = APMPackage.from_apm_yml(Path(APM_YML_FILENAME))
            declared_deps = apm_package.get_all_apm_dependencies()
            project_root = Path.cwd()
            lockfile_path = get_lockfile_path(project_root)
            lockfile = LockFile.read(lockfile_path)
            expected_installed = _build_expected_install_paths(
                declared_deps, lockfile, apm_modules_dir
            )
        except Exception as e:
            logger.error(f"Failed to parse {APM_YML_FILENAME}: {e}")
            sys.exit(1)

        installed_packages = (
            _scan_installed_packages(apm_modules_dir) if apm_modules_dir.exists() else set()
        )
        standalone_installed = _standalone_installed_packages(
            installed_packages,
            apm_modules_dir,
            lockfile=lockfile,
        )
        expected_with_ancestors = _expand_with_ancestors(
            expected_installed,
            standalone_installed,
        )
        orphaned_packages = sorted(
            p for p in installed_packages if p not in expected_with_ancestors
        )
        owner_violations = (
            DeploymentLedgerCodec.owner_reference_violations(lockfile)
            if lockfile is not None
            else ()
        )

        if not orphaned_packages and not owner_violations:
            if not apm_modules_dir.exists():
                logger.progress("No apm_modules/ directory found. Nothing to prune.")
            else:
                logger.success(
                    "No orphaned packages found. apm_modules/ is clean.",
                    symbol="check",
                )
            return

        if orphaned_packages:
            logger.warning(f"Found {len(orphaned_packages)} orphaned package(s):")
            for pkg_name in orphaned_packages:
                suffix = " (would be removed)" if dry_run else ""
                logger.warning(f"  - {pkg_name}{suffix}")
        if owner_violations:
            logger.warning(
                f"Found {len(owner_violations)} invalid deployment ownership "
                "record(s) in apm.lock.yaml."
            )

        if dry_run:
            if owner_violations:
                logger.dry_run_notice(
                    f"repair {len(owner_violations)} deployment ownership record(s)"
                )
            logger.success("Dry run complete - no changes made")
            return

        removed_count = 0
        pruned_keys: list[str] = []
        deleted_pkg_paths: list[Path] = []
        for org_repo_name in orphaned_packages:
            path_parts = org_repo_name.split("/")
            pkg_path = apm_modules_dir.joinpath(*path_parts)
            try:
                safe_rmtree(pkg_path, apm_modules_dir)
                logger.progress(f"Removed {org_repo_name}")
                removed_count += 1
                pruned_keys.append(org_repo_name)
                deleted_pkg_paths.append(pkg_path)
            except Exception as e:
                logger.error(f"Failed to remove {org_repo_name}: {e}")

        BaseIntegrator.cleanup_empty_parents(deleted_pkg_paths, stop_at=apm_modules_dir)

        if lockfile is not None:
            cleanup_claims = {
                dep_key: (
                    tuple(lockfile.dependencies[dep_key].deployed_files),
                    dict(lockfile.dependencies[dep_key].deployed_file_hashes),
                )
                for dep_key in pruned_keys
                if dep_key in lockfile.dependencies
            }
            reconciled = DeploymentLedgerCodec.reconcile_owner_references(
                lockfile,
                excluded_dependency_keys=pruned_keys,
                project_root=project_root,
                diagnostics=logger.diagnostics,
            )
            retained_paths = {
                record.locator.value
                for record in reconciled.ledger.records.values()
                if record.locator.kind == LocatorKind.PROJECT_RELATIVE
            }
            deleted_targets: list[Path] = []
            deployed_cleaned = 0
            trusted_cleanup_paths: set[str] = set()
            for dep_key, (paths, hashes) in cleanup_claims.items():
                trusted_paths = set(paths)
                trusted_cleanup_paths.update(trusted_paths)
                cleanup = remove_stale_deployed_files(
                    trusted_paths - retained_paths,
                    project_root,
                    dep_key=dep_key,
                    targets=None,
                    diagnostics=logger.diagnostics,
                    recorded_hashes=hashes,
                    failed_path_retained=False,
                )
                deployed_cleaned += len(cleanup.deleted)
                deleted_targets.extend(cleanup.deleted_targets)

            BaseIntegrator.cleanup_empty_parents(
                deleted_targets,
                stop_at=project_root,
            )
            if deployed_cleaned:
                logger.progress(f"Cleaned {deployed_cleaned} deployed integration file(s)")

            for violation in owner_violations:
                locator = violation.locator
                if (
                    locator.kind == LocatorKind.PROJECT_RELATIVE
                    and locator.value not in trusted_cleanup_paths
                    and (project_root / locator.value).exists()
                ):
                    logger.diagnostics.warn(
                        f"Preserved {locator.value}: its deployment owner is "
                        "invalid, so the lockfile record was repaired without "
                        "deleting untrusted bytes. Inspect and remove the path "
                        "manually if it is no longer needed."
                    )

            for dep_key in pruned_keys:
                lockfile.dependencies.pop(dep_key, None)
            DeploymentLedgerCodec.apply_to_lockfile(reconciled.ledger, lockfile)
            try:
                if lockfile_has_persisted_state(lockfile):
                    lockfile.write(lockfile_path)
                else:
                    lockfile_path.unlink(missing_ok=True)
            except Exception as e:
                logger.render_summary()
                logger.error(f"Failed to update apm.lock.yaml: {e}")
                logger.error_detail(
                    "Filesystem cleanup may be partial. Rerun 'apm prune', then run 'apm audit'."
                )
                sys.exit(1)

        logger.render_summary()

        if pruned_keys:
            # Reconcile merged-hook ownership (settings.json / hooks.json
            # entries and their apm-hooks.json sidecars) for the packages
            # just pruned. This delegates to the same canonical
            # clear-then-rebuild owner `apm uninstall` already uses --
            # prune must not reimplement hook-entry filtering itself.
            # apm.yml is not mutated by prune (orphaned packages are, by
            # definition, already absent from it), so the manifest parsed
            # at the top of this command still reflects the desired state.
            #
            # Best-effort: package/lockfile pruning has already committed
            # by this point, so a reconciliation failure is a warning
            # (not an error) -- it does not roll back or fail the command.
            try:
                from ..integration.hook_integrator import HookIntegrator

                hook_stats = HookIntegrator().reconcile_after_removal(
                    apm_package, project_root, lockfile=lockfile
                )
                hook_errors = hook_stats.get("errors", 0)
                if hook_errors:
                    logger.warning(
                        f"Hook reconciliation incomplete for {hook_errors} "
                        "dependency(ies) -- some hook entries may be stale. "
                        "Run 'apm install' to rebuild hook configuration."
                    )
                else:
                    logger.progress("Reconciled merged hook ownership for pruned package(s)")
            except Exception as e:
                logger.warning(
                    f"Hook reconciliation failed: {e}. Some hook entries may be "
                    "stale -- run 'apm install' to rebuild hook configuration."
                )

        if removed_count > 0:
            logger.success(f"Pruned {removed_count} orphaned package(s)")
        elif owner_violations:
            logger.success(f"Repaired {len(owner_violations)} deployment ownership record(s)")
        else:
            logger.warning("No packages were removed")

    except Exception as e:
        logger.error(f"Error pruning packages: {e}")
        sys.exit(1)
