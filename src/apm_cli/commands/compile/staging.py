"""Compile pre-flight for local bundle compile staging (#3718)."""

from __future__ import annotations

from pathlib import Path

from apm_cli.core.remediation import (
    STAGED_BUNDLE_DRIFT,
)

from ...core.command_logger import CommandLogger
from ...deps.lockfile import LockFile, resolve_lockfile_path_for_read


def report_staged_hash_drift(logger: CommandLogger, project_root: Path) -> None:
    """Warn when bundle-staged compile input changed since install."""
    lock = LockFile.read(resolve_lockfile_path_for_read(project_root, read_only=True))
    if lock is None:
        return
    for path, _expected, _actual in lock.staged_hash_drift(project_root):
        logger.warning(
            f"hash-drift: staged file {path} changed since install and will be compiled "
            f"as-is -- {STAGED_BUNDLE_DRIFT.clause()}"
        )
