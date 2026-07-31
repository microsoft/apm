"""Heavy-lifting helpers extracted from prune.py to stay under the 800-line guardrail."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.deployment_ledger import DeploymentLedgerCodec
from ..core.deployment_state import LocatorKind
from ..integration.base_integrator import BaseIntegrator
from .uninstall.lockfile_state import lockfile_has_persisted_state

if TYPE_CHECKING:
    from ..core.command_logger import CommandLogger
    from ..deps.lockfile import LockFile
    from ..models.apm_package import APMPackage


def _apply_dry_run_prune(
    lockfile: LockFile | None,
    missing_orphaned_keys: list[str],
    orphaned_packages: list[str],
    owner_violations: Any,
    lock_keys_by_path: dict[str, tuple[str, ...]],
    expected_lock_keys: set[str],
    deployment_ledger: Any,
    logger: CommandLogger,
) -> None:
    """Render the planned prune actions for --dry-run and return.

    Encapsulates the entire dry-run reporting path (planned key removal and
    ownership repair preview) so ``prune`` can delegate without branching.
    """
    if missing_orphaned_keys:
        logger.dry_run_notice(
            f"remove {len(missing_orphaned_keys)} stale lockfile dependency record(s)"
        )
    planned_pruned_keys = set(missing_orphaned_keys)
    for orphaned_package in orphaned_packages:
        planned_pruned_keys.update(
            key
            for key in lock_keys_by_path.get(orphaned_package, ())
            if key not in expected_lock_keys
        )
    planned_owner_repairs = (
        DeploymentLedgerCodec.owner_reference_violations(
            lockfile,
            excluded_dependency_keys=planned_pruned_keys,
            ledger=deployment_ledger,
        )
        if lockfile is not None
        else ()
    )
    if planned_owner_repairs:
        logger.dry_run_notice(f"repair {len(planned_owner_repairs)} deployment ownership record(s)")
        for violation in planned_owner_repairs:
            removed_owners = set(violation.invalid_owners)
            if violation.invalid_active_owner is not None:
                removed_owners.add(violation.invalid_active_owner)
            logger.dry_run_notice(
                f"repair {violation.locator.key} "
                f"(remove owner(s): {', '.join(sorted(removed_owners))})"
            )
    logger.success("Dry run complete - no changes made")


def _flush_lockfile_changes(
    lockfile: LockFile | None,
    lockfile_path: Path,
    pruned_keys: list[str],
    deployment_ledger: Any,
    project_root: Path,
    owner_violations: Any,
    removed_packages: list[str],
    logger: CommandLogger,
) -> None:
    """Reconcile ownership, clean deployed files, and persist the mutated lockfile.

    A no-op when ``lockfile`` is ``None``.  On lockfile write failure, renders
    the current logger summary and exits 1 so the caller never needs to handle
    a partial-write state.
    """
    if lockfile is None:
        return

    # Late import routes through the prune facade so test patches on
    # apm_cli.commands.prune.remove_stale_deployed_files are intercepted.
    from . import prune as _prune

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
        ledger=deployment_ledger,
        project_root=project_root,
        diagnostics=logger.diagnostics,
    )
    retained_paths = {
        DeploymentLedgerCodec.legacy_value(record.locator)
        for record in reconciled.ledger.records.values()
    }
    deleted_targets: list[Path] = []
    deployed_cleaned = 0
    trusted_cleanup_paths: set[str] = set()
    for dep_key, (paths, hashes) in cleanup_claims.items():
        trusted_paths = set(paths)
        trusted_cleanup_paths.update(trusted_paths)
        cleanup = _prune.remove_stale_deployed_files(
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
        if removed_packages:
            logger.error_detail(
                f"Package directories already removed: {', '.join(removed_packages)}."
            )
        logger.error_detail(
            "Filesystem cleanup may be partial. Rerun 'apm prune', then run 'apm audit'."
        )
        sys.exit(1)


def _reconcile_hooks_after_prune(
    apm_package: APMPackage,
    project_root: Path,
    lockfile: LockFile | None,
    logger: CommandLogger,
) -> None:
    """Best-effort hook reconciliation after package removal.

    Delegates to ``HookIntegrator.reconcile_after_removal`` -- the same
    canonical owner ``apm uninstall`` uses.  Failures are logged as warnings
    so the prune summary is always reached.
    """
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
