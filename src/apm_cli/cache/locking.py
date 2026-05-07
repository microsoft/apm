"""Cross-platform shard locking and atomic landing primitives.

Provides per-shard file locks (via ``filelock``) and an atomic
stage-then-rename landing protocol that ensures cache shards are
never visible in a partially-populated state.

Atomic landing protocol
-----------------------
1. Stage content into ``<shard>.incomplete.<pid>.<ts>/``
2. Acquire shard ``.lock`` file (filelock)
3. Re-check final path does not exist (TOCTOU defense)
4. ``os.replace()`` staged dir -> final shard path (atomic on same FS)
5. Release lock
6. On cache init, clean up any stale ``*.incomplete.*`` siblings

Design notes
------------
- One lock file per shard (not a global lock) for maximum concurrency.
- Stale incomplete dirs are cleaned up lazily on next cache access.
- On Windows, ``os.replace`` requires both paths on the same volume;
  staging into the same parent directory guarantees this.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from filelock import FileLock, Timeout

_log = logging.getLogger(__name__)

# Default lock timeout (seconds). If another process holds the shard lock
# for longer than this, we assume it crashed and proceed.
DEFAULT_LOCK_TIMEOUT: float = 120.0


def shard_lock(shard_dir: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT) -> FileLock:
    """Return a :class:`FileLock` for the given shard directory.

    The lock file is placed adjacent to (not inside) the shard directory
    so it can be acquired before the shard exists.

    Args:
        shard_dir: Path to the shard directory to protect.
        timeout: Maximum seconds to wait for lock acquisition.

    Returns:
        A :class:`FileLock` instance (not yet acquired).
    """
    lock_path = shard_dir.with_suffix(".lock")
    return FileLock(str(lock_path), timeout=timeout)


def stage_path(final_path: Path) -> Path:
    """Return a staging directory path adjacent to *final_path*.

    Format: ``<final_path>.incomplete.<pid>.<monotonic_ns>``

    The staging dir lives in the same parent as the final path to
    guarantee ``os.replace`` atomicity (same filesystem).
    """
    pid = os.getpid()
    ts = int(time.monotonic_ns())
    return final_path.with_name(f"{final_path.name}.incomplete.{pid}.{ts}")


def atomic_land(staged: Path, final: Path, lock: FileLock) -> bool:
    """Atomically move *staged* to *final* under *lock*.

    Protocol:
    1. Acquire the file lock.
    2. Re-check that *final* does not already exist (TOCTOU defense).
    3. ``os.replace(staged, final)`` -- atomic on same filesystem.
    4. Release lock.

    If *final* already exists when the lock is acquired (another process
    won the race), the staged directory is removed and ``False`` is
    returned.

    Args:
        staged: Staging directory with fully-populated content.
        final: Target shard path.
        lock: Per-shard :class:`FileLock` instance.

    Returns:
        ``True`` if the landing succeeded, ``False`` if another process
        already populated *final*.

    Raises:
        filelock.Timeout: If the lock cannot be acquired within its
            configured timeout.
    """
    try:
        with lock:
            if final.exists():
                # Another process won the race -- discard our staged copy.
                _safe_rmtree_staged(staged)
                return False
            os.replace(str(staged), str(final))
            return True
    except Timeout:
        _log.warning(
            "[!] Timed out waiting for shard lock: %s",
            lock.lock_file,
        )
        _safe_rmtree_staged(staged)
        raise


def cleanup_incomplete(parent: Path) -> int:
    """Remove stale ``.incomplete.*`` directories under *parent*.

    Called during cache initialization to recover from interrupted
    operations (e.g. kill -9 during a clone).

    Returns:
        Number of stale directories removed.
    """
    if not parent.is_dir():
        return 0

    removed = 0
    try:
        for entry in os.scandir(str(parent)):
            if entry.is_dir(follow_symlinks=False) and ".incomplete." in entry.name:
                _safe_rmtree_staged(Path(entry.path))
                removed += 1
    except OSError as exc:
        _log.debug("Error scanning for incomplete shards in %s: %s", parent, exc)
    return removed


def _safe_rmtree_staged(path: Path) -> None:
    """Remove a staging directory without following symlinks.

    Uses the symlink-safe rmtree from file_ops if available, otherwise
    falls back to shutil with onerror for read-only files.
    """
    if not path.exists() and not path.is_symlink():
        return
    try:
        from ..utils.file_ops import robust_rmtree

        robust_rmtree(path, ignore_errors=True)
    except Exception:
        import shutil

        shutil.rmtree(str(path), ignore_errors=True)
