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
    # The full mode: a setuid, setgid or sticky bit fails validation, so such a path
    # is kept and reported rather than restored without it.
    return stat.S_IMODE(path.lstat().st_mode)


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


# A path that is gone is bounded by what a new file or directory gets.
_NEW_FILE, _NEW_DIR = 0o644, 0o755


def _ceiling(path: Path, directory: bool) -> int | None:
    """The widest permission bits replay may give *path*; ``None``: it is now a link."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return _NEW_DIR if directory else _NEW_FILE
    except OSError:
        return None  # cannot be reached now (a parent is not a directory): refused
    if stat.S_ISLNK(st.st_mode):
        return None
    return stat.S_IMODE(st.st_mode) & _PERMISSION_BITS


def _within(bits: int, ceiling: int | None) -> bool:
    if ceiling is None:
        return False
    if os.name == "nt":
        # Windows modes only carry the read-only flag: widening is clearing it.
        return not (bits & stat.S_IWRITE and not ceiling & stat.S_IWRITE)
    return not bits & ~ceiling


def never_wider(path: Path, copy: Path, kind: str, mode: int | dict[str, int]) -> bool:
    """Whether restoring *mode* adds no permission bit the path lacks now (#5134).

    The recorded mode is journal data; the path on disk is not. A restore may
    narrow permissions back but never widen them beyond what is there now; a
    path that is now a link is refused.
    """
    if isinstance(mode, int):
        return _within(mode, _ceiling(path, kind == "dir"))
    return all(
        _within(bits, _ceiling(path / rel if rel else path, not rel or (copy / rel).is_dir()))
        for rel, bits in mode.items()
    )


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


def apply_entry_modes(staged: Path, mode: int | dict[str, int]) -> None:
    """Give *staged* its verified recorded mode, deepest entries first.

    A directory's own mode is applied by :func:`apply_own_mode` once it is in
    place: a read-only directory cannot be renamed into another directory.
    """
    if isinstance(mode, int):
        os.chmod(staged, mode)
        return
    for rel in sorted((rel for rel in mode if rel), key=lambda n: n.count("/"), reverse=True):
        os.chmod(staged / rel, mode[rel])


def apply_own_mode(path: Path, mode: int | dict[str, int]) -> None:
    """Give a restored directory its own recorded mode, never through a link."""
    if not isinstance(mode, dict):
        return
    if not hasattr(os, "O_NOFOLLOW"):
        os.chmod(path, mode[""])  # Windows: only the read-only flag
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, mode[""])
    finally:
        os.close(fd)


def with_recorded_mode(state: dict[str, Any], mode: int | dict[str, int]) -> dict[str, Any]:
    """*state* (read from the copy) with the recorded mode in place of the copy's bits.

    Replay's keep-or-restore decision compares the path against this prior
    state, so it never trusts a copy's mode bits either.
    """
    if isinstance(mode, int):
        return {**state, "mode": mode}
    entries = {
        rel: f"{fingerprint.rsplit(':', 1)[0]}:{mode[rel]}" if rel in mode else fingerprint
        for rel, fingerprint in state.get("dir", {}).items()
    }
    return {**state, "dir": entries, "mode": mode[""]}
