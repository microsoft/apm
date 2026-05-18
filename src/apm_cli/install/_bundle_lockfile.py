"""Local bundle lockfile helpers extracted from local_bundle_handler.py."""

from __future__ import annotations

from pathlib import Path


def _persist_local_bundle_lockfile(
    *,
    project_root: Path,
    deployed: list[str],
    deployed_hashes: dict[str, str],
    legacy_skill_paths: bool,
    logger,
) -> None:
    """Persist local bundle deployment state into the lockfile."""
    if not deployed:
        return

    from ..deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed

    migrate_lockfile_if_needed(project_root)
    lockfile_path = get_lockfile_path(project_root)
    lockfile = LockFile.read(lockfile_path) or LockFile()
    existing = set(lockfile.local_deployed_files)
    existing.update(deployed)
    lockfile.local_deployed_files = sorted(existing)
    existing_hashes = dict(lockfile.local_deployed_file_hashes)
    existing_hashes.update(deployed_hashes)
    lockfile.local_deployed_file_hashes = existing_hashes

    if not legacy_skill_paths:
        _migrate_legacy_skill_paths(lockfile, lockfile_path, project_root, logger)

    lockfile.write(lockfile_path)


def _migrate_legacy_skill_paths(lockfile, lockfile_path: Path, project_root: Path, logger) -> None:
    """Auto-migrate legacy per-client skill paths after bundle deployment."""
    del lockfile_path
    from ..utils.console import _rich_error, _rich_info
    from .skill_path_migration import (
        COLLISION_DETAIL_TEMPLATE,
        COLLISION_HEADER_TEMPLATE,
        COLLISION_HINT,
        MIGRATION_SUMMARY_TEMPLATE,
    )
    from .skill_path_migration import (
        check_collisions as _check_coll,
    )
    from .skill_path_migration import (
        detect_legacy_skill_deployments as _detect_legacy,
    )
    from .skill_path_migration import (
        execute_migration as _exec_mig,
    )

    plans = _detect_legacy(lockfile, project_root)
    if not plans:
        return

    collisions = _check_coll(plans, project_root)
    if collisions:
        _rich_error(
            COLLISION_HEADER_TEMPLATE.format(count=len(collisions)),
            symbol="error",
        )
        for plan in plans:
            for collision_detail in collisions:
                if plan.dst_path in collision_detail:
                    _rich_error(
                        COLLISION_DETAIL_TEMPLATE.format(
                            dst_path=plan.dst_path,
                            src_path=plan.src_path,
                            dep_name=plan.dep_name,
                        ),
                        symbol="error",
                    )
                    break
        _rich_info(COLLISION_HINT, symbol="info")
        return

    migration_result = _exec_mig(plans, lockfile, project_root)
    total = len(migration_result.deleted) + len(migration_result.skipped_no_file)
    if total:
        _rich_info(MIGRATION_SUMMARY_TEMPLATE.format(count=total), symbol="info")
    if getattr(logger, "verbose", False) and migration_result.deleted:
        for deleted_path in migration_result.deleted:
            _rich_info(f"  removed {deleted_path}", symbol="info")
