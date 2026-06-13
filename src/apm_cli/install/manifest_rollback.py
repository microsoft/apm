"""Manifest snapshot and rollback helpers for APM install."""

import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger


def _restore_manifest_from_snapshot(
    manifest_path: "Path",
    snapshot: bytes,
) -> None:
    """Atomically restore ``apm.yml`` from a raw-bytes snapshot.

    Uses temp-file + ``os.replace`` to avoid torn writes, mirroring the
    W1 cache atomic-write pattern (``discovery.py``).
    """
    import tempfile

    fd, tmp_name = tempfile.mkstemp(
        prefix="apm-restore-",
        dir=str(manifest_path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(snapshot)
        os.replace(tmp_name, str(manifest_path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _maybe_rollback_manifest(
    manifest_path: "Path",
    snapshot: "bytes | None",
    logger: "InstallLogger",
) -> None:
    """Restore ``apm.yml`` from *snapshot* if one was captured, then log.

    No-op when *snapshot* is ``None`` (i.e. the command was not
    ``apm install <pkg>`` or the manifest did not exist before mutation).
    """
    if snapshot is None:
        return
    # RULE B: _restore_manifest_from_snapshot is patched at
    # apm_cli.commands.install.* in tests that exercise this function.
    import apm_cli.commands.install as _m

    try:
        _m._restore_manifest_from_snapshot(manifest_path, snapshot)
        logger.progress("apm.yml restored to its previous state.")
    except Exception:
        # Best-effort: if the restore itself fails, warn but don't mask
        # the original exception that triggered the rollback.
        logger.warning("Failed to restore apm.yml to its previous state.")
