"""Sealed git cache checkouts and their materialization into ``apm_modules``.

A checkout shard in ``checkouts_v1/<shard>/<sha>/<variant>/`` is immutable
content for one commit. Its *seal* (``.git/apm-seal.json``) records the
SHA-256, size and executable bit of every file, every directory, and the
verdict of the tracked-symlink check for the whole tree.

Every read re-hashes the shard against its seal before any byte reaches
``apm_modules`` (verify-before-reuse), so a corrupted or edited entry is
evicted and rebuilt rather than reused. Materialization then avoids a
byte copy where it can:

* ``clone`` (default on macOS): ``clonefile(2)`` of each top-level entry.
  Copy-on-write, so an edit in ``apm_modules`` never reaches the cache.
* ``link`` (opt-in, ``APM_MATERIALIZE=link``): one hard link per file.
  The link shares the cache inode, so an in-place edit DOES change the
  cache entry; verify-before-reuse then evicts it on the next read, and
  any other project linked to it sees the edit. Only opt in when
  ``apm_modules`` is never edited by hand.
* ``copy`` (default elsewhere, and the fallback of the other two): the
  per-file reflink-then-copy of :func:`robust_copytree`.

Removing ``apm_modules`` or a cache entry only unlinks names, so neither
can delete content the other still uses.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..utils.path_security import (
    PathTraversalError,
    ensure_path_within_resolved,
    validate_path_segments,
)

_log = logging.getLogger(__name__)

SEAL_NAME = "apm-seal.json"
_SEAL_VERSION = 1
_GIT_DIR = ".git"
_CHUNK = 1 << 20
MATERIALIZE_ENV = "APM_MATERIALIZE"
_STRATEGIES = ("clone", "link", "copy")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class TreeIntegrityError(RuntimeError):
    """A sealed cache entry no longer matches its seal."""


@dataclass(frozen=True)
class FileRecord:
    path: str
    sha256: str
    size: int
    executable: bool


@dataclass(frozen=True)
class TreeSeal:
    """Content of one cache entry (``.git`` excluded), plus its link verdict.

    ``link_failures`` is ``None`` while the tracked-symlink verdict for the
    whole tree is unknown, ``()`` when every tracked link resolved inside
    the commit, and the failing link paths otherwise.
    """

    files: tuple[FileRecord, ...]
    dirs: tuple[str, ...]
    link_failures: tuple[str, ...] | None = None

    def top_level(self) -> tuple[str, ...]:
        names = {record.path.split("/", 1)[0] for record in self.files}
        names.update(path.split("/", 1)[0] for path in self.dirs)
        return tuple(sorted(names))

    def same_content(self, other: TreeSeal) -> bool:
        return self.files == other.files and self.dirs == other.dirs


@dataclass
class MaterializeStats:
    strategy: str
    clones: int = 0
    links: int = 0
    copies: int = 0


def seal_path(entry: Path) -> Path:
    return entry / _GIT_DIR / SEAL_NAME


def _hash_file(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def scan_tree(entry: Path) -> TreeSeal:
    """Hash the content of *entry* (``.git`` excluded).

    Raises :class:`TreeIntegrityError` for a symlink or special file: Git
    writes every tracked link in a cache checkout as its text, so a real
    link there was not written by Git and could point outside the entry.
    """
    files: list[FileRecord] = []
    dirs: list[str] = []
    pending = [("", os.fspath(entry))]
    while pending:
        prefix, directory = pending.pop()
        with os.scandir(directory) as entries:
            for item in entries:
                if not prefix and item.name == _GIT_DIR:
                    continue
                rel = f"{prefix}{item.name}"
                if item.is_symlink():
                    raise TreeIntegrityError(f"cache entry {entry} holds a symlink: {rel}")
                if item.is_dir(follow_symlinks=False):
                    dirs.append(rel)
                    pending.append((f"{rel}/", item.path))
                elif item.is_file(follow_symlinks=False):
                    digest, size = _hash_file(item.path)
                    mode = item.stat(follow_symlinks=False).st_mode
                    files.append(FileRecord(rel, digest, size, bool(mode & stat.S_IXUSR)))
                else:
                    raise TreeIntegrityError(f"cache entry {entry} holds a special file: {rel}")
    files.sort(key=lambda record: record.path)
    dirs.sort()
    return TreeSeal(tuple(files), tuple(dirs))


def _validated_rel(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise ValueError(f"invalid seal path {value!r}")
    validate_path_segments(value, context="seal path", reject_empty=True)
    return value


def _file_record(raw: object) -> FileRecord:
    """Parse one seal file record strictly: no coercion of a malformed field."""
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError(f"invalid seal file record {raw!r}")
    path, digest, size, executable = raw
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise ValueError(f"invalid seal digest {digest!r}")
    if type(size) is not int or size < 0 or type(executable) is not bool:
        raise ValueError(f"invalid seal size or mode in {raw!r}")
    return FileRecord(_validated_rel(path), digest, size, executable)


def _refuse_linked_seal(entry: Path) -> None:
    """The entry, its ``.git`` and the seal must be real paths, not links."""
    for path in (entry, entry / _GIT_DIR, seal_path(entry)):
        if path.is_symlink():
            raise TreeIntegrityError(f"cache entry {entry} has a linked seal path: {path}")


def read_seal(entry: Path) -> TreeSeal | None:
    """Return the parsed seal of *entry*, or ``None`` when it has none.

    Raises :class:`TreeIntegrityError` for a seal that exists but cannot be
    trusted (unreadable, malformed, or naming a path outside the entry).
    """
    path = seal_path(entry)
    _refuse_linked_seal(entry)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise TreeIntegrityError(f"unreadable seal {path}: {exc}") from exc
    try:
        data = json.loads(text)
        if data.get("version") != _SEAL_VERSION:
            raise ValueError(f"unsupported seal version {data.get('version')!r}")
        if not isinstance(data["files"], list) or not isinstance(data["dirs"], list):
            raise ValueError("files and dirs must be lists")
        files = tuple(_file_record(raw) for raw in data["files"])
        dirs = tuple(_validated_rel(path) for path in data["dirs"])
        failures = data.get("link_failures")
        if failures is not None and not isinstance(failures, list):
            raise ValueError("link_failures must be a list or null")
        link_failures = (
            None if failures is None else tuple(_validated_rel(path) for path in failures)
        )
    except (AttributeError, KeyError, TypeError, ValueError, PathTraversalError) as exc:
        raise TreeIntegrityError(f"malformed seal {path}: {exc}") from exc
    return TreeSeal(files, dirs, link_failures)


def write_seal(entry: Path, seal: TreeSeal) -> None:
    from ..utils.atomic_io import atomic_write_text

    _refuse_linked_seal(entry)
    payload = {
        "version": _SEAL_VERSION,
        "files": [[r.path, r.sha256, r.size, r.executable] for r in seal.files],
        "dirs": list(seal.dirs),
        "link_failures": None if seal.link_failures is None else list(seal.link_failures),
    }
    atomic_write_text(
        seal_path(entry),
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        new_file_mode=0o600,
    )


def verified_seal(entry: Path, *, read_only: bool = False) -> TreeSeal | None:
    """Return the seal of *entry* after re-hashing its content against it.

    The caller holds the entry's shard lock. An unsealed entry is sealed
    from its current content (entries were only ever copied out before
    sealing existed, so that content is the content Git checked out).
    ``None`` means the entry is not a Git checkout or, for a *read_only*
    reader, is not sealed yet; the caller keeps the legacy copy path.

    Raises :class:`TreeIntegrityError` when the content no longer matches.
    """
    _refuse_linked_seal(entry)
    if not (entry / _GIT_DIR).is_dir():
        return None
    current = scan_tree(entry)
    sealed = read_seal(entry)
    if sealed is None:
        if read_only:
            return None
        write_seal(entry, current)
        return current
    if not sealed.same_content(current):
        raise TreeIntegrityError(f"cache entry {entry} does not match its seal")
    return sealed


def record_link_verdict(entry: Path, seal: TreeSeal, failures: tuple[str, ...]) -> TreeSeal:
    """Persist the tracked-symlink verdict of a verified seal."""
    updated = TreeSeal(seal.files, seal.dirs, tuple(sorted(failures)))
    write_seal(entry, updated)
    return updated


def _strategy() -> str:
    requested = os.environ.get(MATERIALIZE_ENV, "").strip().lower()
    if requested in _STRATEGIES:
        return requested
    from ..utils.reflink import reflink_supported

    return "clone" if sys.platform == "darwin" and reflink_supported() else "copy"


def _copy_item(source: Path, destination: Path, stats: MaterializeStats) -> None:
    from ..utils.file_ops import robust_copy2, robust_copytree

    if source.is_dir():
        robust_copytree(source, destination)
    else:
        robust_copy2(source, destination)
    stats.copies += 1


def _clone_item(source: Path, destination: Path, stats: MaterializeStats) -> None:
    from ..utils.reflink import clone_file

    # clonefile(2) clones a whole directory; other platforms clone per file
    # inside robust_copytree.
    if (sys.platform == "darwin" or not source.is_dir()) and clone_file(source, destination):
        if sys.platform != "darwin":
            # FICLONE clones data only; clonefile(2) keeps the metadata.
            shutil.copystat(source, destination)
        stats.clones += 1
        return
    if destination.exists():
        from ..utils.file_ops import robust_rmtree

        robust_rmtree(destination) if destination.is_dir() else destination.unlink()
    _copy_item(source, destination, stats)


_LINK_FALLBACK_ERRNOS = frozenset(
    {errno.EXDEV, errno.EPERM, errno.EACCES, errno.EMLINK, errno.ENOTSUP, errno.EOPNOTSUPP}
)


def _link_tree(
    entry: Path, seal: TreeSeal, dest: Path, resolved_dest: Path, stats: MaterializeStats
) -> None:
    from ..utils.file_ops import robust_copy2

    for rel in seal.dirs:
        target = dest / rel
        ensure_path_within_resolved(target, resolved_dest)
        target.mkdir(exist_ok=True)
    for record in seal.files:
        target = dest / record.path
        ensure_path_within_resolved(target, resolved_dest)
        source = os.fspath(entry / record.path)
        try:
            os.link(source, target)
            stats.links += 1
            continue
        except OSError as exc:
            if exc.errno not in _LINK_FALLBACK_ERRNOS and not sys.platform.startswith("win"):
                raise
        robust_copy2(source, target)
        stats.copies += 1


def copy_checkout(entry: Path, dest: Path) -> MaterializeStats:
    """Legacy materialization: copy every top-level entry except ``.git``."""
    stats = MaterializeStats("copy")
    items = [item for item in sorted(entry.iterdir()) if item.name != _GIT_DIR]
    _checked_destination(dest, tuple(item.name for item in items))
    for item in items:
        _copy_item(item, dest / item.name, stats)
    return stats


def _evict(entry: Path) -> None:
    """Remove a refused entry; a linked entry loses only the link, never its target."""
    if entry.is_symlink():
        entry.unlink()
        return
    from ..utils.file_ops import robust_rmtree

    robust_rmtree(entry, ignore_errors=True)


def materialize_cached_checkout(
    entry: Path,
    dest: Path,
    *,
    validate_links: Callable[[], None],
    read_only: bool = False,
) -> MaterializeStats:
    """Verify the cache entry *entry* by hash, then materialize it into *dest*.

    *validate_links* is the tracked-symlink check for the whole tree (it
    spawns ``git ls-tree``). It runs unless the verified seal records that
    every tracked link resolved; a passing run is recorded in the seal, so
    each entry pays it once. A mismatch evicts the entry (unless
    *read_only*) and raises :class:`TreeIntegrityError`; nothing reaches
    *dest* from a mismatching entry.
    """
    if read_only:
        seal = verified_seal(entry, read_only=True)
        if seal is None or seal.link_failures != ():
            validate_links()
        return copy_checkout(entry, dest) if seal is None else materialize_tree(entry, seal, dest)

    from .locking import shard_lock

    with shard_lock(entry):
        try:
            seal = verified_seal(entry)
        except TreeIntegrityError:
            _log.warning("[!] Evicting cache entry that no longer matches its seal: %s", entry)
            _evict(entry)
            raise
        if seal is None:
            validate_links()
            return copy_checkout(entry, dest)
        if seal.link_failures != ():
            validate_links()
            seal = record_link_verdict(entry, seal, ())
        return materialize_tree(entry, seal, dest)


def _checked_destination(dest: Path, names: tuple[str, ...]) -> Path:
    """Refuse a destination that is a link or not empty, before the first write."""
    if dest.is_symlink() or not dest.is_dir() or any(dest.iterdir()):
        raise PathTraversalError(f"refusing to materialize into {dest}: not an empty directory")
    resolved_dest = dest.resolve()
    for name in names:
        ensure_path_within_resolved(dest / name, resolved_dest)
    return resolved_dest


def materialize_tree(entry: Path, seal: TreeSeal, dest: Path) -> MaterializeStats:
    """Place the sealed content of *entry* into the empty directory *dest*.

    The caller verified *seal* against *entry* under the shard lock and
    still holds it. Every destination path is checked to stay in *dest*.
    """
    stats = MaterializeStats(_strategy())
    resolved_dest = _checked_destination(dest, seal.top_level())
    if stats.strategy == "link":
        _link_tree(entry, seal, dest, resolved_dest, stats)
        return stats
    for name in seal.top_level():
        source = entry / name
        target = dest / name
        if stats.strategy == "clone":
            _clone_item(source, target, stats)
        else:
            _copy_item(source, target, stats)
    return stats
