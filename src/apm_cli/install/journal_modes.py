"""Permission bits a deploy journal records and replay restores (#5111).

A journaled copy is authenticated by its content hash only, so its own mode bits
are never evidence. The journal unit line records the mode of the path when the
install first touched it (``mode``: an int for a file; for a directory, relative
path -> int, ``""`` being the directory). Replay stages restored bytes with
private permissions and applies the recorded mode only when it is valid
permission bits for every staged entry; otherwise the unit is kept and reported.
"""

from __future__ import annotations

import errno
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


_ACL_XATTRS = frozenset({"system.posix_acl_access", "system.posix_acl_default"})
_NO_XATTRS = frozenset(
    getattr(errno, name)
    for name in ("ENOTSUP", "EOPNOTSUPP", "ENODATA", "ENOENT")
    if hasattr(errno, name)
)


def carries_acl(*paths: Path) -> bool:
    """Whether any of *paths* (or an entry below one) carries a POSIX ACL.

    Restored bytes are staged with permission bits only, so an ACL would be lost.
    """
    if not hasattr(os, "listxattr"):
        return False
    for root in paths:
        for path in [root, *(_entries(root) if root.is_dir() and not root.is_symlink() else ())]:
            try:
                names = os.listxattr(path, follow_symlinks=False)
            except OSError as exc:
                if exc.errno in _NO_XATTRS:
                    continue  # unsupported, none, or nothing there: no ACL
                return True  # cannot be checked: never assumed absent
            if _ACL_XATTRS.intersection(names):
                return True
    return False


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def stage_private(copy: Path, staged: Path, kind: str, read: Any) -> None:
    """Write *copy*'s bytes (and links) to *staged*, never taking the copy's modes."""
    if kind == "file":
        _write_private(staged, read(copy))
    else:
        _stage_tree(copy, staged, read)
    if carries_acl(staged):
        # Inherited from the destination's default ACL: never published (#5134).
        raise OSError(f"{staged} would carry an inherited POSIX ACL")


def _stage_tree(copy: Path, staged: Path, read: Any) -> None:
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
        _chmod_nofollow(staged, mode)
        return
    for rel in sorted((rel for rel in mode if rel), key=lambda n: n.count("/"), reverse=True):
        entry = staged / rel
        _chmod_nofollow(entry, mode[rel], directory=stat.S_ISDIR(os.lstat(entry).st_mode))


def apply_own_mode(path: Path, mode: int | dict[str, int]) -> None:
    """Give a restored directory its own recorded mode, never through a link."""
    if isinstance(mode, dict):
        _chmod_nofollow(path, mode[""], directory=True)


def _chmod_nofollow(path: Path, mode: int, *, directory: bool = False) -> None:
    """Set *path*'s mode through a handle that refuses a link (#5134)."""
    if not hasattr(os, "O_NOFOLLOW"):
        os.chmod(path, mode)  # Windows: only the read-only flag
        return
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0)
    fd = os.open(path, flags)
    try:
        os.fchmod(fd, mode)
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


# -- the snapshot store is private to this user (#5134) ---------------------------------

NOT_PRIVATE = "the snapshot store is not private to this user; nothing was restored"
_MANIFEST = "journal.jsonl"


def private_dir(path: Path) -> None:
    """Create *path* (and its parents) without group or other write, narrowing it if needed."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        st = os.stat(path)
        if st.st_mode & 0o022 and st.st_uid == os.geteuid():
            os.chmod(path, stat.S_IMODE(st.st_mode) & ~0o022)


def append_private(path: Path, text: str) -> None:
    """Append *text* to the journal file *path*, which is only ever this user's to write."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if os.name != "nt" and os.fstat(fd).st_mode & 0o077:
            os.fchmod(fd, 0o600)  # set explicitly, never left to the umask
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def require_private_store(store: Path) -> None:
    """Raise ``ValueError`` unless the store chain is private to this user.

    The install-snapshots directory, the store and its journal must each be a real
    directory or regular file (never a link), and on POSIX owned by this user with
    no group or other write.
    """
    for path in (store.parent, store, store / _MANIFEST):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            raise ValueError(NOT_PRIVATE)
        if os.name != "nt" and (st.st_uid != os.geteuid() or st.st_mode & 0o022):
            raise ValueError(NOT_PRIVATE)


def for_destination(path: Path, mode: int | dict[str, int], copy: Path) -> int | dict[str, int]:
    """The mode to give a restore: a path recreated from absence is capped by the umask."""
    if os.name == "nt" or os.path.lexists(path):
        return mode
    from apm_cli.cache.deploy_store import process_umask

    umask = process_umask()  # the process's umask, read once
    if isinstance(mode, int):
        return mode & _NEW_FILE & ~umask
    return {
        rel: bits & (_NEW_DIR if not rel or (copy / rel).is_dir() else _NEW_FILE) & ~umask
        for rel, bits in mode.items()
    }
