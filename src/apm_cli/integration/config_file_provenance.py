"""Whether APM may delete a native MCP or hook config file it emptied (#4867).

Single owner of that decision. Integrators decide only the content fact (the
file holds nothing but APM's empty skeleton after their cleanup) and hand it
to :func:`delete_emptied_config`, which deletes only when all of these hold:

(a) this machine's record says APM created the file;
(b) after cleanup the file holds only APM's empty skeleton (the content fact);
(c) the record store is local state outside any VCS-tracked path, so a clone
    can never ship a "created by APM" record.

A file the record shows APM did not create (for example an empty skeleton the
user wrote before APM merged into it) is kept. A file with no record, written
before records existed, keeps the content proof (#4852) until APM next creates
it. Records are keyed by the resolved file and stored additively beside the
deployment receipts (machine-local state under ``$APM_HOME``); they are written by
:func:`recorded_config_write` around every APM write of such a file.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import secrets
import stat
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any

_log = logging.getLogger(__name__)

# Layout 2 (#5037): ``records.json`` is an index marker a layout 1 reader rejects,
# and records live beside it in one file per config file directory
# (``d-<id>.json``), so a write reads and saves only its own directory's records.
_VERSION = 2
_V1 = 1
_LAYOUT = "by-directory"
_SHARD_GLOB = "d-*.json"
_VCS_MARKERS = (".git", ".hg", ".svn")
_LOCKFILE = "apm.lock.yaml"

# Set while a replay renders into a private scratch tree: not a deployment.
_replaying: ContextVar[bool] = ContextVar("apm_config_provenance_replay", default=False)


class Origin(Enum):
    """Who created a config file, as this machine recorded it."""

    CREATED = "created"
    PREEXISTING = "preexisting"
    # Written into by an install that predates the records: cannot tell; content proof.
    LEGACY = "legacy"


def config_file_key(path: str | os.PathLike[str]) -> str:
    """The record key of *path*: the resolved absolute file.

    A missing file resolves through its parent; ``..`` and linked directories
    collapse into the one file they name. Spellings the file system may keep
    apart (NFC and NFD, case) stay distinct keys; :meth:`_Store.lookup`
    matches them only when the file system says they are one file.
    """
    # resolve() follows each link before applying a following "..", as the file
    # system does; abspath() would collapse "link/.." lexically to the wrong file.
    return Path(path).resolve(strict=False).as_posix()


def installation_root(project_root: Path, user_scope: bool) -> Path:
    """The root holding an installation's manifest and lockfile (``$APM_HOME`` at user scope)."""
    from apm_cli.core.scope import manifest_root

    return manifest_root(project_root, user_scope=user_scope)


@contextmanager
def replay_render() -> Iterator[None]:
    """Record nothing while a replay (audit, locked render) writes a scratch tree."""
    token = _replaying.set(True)
    try:
        yield
    finally:
        _replaying.reset(token)


def _store_path() -> Path:
    from apm_cli.core.deployment_ledger import materialization_evidence_paths
    from apm_cli.core.scope import InstallScope

    # The deployment owner names the receipts directory; a subdirectory of it (like
    # its MCP evidence) keeps the receipts beside it unchanged. One file, every root.
    receipt, _ = materialization_evidence_paths(Path(os.sep), InstallScope.USER)
    return receipt.parent / "config-files" / "records.json"


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _regular_bytes(path: Path) -> bytes | None:
    """The bytes of *path* when it is a regular file (never opens a FIFO or a link)."""
    return path.read_bytes() if _regular(path) else None


def _canonical_key(key: str) -> bool:
    """Whether *key* is an absolute POSIX-spelled path with no ``.``/``..``/empty parts."""
    if not Path(key).is_absolute():
        return False
    if "\\" in key:
        if os.name == "nt":
            return False  # a Windows key is POSIX-spelled
        rest = key  # on POSIX a backslash is a filename character
    else:
        # The parsed Windows anchor ("C:", "//server/share") is stripped, so a UNC key
        # is checked below its share like any drive-letter key.
        rest = key[len(PureWindowsPath(key).drive) :]
    if not rest.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in rest[1:].split("/"))


class _UnreadableStore(ValueError):
    pass


class _StoreBusy(OSError):
    """Another apm holds APM_HOME's lifecycle lock (or the lock cannot be used)."""


