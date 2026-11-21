"""Lazy pre-write journal of the paths one install touches (#4282).

:class:`~apm_cli.install.transaction.InstallTransaction` listens at the
integrator write seam (``integration/write_seam.py``). The first time a path
is about to be created, replaced or removed, :meth:`DeployJournal.record`
keeps its prior bytes (or notes that it was absent) in a store under the APM
home ``install-snapshots`` directory. Paths the install never touches are not
copied. :meth:`DeployJournal.restore` puts every recorded path back, newest
first, and removes the directories the install created;
:meth:`DeployJournal.discard` deletes the store once the install committed.

A store a killed process left behind (``deploy-*``) is removed by
:func:`reap_abandoned_journals` when the next install starts under the
lifecycle lock. A store kept after a failed restore is marked retained
(a ``RETAINED`` file inside it) and then renamed ``retained-*``; the reaper
never deletes a marked store, so the copies survive even a failed rename.
:func:`remove_empty_directories` also lets the transaction remove target roots
a failed install created and left empty (#4159).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apm_cli.utils.file_ops import robust_rmtree

_Kind = Literal["absent", "file", "dir", "symlink"]
_STORE_PREFIX = "deploy-"
_RETAINED_PREFIX = "retained-"
_RETAINED_MARKER = "RETAINED"


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
                directory.rmdir()


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        robust_rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def reap_abandoned_journals(store_parent: Path) -> None:
    """Delete the journals of installs that were killed before they finished.

    Call only while holding the lifecycle lock: every install that could own a
    live journal under this APM home is then this process.
    """
    with contextlib.suppress(OSError):
        for store in store_parent.glob(f"{_STORE_PREFIX}*"):
            retained = os.path.lexists(store / _RETAINED_MARKER)
            if store.is_dir() and not store.is_symlink() and not retained:
                shutil.rmtree(store, ignore_errors=True)


class DeployJournal:
    """Prior state of every path one install touched, recorded on first touch."""

    def __init__(self, store_parent: Path) -> None:
        self.store_parent = Path(os.path.abspath(store_parent))
        self.store: Path | None = None
        self._units: list[_Unit] = []
        self._recorded: set[Path] = set()
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
            self.store_parent.mkdir(parents=True, exist_ok=True)
            self.store = Path(tempfile.mkdtemp(prefix=_STORE_PREFIX, dir=self.store_parent))
        return self.store

    def record(self, path: Path, *, contents: bool = True) -> None:
        """Keep the prior state of *path* the first time it is touched.

        With ``contents=False`` (a directory about to be created) only an
        absent path is recorded: creating an existing directory changes nothing.
        """
        path = Path(os.path.abspath(path))
        with self._lock:
            if self._covered(path) or path.is_relative_to(self.store_parent):
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
            self._units.append(unit)
            self._recorded.add(path)

    def restore(self) -> list[tuple[Path, str]]:
        """Put every recorded path back; return the paths that could not be restored."""
        issues: list[tuple[Path, str]] = []
        with self._lock:
            units = list(reversed(self._units))
        for unit in units:
            try:
                self._restore_unit(unit)
            except OSError as exc:
                issues.append((unit.path, f"could not restore: {exc}"))
        remove_empty_directories(a for unit in units for a in unit.absent_ancestors)
        if issues and self.store is not None:
            issues.append((self._retain(), "pre-install copies retained here for recovery"))
        else:
            self.discard()
        return issues

    def _retain(self) -> Path:
        """Rename the store so no later install reaps the copies a user still needs."""
        if self.store is None:
            raise RuntimeError("no store to retain")
        # The marker keeps the copies from the reaper even if the rename fails.
        (self.store / _RETAINED_MARKER).write_text(
            "Pre-install copies kept after a failed restore.\n", encoding="ascii"
        )
        retained = self.store.with_name(_RETAINED_PREFIX + self.store.name[len(_STORE_PREFIX) :])
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
            os.symlink(unit.link, path)
        elif unit.kind == "dir" and copy is not None:
            shutil.copytree(copy, path, symlinks=True)
        elif copy is not None:
            shutil.copy2(copy, path)

    def discard(self) -> None:
        """Delete the stored copies and the empty store parent."""
        store, self.store = self.store, None
        if store is None:
            return
        shutil.rmtree(store, ignore_errors=True)
        with contextlib.suppress(OSError):
            store.parent.rmdir()
