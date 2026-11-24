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
:data:`MAX_DEPLOY_STORE_BYTES`, down to 80% of it. Package records under
``records/`` count toward that bound and are evicted the same way; evicting
a record or a blob it names only turns the next replay into a miss. ``apm cache clean``
empties it; an empty store only means the next command does the full work.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

from ..core.invocation_memo import invocation_memo
from ..utils.content_hash import canonical_digest
from .paths import cache_writes_allowed, get_cache_root

DEPLOY_BUCKET = "deploy_v1"
MAX_DEPLOY_STORE_BYTES = 256 * 1024 * 1024
MAX_DEPLOY_ENTRY_BYTES = 4 * 1024 * 1024
RECORDS_DIR = "records"
_RECORD_VERSION = 1

_KEY_RE = re.compile(r"[0-9a-f]{64}")
_RECORD_NAME_RE = re.compile(r"[0-9a-f]{64}\.json")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)
# A FIFO planted at an entry path must be a miss, not a blocked open.
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# Windows junctions and other reparse points pass lstat as directories.
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def _oversized(entry: Path) -> bool:
    try:
        info = os.lstat(entry)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size > MAX_DEPLOY_ENTRY_BYTES


def _read_bounded(entry: Path) -> bytes | None:
    """Read a regular, non-linked entry of at most the entry bound; ``None`` otherwise.

    Opens without following links and without blocking (a FIFO is refused by
    the descriptor type check), and reads at most one byte past the bound so
    an entry grown out of band cannot exhaust memory.
    """
    try:
        fd = os.open(entry, os.O_RDONLY | _NOFOLLOW | _BINARY | _NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DEPLOY_ENTRY_BYTES:
            return None
        if os.path.islink(entry) or _is_reparse(os.lstat(entry)):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_DEPLOY_ENTRY_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data if len(data) <= MAX_DEPLOY_ENTRY_BYTES else None


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
        data = _read_bounded(entry)
        if data is None or canonical_digest(data) != key:
            if (data is not None or _oversized(entry)) and cache_writes_allowed():
                with contextlib.suppress(OSError):
                    entry.unlink()
            return None
        if raw_sha256 is not None and hashlib.sha256(data).hexdigest() != raw_sha256:
            return None
        if cache_writes_allowed():
            with contextlib.suppress(OSError):
                os.utime(entry, None)
        return data

    def _holds(self, entry: Path, key: str) -> bool:
        """Whether the existing entry is a regular file whose bytes still hash to *key*."""
        data = _read_bounded(entry)
        return data is not None and canonical_digest(data) == key

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
        # A same-size regular entry is verified before it is kept; a damaged
        # or different-size one is atomically replaced.
        return stat.S_ISREG(existing.st_mode) and existing.st_size == size

    def put(self, key: str, data: bytes) -> bool:
        """Store *data* under *key*; return whether a usable entry is present.

        *data* must hash to *key*; a mismatch is refused and nothing is written.
        """
        entry = self._entry(key)
        if entry is None or len(data) > MAX_DEPLOY_ENTRY_BYTES or key != canonical_digest(data):
            return False
        try:
            ready = self._prepare(entry, len(data))
            if ready is None:
                return False
            if ready and self._holds(entry, key):
                return True
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
            if ready is None:
                return False
            if ready and self._holds(entry, key):
                return True
            data = path.read_bytes()
        except OSError:
            return False
        return self.put(key, data)

    def _record_path(self, key: str) -> Path | None:
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            return None
        return self._root / RECORDS_DIR / key[:2] / f"{key}.json"

    def put_record(self, key: str, record: dict) -> bool:
        """Write a package-bound render record (atomic, owner-only); return success."""
        path = self._record_path(key)
        if path is None or _valid_record(record) is None:
            return False
        data = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
        try:
            for directory in (self._root, path.parent.parent, path.parent):
                if not os.path.lexists(directory):
                    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not self._real_dir(directory):
                    return False
            if _read_bounded(path) == data:
                # Unchanged: refresh recency without a write that would trigger a cap scan.
                with contextlib.suppress(OSError):
                    os.utime(path, None)
                return True
            fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
        except OSError:
            return False
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp, path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            return False
        self.written += 1
        return True

    def package_record(
        self,
        repo_url: str,
        resolved_commit: str,
        content_hash: str,
        targets: object,
        version: str,
        scope: object,
    ) -> dict | None:
        """Return the validated render record bound to this exact package and setup, or ``None``.

        The record names, per deployed path, the canonical digest (the
        lockfile value) and the raw sha256 of the exact bytes this machine
        deployed for that verified package. A linked, non-regular, oversized
        or malformed record is ``None``.
        """
        path = self._record_path(
            package_record_key(repo_url, resolved_commit, content_hash, targets, version, scope)
        )
        if path is None or not all(
            self._real_dir(d) for d in (self._root, path.parent.parent, path.parent)
        ):
            return None
        raw = _read_bounded(path)
        if raw is None:
            return None
        try:
            record = _valid_record(json.loads(raw.decode("ascii")))
        except (UnicodeDecodeError, ValueError):
            return None
        if record is not None and cache_writes_allowed():
            with contextlib.suppress(OSError):
                os.utime(path, None)
        return record

    def _scan_shards(self, base: Path, name_re: re.Pattern[str]) -> list[tuple[float, int, str]]:
        """``(mtime, size, path)`` of every key-named regular file in its own real shard."""
        found: list[tuple[float, int, str]] = []
        with os.scandir(base) as shards:
            for shard in shards:
                # Windows junctions pass is_dir(follow_symlinks=False); refuse them.
                if len(shard.name) != 2 or not self._real_dir(Path(shard.path)):
                    continue
                with os.scandir(shard.path) as files:
                    for item in files:
                        if not (
                            name_re.fullmatch(item.name)
                            and item.name.startswith(shard.name)
                            and item.is_file(follow_symlinks=False)
                        ):
                            continue
                        info = item.stat(follow_symlinks=False)
                        found.append((info.st_mtime, info.st_size, item.path))
        return found

    def _scan(self) -> tuple[list[tuple[float, int, str]], list[tuple[float, int, str]]]:
        """``(blobs, records)``: every key-named blob and package record in its own real shard."""
        if not self._real_dir(self._root):
            return [], []
        records_root = self._root / RECORDS_DIR
        try:
            blobs = self._scan_shards(self._root, _KEY_RE)
            records = (
                self._scan_shards(records_root, _RECORD_NAME_RE)
                if self._real_dir(records_root)
                else []
            )
        except OSError:
            return [], []
        return blobs, records

    def get_stats(self) -> dict[str, int]:
        """Deployed-file entry count and total size of the store (blobs and records)."""
        blobs, records = self._scan()
        total = sum(e[1] for e in blobs) + sum(e[1] for e in records)
        return {"entry_count": len(blobs), "total_size_bytes": total}

    def enforce_size_cap(self, limit: int = MAX_DEPLOY_STORE_BYTES) -> int:
        """Evict least recently used blobs and records above *limit*, down to 80% of it.

        Returns the eviction count. A record names blobs by digest and every
        reader verifies on read, so evicting either one is only a later miss.
        """
        blobs, records = self._scan()
        entries = blobs + records
        total = sum(size for _mtime, size, _path in entries)
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


@functools.cache
def process_umask() -> int:
    """This process's umask, read once.

    Linux reports it in ``/proc``; elsewhere the only way is a process-wide
    ``os.umask`` swap, so it happens once, primed by the install transaction
    (and the fast entry) before any worker thread exists.
    """
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/status", encoding="ascii") as status:
                for line in status:
                    if line.startswith("Umask:"):
                        return int(line.split()[1], 8)
        except (OSError, ValueError):
            pass
    mask = os.umask(0o077)  # a create in this instant gets the more private mode
    os.umask(mask)
    return mask


def plain_file_mode() -> int:
    """The permission bits a plain new file gets in this process (``0o666 & ~umask``)."""
    return 0o666 & ~process_umask()


def package_record_key(
    repo_url: str,
    resolved_commit: str,
    content_hash: str,
    targets: object,
    version: str,
    scope: object,
) -> str:
    """The record key both the writer and the reader derive for one package and setup."""
    names = sorted(str(getattr(target, "name", target)) for target in (targets or ()))
    parts = (
        str(version),
        str(repo_url),
        str(resolved_commit),
        str(content_hash),
        ",".join(names),
        str(getattr(scope, "value", scope)),
        # A replay writes plain files: the record holds only under the same mode policy.
        oct(plain_file_mode()),
    )
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


# A record's body is replayed to the terminal: printable ASCII lines only, bounded.
_MAX_BODY_LINES = 2000
_MAX_BODY_LINE_CHARS = 1000
_PRINTABLE_LINE = re.compile(r"[\x20-\x7e]*")


def _printable_body(body: object) -> bool:
    return (
        isinstance(body, list)
        and len(body) <= _MAX_BODY_LINES
        and all(
            isinstance(line, str)
            and len(line) <= _MAX_BODY_LINE_CHARS
            and _PRINTABLE_LINE.fullmatch(line) is not None
            for line in body
        )
    )


def _valid_record(obj: object) -> dict | None:
    """Return a normalized record, or ``None`` for any malformed shape."""
    if not isinstance(obj, dict) or obj.get("v") != _RECORD_VERSION:
        return None
    files, ascii_only, body = obj.get("files"), obj.get("ascii_only"), obj.get("body")
    if not isinstance(files, dict) or not files or not isinstance(ascii_only, bool):
        return None
    for rel, pair in files.items():
        if not (isinstance(rel, str) and rel and isinstance(pair, list) and len(pair) == 2):
            return None
        if not all(isinstance(x, str) and _KEY_RE.fullmatch(x) for x in pair):
            return None
    if body is not None and not _printable_body(body):
        return None
    record = {
        "v": _RECORD_VERSION,
        "files": {rel: list(pair) for rel, pair in files.items()},
        "ascii_only": ascii_only,
        "body": list(body) if body is not None else None,
    }
    # The effective-policy fingerprint the install gate used (drift replay binds to it).
    policy = obj.get("policy")
    if policy is not None:
        if not (isinstance(policy, str) and _KEY_RE.fullmatch(policy)):
            return None
        record["policy"] = policy
    return record


def record_matches(record: dict | None, lock_hashes: dict[str, str]) -> bool:
    """Whether *record* binds exactly the lockfile's ``deployed_file_hashes`` for a package.

    The lockfile authorizes nothing by itself: a hand-edited row pointing a
    path at any other digest (even one cached on this machine) does not match.
    """
    if not record or not lock_hashes:
        return False
    recorded = {rel: f"sha256:{pair[0]}" for rel, pair in record["files"].items()}
    return recorded == dict(lock_hashes)


# Deployed files hashed during an install wait here, per command run, until
# the install commits: a refused or failed install never reaches the store.
_STAGED = "deploy-store-staged"
_STAGED_RECORDS = "deploy-store-records"


def stage(path: Path, key: str, raw_sha256: str | None = None, ascii_only: bool = False) -> None:
    """Remember a deployed file for :func:`commit_staged` (no-op outside a command run)."""
    staged = invocation_memo(_STAGED)
    if staged is not None:
        staged[str(path)] = (key, raw_sha256, ascii_only)


def staged_file(path: Path) -> tuple[str, str | None, bool] | None:
    """Return ``(digest, raw sha256, ascii)`` staged for *path* this command run, if any."""
    staged = invocation_memo(_STAGED)
    return staged.get(str(path)) if staged else None


def stage_record(key: str, record: dict) -> None:
    """Remember a package-bound render record for :func:`commit_staged`."""
    staged = invocation_memo(_STAGED_RECORDS)
    if staged is not None and _valid_record(record) is not None:
        staged[key] = record


def discard_staged() -> None:
    """Forget every staged file and record without storing them."""
    for name in (_STAGED, _STAGED_RECORDS):
        staged = invocation_memo(name)
        if staged:
            staged.clear()


StagedEvidence = tuple[list[tuple[str, tuple[str, str | None, bool]]], list[tuple[str, dict]]]


def take_staged() -> StagedEvidence:
    """Remove and return the files and records staged so far this command run.

    An install transaction takes them when its pipeline returns, so a later
    reset of the invocation memos (a lifecycle script) cannot lose them.
    """
    staged = invocation_memo(_STAGED)
    records = invocation_memo(_STAGED_RECORDS)
    taken = (list(staged.items()) if staged else [], list(records.items()) if records else [])
    discard_staged()
    return taken


def commit_staged(taken: StagedEvidence | None = None) -> int:
    """Store every staged file now that the install committed; return how many are present.

    *taken* is evidence an install transaction took earlier; anything still
    staged is stored with it. Each file is re-read and must still hash to its
    staged key, so bytes that changed after hashing are never stored under the
    old digest. A record is written only when every blob it names is present.
    """
    items, pending = take_staged()
    if taken is not None:
        items, pending = taken[0] + items, taken[1] + pending
    if not items:
        return 0
    store = DeployStore.open_default()
    if store is None:
        return 0
    present = {key for path, (key, _raw, _ascii) in items if store.put_file(key, Path(path))}
    for key, record in pending:
        if all(pair[0] in present for pair in record["files"].values()):
            store.put_record(key, record)
    if store.written:
        store.enforce_size_cap()
    return len(present)
