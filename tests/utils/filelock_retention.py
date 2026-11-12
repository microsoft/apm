"""Simulate filelock retaining a released lock file across filelock versions."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def retain_native_lock_unlink(monkeypatch: pytest.MonkeyPatch, lock_path: Path) -> None:
    """Make filelock's native Windows post-release unlink of *lock_path* fail.

    filelock 3.x removes a released lock file through ``Path.unlink`` (which
    callers patch directly), while filelock 4.x on Windows uses a native
    helper instead. Both suppress ``OSError``, so failing the helper leaves
    the file in place exactly as a contending handle would. No-op elsewhere.
    """
    try:
        from filelock import _windows
    except ImportError:
        return
    native_unlink = getattr(_windows, "unlink_without_oplock_wait", None)
    if native_unlink is None:
        return

    def retain(path: str) -> None:
        if _same_path(path, lock_path):
            raise PermissionError("Fixture retains the lock file")
        native_unlink(path)

    monkeypatch.setattr(_windows, "unlink_without_oplock_wait", retain)
