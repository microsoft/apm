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
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import tempfile
from pathlib import Path

from ..utils.content_hash import canonical_digest
from .paths import get_cache_root

DEPLOY_BUCKET = "deploy_v1"

_KEY_RE = re.compile(r"[0-9a-f]{64}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)


class DeployStore:
    """Read and write deployed-file bytes under ``<cache>/deploy_v1``."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @classmethod
    def open_default(cls) -> DeployStore | None:
        """Return the store in the machine cache, or ``None`` when caching is off."""
        if os.environ.get("APM_NO_CACHE"):
            return None
        try:
            return cls(get_cache_root() / DEPLOY_BUCKET)
        except (OSError, ValueError):
            return None

    def _entry(self, key: str) -> Path | None:
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            return None
        return self._root / key[:2] / key

    @staticmethod
    def _real_dir(path: Path) -> bool:
        try:
            return stat.S_ISDIR(os.lstat(path).st_mode)
        except OSError:
            return False

    def get(self, key: str) -> bytes | None:
        """Return the verified bytes stored under *key*, or ``None``.

        ``None`` covers a malformed key, a missing entry, a symbolic link
        anywhere in the store path, a non-regular file and an entry whose
        bytes do not hash to *key* (which is also evicted).
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
        return data

    def put(self, key: str, data: bytes) -> bool:
        """Store *data* under *key*; return whether a usable entry is present.

        *data* must hash to *key*; a mismatch is refused and nothing is written.
        """
        entry = self._entry(key)
        if entry is None:
            return False
        try:
            for directory in (self._root, entry.parent):
                if not os.path.lexists(directory):
                    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not self._real_dir(directory):
                    return False
            try:
                existing = os.lstat(entry)
            except FileNotFoundError:
                existing = None
            # Reads verify; a same-size regular entry is left alone so a warm
            # install costs one lstat, while a damaged one is rewritten.
            if existing and stat.S_ISREG(existing.st_mode) and existing.st_size == len(data):
                return True
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
        return True
