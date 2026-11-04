"""Install-pipeline no-work and empty-lockfile handling."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.target_detection import EffectiveTargetDecision
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.context import InstallContext
    from apm_cli.models.apm_package import APMPackage


def write_empty_lockfile(
    apm_dir: Path,
    *,
    apm_package: APMPackage | None = None,
    logger: InstallLogger | None = None,
    lockfile_only: bool = True,
) -> None:
    """Persist dependency-free root intent without resolving sources or targets."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    from apm_cli.install.context import InstallContext
    from apm_cli.install.phases.lockfile import LockfileBuilder

    lock_path = get_lockfile_path(apm_dir)
    existing_lock = LockFile.read(lock_path) if lock_path.exists() else None
    builder = LockfileBuilder(
        InstallContext(
            project_root=apm_dir,
            apm_dir=apm_dir,
            apm_package=apm_package,
            existing_lockfile=existing_lock,
            logger=logger,
            lockfile_only=lockfile_only,
        )
    )
    builder.save_replacement_intent()


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
    target_decision: EffectiveTargetDecision | None = None,
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
    if target_decision is not None:
        return target_decision.value is not None
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
    local_cleanup_pending: bool,
    has_orphan_deps: bool,
    lockfile_only: bool,
    apm_dir: Path | None,
    apm_package: APMPackage | None = None,
    logger: InstallLogger | None = None,
) -> bool:
    """Return whether an install has no deployment, cleanup, or lock work."""
    if all_apm_deps or root_has_local_primitives or local_cleanup_pending or has_orphan_deps:
        return False
    if apm_dir is not None:
        write_empty_lockfile(
            apm_dir, apm_package=apm_package, logger=logger, lockfile_only=lockfile_only
        )
    return True
