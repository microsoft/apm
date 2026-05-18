"""Cache maintenance functions extracted from git_cache.py."""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from ._git_helpers import _dir_size

_log = logging.getLogger(__name__)


def _evict_checkout_impl(checkout_dir: Path) -> None:
    """Safely remove a corrupt checkout shard."""
    from ..utils.file_ops import robust_rmtree

    try:
        robust_rmtree(checkout_dir, ignore_errors=True)
    except Exception as exc:
        _log.debug("Failed to evict checkout %s: %s", checkout_dir, exc)


def get_cache_stats_impl(db_root: Path, checkouts_root: Path) -> dict[str, int]:
    """Return cache statistics for ``apm cache info``.

    Returns:
        Dict with keys: db_count, checkout_count, total_size_bytes.
    """
    db_count = 0
    checkout_count = 0
    total_size = 0

    if db_root.is_dir():
        for entry in os.scandir(str(db_root)):
            if entry.is_dir(follow_symlinks=False) and not entry.name.endswith(".lock"):
                db_count += 1
                total_size += _dir_size(Path(entry.path))

    if checkouts_root.is_dir():
        for shard_entry in os.scandir(str(checkouts_root)):
            if shard_entry.is_dir(follow_symlinks=False):
                for sha_entry in os.scandir(shard_entry.path):
                    if sha_entry.is_dir(follow_symlinks=False):
                        checkout_count += 1
                        total_size += _dir_size(Path(sha_entry.path))

    return {
        "db_count": db_count,
        "checkout_count": checkout_count,
        "total_size_bytes": total_size,
    }


def clean_all_impl(db_root: Path, checkouts_root: Path) -> None:
    """Remove ALL cache content (db + checkouts). Used by ``apm cache clean``."""
    from ..utils.file_ops import robust_rmtree

    for bucket in (db_root, checkouts_root):
        if bucket.is_dir():
            for entry in os.scandir(str(bucket)):
                if entry.is_dir(follow_symlinks=False):
                    robust_rmtree(Path(entry.path), ignore_errors=True)
                elif entry.is_file(follow_symlinks=False):
                    with contextlib.suppress(OSError):
                        os.unlink(entry.path)


def prune_impl(checkouts_root: Path, *, max_age_days: int = 30) -> int:
    """Remove checkout entries older than *max_age_days*.

    Uses mtime of the checkout directory as the access indicator.

    Returns:
        Number of entries pruned.
    """
    import time

    from ..utils.file_ops import robust_rmtree

    cutoff = time.time() - (max_age_days * 86400)
    pruned = 0

    if not checkouts_root.is_dir():
        return 0

    for shard_entry in os.scandir(str(checkouts_root)):
        if not shard_entry.is_dir(follow_symlinks=False):
            continue
        for sha_entry in os.scandir(shard_entry.path):
            if not sha_entry.is_dir(follow_symlinks=False):
                continue
            try:
                stat = sha_entry.stat(follow_symlinks=False)
                if stat.st_mtime < cutoff:
                    robust_rmtree(Path(sha_entry.path), ignore_errors=True)
                    pruned += 1
            except OSError:
                continue

    return pruned
