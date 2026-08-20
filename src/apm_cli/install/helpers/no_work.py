"""Install-pipeline no-work and empty-lockfile handling."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path


def write_empty_lockfile(apm_dir: Path) -> None:
    """Materialize an empty lockfile for a dependency-free ``apm lock``."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    lock_path = get_lockfile_path(apm_dir)
    new_lock = LockFile.from_installed_packages([], None)
    existing_lock = LockFile.read(lock_path) if lock_path.exists() else None
    if not (existing_lock and new_lock.is_semantically_equivalent(existing_lock)):
        new_lock.save(lock_path)


def is_no_work_install(
    *,
    all_apm_deps: Collection[object],
    root_has_local_primitives: bool,
    old_local_deployed: Collection[object],
    has_orphan_deps: bool,
    lockfile_only: bool,
    apm_dir: Path | None,
) -> bool:
    """Return whether an install has no deployment, cleanup, or lock work."""
    if all_apm_deps or root_has_local_primitives or old_local_deployed or has_orphan_deps:
        return False
    if lockfile_only and apm_dir is not None:
        write_empty_lockfile(apm_dir)
    return True
