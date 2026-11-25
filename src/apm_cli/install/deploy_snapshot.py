"""Lazy pre-write journal of the paths one install touches (#4282).

:class:`~apm_cli.install.transaction.InstallTransaction` listens at the integrator write seam
(``integration/write_seam.py``). The first time a path is about to be created, replaced or removed,
:meth:`DeployJournal.record` keeps its prior bytes (or notes that it was absent) in a store under
the APM home ``install-snapshots`` directory. Paths the install never touches are not copied.
:meth:`DeployJournal.restore` puts every recorded path back, newest first, and removes the
directories the install created; :meth:`DeployJournal.discard` deletes the store once the install
committed.

A journal bound to a deploy root also writes each unit, as it records it, to ``journal.jsonl`` in
its store. A store a killed process left behind (``deploy-*``) is replayed by
:func:`replay_abandoned_journals` when the next install for the same deploy root starts under the
lifecycle lock (#4863). The journal is data on disk and is trusted no further than the deployment
ledger: replay first checks every unit (well-formed, inside the deploy root, copy bytes that re-hash
to the recorded hash and to the ledger's hash for the path), applies nothing when any check fails,
and keeps the store for recovery then or when a path cannot be restored. A store kept after a failed
restore is marked retained (a ``RETAINED`` file inside it) and then renamed ``retained-*``; replay
never touches a marked store, so the copies survive even a failed rename.
:func:`remove_empty_directories` also lets the transaction remove target roots a failed install
created and left empty (#4159).
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from apm_cli.install import journal_modes as _modes
from apm_cli.install.deploy_replay_state import _holds_apm_state
from apm_cli.install.deploy_state import ABSENT as _ABSENT
from apm_cli.install.deploy_state import entry_state as _entry
from apm_cli.install.deploy_state import path_state as _state
from apm_cli.install.deploy_state import tree_hashes as _tree_hashes
from apm_cli.install.deploy_state import valid_state as _valid_state
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.file_ops import robust_rmtree
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
)
from apm_cli.utils.realpath_win import release_held

_Kind = Literal["absent", "file", "dir", "symlink"]
_STORE_PREFIX = "deploy-"
_RETAINED_PREFIX = "retained-"
_RETAINED_MARKER = "RETAINED"
_MANIFEST = "journal.jsonl"
_FORMAT = "apm-deploy-journal"
_VERSION = 2  # 2 records modes (#5111); version 1 readers refuse it
_KINDS = ("absent", "file", "dir", "symlink")
_COPY_NAME = re.compile(r"[0-9]+")
# APM-home metadata the journal may restore: deployment receipts, files only.
_RECEIPT_KINDS = frozenset(_KINDS[:2])


@dataclass(frozen=True)
class _Unit:
    """One recorded path and how to put it back."""

    path: Path
    kind: _Kind
    copy: Path | None
    link: str | None
    absent_ancestors: tuple[Path, ...]


def _absent_ancestors(path: Path) -> tuple[Path, ...]:
    """Return the missing parents of *path*, deepest first."""
    missing = []
    parent = path.parent
    while parent != parent.parent and not os.path.lexists(parent):
        missing.append(parent)
        parent = parent.parent
    return tuple(missing)


def remove_empty_directories(directories: Iterable[Path]) -> None:
    """Remove each directory that is empty, deepest first; keep anything else."""
    for directory in sorted(set(directories), key=lambda p: len(p.parts), reverse=True):
        with contextlib.suppress(OSError):
            if directory.is_dir() and not directory.is_symlink():
                release_held(directory)
                directory.rmdir()


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        robust_rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


class DeployJournal:
    """Prior state of every path one install touched, recorded on first touch."""

    def __init__(
        self, store_parent: Path, *, root: Path | None = None, workspace: Path | None = None
    ) -> None:
        self.store_parent = Path(os.path.abspath(store_parent))
        # With a deploy root every unit is also written to the store's
        # manifest, so the next install can replay a journal a kill abandoned.
        self.root = Path(os.path.abspath(root)) if root is not None else None
        self.contain = self.root  # rollback-only containment; a fast apply sets its project
        # The metadata workspace (lockfile directory) whose ledger vouches for it.
        self.workspace = Path(os.path.abspath(workspace or root or store_parent))
        self.store: Path | None = None
        # Names this journal in the config file records' first-touch digests (#5051).
        self.token = secrets.token_hex(16)
        self._units: list[_Unit] = []
        self._recorded: dict[Path, int] = {}
        self._lock = threading.Lock()

    @property
    def units(self) -> tuple[_Unit, ...]:
        """Return the recorded units, oldest first."""
        return tuple(self._units)

    def _covered(self, path: Path) -> bool:
        """Whether *path* or one of its parents is already recorded."""
        return path in self._recorded or any(p in self._recorded for p in path.parents)

    def _store(self) -> Path:
        if self.store is None:
            _modes.private_dir(self.store_parent)
            self.store = Path(tempfile.mkdtemp(prefix=_STORE_PREFIX, dir=self.store_parent))
            if self.root is not None:
                header = {
                    "format": _FORMAT,
                    "version": _VERSION,
                    "root": str(self.root),
                    "workspace": str(self.workspace),
                    "token": self.token,
                }
                self._append_line(header)
        return self.store

    def _append_line(self, line: dict[str, Any]) -> None:
        if self.store is None:
            raise RuntimeError("no store to write the journal manifest to")
        _modes.append_held(self, self.store / _MANIFEST, json.dumps(line, sort_keys=True) + "\n")

    def _relative(self, path: Path) -> tuple[str, str]:
        """``(base, path)``: *path* as POSIX under APM home or the deploy root.

        A path under neither is kept absolute, and replay refuses it.
        """
        root = self.root if self.root is not None else self.store_parent.parent
        for base, directory in (("apm_home", self.store_parent.parent), ("root", root)):
            for candidate, anchor in (
                (path, directory),
                (Path(os.path.realpath(path)), Path(os.path.realpath(directory))),
            ):
                with contextlib.suppress(ValueError):
                    return base, candidate.relative_to(anchor).as_posix()
        return "root", str(path)

    def _persist(self, unit: _Unit, expected: str | None = None, size: int | None = None) -> None:
        """Write *unit* to the manifest before the install changes its path."""
        if self.root is None:
            return
        self._store()
        base, rel = self._relative(unit.path)
        line: dict[str, Any] = {
            "base": base,
            "path": rel,
            "kind": unit.kind,
            "copy": unit.copy.name if unit.copy is not None else None,
            "link": unit.link,
            # Only directories strictly inside the base; never the base itself.
            "ancestors": [
                a for b, a in map(self._relative, unit.absent_ancestors) if b == base and a != "."
            ],
        }
        # The unit line also opens the first write: what it will leave there.
        line["expected"] = expected
        if size is not None:
            line["size"] = size
        if unit.copy is not None:
            line["mode"] = _modes.recorded_mode(unit.copy, unit.kind)  # never the copy's later bits
        if unit.kind == "file" and unit.copy is not None:
            line["hash"] = compute_file_hash(unit.copy)
        if unit.kind == "dir" and unit.copy is not None:
            line["files"] = _tree_hashes(unit.copy)
        self._append_line(line)

    def record(
        self,
        path: Path,
        *,
        contents: bool = True,
        expected: str | None = None,
        size: int | None = None,
    ) -> None:
        """Keep the prior state of *path* the first time it is touched.

        With ``contents=False`` (a directory about to be created) only an absent path is recorded:
        creating an existing directory changes nothing. *expected* is the content hash the write
        will leave (``"absent"``, ``"dir"``, or ``None`` when the writer cannot know it); a write
        killed before its seal is restored only over those bytes. *size* is their byte length when
        the writer knows it (#5411).
        """
        path = Path(os.path.abspath(path))
        with self._lock:
            if path.is_relative_to(self.store_parent):
                return
            if self._covered(path):
                # A later write to a journaled path: APM is about to change it.
                self._event("pending", path, expected, size)
                return
            exists = os.path.lexists(path)
            if exists and not contents:
                return
            ancestors = _absent_ancestors(path)
            if not exists:
                unit = _Unit(path, "absent", None, None, ancestors)
            elif path.is_symlink():
                unit = _Unit(path, "symlink", None, os.readlink(path), ancestors)
            else:
                copy = self._store() / str(len(self._units))
                if path.is_dir():
                    shutil.copytree(path, copy, symlinks=True)
                    unit = _Unit(path, "dir", copy, None, ancestors)
                else:
                    shutil.copy2(path, copy)
                    unit = _Unit(path, "file", copy, None, ancestors)
            self._persist(unit, expected, size)
            self._recorded[path] = len(self._units)
            self._units.append(unit)

    def _unit_of(self, path: Path) -> tuple[int, str] | None:
        """``(unit index, path below the unit)`` of the unit that journals *path*."""
        for candidate in (path, *path.parents):
            index = self._recorded.get(candidate)
            if index is not None:
                rel = path.relative_to(candidate).as_posix()
                return index, "" if rel == "." else rel
        return None

    def _event(
        self, event: str, path: Path, expected: str | None = None, size: int | None = None
    ) -> None:
        """Append a write event for *path*: ``pending`` before, ``sealed`` after."""
        if self.root is None or self.store is None:
            return
        found = self._unit_of(path)
        if found is None:
            return
        index, sub = found
        line: dict[str, Any] = {event: index, "sub": sub}
        if event == "sealed":
            line["state"] = _state(path) if not sub else _entry(path)
        else:
            line["expected"] = expected
            if size is not None:
                line["size"] = size
        self._append_line(line)

    def after_write(self, path: Path) -> None:
        """Seal what APM just wrote at *path* (the post-write seal, #4863).

        Replay changes a path only while it holds its prior state, nothing,
        these bytes, or a write APM began and never sealed.
        """
        with self._lock:
            self._event("sealed", Path(os.path.abspath(path)))

    def mark_commit(self, lockfile_identity: str) -> None:
        """Name the lockfile this install is about to commit, or committed.

        A journal killed after that lockfile reached disk recorded a committed
        install: replay settles it instead of rolling the deployment back.
        """
        with self._lock:
            if self.root is not None and self.store is not None:
                self._append_line({"commit": lockfile_identity})

    def restore(self) -> list[tuple[Path, str]]:
        """Put every recorded path back; return the paths that could not be restored."""
        issues: list[tuple[Path, str]] = []
        with self._lock:
            units = list(reversed(self._units))
        for unit in units:
            if self._crosses_link(unit.path):
                issues.append((unit.path, "kept: a parent directory is a symbolic link"))
                continue
            try:
                self._restore_unit(unit)
            except OSError as exc:
                issues.append((unit.path, f"could not restore: {exc}"))
        remove_empty_directories(
            a for unit in units for a in unit.absent_ancestors if not self._crosses_link(a)
        )
        if issues and self.store is not None:
            issues.append((self._retain(), "pre-install copies retained here for recovery"))
        else:
            self.discard()
        return issues

    def _crosses_link(self, path: Path) -> bool:
        """Whether a directory between *path* and its deploy root (or APM home) is a link."""
        for base in (self.contain, self.store_parent.parent):
            if base is not None and path.parent.is_relative_to(base):
                return has_symlink_component(base, path.parent)
        return False

    def _retain(self) -> Path:
        """Rename the store so no later install reaps the copies a user still needs."""
        if self.store is None:
            raise RuntimeError("no store to retain")
        # The marker keeps the copies from the reaper even if the rename fails.
        (self.store / _RETAINED_MARKER).write_text(
            "Pre-install copies kept after a failed restore.\n", encoding="ascii"
        )
        retained = self.store.with_name(_RETAINED_PREFIX + self.store.name[len(_STORE_PREFIX) :])
        release_held(self.store)
        try:
            self.store.rename(retained)
        except OSError:
            return self.store
        self.store = retained
        return retained

    @staticmethod
    def _restore_unit(unit: _Unit) -> None:
        path, copy = unit.path, unit.copy
        if unit.kind == "file" and copy is not None and path.is_file() and not path.is_symlink():
            if path.read_bytes() == copy.read_bytes():
                return
        if unit.kind == "symlink" and path.is_symlink() and os.readlink(path) == unit.link:
            return
        if os.path.lexists(path):
            _remove(path)
        if unit.kind == "absent":
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if unit.link is not None:
            release_held(path)  # an APM-created link: no proof may survive at or below it
            os.symlink(unit.link, path)
        elif unit.kind == "dir" and copy is not None:
            shutil.copytree(copy, path, symlinks=True)
        elif copy is not None:
            shutil.copy2(copy, path)

    def discard(self) -> None:
        """Delete the stored copies and the empty store parent."""
        _modes.close_held(self)
        store, self.store = self.store, None
        if store is None:
            return
        release_held(store)
        shutil.rmtree(store, ignore_errors=True)
        with contextlib.suppress(OSError):
            release_held(store.parent)
            store.parent.rmdir()


def lockfile_identity(text: str) -> str:
    """Identify a lockfile by content: other line endings keep the on-disk identity."""
    import yaml

    from apm_cli.deps.lockfile_container import parse_lockfile_text
    from apm_cli.utils.yaml_io import load_yaml_str

    try:
        try:  # Owner's memoized parse; refused or non-JSON-native text keeps the old identity.
            canonical = json.dumps(parse_lockfile_text(text), sort_keys=True)
        except Exception:
            canonical = json.dumps(load_yaml_str(text), sort_keys=True, default=str)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        canonical = text.replace("\r\n", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReplayOutcome:
    """What replay restored, the paths it kept because they changed, and where."""

    restored: int = 0
    kept: tuple[tuple[str, str], ...] = ()
    retained: tuple[Path, ...] = ()


class LedgerUnavailable(Exception):
    """The deployment ledger exists but cannot be read; replay proves nothing."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(reason)
        self.issue = (str(path), reason)


class JournalReplayError(Exception):
    """An abandoned journal was kept: replay could not prove or finish it (#4863)."""

    def __init__(
        self,
        store: Path,
        issues: list[tuple[str, str]],
        *,
        applied: bool,
        home: Path | None = None,
    ) -> None:
        super().__init__(f"abandoned deploy journal kept at {store}")
        self.store = store
        self.issues = issues
        self.applied = applied
        # Set when only APM_HOME's group write refused: the store is left to replay (#5385).
        self.home = home


@dataclass(frozen=True)
class _Replay:
    """One validated unit of an abandoned journal, ready to apply."""

    rel: str
    path: Path
    kind: _Kind
    copy: Path | None
    expected: Mapping[str, str]
    ancestors: tuple[Path, ...]
    # Each written path below the unit ("" for the unit itself) -> its last
    # write event: ("pending", expected hash) when begun, never sealed, or the sealed state.
    events: Mapping[str, Any] = field(default_factory=dict)
    # The state replay puts back: the journaled copy's, or nothing.
    prior: dict[str, Any] = field(default_factory=lambda: dict(_ABSENT))
    # The verified recorded mode replay gives the restored path (#5111).
    mode: int | dict[str, int] | None = None


def _read_manifest(
    store: Path, *, allow_group_home: bool = False
) -> tuple[dict[str, Any], list[Any]]:
    """Parse ``journal.jsonl``; raise ``ValueError`` when it is not a journal."""
    # The store is this user's alone (#5134).
    _modes.require_private_store(store, allow_group_home=allow_group_home)
    try:
        text = _modes.read_private_journal(store / _MANIFEST)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"journal manifest unreadable: {exc}") from exc
    try:
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    except RecursionError:
        raise ValueError("journal manifest is nested too deeply") from None
    if not lines or not isinstance(lines[0], dict):
        raise ValueError("journal manifest has no header")
    header = lines[0]
    if header.get("format") != _FORMAT or header.get("version") != _VERSION:
        raise ValueError("journal manifest has an unknown format or version")
    if not isinstance(header.get("root"), str) or not isinstance(header.get("workspace"), str):
        raise ValueError("journal manifest names no deploy root or workspace")
    return header, lines[1:]


def _parse(lines: list[Any]) -> tuple[list[Any], dict[int, dict[str, Any]], str | None]:
    """Split manifest lines into units, each unit's last write events and the commit mark."""
    units = [line for line in lines if not (isinstance(line, dict) and line.keys() & _MARKS)]
    events: dict[int, dict[str, Any]] = {}
    commit: str | None = None
    for line in lines:
        if not (isinstance(line, dict) and line.keys() & _MARKS):
            continue
        if "commit" in line:
            if not isinstance(line["commit"], str):
                raise ValueError("journal manifest holds a malformed commit mark")
            commit = line["commit"]
            continue
        pending = "pending" in line
        index, sub = line.get("pending" if pending else "sealed"), line.get("sub")
        state = line.get("state")
        if pending:
            # A write APM began keeps the last seal: a kill before it starts leaves APM's bytes.
            last = (
                events.get(index, {}).get(sub)
                if type(index) is int and isinstance(sub, str)
                else None
            )
            state = (
                "pending",
                _pending(line)[1],
                last[2] if isinstance(last, tuple) else last,
                _pending(line)[3],
            )
        if (
            type(index) is not int
            or not 0 <= index < len(units)
            or not isinstance(sub, str)
            or (sub and PurePosixPath(sub).as_posix() != sub)
            or ".." in PurePosixPath(sub).parts
            or not (pending or _valid_state(state, entry=bool(sub)))
        ):
            raise ValueError("journal manifest holds a malformed write event")
        events.setdefault(index, {})[sub] = state
    return units, events, commit


def _pending(line: dict[str, Any]) -> tuple[str, str | None, Any, int | None]:
    """A write APM began: ``("pending", <expected hash>, <last seal>, <expected size>)``."""
    expected = line.get("expected")
    if expected is not None and not isinstance(expected, str):
        raise ValueError("journal manifest holds a malformed expected hash")
    size = line.get("size")
    if size is not None and (type(size) is not int or size < 0):
        raise ValueError("journal manifest holds a malformed expected size")
    return ("pending", expected, None, size)


_MARKS = frozenset({"pending", "sealed", "commit"})


def _inside(rel: Any, root: Path) -> Path:
    """The path canonical *rel* names under *root*; raise ``PathTraversalError`` otherwise."""
    if not isinstance(rel, str) or not rel:
        raise PathTraversalError(f"journal path {rel!r} is not a relative path")
    posix, windows = PurePosixPath(rel), PureWindowsPath(rel)
    if "\\" in rel or posix.is_absolute() or windows.drive or windows.root or ".." in posix.parts:
        raise PathTraversalError(f"journal path {rel!r} escapes the deploy root")
    # One spelling per path: no "./", "//", trailing "/" or "." naming the base.
    if not posix.parts or posix.as_posix() != rel:
        raise PathTraversalError(f"journal path {rel!r} is not canonical")
    path = root.joinpath(*posix.parts)
    ensure_path_within(path, root)
    return path


def _under(rel: Any, directory: str) -> str:
    """*rel* (``<directory>[/<rest>]``) as ``<rest>`` (``""`` for the directory itself)."""
    parts = PurePosixPath(rel).parts if isinstance(rel, str) and "\\" not in rel else ()
    prefix = PurePosixPath(directory).parts
    if len(parts) < len(prefix) or parts[: len(prefix)] != prefix:
        raise PathTraversalError(f"journal path {rel!r} is outside {directory}")
    if PurePosixPath(*parts).as_posix() != rel:
        raise PathTraversalError(f"journal path {rel!r} is not canonical")
    return PurePosixPath(*parts[len(prefix) :]).as_posix() if len(parts) > len(prefix) else ""


@dataclass(frozen=True)
class _Bases:
    """Where replay may write: the deploy root, and the receipts directory in APM home."""

    root: Path
    receipts: Path
    journal: str | None = None  # the abandoned journal's token (#5051)


class _Unproven(Exception):
    """One journal unit replay cannot prove; ``(path, reason)`` names it."""

    def __init__(self, path: Any, reason: str) -> None:
        super().__init__(reason)
        self.issue = (str(path), reason)


def _validate_unit(
    line: Any,
    store: Path,
    bases: _Bases,
    ledger: Mapping[str, str | None],
    events: Mapping[str, Any] | None = None,
) -> _Replay:
    """Check one manifest line against the store and the ledger, without applying it.

    A path under the deploy root (``base: root``) is restored only when the deployment ledger
    vouches for it, a directory only when the ledger records exactly its files. APM's own metadata
    under APM home (``base: apm_home``) is limited to deployment receipts, which have journal-only
    integrity: confined to the receipts directory and restored only when they re-hash as journaled.
    A shared config file the ledger never claims (hook settings, the hook sidecar, an MCP config) is
    restored when the config file records vouch for it (:func:`_shared_config_vouched`). Raise
    ``_Unproven`` otherwise.
    """
    if not isinstance(line, dict) or line.get("kind") not in _KINDS:
        raise _Unproven(store, "journal manifest holds a malformed unit")
    if line.get("base") not in ("root", "apm_home"):
        raise _Unproven(store, "journal manifest holds a malformed unit")
    metadata = line["base"] == "apm_home"
    rel = line.get("path")
    try:
        path, ancestors = _unit_paths(line, store, bases, metadata=metadata)
    except PathTraversalError:
        raise _Unproven(rel, "journal path escapes the deploy root") from None
    kind = line["kind"]
    if metadata:
        _check_receipt(rel, path, kind)
    if kind == "symlink":
        raise _Unproven(rel, "the deployment ledger cannot vouch for a symbolic link")
    if kind == "absent":
        if not metadata and (rel in ledger or any(k.startswith(f"{rel}/") for k in ledger)):
            raise _Unproven(rel, "the deployment ledger records a file the journal says was absent")
        return _Replay(rel, path, kind, None, {}, ancestors)
    copy, expected = _journaled_copy(line, store)
    if not metadata:
        try:
            _check_vouched(rel, kind, expected, ledger)
        except _Unproven:
            if not _shared_config_vouched(rel, path, kind, copy, events, ledger, bases.journal):
                raise
    mode = _modes.verified_mode(line.get("mode"), kind, copy)
    if mode is None:
        raise _Unproven(rel, "the journal does not record a verifiable mode")
    prior = _modes.with_recorded_mode(_state(copy), mode)
    return _Replay(rel, path, kind, copy, expected, ancestors, prior=prior, mode=mode)


def _check_vouched(
    rel: str, kind: str, expected: Mapping[str, str], ledger: Mapping[str, str | None]
) -> None:
    """The ledger records exactly the journaled bytes (all of a directory's files)."""
    keys = {name: f"{rel}/{name}" if name else rel for name in expected}
    if not all(digest and ledger.get(keys[name]) == digest for name, digest in expected.items()):
        raise _Unproven(rel, "the deployment ledger does not record these bytes")
    if kind == "dir" and {k for k in ledger if k.startswith(f"{rel}/")} != set(keys.values()):
        raise _Unproven(rel, "the deployment ledger records other files under this directory")


def _shared_config_vouched(
    rel: str,
    path: Path,
    kind: str,
    copy: Path,
    events: Mapping[str, Any] | None,
    ledger: Mapping[str, str | None],
    journal: str | None = None,
) -> bool:
    """A file the ledger never claims, restorable from the config file records (#5009).

    It still holds its journaled prior bytes (nothing to undo), or exactly the
    bytes the interrupted install sealed and the records say APM wrote. Other
    content after a seal is a later write, which :func:`_decide` keeps and
    names. A sealed write the records do not vouch for (missing or tampered
    baseline), an unsealed or missing file, or a link never qualifies.
    """
    from apm_cli.integration.config_file_provenance import vouches_for_current_bytes

    if kind != "file" or rel in ledger or path.is_symlink():
        return False
    current = _state(path)
    if "dir" in current:
        return False  # a directory swapped in is never replaced by a config file
    if current == _state(copy):
        return True
    sealed = (events or {}).get("")
    if not isinstance(sealed, dict) or current == _ABSENT:
        return False  # a write never sealed, or a missing file: nothing proves it
    if current == sealed:
        try:
            prior = _read_no_follow(copy)
        except OSError:
            return False
        return vouches_for_current_bytes(path, prior, journal=journal)
    return True  # a later write: _decide keeps and names it, never replaces it


def _check_receipt(rel: str, path: Path, kind: str) -> None:
    """APM-home units are deployment receipts: never links, a file only over a file."""
    if kind not in _RECEIPT_KINDS or path.is_symlink():
        raise _Unproven(rel, "a deployment receipt is a regular file")
    if kind == "file" and os.path.lexists(path) and not path.is_file():
        raise _Unproven(rel, "a deployment receipt is a regular file")


def _journaled_copy(line: dict[str, Any], store: Path) -> tuple[Path, dict[str, str]]:
    """The unit's stored copy and the hashes it must re-hash to (``""`` keys a file)."""
    rel, kind, name = line.get("path"), line["kind"], line.get("copy")
    copy = store / name if isinstance(name, str) and _COPY_NAME.fullmatch(name) else None
    if copy is None or copy.is_symlink():
        raise _Unproven(rel, "journal copy is missing or malformed")
    if kind == "file":
        recorded = line.get("hash")
        if not copy.is_file() or not isinstance(recorded, str):
            raise _Unproven(rel, "journal copy is missing or malformed")
        expected, actual = {"": recorded}, {"": compute_file_hash(copy)}
    else:
        files = line.get("files")
        if not copy.is_dir() or not isinstance(files, dict):
            raise _Unproven(rel, "journal copy is missing or malformed")
        expected, actual = files, _tree_hashes(copy)
    if actual != expected:
        raise _Unproven(rel, "journal copy does not match its recorded hash")
    return copy, expected


def _unit_paths(
    line: dict[str, Any], store: Path, bases: _Bases, *, metadata: bool
) -> tuple[Path, tuple[Path, ...]]:
    """The unit's path and absent ancestors, each confined to its base directory.

    APM-home units are confined to the deployment receipts directory, both as written
    (canonical, no ``..``) and as resolved: no component from APM home down may be a link.
    """
    snapshots, rels = store.parent, [line.get("path")]
    ancestors = line.get("ancestors") or []
    if not isinstance(ancestors, list):
        raise PathTraversalError("journal ancestors are not a list")
    rels += ancestors
    if metadata:
        home, prefix = snapshots.parent, bases.receipts.relative_to(snapshots.parent).as_posix()
        rests = [_under(rel, prefix) for rel in rels]
        paths = [_inside(rest, bases.receipts) if rest else bases.receipts for rest in rests]
        if any(has_symlink_component(home, path) for path in paths):
            raise PathTraversalError("a deployment receipt path crosses a symbolic link")
    else:
        paths = [_inside(rel, bases.root) for rel in rels]
        # A link inside the root is another spelling of a path the ledger keys.
        if any(has_symlink_component(bases.root, path) for path in paths):
            raise PathTraversalError("a journal path crosses a symbolic link")
    for path in paths:
        if path == snapshots or snapshots in path.parents:
            raise PathTraversalError(f"journal path {path} names the journal stores")
    if any(ancestor not in paths[0].parents for ancestor in paths[1:]):
        raise PathTraversalError("a journal ancestor is not an ancestor of its path")
    return paths[0], tuple(paths[1:])


def _read_no_follow(path: Path) -> bytes:
    """The bytes of regular file *path*, refusing a symbolic link."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"{path} is not a regular file")
        return handle.read()


def _apply(unit: _Replay) -> None:
    """Put *unit*'s prior state back; the bytes written must re-hash as recorded."""
    path = unit.path
    if unit.kind == "absent":
        if os.path.lexists(path):
            _remove(path)
        return
    if unit.copy is None:
        raise OSError("no journal copy to restore from")
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.parent / f".apm-replay-{secrets.token_hex(8)}"  # moded before the rename
    try:
        _modes.stage_private(unit.copy, staged, unit.kind, _read_no_follow)
        written = {"": compute_file_hash(staged)} if unit.kind == "file" else _tree_hashes(staged)
        if written != unit.expected or unit.mode is None:
            raise OSError("restored bytes do not re-hash to the recorded hash")
        mode = _modes.for_destination(path, unit.mode, unit.copy)  # umask if recreated
        _modes.apply_entry_modes(staged, mode)
        _modes.apply_own_mode(staged, mode)
        if os.path.lexists(path):
            _remove(path)
        release_held(path)
        os.replace(staged, path)
    finally:
        if os.path.lexists(staged):
            _remove(staged)


def _keep(store: Path) -> Path:
    """Mark *store* retained and rename it, as a failed in-process restore does."""
    with contextlib.suppress(OSError):
        (store / _RETAINED_MARKER).write_text(
            "Deploy journal kept: an abandoned install could not be replayed.\n",
            encoding="ascii",
        )
    retained = store.with_name(_RETAINED_PREFIX + store.name[len(_STORE_PREFIX) :])
    release_held(store)
    try:
        store.rename(retained)
    except OSError:
        return store
    return retained


_KEEP_CHANGED = "it changed after the interrupted install"
_KEEP_WIDER = "its recorded mode is wider than it has now"
_KEEP_ACL = (
    "it carries a Windows ACL"
    if os.name == "nt"
    else "it carries a macOS ACL"
    if sys.platform == "darwin"
    else "it carries a POSIX ACL"
)


def _decide(units: list[_Replay]) -> tuple[list[_Replay], list[tuple[str, str]]]:
    """Split *units* into those to restore and those holding data a user wrote.

    A unit is restored only while what is there now is APM's: nothing, its prior state, the bytes
    APM's last write sealed, or a write APM began and never sealed (a kill mid-write; the unit line
    itself opens the first write). A directory a removal APM began left partial holds only entries
    APM journaled: it is rolled back whole. Anything else, such as a file a user edited after the
    kill, is kept.
    """
    apply: list[_Replay] = []
    kept: list[tuple[str, str]] = []
    for unit in units:
        try:
            ours = _holds_apm_state(unit)
        except OSError:
            ours = False  # unreadable now: never destroy what cannot be inspected
        if not ours:
            kept.append((unit.rel, _KEEP_CHANGED))
        elif unit.copy is not None and _modes.carries_acl(unit.path, unit.copy):
            kept.append((unit.rel, _KEEP_ACL))
        elif unit.copy is not None and not _modes.never_wider(
            unit.path, unit.copy, unit.kind, unit.mode
        ):
            kept.append((unit.rel, _KEEP_WIDER))  # never widen beyond what is there (#5134)
        else:
            apply.append(unit)
    return apply, kept


def _replay_store(
    store: Path,
    bases: _Bases,
    ledger: Callable[[], Mapping[str, str | None]],
    committed: Callable[[], str | None],
) -> ReplayOutcome:
    """Replay one abandoned store, or raise ``JournalReplayError`` after keeping it."""
    try:
        header, body = _read_manifest(store)
        lines, seals, commit = _parse(body)
    except _modes.HomeGroupWritable as exc:
        # Left as it is, never retained: it replays once the group write is removed.
        issues = [(rel, str(exc)) for rel in _deploy_paths(store)] or [(str(store), str(exc))]
        raise JournalReplayError(store, issues, applied=False, home=exc.home) from None
    except ValueError as exc:
        raise JournalReplayError(_keep(store), [(str(store), str(exc))], applied=False) from None
    if commit is not None and commit == committed():
        # The killed install's lockfile reached disk: its deployment is committed.
        release_held(store)
        shutil.rmtree(store, ignore_errors=True)
        return ReplayOutcome()
    try:
        vouched = ledger()
    except LedgerUnavailable as exc:
        raise JournalReplayError(_keep(store), [exc.issue], applied=False) from None
    token = header.get("token")
    bases = replace(bases, journal=token if isinstance(token, str) else None)
    units, issues = _validated(lines, seals, store, bases, vouched)
    if issues:
        raise JournalReplayError(_keep(store), issues, applied=False)
    apply, kept = _decide(units)
    for unit in reversed(apply):
        try:
            _apply(unit)
        except OSError as exc:
            issues.append((unit.rel, f"could not restore: {exc}"))
    remove_empty_directories(a for unit in apply for a in unit.ancestors)
    if issues:
        raise JournalReplayError(_keep(store), issues, applied=True)
    if kept:
        # The copies of what was kept stay available for recovery.
        return ReplayOutcome(len(apply), tuple(kept), (_keep(store),))
    release_held(store)
    shutil.rmtree(store, ignore_errors=True)
    return ReplayOutcome(len(apply))


def _deploy_paths(store: Path) -> list[str]:
    """The deploy-root paths *store* journals, read only to name them (#5385)."""
    try:
        _header, body = _read_manifest(store, allow_group_home=True)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return []
    return [
        line["path"]
        for line in body
        if isinstance(line, dict)
        and line.get("base") == "root"
        and isinstance(line.get("path"), str)
    ]


def _validated(
    lines: list[Any],
    seals: Mapping[int, dict[str, Any]],
    store: Path,
    bases: _Bases,
    ledger: Mapping[str, str | None],
) -> tuple[list[_Replay], list[tuple[str, str]]]:
    """Validate every unit line; return the units and the issues found."""
    units: list[_Replay] = []
    issues: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        try:
            unit = _validate_unit(line, store, bases, ledger, seals.get(index))
            events = {"": _pending(line), **seals.get(index, {})}
        except _Unproven as exc:
            issues.append(exc.issue)
            continue
        except (TypeError, ValueError, AttributeError, KeyError):
            issues.append((str(store), "journal manifest holds a malformed unit"))
            continue
        except OSError as exc:
            issues.append((str(line.get("path", store)), f"journal copy unreadable: {exc}"))
            continue
        units.append(replace(unit, events=events))
    return units, issues


def _store_identity(store: Path) -> tuple[str, str] | None:
    """``(deploy root, workspace)`` an abandoned store names, or ``None`` if unreadable."""
    # RecursionError: a deeply nested header is unreadable; replay keeps and names it.
    with contextlib.suppress(OSError, ValueError, UnicodeDecodeError, RecursionError):
        # Validated before any open (#5134); identity only, so group write is reported later.
        _modes.require_private_store(store, allow_group_home=True)
        header = json.loads(_modes.read_private_journal(store / _MANIFEST, first_line=True))
        if isinstance(header, dict):
            root, workspace = header.get("root"), header.get("workspace")
            if isinstance(root, str) and isinstance(workspace, str):
                return os.path.realpath(root), os.path.realpath(workspace)
    return None


def has_abandoned_journals(store_parent: Path, root: Any = None, workspace: Any = None) -> bool:
    """Whether a killed install left a journal the next full install of *root* must handle."""
    with contextlib.suppress(OSError):
        return any(
            store.is_dir()
            and not os.path.lexists(store / _RETAINED_MARKER)
            and (root is None or _replays_for(store, root, workspace))  # else any (#5064)
            for store in store_parent.glob(f"{_STORE_PREFIX}*")
        )
    return False


def _replays_for(store: Path, root: Path, workspace: Path | None) -> bool:
    """Replay for *root* handles *store*: no manifest, an unreadable identity, or its own."""
    named = _store_identity(store) if os.path.lexists(store / _MANIFEST) else None
    root = Path(os.path.abspath(root))
    return named is None or named == (os.path.realpath(root), os.path.realpath(workspace or root))


def replay_abandoned_journals(
    store_parent: Path,
    root: Path,
    ledger: Callable[[], Mapping[str, str | None]],
    *,
    receipts: Path,
    committed: Callable[[], str | None] = lambda: None,
    workspace: Path | None = None,
) -> ReplayOutcome:
    """Replay the journals a killed install left for *root*.

    Call only while holding the lifecycle lock, before the install changes anything: every install
    that could own a live journal is then this process. *ledger* returns each deployed path the
    lockfile claims (POSIX, relative to *root*) mapped to its recorded content hash, or ``None``; it
    raises ``LedgerUnavailable`` when the lockfile exists but cannot be read. *committed* returns
    the :func:`lockfile_identity` of the lockfile on disk. *receipts* is the deployment receipts
    directory, the only APM-home location a journal may restore. A journal for another deploy root,
    or for another metadata *workspace* (lockfile directory, default *root*) sharing it, is left for
    that workspace's next install. A path holding data the killed install did not write is kept and
    listed in the outcome, never removed or replaced. Raise ``JournalReplayError`` on the first
    journal that cannot be replayed, after keeping it.
    """
    root = Path(os.path.abspath(root))
    bases = _Bases(root, Path(os.path.abspath(receipts)))
    ledger, committed = functools.cache(ledger), functools.cache(committed)
    stores: list[Path] = []
    with contextlib.suppress(OSError):
        stores = sorted(store_parent.glob(f"{_STORE_PREFIX}*"))
    restored, kept, retained = 0, [], []
    for store in stores:
        if not store.is_dir() or store.is_symlink() or os.path.lexists(store / _RETAINED_MARKER):
            continue
        if not os.path.lexists(store / _MANIFEST):
            # An in-memory journal (a fast apply, or an older APM) recorded
            # nothing replay can prove: reclaim it, as every install did before.
            release_held(store)
            shutil.rmtree(store, ignore_errors=True)
            continue
        if not _replays_for(store, root, workspace):
            continue
        outcome = _replay_store(store, bases, ledger, committed)
        restored += outcome.restored
        kept += outcome.kept
        retained += outcome.retained
    # Leave no empty store parent behind, as a settled journal does.
    with contextlib.suppress(OSError):
        release_held(store_parent)
        store_parent.rmdir()
    return ReplayOutcome(restored, tuple(kept), tuple(retained))
