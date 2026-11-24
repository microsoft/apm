"""One command's verified file digests, so a package tree is not read twice (#4939).

The checkout verifier reads every copied package file to prove its bytes; the
content hash of the same tree used to read every file again. Inside an
invocation (see :mod:`apm_cli.core.invocation_memo`) the verifier records each
verified file's SHA-256 here, keyed by path and file identity, and the content
hash reuses it instead of re-reading.

- Only bytes verified in this command are recorded, in memory, never persisted.
- APM's own writers invalidate the paths they touch (the write owners call
  :func:`forget` / :func:`forget_tree`), so an edit is seen even when the
  filesystem keeps the same size and a coarse timestamp.
- A lookup re-``lstat``\\ s the path: a different device, inode, size,
  ``mtime_ns`` or ``ctime_ns``, a link or a non-regular file is a miss and the
  caller reads the file. This guards against writers outside APM.
- ``APM_DEBUG_HASH_MEMO=1`` re-reads every hit and fails on any difference.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from pathlib import Path

from apm_cli.core.invocation_memo import invocation_memo

_MEMO = "verified-file-digest"
_lock = threading.Lock()
_Identity = tuple[int, int, int, int, int]


class DigestMemoMismatch(RuntimeError):
    """``APM_DEBUG_HASH_MEMO``: a memoized digest differs from the file's bytes."""


def _identity(info: os.stat_result) -> _Identity:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _key(path: Path | str) -> str:
    return os.path.abspath(path)


def record(path: Path | str, info: os.stat_result, data: bytes) -> None:
    """Remember the SHA-256 of *data*, the verified bytes read from *path* with identity *info*."""
    memo = invocation_memo(_MEMO)
    if memo is None or not stat.S_ISREG(info.st_mode):
        return
    digest = hashlib.sha256(data).digest()
    with _lock:
        memo[_key(path)] = (_identity(info), digest)


def lookup(path: Path | str) -> bytes | None:
    """Return the recorded SHA-256 of *path* if the file is unchanged, else ``None``."""
    memo = invocation_memo(_MEMO)
    if memo is None:
        return None
    key = _key(path)
    with _lock:
        entry = memo.get(key)
    if entry is None:
        return None
    try:
        info = os.lstat(key)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or _identity(info) != entry[0]:
        return None
    if os.environ.get("APM_DEBUG_HASH_MEMO") == "1":
        actual = hashlib.sha256(Path(key).read_bytes()).digest()
        if actual != entry[1]:
            raise DigestMemoMismatch(f"memoized digest of {key} differs from its bytes")
    return entry[1]


def forget(path: Path | str) -> None:
    """Drop *path*: APM is about to write it."""
    memo = invocation_memo(_MEMO)
    if memo is None:
        return
    with _lock:
        memo.pop(_key(path), None)


def forget_tree(root: Path | str) -> None:
    """Drop every path under *root*: APM is about to copy over or remove it."""
    memo = invocation_memo(_MEMO)
    if memo is None:
        return
    prefix = os.path.join(_key(root), "")
    with _lock:
        for key in [k for k in memo if k == prefix[:-1] or k.startswith(prefix)]:
            del memo[key]
