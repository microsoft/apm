"""Permission bits a deploy journal records and replay restores (#5111).

A journaled copy is authenticated by its content hash only, so its own mode bits
are never evidence. The journal unit line records the mode of the path when the
install first touched it (``mode``: an int for a file; for a directory, relative
path -> int, ``""`` being the directory). Replay stages restored bytes with
private permissions and applies the recorded mode only when it is valid
permission bits for every staged entry; otherwise the unit is kept and reported.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

_PERMISSION_BITS = 0o777


def _bits(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode) & _PERMISSION_BITS


def _entries(directory: Path) -> list[Path]:
    """Every non-link entry below *directory*, parents first."""
    return [entry for entry in sorted(directory.rglob("*")) if not entry.is_symlink()]


def recorded_mode(copy: Path, kind: str) -> int | dict[str, int]:
    """The mode to record for a unit, read from APM's copy as it is taken."""
    if kind == "file":
        return _bits(copy)
    modes = {"": _bits(copy)}
    modes.update({entry.relative_to(copy).as_posix(): _bits(entry) for entry in _entries(copy)})
    return modes


def _valid(mode: Any) -> bool:
    return type(mode) is int and 0 <= mode <= _PERMISSION_BITS


def verified_mode(recorded: Any, kind: str, copy: Path) -> int | dict[str, int] | None:
    """*recorded* when it is valid permission bits for every entry of the unit, else ``None``."""
    if kind == "file":
        return recorded if _valid(recorded) else None
    expected = {""} | {entry.relative_to(copy).as_posix() for entry in _entries(copy)}
    if not isinstance(recorded, dict) or set(recorded) != expected:
        return None
    return recorded if all(_valid(mode) for mode in recorded.values()) else None


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def stage_private(copy: Path, staged: Path, kind: str, read: Any) -> None:
    """Write *copy*'s bytes (and links) to *staged*, never taking the copy's modes."""
    if kind == "file":
        _write_private(staged, read(copy))
        return
    staged.mkdir(mode=0o700)
    for entry in sorted(copy.rglob("*")):
        target = staged / entry.relative_to(copy)
        if entry.is_symlink():
            os.symlink(os.readlink(entry), target)
        elif entry.is_dir():
            target.mkdir(mode=0o700)
        else:
            _write_private(target, read(entry))


def apply_mode(staged: Path, mode: int | dict[str, int]) -> None:
    """Give *staged* its verified recorded mode, deepest entries first."""
    if isinstance(mode, int):
        os.chmod(staged, mode)
        return
    for rel in sorted(mode, key=lambda name: name.count("/") + bool(name), reverse=True):
        os.chmod(staged / rel if rel else staged, mode[rel])
