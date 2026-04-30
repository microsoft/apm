"""Shared I/O helpers for marketplace modules."""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["atomic_write"]


def atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via tmp + fsync + rename.

    The caller sees either the complete new content or the previous
    content -- never a partial write.
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp_path), str(path))
    except BaseException:
        # Clean up tmp file on failure.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
