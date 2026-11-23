"""The integrator write seam: every deploy write records its path first (#4282).

Integrators never touch a deploy path directly. They call the writers below,
each of which hands the path to :func:`record_before_write` (or, for a
directory it only creates, :func:`record_before_mkdir`) before changing it.
While an install is in progress,
:class:`~apm_cli.install.transaction.InstallTransaction` listens here and
keeps the prior bytes (or the absence) of each path the first time it is
touched (``install/deploy_snapshot.py``), so a failed or interrupted install
can put every touched path back. Paths the install does not touch cost
nothing. Outside an install, the writers just write.

The ``install-deployment-deploy-write-seam`` architecture guard rejects a raw
file write in ``src/apm_cli/integration/`` that bypasses these writers.
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

from apm_cli.utils import atomic_io

# (path, contents): contents=False records only an absent path (mkdir).
Recorder = Callable[[Path, bool], None]

_recorder: Recorder | None = None
_planning = False
_guard = threading.Lock()


class PlannedWriteError(RuntimeError):
    """A deploy write reached the seam while install computed its write plan (#4381)."""


@contextmanager
def planning() -> Iterator[None]:
    """Run integrators without touching disk while install plans its writes (#4381).

    Directory creation is skipped; any other write raises
    :class:`PlannedWriteError`, so a plan can never deploy a byte.
    """
    global _planning
    with _guard:
        _planning = True
    try:
        yield
    finally:
        with _guard:
            _planning = False


def listen(recorder: Recorder) -> None:
    """Route every deploy write through *recorder* (only InstallTransaction listens)."""
    global _recorder
    with _guard:
        _recorder = recorder


def stop_listening(recorder: Recorder) -> None:
    """Stop routing deploy writes to *recorder* if it is still the listener."""
    global _recorder
    with _guard:
        if _recorder == recorder:
            _recorder = None


@contextmanager
def private_scratch() -> Iterator[None]:
    """Write a private scratch tree no target deploys to: not journaled, not planned.

    The locked render that proves deployment evidence replays packages into
    such a tree, also while install plans its writes (#4381).
    """
    global _recorder, _planning
    with _guard:
        saved = (_recorder, _planning)
        _recorder, _planning = None, False
    try:
        yield
    finally:
        with _guard:
            _recorder, _planning = saved


def record_before_write(path: str | os.PathLike[str], *, follow: bool = False) -> None:
    """Record *path* before it is created, replaced or removed.

    *follow* marks a writer that writes through a symlink (an append or an
    in-place write): the link's destination is recorded too, since that is
    where the bytes change. Replacing or removing writers record the link only.
    """
    if _planning:
        raise PlannedWriteError(f"Install planned a write to {path} before its preflight passed")
    recorder = _recorder
    if recorder is not None:
        recorder(Path(path), True)
        if follow and os.path.islink(path):
            recorder(Path(os.path.realpath(path)), True)


def record_before_mkdir(path: str | os.PathLike[str]) -> None:
    """Record *path* before it is created as a directory; an existing one is kept as is."""
    recorder = _recorder
    if recorder is not None:
        recorder(Path(path), False)


def mkdir(path: Path, *, parents: bool = False, exist_ok: bool = False, mode: int = 0o777) -> None:
    """Create the directory *path* through the seam (skipped while planning)."""
    if _planning:
        return
    record_before_mkdir(path)
    path.mkdir(mode=mode, parents=parents, exist_ok=exist_ok)


def write_text_lf(path: Path, data: str) -> None:
    """:func:`apm_cli.utils.atomic_io.write_text_lf` through the seam."""
    record_before_write(path)
    atomic_io.write_text_lf(path, data)


def atomic_write_text(path: Path, data: str, **kwargs: Any) -> None:
    """:func:`apm_cli.utils.atomic_io.atomic_write_text` through the seam."""
    record_before_write(path)
    atomic_io.atomic_write_text(path, data, **kwargs)


def write_text(path: Path, data: str, *, encoding: str = "utf-8") -> None:
    """``Path.write_text`` through the seam."""
    record_before_write(path, follow=True)
    path.write_text(data, encoding=encoding)


def write_json_config(path: Path, data: Any, **kwargs: Any) -> bool:
    """:func:`apm_cli.utils.json_config.write_json_config` through the seam."""
    from apm_cli.utils.json_config import write_json_config as _write

    record_before_write(path)
    return _write(path, data, **kwargs)


def open_for_write(path: Path, mode: str = "w", **kwargs: Any) -> IO[Any]:
    """``open(path, mode)`` for writing, through the seam."""
    record_before_write(path, follow=True)
    return open(path, mode, **kwargs)


def copy2(source: Path, destination: Path) -> Path:
    """``shutil.copy2`` through the seam, replacing the destination atomically (#4691)."""
    record_before_write(destination, follow=True)
    return atomic_io.atomic_copy(source, destination, metadata=True)


def copyfile(source: Path, destination: Path) -> Path:
    """``shutil.copyfile`` through the seam, replacing the destination atomically (#4691)."""
    record_before_write(destination, follow=True)
    return atomic_io.atomic_copy(source, destination, metadata=False)


def rename(source: Path, destination: Path) -> None:
    """``os.rename`` through the seam: both paths change."""
    record_before_write(source)
    record_before_write(destination)
    os.rename(source, destination)


def unlink(path: Path, *, missing_ok: bool = False) -> None:
    """``Path.unlink`` through the seam."""
    record_before_write(path)
    path.unlink(missing_ok=missing_ok)


def rmdir(path: Path) -> None:
    """``Path.rmdir`` through the seam."""
    record_before_write(path)
    path.rmdir()


def rmtree(path: Path, **kwargs: Any) -> None:
    """``shutil.rmtree`` through the seam."""
    record_before_write(path)
    shutil.rmtree(path, **kwargs)
