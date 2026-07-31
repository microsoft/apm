"""APM prune command."""

import sys
from pathlib import Path

import click

from ..constants import APM_MODULES_DIR, APM_YML_FILENAME
from ..core.command_logger import CommandLogger
from ..core.deployment_ledger import DeploymentLedgerCodec

# APM Dependencies
from ..deps.lockfile import LockFile, get_lockfile_path
from ..integration.base_integrator import BaseIntegrator
from ..integration.cleanup import (
    remove_stale_deployed_files as remove_stale_deployed_files,
)
from ..models.apm_package import APMPackage
from ..utils.path_security import safe_rmtree
from ._helpers import (
    _build_expected_install_paths,
    _expand_with_ancestors,
    _scan_installed_packages,
    _standalone_installed_packages,
)
from ._prune_ops import (
    _apply_dry_run_prune,
    _flush_lockfile_changes,
    _reconcile_hooks_after_prune,
)


def _lock_keys_by_install_path(
    lockfile: LockFile,
    apm_modules_dir: Path,
) -> dict[str, tuple[str, ...]]:
    """Index canonical lock keys by their host-blind installed path."""
    grouped: dict[str, list[str]] = {}
    for dep_key, dependency in sorted(lockfile.dependencies.items()):
        if dep_key == ".":
            continue
        install_path = dependency.to_dependency_ref().get_install_path(apm_modules_dir)
        try:
            relative_path = install_path.relative_to(apm_modules_dir).as_posix()
        except ValueError:
            continue
        grouped.setdefault(relative_path, []).append(dep_key)
    return {path: tuple(keys) for path, keys in grouped.items()}


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
        expected_lock_keys = {dependency.get_unique_key() for dependency in declared_deps}
        if lockfile is not None:
            expected_lock_keys.update(
                dep_key
                for dep_key, dependency in lockfile.dependencies.items()
                if dependency.depth is not None and dependency.depth > 1
            )
        lock_keys_by_path = (
            _lock_keys_by_install_path(lockfile, apm_modules_dir) if lockfile is not None else {}
        )
        orphaned_packages = sorted(
            p for p in installed_packages if p not in expected_with_ancestors
        )
        missing_orphaned_keys = sorted(
            dep_key
            for relative_path, dep_keys in lock_keys_by_path.items()
            if relative_path in expected_with_ancestors
            or not (apm_modules_dir / relative_path).exists()
            for dep_key in dep_keys
            if dep_key not in expected_lock_keys
        )
        deployment_ledger = (
            DeploymentLedgerCodec.from_lockfile(lockfile) if lockfile is not None else None
        )
        owner_violations = (
            DeploymentLedgerCodec.owner_reference_violations(
                lockfile,
                ledger=deployment_ledger,
            )
            if lockfile is not None
            else ()
        )

        if not orphaned_packages and not missing_orphaned_keys and not owner_violations:
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
        if missing_orphaned_keys:
            logger.progress(
                f"Found {len(missing_orphaned_keys)} stale lockfile dependency "
                "record(s) without installed package content."
            )

        if dry_run:
            _apply_dry_run_prune(
                lockfile=lockfile,
                missing_orphaned_keys=missing_orphaned_keys,
                orphaned_packages=orphaned_packages,
                owner_violations=owner_violations,
                lock_keys_by_path=lock_keys_by_path,
                expected_lock_keys=expected_lock_keys,
                deployment_ledger=deployment_ledger,
                logger=logger,
            )
            return

        removed_count = 0
        removed_packages: list[str] = []
        pruned_keys = list(missing_orphaned_keys)
        pruned_key_set = set(pruned_keys)
        deleted_pkg_paths: list[Path] = []
        for org_repo_name in orphaned_packages:
            path_parts = org_repo_name.split("/")
            pkg_path = apm_modules_dir.joinpath(*path_parts)
            try:
                safe_rmtree(pkg_path, apm_modules_dir)
                logger.progress(f"Removed {org_repo_name}")
                removed_count += 1
                removed_packages.append(org_repo_name)
                for dep_key in (
                    key
                    for key in lock_keys_by_path.get(org_repo_name, ())
                    if key not in expected_lock_keys
                ):
                    if dep_key not in pruned_key_set:
                        pruned_keys.append(dep_key)
                        pruned_key_set.add(dep_key)
                deleted_pkg_paths.append(pkg_path)
            except Exception as e:
                logger.error(f"Failed to remove {org_repo_name}: {e}")

        BaseIntegrator.cleanup_empty_parents(deleted_pkg_paths, stop_at=apm_modules_dir)

        _flush_lockfile_changes(
            lockfile=lockfile,
            lockfile_path=lockfile_path,
            pruned_keys=pruned_keys,
            deployment_ledger=deployment_ledger,
            project_root=project_root,
            owner_violations=owner_violations,
            removed_packages=removed_packages,
            logger=logger,
        )

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
            _reconcile_hooks_after_prune(apm_package, project_root, lockfile, logger)

        if removed_count > 0:
            message = f"Pruned {removed_count} orphaned package(s)"
            if owner_violations:
                message += f" and repaired {len(owner_violations)} deployment ownership record(s)"
            logger.success(message)
        elif pruned_keys:
            logger.success(f"Repaired {len(pruned_keys)} stale dependency record(s)")
        elif owner_violations:
            logger.success(f"Repaired {len(owner_violations)} deployment ownership record(s)")
        else:
            logger.warning("No packages were removed")

    except Exception as e:
        logger.error(f"Error pruning packages: {e}")
        sys.exit(1)
