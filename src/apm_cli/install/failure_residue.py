"""Report native target files a failed install leaves on disk.

``InstallTransaction`` restores ``apm.yml`` and ``apm_modules`` staging only.
Native target integrations run outside it, so a failure after integration
leaves those files in place. This module names them truthfully instead of
letting the failure read as a full rollback (#3525). It never deletes,
restores, or records ownership of any file.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def _normalize(path: object) -> str:
    return str(path).replace("\\", "/")


def _lockfile_recorded_paths(ctx: InstallContext) -> set[str]:
    """Return the deployed paths the on-disk lockfile records right now."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    lockfile = None
    # An unreadable lockfile records nothing the next run can rely on.
    with contextlib.suppress(Exception):
        lockfile = LockFile.read(get_lockfile_path(ctx.apm_dir))
    if lockfile is None:
        return set()
    recorded = {_normalize(path) for path in lockfile.local_deployed_files}
    for dependency in lockfile.dependencies.values():
        recorded.update(_normalize(path) for path in dependency.deployed_files)
    return recorded


def unrecorded_deployed_paths(ctx: InstallContext) -> list[str]:
    """Return native target paths this attempt deployed that no lockfile records.

    These are the files a later install would treat as not managed by APM.
    """
    deployed = {_normalize(path) for path in ctx.local_deployed_files}
    for paths in ctx.package_deployed_files.values():
        deployed.update(_normalize(path) for path in paths)
    if not deployed:
        return []
    return sorted(deployed - _lockfile_recorded_paths(ctx))


def report_failed_install_residue(ctx: InstallContext) -> None:
    """Name the native target files a failed install attempt left on disk."""
    if ctx.logger is None or ctx.dry_run or ctx.lockfile_only:
        return
    paths = unrecorded_deployed_paths(ctx)
    if paths:
        ctx.logger.failed_install_residue(paths)
