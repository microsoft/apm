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
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

from apm_cli.utils import atomic_io
from apm_cli.utils.realpath_win import release_held

# (path, contents, expected[, size=]): contents=False records only an absent path
# (mkdir); expected is the content hash the write will leave there, or
# "absent" / "dir", or None when the writer cannot know it (#4863).
Recorder = Callable[..., None]
_ABSENT, _DIR = "absent", "dir"

_recorder: Recorder | None = None
# Notified after each write completes: the journal seals what APM wrote (#4863).
_after: Callable[[Path], None] | None = None
# The listening journal's token, so the config file records can name it (#5051).
_journal: str | None = None
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


def listen(
    recorder: Recorder, after: Callable[[Path], None] | None = None, journal: str | None = None
) -> None:
    """Route every deploy write through *recorder*, and each finished one through *after*.

    Only InstallTransaction listens; *journal* is its deploy journal's token.
    """
    global _recorder, _after, _journal
    with _guard:
        _recorder, _after, _journal = recorder, after, journal


def stop_listening(recorder: Recorder) -> None:
    """Stop routing deploy writes to *recorder* if it is still the listener."""
    global _recorder, _after, _journal
    with _guard:
        if _recorder == recorder:
            _recorder, _after, _journal = None, None, None


def active_journal() -> str | None:
    """The token of the deploy journal recording writes now, or ``None``."""
    return _journal


@contextmanager
def private_scratch() -> Iterator[None]:
    """Write a private scratch tree no target deploys to: not journaled, not planned.

    The locked render that proves deployment evidence replays packages into
    such a tree, also while install plans its writes (#4381).
    """
    global _recorder, _after, _planning
    with _guard:
        saved = (_recorder, _after, _planning)
        _recorder, _after, _planning = None, None, False
    try:
        yield
    finally:
        with _guard:
            _recorder, _after, _planning = saved


def record_before_write(
    path: str | os.PathLike[str],
    *,
    follow: bool = False,
    expected: str | None = None,
    size: int | None = None,
) -> None:
    """Record *path* before it is created, replaced or removed.

    *follow* marks a writer that writes through a symlink (an append or an
    in-place write): the link's destination is recorded too, since that is
    where the bytes change. Replacing or removing writers record the link only.
    *size* is the byte length of the content *expected* hashes, when the writer knows it (#5411).
    """
    if _planning:
        raise PlannedWriteError(f"Install planned a write to {path} before its preflight passed")
    recorder = _recorder
    if recorder is not None:
        extra = {} if size is None else {"size": size}
        recorder(Path(path), True, expected, **extra)
        if follow and os.path.islink(path):
            recorder(Path(os.path.realpath(path)), True, expected, **extra)


def record_after_write(path: str | os.PathLike[str], *, follow: bool = False) -> None:
    """Report that the write to *path* finished, so the journal seals its bytes (#4863).

    *follow* reports a symlink's destination too, as :func:`record_before_write` does.
    """
    after = _after
    if after is not None:
        after(Path(path))
        if follow and os.path.islink(path):
            after(Path(os.path.realpath(path)))


def record_before_mkdir(path: str | os.PathLike[str]) -> None:
    """Record *path* before it is created as a directory; an existing one is kept as is."""
    recorder = _recorder
    if recorder is not None:
        recorder(Path(path), False, _DIR)


def mkdir(path: Path, *, parents: bool = False, exist_ok: bool = False, mode: int = 0o777) -> None:
    """Create the directory *path* through the seam (skipped while planning).

    A directory that already exists (not a link) is left as it is and changes
    nothing, so it is neither journaled nor sealed: sealing it would fingerprint
    its whole tree, once per caller, though every write below it seals its own
    entry.
    """
    if _planning:
        return
    if exist_ok and _is_real_dir(path):
        return
    record_before_mkdir(path)
    path.mkdir(mode=mode, parents=parents, exist_ok=exist_ok)
    record_after_write(path)


def _is_real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def write_text_lf(path: Path, data: str) -> None:
    """:func:`apm_cli.utils.atomic_io.write_text_lf` through the seam."""
    record_before_write(path, expected=_text_hash(data, lf=True), size=_text_size(data, lf=True))
    atomic_io.write_text_lf(path, data)
    record_after_write(path)


def atomic_write_text(path: Path, data: str, **kwargs: Any) -> None:
    """:func:`apm_cli.utils.atomic_io.atomic_write_text` through the seam."""
    lf = kwargs.get("normalize_line_endings", True)
    record_before_write(path, expected=_text_hash(data, lf=lf), size=_text_size(data, lf=lf))
    atomic_io.atomic_write_text(path, data, **kwargs)
    record_after_write(path)


