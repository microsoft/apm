"""Install-pipeline no-work and empty-lockfile handling."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.context import InstallContext


def write_empty_lockfile(apm_dir: Path) -> None:
    """Materialize an empty lockfile for a dependency-free ``apm lock``."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    lock_path = get_lockfile_path(apm_dir)
    new_lock = LockFile.from_installed_packages([], None)
    existing_lock = LockFile.read(lock_path) if lock_path.exists() else None
    if not (existing_lock and new_lock.is_semantically_equivalent(existing_lock)):
        new_lock.save(lock_path)


def _has_authored_rows(lockfile: LockFile | None) -> bool:
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    return lockfile is not None and bool(DeploymentLedgerCodec.authored_local_paths(lockfile))


def local_rows_need_reconciling(
    lockfile: LockFile | None,
    project_root: Path,
    package: object,
    *,
    target: str | list[str] | None,
    scope: object,
) -> bool:
    """Return whether *lockfile*'s retained project rows give an install work.

    Rows outlive their deleted ``.apm/`` sources until stale cleanup runs;
    imperative bundle rows are not install-owned. *target* is the CLI
    selection the pipeline receives. When no single target set resolves
    (none detected, or several), the install stays a no-op instead of
    failing target resolution (#3179).
    """
    from apm_cli.core.errors import TargetResolutionError
    from apm_cli.core.scope import is_user_scope
    from apm_cli.core.target_detection import resolve_package_target_decision

    if not _has_authored_rows(lockfile):
        return False
    try:
        decision = resolve_package_target_decision(
            project_root,
            package=package,
            explicit_target=target,
            user_scope=is_user_scope(scope),
            create_config=False,
        )
    except TargetResolutionError:
        return False
    return decision.value is not None


def retained_local_work(
    ctx: InstallContext, lockfile: LockFile | None, lockfile_only: bool
) -> bool:
    """Pipeline view of :func:`local_rows_need_reconciling`, reusing the run's target decision.

    ``apm lock`` never cleans up.
    """
    decision = ctx.target_decision
    return (
        not lockfile_only
        and decision is not None
        and decision.value is not None
        and _has_authored_rows(lockfile)
    )


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
