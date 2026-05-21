"""Manifest rollback helpers for package install failures."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol


class _RollbackLogger(Protocol):
    def progress(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...


def restore_manifest_from_snapshot(
    manifest_path: Path,
    snapshot: bytes,
) -> None:
    """Atomically restore ``apm.yml`` from a raw-bytes snapshot."""
    fd, tmp_name = tempfile.mkstemp(
        prefix="apm-restore-",
        dir=str(manifest_path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(snapshot)
        os.replace(tmp_name, str(manifest_path))
    except Exception:
        try:  # noqa: SIM105
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def maybe_rollback_manifest(
    manifest_path: Path,
    snapshot: bytes | None,
    logger: _RollbackLogger,
) -> None:
    """Restore ``apm.yml`` from *snapshot* if one was captured, then log."""
    if snapshot is None:
        return
    try:
        restore_manifest_from_snapshot(manifest_path, snapshot)
        logger.progress("apm.yml restored to its previous state.")
    except Exception:
        logger.warning("Failed to restore apm.yml to its previous state.")
