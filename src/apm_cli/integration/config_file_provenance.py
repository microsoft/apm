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

import hashlib
import json
import logging
import os
import stat
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any

_log = logging.getLogger(__name__)

_VERSION = 1
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


@dataclass
class _Store:
    path: Path
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Per recorded file, the digest of the bytes it held before APM's last write
    # (``prior``), so crash recovery can prove a journal copy (#5009). Kept under a
    # top-level ``history`` map: earlier contents, not the file's current record.
    history: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> _Store:
        path = _store_path()
        store = cls(path)
        if not os.path.lexists(path):
            return store
        try:
            if not _regular(path):
                raise ValueError("not a regular file")
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != _VERSION or not isinstance(data.get("files"), list):
                raise ValueError("unknown version or layout")
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError) as exc:
            raise _UnreadableStore(f"{path}: {exc}") from exc
        for row in data["files"]:
            key = row.get("path") if isinstance(row, dict) else None
            # A key not in canonical form (relative, ".", "..", "//") proves nothing. It is
            # checked as written, never re-resolved: a path linked for now keeps its record.
            if isinstance(key, str) and _canonical_key(key):
                store.files[key] = {k: v for k, v in row.items() if k != "path"}
        history = data.get("history")
        if isinstance(history, dict):
            store.history = {
                key: prior
                for key, prior in history.items()
                if key in store.files and isinstance(prior, str)
            }
        return store

    def lookup(self, path: Path) -> tuple[str, dict[str, Any] | None]:
        """The key and record of *path*; another spelling of the same file finds it too."""
        key = config_file_key(path)
        if key in self.files:
            return key, self.files[key]
        if os.path.lexists(path):
            # Another spelling (case, Unicode form) or hard link of this existing file.
            # Callers never look up a file APM is about to create, so a new config
            # costs no identity checks however many records the machine holds.
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
        return key, None

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
        from apm_cli.integration.write_seam import atomic_write_text, mkdir, unlink

        # Never pruned by absence: the store is shared, and a file away for now (an
        # unmounted drive) keeps its record. Only a retirement forgets a record.
        rows = [{"path": key, **self.files[key]} for key in sorted(self.files)]
        if not rows:
            if os.path.lexists(self.path):
                unlink(self.path)  # like the other receipts: no records, no file
            return
        # Machine-local evidence, not a user-editable config.
        data: dict[str, Any] = {"version": _VERSION, "files": rows}
        history = {key: self.history[key] for key in sorted(self.history) if key in self.files}
        if history:
            data["history"] = history
        text = json.dumps(data, indent=2) + "\n"  # architecture-authority-exempt: evidence
        if _regular(self.path) and self.path.read_text(encoding="utf-8") == text:
            return  # a converged install rewrites nothing (I2)
        mkdir(self.path.parent, mode=0o700, parents=True, exist_ok=True)
        atomic_write_text(self.path, text, new_file_mode=0o600)


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
    except (OSError, ValueError) as exc:
        raise OSError(
            f"Could not record that {path} existed before APM wrote to it ({exc}); "
            "refusing to write it. Fix or remove the config file records under APM_HOME "
            "and retry."
        ) from exc


def _note(path: Path, before: bytes | None, after: bytes, origin: Origin | None) -> None:
    """Record *origin* and the bytes APM wrote under *path*'s own key."""
    if before == after or origin is None:
        return  # nothing written (a converged install records nothing, I2), or unrecorded
    store = _Store.load()
    if origin is Origin.LEGACY and store.lookup(path)[1] is None and not _holds_entries(after):
        return  # a new legacy record never claims a bare skeleton (#5038)
    key = config_file_key(path)
    store.files[key] = {"origin": origin.value, "hash": _digest(after)}
    if before is None:
        store.history.pop(key, None)  # created: no earlier bytes to restore
    else:
        store.history[key] = _digest(before)
    store.save()


@contextmanager
def recorded_config_write(
    path: str | os.PathLike[str], *, manifest_root: str | os.PathLike[str] | None = None
) -> Iterator[None]:
    """Record who created *path* around one APM write of it.

    *manifest_root* (the project, or ``$APM_HOME`` at user scope) classifies a
    file that existed without a record; a cleanup rewrite passes none and
    records such a file as legacy only when it holds entries before and after
    (#5038). A link or other non-regular path is
    never recorded. Recording failures never fail the write.
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
            _note(path, before, after, origin)
    except (OSError, ValueError):
        _log.debug("Could not record the creation of %s", path, exc_info=True)


def recorded_install_write(
    path: str | os.PathLike[str], project_root: Path, user_scope: bool
) -> AbstractContextManager[None]:
    """:func:`recorded_config_write` for an install write at *project_root*'s scope."""
    return recorded_config_write(path, manifest_root=installation_root(project_root, user_scope))


def vouches_for_current_bytes(path: str | os.PathLike[str], prior: bytes) -> bool:
    """Whether this machine's record vouches for undoing APM's last write to *path*.

    APM last wrote exactly the bytes *path* holds, and *prior* (the copy crash
    recovery would put back, #5009) is exactly what the file held before that
    write. A link or other non-regular file, a missing, unreadable or
    VCS-tracked record, a record without its ``prior`` digest, or other bytes
    never vouch.
    """
    path = Path(path)
    if not _regular(path):
        return False
    try:
        store = _Store.load()
        current = _regular_bytes(path)
    except (OSError, _UnreadableStore):
        return False
    key, row = store.lookup(path)
    return (
        current is not None
        and _origin(row) is not None
        and row.get("hash") == _digest(current)
        and store.history.get(key) == _digest(prior)
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
    path: str | os.PathLike[str], *, root: str | os.PathLike[str] | None, empty: bool
) -> bool:
    """Delete *path* if APM may, after a cleanup left it *empty*; return whether it did.

    *empty* is the caller's content fact: the file now holds only APM's empty
    skeleton. *root* (project or home) bounds the file: without it, or when
    any component below it is a link, nothing is deleted. A non-regular file
    (link, FIFO) is never deleted or followed.
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
    except _UnreadableStore:
        _log.debug("Unreadable config file records; keeping %s", path, exc_info=True)
        return False
    key, row = store.lookup(path)
    origin = _origin(row)
    if row is not None and origin is None:
        return False  # a record APM cannot read proves nothing either way
    if origin is Origin.PREEXISTING:
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
        except OSError:
            _log.debug("Could not forget the record of %s", path, exc_info=True)
    return True