def write_text(
    path: Path, data: str, *, encoding: str = "utf-8", newline: str | None = None
) -> None:
    """``Path.write_text`` through the seam (``newline=""`` keeps *data*'s line endings)."""
    expected = _text_hash(data, encoding=encoding, newline=newline)
    record_before_write(path, follow=True, expected=expected)
    path.write_text(data, encoding=encoding, newline=newline)
    record_after_write(path, follow=True)


def write_json_config(path: Path, data: Any, **kwargs: Any) -> bool:
    """:func:`apm_cli.utils.json_config.write_json_config` through the seam."""
    from apm_cli.utils.json_config import json_config_text
    from apm_cli.utils.json_config import write_json_config as _write

    text = json_config_text(
        path, data, default_trailing_newline=kwargs.get("default_trailing_newline", True)
    )
    record_before_write(path, expected=_text_hash(text), size=_text_size(text, lf=False))
    written = _write(path, data, **kwargs)
    record_after_write(path)
    return written


def open_for_write(path: Path, mode: str = "w", **kwargs: Any) -> IO[Any]:
    """``open(path, mode)`` for writing, through the seam."""
    record_before_write(path, follow=True)
    return _SealOnClose(open(path, mode, **kwargs), path)


def copy2(source: Path, destination: Path) -> Path:
    """``shutil.copy2`` through the seam, replacing the destination atomically (#4691)."""
    record_before_write(
        destination, follow=True, expected=_file_hash(source), size=_file_size(source)
    )
    copied = atomic_io.atomic_copy(source, destination, metadata=True)
    record_after_write(destination, follow=True)
    return copied


def copyfile(source: Path, destination: Path) -> Path:
    """``shutil.copyfile`` through the seam, replacing the destination atomically (#4691)."""
    record_before_write(
        destination, follow=True, expected=_file_hash(source), size=_file_size(source)
    )
    copied = atomic_io.atomic_copy(source, destination, metadata=False)
    record_after_write(destination, follow=True)
    return copied


def rename(source: Path, destination: Path) -> None:
    """``os.rename`` through the seam: both paths change."""
    record_before_write(source, expected=_ABSENT)
    record_before_write(destination, expected=_file_hash(source))
    release_held(source)
    release_held(destination)
    os.rename(source, destination)
    record_after_write(source)
    record_after_write(destination)


def chmod(path: Path, mode: int) -> None:
    """``os.chmod`` through the seam: the mode is part of what the journal seals (#4863)."""
    record_before_write(path, expected=_file_hash(path))
    os.chmod(path, mode)
    record_after_write(path)


def unlink(path: Path, *, missing_ok: bool = False) -> None:
    """``Path.unlink`` through the seam."""
    record_before_write(path, expected=_ABSENT)
    path.unlink(missing_ok=missing_ok)
    record_after_write(path)


def rmdir(path: Path) -> None:
    """``Path.rmdir`` through the seam."""
    record_before_write(path, expected=_ABSENT)
    release_held(path)
    path.rmdir()
    record_after_write(path)


def rmtree(path: Path, **kwargs: Any) -> None:
    """``shutil.rmtree`` through the seam."""
    record_before_write(path, expected=_ABSENT)
    release_held(path)
    shutil.rmtree(path, **kwargs)
    record_after_write(path)


def _text_hash(data: str, **kwargs: Any) -> str:
    from apm_cli.utils.content_hash import text_write_hash

    return text_write_hash(data, **kwargs)


def _text_size(data: str, *, lf: bool) -> int:
    from apm_cli.utils.content_hash import text_write_size

    return text_write_size(data, lf=lf)


def _file_size(source: Path) -> int | None:
    try:
        return os.stat(source).st_size if os.path.isfile(source) else None
    except OSError:
        return None


def _file_hash(source: Path) -> str:
    """What a copy or move of *source* leaves: its content hash, a directory, or nothing."""
    from apm_cli.utils.content_hash import compute_exact_file_hash

    if os.path.isdir(source) and not os.path.islink(source):
        return _DIR
    try:
        return compute_exact_file_hash(source) if os.path.isfile(source) else _ABSENT
    except OSError:
        return _ABSENT


class _SealOnClose:
    """A file opened through the seam: closing it reports the finished write."""

    def __init__(self, handle: IO[Any], path: Path) -> None:
        self._handle = handle
        self._path = path

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)

    def __enter__(self) -> _SealOnClose:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        closed = self._handle.closed
        self._handle.close()
        if not closed:
            record_after_write(self._path, follow=True)
