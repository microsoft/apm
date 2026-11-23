"""Content-addressed store of deployed file bytes (the deploy store).

Every full install records each deployed file's exact bytes here, keyed by
the file's canonical content digest: the same ``sha256`` hex that the
lockfile records as its ``deployed_file_hashes`` value. A later command can
then rebuild a deployed file from the lockfile hash alone.

The store is a cache, never a trust root:

- Keys are exactly 64 lowercase hex characters, so a key cannot carry a path.
- Every read re-hashes the entry and rejects (and evicts) one whose bytes no
  longer hash to its key; callers treat a rejection as a miss and run the
  full pipeline.
- Neither the bucket directories nor the entries are followed when they are
  symbolic links, and only regular files are read.
- Entries are written to an exclusive owner-only temp file and renamed into
  place, so readers never see a partial entry.

Two byte images that differ only in CRLF/LF line endings share a key (the
digest is line-ending-normalized, apm#1952); either may be the one kept.

Size bound: an entry larger than :data:`MAX_DEPLOY_ENTRY_BYTES` is never
stored, and after a command adds entries the store evicts least recently
used entries (a verified read refreshes recency) once it exceeds
:data:`MAX_DEPLOY_STORE_BYTES`, down to 80% of it. ``apm cache clean``
empties it; an empty store only means the next command does the full work.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

from ..core.invocation_memo import invocation_memo
from ..utils.content_hash import canonical_digest
from .paths import get_cache_root

DEPLOY_BUCKET = "deploy_v1"
MAX_DEPLOY_STORE_BYTES = 256 * 1024 * 1024
MAX_DEPLOY_ENTRY_BYTES = 4 * 1024 * 1024

_KEY_RE = re.compile(r"[0-9a-f]{64}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)
# Windows junctions and other reparse points pass lstat as directories.
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


class DeployStore:
    """Read and write deployed-file bytes under ``<cache>/deploy_v1``."""

    def __init__(self, cache_root: Path) -> None:
        self._root = Path(cache_root) / DEPLOY_BUCKET
        self.written = 0

    @classmethod
    def open_default(cls) -> DeployStore | None:
        """Return the store in the machine cache, or ``None`` when caching is off."""
        if os.environ.get("APM_NO_CACHE", "").strip() in ("1", "true", "yes"):
            return None
        try:
            return cls(get_cache_root())
        except (OSError, ValueError):
            return None

    def _entry(self, key: str) -> Path | None:
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            return None
        return self._root / key[:2] / key

    @staticmethod
    def _real_dir(path: Path) -> bool:
        try:
            info = os.lstat(path)
        except OSError:
            return False
        return stat.S_ISDIR(info.st_mode) and not _is_reparse(info)

    def get(self, key: str, *, raw_sha256: str | None = None) -> bytes | None:
        """Return the verified bytes stored under *key*, or ``None``.

        ``None`` covers a malformed key, a missing entry, a symbolic link or
        reparse point anywhere in the store path, a non-regular file and an
        entry whose bytes do not hash to *key* (which is also evicted). With
        *raw_sha256*, the exact bytes must also match it, so a caller that
        must restore a byte-identical file never gets the other line-ending
        image that shares the line-ending-normalized key.
        """
        entry = self._entry(key)
        if entry is None or not (self._real_dir(self._root) and self._real_dir(entry.parent)):
            return None
        try:
            fd = os.open(entry, os.O_RDONLY | _NOFOLLOW | _BINARY)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or os.path.islink(entry):
                return None
            if _is_reparse(os.lstat(entry)):
                return None
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read()
        except OSError:
            return None
        finally:
            os.close(fd)
        if canonical_digest(data) != key:
            with contextlib.suppress(OSError):
                entry.unlink()
            return None
        if raw_sha256 is not None and hashlib.sha256(data).hexdigest() != raw_sha256:
            return None
        with contextlib.suppress(OSError):
            os.utime(entry, None)
        return data

    def _prepare(self, entry: Path, size: int) -> bool | None:
        """Create the bucket dirs; ``True`` if a same-size entry exists, ``None`` to refuse."""
        for directory in (self._root, entry.parent):
            if not os.path.lexists(directory):
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not self._real_dir(directory):
                return None
        try:
            existing = os.lstat(entry)
        except FileNotFoundError:
            return False
        # Reads verify; a same-size regular entry is left alone so a warm
        # install costs one lstat, while a damaged one is rewritten.
        return stat.S_ISREG(existing.st_mode) and existing.st_size == size

    def put(self, key: str, data: bytes) -> bool:
        """Store *data* under *key*; return whether a usable entry is present.

        *data* must hash to *key*; a mismatch is refused and nothing is written.
        """
        entry = self._entry(key)
        if entry is None or len(data) > MAX_DEPLOY_ENTRY_BYTES:
            return False
        try:
            ready = self._prepare(entry, len(data))
            if ready is not False:
                return bool(ready)
            if key != canonical_digest(data):
                return False
            fd, temp = tempfile.mkstemp(dir=entry.parent, prefix=".tmp-", suffix=".part")
        except OSError:
            return False
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp, entry)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            return False
        self.written += 1
        return True

    def put_file(self, key: str, path: Path) -> bool:
        """Store the regular file at *path* under *key*, reading it only when needed."""
        entry = self._entry(key)
        if entry is None:
            return False
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
                return False
            if info.st_size > MAX_DEPLOY_ENTRY_BYTES:
                return False
            ready = self._prepare(entry, info.st_size)
            if ready is not False:
                return bool(ready)
            data = path.read_bytes()
        except OSError:
            return False
        return self.put(key, data)

    def enforce_size_cap(self, limit: int = MAX_DEPLOY_STORE_BYTES) -> int:
        """Evict least recently used entries above *limit*, down to 80% of it; return count."""
        if not self._real_dir(self._root):
            return 0
        entries: list[tuple[float, int, str]] = []
        total = 0
        try:
            with os.scandir(self._root) as shards:
                for shard in shards:
                    if not shard.is_dir(follow_symlinks=False):
                        continue
                    with os.scandir(shard.path) as files:
                        for item in files:
                            if item.is_file(follow_symlinks=False):
                                info = item.stat(follow_symlinks=False)
                                entries.append((info.st_mtime, info.st_size, item.path))
                                total += info.st_size
        except OSError:
            return 0
        if total <= limit:
            return 0
        evicted = 0
        for _mtime, size, path in sorted(entries):
            if total <= limit * 0.8:
                break
            with contextlib.suppress(OSError):
                os.unlink(path)
                total -= size
                evicted += 1
        return evicted

    def clean_all(self) -> list[str]:
        """Remove every entry, returning details of every incomplete removal."""
        from .cleanup import clean_cache_buckets

        return clean_cache_buckets((self._root,))


# Deployed files hashed during an install wait here, per command run, until
# the install commits: a refused or failed install never reaches the store.
_STAGED = "deploy-store-staged"


def stage(path: Path, key: str) -> None:
    """Remember a deployed file for :func:`commit_staged` (no-op outside a command run)."""
    staged = invocation_memo(_STAGED)
    if staged is not None:
        staged[str(path)] = key


def discard_staged() -> None:
    """Forget every staged file without storing it."""
    staged = invocation_memo(_STAGED)
    if staged:
        staged.clear()


def commit_staged() -> int:
    """Store every staged file now that the install committed; return how many are present.

    Each file is re-read and must still hash to its staged key, so bytes that
    changed after hashing are never stored under the old digest.
    """
    staged = invocation_memo(_STAGED)
    if not staged:
        return 0
    items = list(staged.items())
    staged.clear()
    store = DeployStore.open_default()
    if store is None:
        return 0
    present = sum(store.put_file(key, Path(path)) for path, key in items)
    if store.written:
        store.enforce_size_cap()
    return present