def _shard_id(key: str) -> str:
    """The record file of *key*: its directory, every case and Unicode spelling alike."""
    directory = unicodedata.normalize("NFC", key.rpartition("/")[0]).casefold()
    # A POSIX name that is not UTF-8 is surrogate-escaped; its bytes key the file.
    return hashlib.sha256(directory.encode("utf-8", "surrogateescape")).hexdigest()[:32]


def _read_json(path: Path) -> dict[str, Any]:
    if not _regular(path):
        raise ValueError("not a regular file")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not a mapping")
    return data


@dataclass
class _Store:
    path: Path
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Per recorded file, the digest of the bytes it held before APM's last write
    # (``prior``), so crash recovery can prove a journal copy (#5009). Kept under a
    # top-level ``history`` map: earlier contents, not the file's current record.
    # A write inside an install's deploy journal stores ``{"prior", "journal"}``:
    # the bytes before that install's first write and the journal's token (#5051).
    history: dict[str, str | dict[str, str]] = field(default_factory=dict)
    # The record files read so far, and whether the store is still layout 1.
    loaded: set[str] = field(default_factory=set)
    legacy_layout: bool = False
    indexed: bool = False
    # The index marker's generation; a record file of another generation (left by a
    # store removed by hand, or an interrupted migration) is never read (#5037).
    generation: str = ""
    # The index as this store last read or wrote it: a save re-reads it only if changed.
    marker_stamp: tuple[int, int, int] | None = None
    # The record files the index lists. A listed file that is gone or unreadable
    # fails closed; it never reads as a directory with no records (#5037).
    listed: set[str] | None = None
    # Each record and its history as this store read them before saving: the base of
    # the per record file 3-way merge a save runs under the lock (#5270).
    base: dict[str, tuple[dict[str, Any], Any]] = field(default_factory=dict)
    # Records both this store and another writer changed; the save kept the protective one.
    conflicts: list[str] = field(default_factory=list)
    # Record files this store published that its index write has not listed yet. Kept
    # apart from ``base``: a retry after a failed save re-merges and lists them (#5444).
    unlisted: set[str] = field(default_factory=set)
    saving: bool = False

    def _shard_path(self, shard: str) -> Path:
        return self.path.parent / f"d-{shard}.json"

    def _shard_files(self) -> list[Path]:
        directory = self.path.parent
        return sorted(directory.glob(_SHARD_GLOB)) if directory.is_dir() else []

    @classmethod
    def load(cls) -> _Store:
        """The store; each record file is read when a lookup or a save first needs it."""
        store = cls(_store_path())
        if not os.path.lexists(store.path):
            return store
        try:
            store._stamp_marker()
            data = _read_json(store.path)
            if data.get("version") == _V1 and isinstance(data.get("files"), list):
                store.legacy_layout = True  # read whole; the next save migrates it
                store._absorb(_rows(data))
                return store
            store._adopt(data)
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError) as exc:
            raise _UnreadableStore(f"{store.path}: {exc}") from exc
        return store

    def _stamp_marker(self) -> None:
        self.marker_stamp = _stamp(self.path)

    def _adopt(self, marker: dict[str, Any]) -> None:
        """Read the store through *marker* (a layout 2 index), or raise ``ValueError``."""
        generation = marker.get("generation", "")
        if marker.get("version") != _VERSION or marker.get("layout") != _LAYOUT:
            raise ValueError("unknown version or layout")
        if not isinstance(generation, str):
            raise ValueError("unknown version or layout")
        shards = marker.get("shards")
        if shards is not None and not (
            isinstance(shards, list) and all(isinstance(s, str) for s in shards)
        ):
            raise ValueError("unknown version or layout")
        self.generation, self.indexed = generation, True
        self.listed = set(shards) if shards is not None else None

    def _absorb(self, rows: dict[str, tuple[dict[str, Any], Any]]) -> None:
        for key, value in rows.items():
            if not self.saving:
                # What this writer read: a save applies only its changes from here.
                self.base.setdefault(key, copy.deepcopy(value))
            # A record changed in memory before its file was read stays as changed.
            if key not in self.files:
                self._set(key, value)

    def _value(self, key: str) -> tuple[dict[str, Any], Any] | None:
        """The record of *key* with its history entry, or ``None`` when unrecorded."""
        return (self.files[key], self.history.get(key)) if key in self.files else None

    def _set(self, key: str, value: tuple[dict[str, Any], Any] | None) -> None:
        if value is None:
            self.files.pop(key, None)
            self.history.pop(key, None)
            return
        self.files[key] = value[0]
        if value[1] is None:
            self.history.pop(key, None)
        else:
            self.history[key] = value[1]

    def _read_shard(self, shard: str) -> dict[str, tuple[dict[str, Any], Any]] | None:
        """The records of *shard*'s file as on disk: none when absent, ``None`` when it is
        another generation's leftover. Unreadable, or listed and missing: fails closed."""
        path = self._shard_path(shard)
        listed = self.listed is not None and shard in self.listed
        if not os.path.lexists(path):
            if listed:
                raise _UnreadableStore(f"{path}: a listed record file is missing")
            return {}
        try:
            data = _read_json(path)
            if data.get("version") != _VERSION:
                raise ValueError("unknown version or layout")
            if listed and data.get("generation", "") != self.generation:
                raise ValueError("a listed record file belongs to another generation")
            if data.get("generation", "") != self.generation:
                # Another generation's leftover proves nothing. It is not loaded, so a
                # save never deletes it; a save with records there overwrites it.
                return None
            return _rows(data)
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError) as exc:
            raise _UnreadableStore(f"{path}: {exc}") from exc

    def _load_shard(self, shard: str) -> None:
        if shard in self.loaded:
            return
        rows = self._read_shard(shard)
        if rows is None:
            return
        self._absorb(rows)
        self.loaded.add(shard)

    def _merge_shard(self, shard: str) -> None:
        """Apply only this writer's changes to *shard*'s record file as it is now (#5270).

        Called under the lifecycle lock. Per record: base is what this writer read, ours
        what it holds, theirs what the file holds now. A side that did not change the
        record takes the other's; when both changed it, the record that protects the file
        more is kept and reported, so another writer's record is never silently dropped.
        The file as read becomes the base: a retry after a failed save applies only this
        writer's own changes again, never a record it merely imported (#5444).
        """
        theirs = self._read_shard(shard) or {}
        keys = {key for key in (*self.files, *self.base) if _shard_id(key) == shard}
        for key in sorted(keys | {key for key in theirs if _shard_id(key) == shard}):
            base, ours, other = self.base.get(key), self._value(key), theirs.get(key)
            if ours in (base, other):
                self._set(key, other)
            elif other != base:
                if _protection(other) > _protection(ours):
                    self._set(key, other)
                self.conflicts.append(key)
                _log.warning(
                    "Another apm changed the config file record of %s at the same time; "
                    "kept the record that protects the file",
                    key,
                )
            self._set_base(key, other)
        self.loaded.add(shard)

    def _changed_shards(self) -> set[str]:
        """The record files holding a record this writer changed since it read it."""
        return {
            _shard_id(key)
            for key in {*self.files, *self.base}
            if self._value(key) != self.base.get(key)
        }

    def _rebase(self) -> None:
        """What this store saved is the base of its next save."""
        self.base = {key: copy.deepcopy(self._value(key)) for key in self.files}

    def _set_base(self, key: str, value: tuple[dict[str, Any], Any] | None) -> None:
        if value is None:
            self.base.pop(key, None)
        else:
            self.base[key] = copy.deepcopy(value)

    def _rebase_shard(self, shard: str) -> None:
        """*shard*'s record file is published: what it holds is the base from now on."""
        for key in [key for key in (*self.files, *self.base) if _shard_id(key) == shard]:
            self._set_base(key, self._value(key))

    def _load_all(self) -> None:
        for path in self._shard_files():
            self._load_shard(path.stem.removeprefix("d-"))

    def lookup(self, path: Path) -> tuple[str, dict[str, Any] | None]:
        """The key and record of *path*; another spelling of the same file finds it too."""
        key = config_file_key(path)
        self._load_if_indexed(_shard_id(key))
        if key in self.files:
            return key, self.files[key]
        if os.path.lexists(path):
            # Another spelling (case, Unicode form) shares the directory's record file; a
            # hard link elsewhere is searched for only when the file has another link.
            # Callers never look up a file APM is about to create, so a new config
            # costs no identity checks however many records the machine holds.
            found = self._same_file(path)
            if found is None and self.indexed and _has_other_links(path):
                self._load_all()
                found = self._same_file(path)
            if found is not None:
                return found
        return key, None

    def _load_if_indexed(self, shard: str) -> None:
        if self.indexed:
            self._load_shard(shard)

    def _same_file(self, path: Path) -> tuple[str, dict[str, Any]] | None:
        for other, row in self.files.items():
            try:
                # A key that now resolves elsewhere passes through a link: its
                # record never transfers to the file that link reaches.
                if config_file_key(other) == other and os.path.samefile(other, path):
                    return other, row
            except (OSError, RuntimeError):
                # RuntimeError: a key that became a symlink loop (Python < 3.13);
                # its record stays, it just never aliases another file.
                continue
        return None

    def identity_recorded(self, path: Path) -> bool:
        """Whether a record carries *path*'s device and inode and its current bytes (#5189).

        Consulted only on a lookup miss, and only ever to keep a file. The
        bytes APM last wrote must still be there too, so an inode a deleted
        file freed and another file reused does not match.
        """
        if self.indexed:
            try:
                self._load_all()  # an identity can sit in any directory's record file
            except _UnreadableStore:
                return True  # a record elsewhere cannot be ruled out: keep the file
        try:
            info = os.stat(path)
            current = _regular_bytes(path)
        except OSError:
            return True  # an identity that cannot be read keeps the file
        if current is None:
            return True
        identity, digest = [info.st_dev, info.st_ino], _digest(current)
        return any(
            row.get("identity") == identity and row.get("hash") == digest
            for row in self.files.values()
        )

    def local(self) -> bool:
        """(c): the store is reached through no link below ``$APM_HOME`` and no ancestor
        of it, as spelled or as resolved, is a VCS working tree."""
        from apm_cli.core.scope import get_apm_home
        from apm_cli.utils.path_security import has_symlink_component

        if has_symlink_component(get_apm_home(), self.path):
            return False
        spelled = self.path.parent.absolute()
        directories = {spelled, spelled.resolve()}
        return not any(
            os.path.lexists(parent / marker)
            for directory in directories
            for parent in (directory, *directory.parents)
            for marker in _VCS_MARKERS
        )

    def save(self) -> None:
        """Save under APM_HOME's lifecycle lock, after re-reading the index (#5037).

        One writer at a time across processes: a writer that read the store before
        another migrated it or started its index joins that index, so no record file
        is ever written under a generation the index does not name.
        """
        from filelock import Timeout

        from apm_cli.install.locking import LifecycleBusyError, lifecycle_lock

        try:
            lock = lifecycle_lock()
            lock.acquire()
        except Timeout as exc:
            raise _StoreBusy("another apm is running on this APM_HOME") from exc
        except LifecycleBusyError as exc:
            raise _StoreBusy(f"another apm cannot be ruled out: {exc}") from exc
        try:
            self.saving = True
            self._refresh()
            self._save_locked()
        finally:
            self.saving = False
            lock.release()

    def _refresh(self) -> None:
        stamp = _stamp(self.path)
        if stamp is not None and stamp == self.marker_stamp:
            return  # unchanged since this store read or wrote it (H20)
        if stamp is None:
            self.indexed = self.legacy_layout = False
            return
        self.marker_stamp = stamp
        try:
            data = _read_json(self.path)
            if data.get("version") == _V1 and isinstance(data.get("files"), list):
                self.legacy_layout = True
                return
            reset = self.legacy_layout or not self.indexed
            reset = reset or data.get("generation") != self.generation
            self.legacy_layout = False
            # Same generation too: the record files another writer listed stay listed.
            self._adopt(data)
            if reset:
                self._loaded_generation_reset()
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError) as exc:
            raise _UnreadableStore(f"{self.path}: {exc}") from exc

    def _save_locked(self) -> None:
        # Never pruned by absence: the store is shared, and a file away for now (an
        # unmounted drive) keeps its record. Only a retirement forgets a record.
        if self.legacy_layout:
            self._migrate(self._grouped())
            self._rebase()
            return
        # Only record files this writer changed are saved, each re-read and merged
        # first: one extra read per changed record file, under the lock (#5270).
        changed = self._changed_shards() | self.unlisted
        if not self.indexed and changed:
            self._start_index()
        for shard in sorted(changed):
            self._merge_shard(shard)  # a record file is never saved unread
        grouped = self._grouped()
        written = {shard for shard in changed if grouped.get(shard)}
        removed = changed - written
        # A record file is written before the index lists it, and unlisted before it
        # is removed, so a kill never leaves a listed file missing (#5037).
        for shard in sorted(written):
            self._save_shard(shard, sorted(grouped[shard]))
            self.unlisted.add(shard)
            self._rebase_shard(shard)
        if self.indexed:
            self._save_listing(removed, written)
        self.unlisted.clear()
        for shard in sorted(removed):
            self._save_shard(shard, [])
            self._rebase_shard(shard)
        if self.indexed and not grouped and not self._shard_files():
            self._drop_index()  # like the other receipts: no records, no file

    def _save_listing(self, removed: set[str], written: set[str]) -> None:
        """Rewrite the index only when the record files it lists change."""
        known = self.listed
        if known is None:
            known = {path.stem.removeprefix("d-") for path in self._shard_files()}
        listed = (known - removed) | written
        if listed == self.listed:
            return
        self._write(self.path, self._marker(listed))
        self._stamp_marker()
        self.listed = listed

    def _marker(self, listed: set[str]) -> dict[str, Any]:
        return {
            "version": _VERSION,
            "layout": _LAYOUT,
            "generation": self.generation,
            "shards": sorted(listed),
        }

    def _grouped(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for key in self.files:
            grouped.setdefault(_shard_id(key), []).append(key)
        return grouped

    def _start_index(self) -> None:
        """A new store: the marker first, so a record file is never read without it.

        Created exclusively with a new generation, so record files left by a store
        removed by hand are never read. A writer that finds another's marker joins
        its generation; nothing is deleted, so two first writers keep each other's
        records.
        """
        from apm_cli.integration.write_seam import atomic_write_text, mkdir

        generation = secrets.token_hex(16)
        marker = {"version": _VERSION, "layout": _LAYOUT, "generation": generation, "shards": []}
        mkdir(self.path.parent, mode=0o700, parents=True, exist_ok=True)
        try:
            # Published whole or not at all: a kill never leaves an empty index.
            text = json.dumps(marker, indent=2) + "\n"  # architecture-authority-exempt: evidence
            atomic_write_text(self.path, text, create_only=True, new_file_mode=0o600)
        except FileExistsError:
            marker = _read_json(self.path)  # another writer started the store
        self._stamp_marker()
        self._adopt(marker)
        self._loaded_generation_reset()

    def _loaded_generation_reset(self) -> None:
        # Record files read before the generation was known are read again under it.
        pending = set(self.loaded)
        self.loaded.clear()
        for shard in sorted(pending):
            self._load_shard(shard)

    def _migrate(self, grouped: dict[str, list[str]]) -> None:
        """Layout 1 to 2: every record file first, then the marker in one replace.

        Until the marker is replaced the layout 1 file is whole and is the store, so a
        kill midway leaves the old store and the next save starts again.
        """
        from apm_cli.integration.write_seam import unlink

        self.generation = secrets.token_hex(16)  # leftovers of an earlier attempt: unread
        for shard in sorted(grouped):
            self._save_shard(shard, sorted(grouped[shard]))
        if grouped:
            self._write(self.path, self._marker(set(grouped)))
            self._stamp_marker()
        else:
            unlink(self.path)
        self.legacy_layout, self.indexed = False, bool(grouped)
        self.listed = set(grouped) if grouped else None

    def _save_shard(self, shard: str, keys: list[str]) -> None:
        from apm_cli.integration.write_seam import unlink

        path = self._shard_path(shard)
        if not keys:
            if os.path.lexists(path):
                unlink(path)
            return
        data: dict[str, Any] = {
            "version": _VERSION,
            "generation": self.generation,
            "files": [{"path": key, **self.files[key]} for key in keys],
        }
        history = {key: self.history[key] for key in keys if key in self.history}
        if history:
            data["history"] = history
        self._write(path, data)

    def _drop_index(self) -> None:
        from apm_cli.integration.write_seam import unlink

        if os.path.lexists(self.path):
            unlink(self.path)
        self.indexed = False

    @staticmethod
    def _write(path: Path, data: dict[str, Any]) -> None:
        from apm_cli.integration.write_seam import atomic_write_text, mkdir

        # Machine-local evidence, not a user-editable config.
        text = json.dumps(data, indent=2) + "\n"  # architecture-authority-exempt: evidence
        if _regular(path) and path.read_text(encoding="utf-8") == text:
            return  # a converged install rewrites nothing (I2)
        mkdir(path.parent, mode=0o700, parents=True, exist_ok=True)
        atomic_write_text(path, text, new_file_mode=0o600)


def _rows(data: dict[str, Any]) -> dict[str, tuple[dict[str, Any], Any]]:
    """Each canonical record of a record file with its history entry, or ``ValueError``."""
    if not isinstance(data.get("files"), list):
        raise ValueError("unknown version or layout")
    history = data.get("history")
    rows: dict[str, tuple[dict[str, Any], Any]] = {}
    for row in data["files"]:
        key = row.get("path") if isinstance(row, dict) else None
        # A key not in canonical form (relative, ".", "..", "//") proves nothing. It is
        # checked as written, never re-resolved: a path linked for now keeps its record.
        if isinstance(key, str) and _canonical_key(key):
            prior = history.get(key) if isinstance(history, dict) else None
            # A str, or a journal's {"prior", "journal"}.
            rows[key] = (
                {k: v for k, v in row.items() if k != "path"},
                prior if _history_entry(prior) else None,
            )
    return rows


def _protection(value: tuple[dict[str, Any], Any] | None) -> int:
    """How firmly a record keeps its file from deletion, to settle a conflict (#5270).

    A user's file, or a record APM cannot read, is never deleted; a created record
    deletes only the bytes APM wrote; a legacy record, or none, falls back to the
    content proof.
    """
    if value is None:
        return 0
    origin = _origin(value[0])
    if origin is None or origin is Origin.PREEXISTING:
        return 3
    return 2 if origin is Origin.CREATED else 1


def _has_other_links(path: Path) -> bool:
    try:
        return os.lstat(path).st_nlink > 1
    except OSError:
        return False


def _stamp(path: Path) -> tuple[int, int, int] | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return st.st_ino, st.st_mtime_ns, st.st_size


def _origin(row: dict[str, Any] | None) -> Origin | None:
    try:
        return Origin(row.get("origin")) if row is not None else None
    except ValueError:
        return None


def config_file_origin(path: str | os.PathLike[str]) -> Origin | None:
    """The recorded origin of *path*, or ``None`` when unrecorded or unreadable."""
    try:
        return _origin(_Store.load().lookup(Path(path))[1])
    except _UnreadableStore:
        return None


def _holds_entries(data: bytes) -> bool:
    """Whether a config holds any entry: a non-empty mapping or list at its top level.

    An empty skeleton (``{}``, ``{"mcpServers": {}}``, ``{"version": 1}``) holds none.
    Unparseable content counts as entries, so it is never taken for a skeleton.
    """
    try:
        parsed = json.loads(data.decode("utf-8")) if data.strip() else {}
    except (UnicodeError, ValueError):
        return True
    if not isinstance(parsed, dict):
        return True
    return any(isinstance(value, (dict, list)) and _has_leaf(value) for value in parsed.values())


def _bare_skeleton(data: bytes, skeleton: Mapping[str, Any]) -> bool:
    """Whether a cleaned config holds only its target's *skeleton* (#5067).

    The skeleton is the target's injected top-level defaults (same type and
    value) plus containers with nothing in them. Any other key, a scalar user
    setting included, is the user's content, so the file is not bare.
    Unparseable content is never taken for a skeleton.
    """
    try:
        parsed = json.loads(data.decode("utf-8")) if data.strip() else {}
    except (UnicodeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    return all(
        (key in skeleton and type(skeleton[key]) is type(value) and skeleton[key] == value)
        or (isinstance(value, (dict, list)) and not _has_leaf(value))
        for key, value in parsed.items()
    )


def _has_leaf(value: object) -> bool:
    """Whether a nested value holds anything but empty containers (event scaffolding)."""
    if isinstance(value, dict):
        return any(_has_leaf(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_leaf(item) for item in value)
    return True


def _unrecorded_origin(manifest_root: Path, before: bytes) -> Origin:
    """A file APM writes into that already existed and has no record.

    Only an installation that already has a lockfile can hold such a file an
    earlier APM created, and that file holds APM's entries: it keeps the content
    proof. An empty skeleton holds nothing APM wrote, so it is the user's.
    """
    if (manifest_root / _LOCKFILE).exists() and _holds_entries(before):
        return Origin.LEGACY
    return Origin.PREEXISTING


def _origin_before_write(
    store: _Store, path: Path, before: bytes | None, manifest_root: Path | None
) -> tuple[str, Origin | None]:
    """The key of *path* and its origin once APM writes it (``None``: stays unrecorded)."""
    key, row = store.lookup(path)
    recorded = _origin(row)
    if before is None:
        return key, Origin.CREATED
    if recorded is Origin.CREATED and row.get("hash") != _digest(before):
        # Not the bytes APM last wrote: replaced since (by the user, or recreated
        # after a rollback), so never APM's again. Every APM writer records here.
        return key, Origin.PREEXISTING
    if recorded is not None:
        return key, recorded
    if manifest_root is None:
        # A cleanup rewrite (#5038): an older APM's entries are legacy's content proof.
        return key, Origin.LEGACY if _holds_entries(before) else None
    return key, _unrecorded_origin(manifest_root, before)


def _plan_write(path: Path, before: bytes | None, manifest_root: Path | None) -> Origin | None:
    """The origin APM's write gives *path*, decided before the write (``None``: unrecorded).

    Decided once, before the write: an atomic rewrite can break a hard link the
    lookup followed, so a post-write lookup could miss the user's record. A file
    that will be the user's has that record saved first; when it cannot be, the
    write is refused, since without it the file could later look like a
    pre-record APM file and be deleted.
    """
    if before is None:
        return Origin.CREATED
    try:
        store = _Store.load()
        origin = _origin_before_write(store, path, before, manifest_root)[1]
        key = config_file_key(path)
        if origin is Origin.PREEXISTING and _origin(store.files.get(key)) is not origin:
            store.files[key] = {"origin": origin.value, "hash": _digest(before)}
            store.save()
        return origin
    except _StoreBusy as exc:
        raise OSError(
            f"Could not record that {path} existed before APM wrote to it: {exc}; "
            "refusing to write it. Wait for the other apm to finish, then retry."
        ) from exc
    except (OSError, ValueError) as exc:
        raise OSError(
            f"Could not record that {path} existed before APM wrote to it ({exc}); "
            "refusing to write it. Fix or remove the config file records under APM_HOME "
            "and retry."
        ) from exc


def _note(
    path: Path,
    before: bytes | None,
    after: bytes,
    origin: Origin | None,
    skeleton: Mapping[str, Any],
) -> None:
    """Record *origin* and the bytes APM wrote under *path*'s own key."""
    if before == after or origin is None:
        return  # nothing written (a converged install records nothing, I2), or unrecorded
    store = _Store.load()
    # Its record file, with its history, is read before the record changes (#5037).
    recorded = store.lookup(path)[1]
    if origin is Origin.LEGACY and recorded is None and _bare_skeleton(after, skeleton):
        return  # a new legacy record never claims a bare skeleton (#5038, #5067)
    key = config_file_key(path)
    row: dict[str, Any] = {"origin": origin.value, "hash": _digest(after)}
    try:
        # The file's identity, so another path to it still finds this record (#5189).
        info = os.stat(path)
        row["identity"] = [info.st_dev, info.st_ino]
    except OSError:
        pass
    store.files[key] = row
    from apm_cli.integration.write_seam import active_journal

    journal = active_journal()
    entry = store.history.get(key)
    if journal is not None and isinstance(entry, dict) and entry.get("journal") == journal:
        pass  # a later write of the same install: its journal keeps the first bytes
    elif before is None:
        store.history.pop(key, None)  # created: no earlier bytes to restore
    elif journal is not None:
        store.history[key] = {"prior": _digest(before), "journal": journal}
    else:
        store.history[key] = _digest(before)
    store.save()


def _history_entry(entry: Any) -> bool:
    if isinstance(entry, dict):
        return set(entry) == {"prior", "journal"} and all(
            isinstance(value, str) for value in entry.values()
        )
    return isinstance(entry, str)


def _prior_digest(entry: Any, journal: str | None) -> str | None:
    """The prior digest *entry* vouches for when replaying *journal*'s writes."""
    if isinstance(entry, dict):
        return entry["prior"] if journal is not None and entry["journal"] == journal else None
    return entry


@contextmanager
def recorded_config_write(
    path: str | os.PathLike[str],
    *,
    manifest_root: str | os.PathLike[str] | None = None,
    skeleton: Mapping[str, Any] | None = None,
) -> Iterator[None]:
    """Record who created *path* around one APM write of it.

    *manifest_root* (the project, or ``$APM_HOME`` at user scope) classifies a
    file that existed without a record; a cleanup rewrite passes none and
    records such a file as legacy only when it held entries before and is not
    left as the target's bare *skeleton* (its injected top-level defaults,
    plus empty containers) after (#5038, #5067). A link or other non-regular
    path is never recorded. Recording failures never fail the write.
    """
    path = Path(path)
    if _replaying.get():
        # A replay render into private scratch is not a deployment: nothing to record.
        yield
        return
    linked = os.path.lexists(path) and not _regular(path)
    try:
        before = None if linked else _regular_bytes(path)
    except OSError:
        linked = True
    origin = None
    if not linked:
        origin = _plan_write(path, before, Path(manifest_root) if manifest_root else None)
    yield
    if linked:
        return
    try:
        after = _regular_bytes(path)
        if after is not None:
            _note(path, before, after, origin, skeleton or {})
    except (OSError, ValueError):
        _log.debug("Could not record the creation of %s", path, exc_info=True)


def recorded_install_write(
    path: str | os.PathLike[str], project_root: Path, user_scope: bool
) -> AbstractContextManager[None]:
    """:func:`recorded_config_write` for an install write at *project_root*'s scope."""
    return recorded_config_write(path, manifest_root=installation_root(project_root, user_scope))


def vouches_for_current_bytes(
    path: str | os.PathLike[str], prior: bytes, *, journal: str | None = None
) -> bool:
    """Whether this machine's record vouches for undoing APM's last write to *path*.

    APM last wrote exactly the bytes *path* holds, and *prior* (the copy crash
    recovery would put back, #5009) is exactly what the file held before that
    write, or before the first write of the install whose deploy *journal* is
    replayed (#5051). A link or other non-regular file, a missing, unreadable or
    VCS-tracked record, a record without its ``prior`` digest, or other bytes
    never vouch.
    """
    path = Path(path)
    if not _regular(path):
        return False
    try:
        store = _Store.load()
        current = _regular_bytes(path)
        key, row = store.lookup(path)
    except (OSError, _UnreadableStore):
        return False
    return (
        current is not None
        and _origin(row) is not None
        and row.get("hash") == _digest(current)
        and _prior_digest(store.history.get(key), journal) == _digest(prior)
        and store.local()
    )


def forget_config_file(path: str | os.PathLike[str]) -> None:
    """Forget the record of a file APM itself removed (for example its hook sidecar)."""
    try:
        store = _Store.load()
        key, row = store.lookup(Path(path))
        if row is not None:
            store.files.pop(key, None)
            store.save()
    except (OSError, _UnreadableStore):
        _log.debug("Could not forget the record of %s", path, exc_info=True)


def delete_emptied_config(
    path: str | os.PathLike[str],
    *,
    root: str | os.PathLike[str] | None,
    empty: bool,
    require_created: bool = False,
) -> bool:
    """Delete *path* if APM may, after a cleanup left it *empty*; return whether it did.

    *empty* is the caller's content fact: the file now holds only APM's empty
    skeleton. *root* (project or home) bounds the file: without it, or when
    any component below it is a link, nothing is deleted. A non-regular file
    (link, FIFO) is never deleted or followed. With *require_created* (hook
    files, #4962) the content proof of an unrecorded or legacy file is not
    enough: only a record that APM created the file deletes it.
    """
    from apm_cli.integration.write_seam import unlink
    from apm_cli.utils.path_security import has_symlink_component

    path = Path(path)
    if not empty or root is None or not _regular(path):
        return False
    if has_symlink_component(Path(root), path):
        return False
    try:
        store = _Store.load()
        key, row = store.lookup(path)
    except _UnreadableStore:
        _log.debug("Unreadable config file records; keeping %s", path, exc_info=True)
        return False
    origin = _origin(row)
    if (row is not None and origin is None) or (row is None and store.identity_recorded(path)):
        # A record APM cannot read proves nothing either way; one under another path
        # (a second mount) means the unrecorded fallback never applies (#5189).
        return False
    if origin is Origin.PREEXISTING or (require_created and origin is not Origin.CREATED):
        return False
    if origin is Origin.CREATED:
        try:
            current = _regular_bytes(path)
        except OSError:
            return False
        # (b) the bytes APM last wrote, so a replaced file is never taken; (c) local.
        if current is None or row.get("hash") != _digest(current) or not store.local():
            return False
    unlink(path)
    if row is not None:
        store.files.pop(key, None)
        try:
            store.save()
        except (OSError, _UnreadableStore):
            _log.debug("Could not forget the record of %s", path, exc_info=True)
    return True
