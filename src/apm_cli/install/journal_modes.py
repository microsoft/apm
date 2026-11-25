"""Permission bits a deploy journal records and replay restores (#5111).

A journaled copy is authenticated by its content hash only, so its own mode bits
are never evidence. The journal unit line records the mode of the path when the
install first touched it (``mode``: an int for a file; for a directory, relative
path -> int, ``""`` being the directory). Replay stages restored bytes with
private permissions and applies the recorded mode only when it is valid
permission bits for every staged entry; otherwise the unit is kept and reported.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import os
import stat
import sys
import weakref
from pathlib import Path
from typing import Any

from apm_cli.utils.realpath_win import release_held

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


# Windows: an ACE a file did not inherit, or a DACL protected from inheritance (#5178).
_INHERITED_ACE = 0x10
_SE_DACL_PROTECTED = 0x1000
_WIN_NOT_THERE = frozenset({2, 3})  # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND


@functools.cache
def _windows_security_api() -> tuple[Any, Any]:
    """``advapi32`` and ``kernel32`` with the signatures :func:`_windows_acl` calls."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.POINTER(ctypes.c_void_p)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        *([pointer] * 5),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, pointer]
    advapi32.GetAce.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return advapi32, kernel32


def _windows_acl(path: Path) -> bool:
    """Whether *path*'s DACL holds an ACE it did not inherit, or is protected (#5178).

    A file APM writes takes every ACE from its parent; an explicit grant or deny
    would be lost by a restore. ``OSError`` when the DACL cannot be read.
    """
    import ctypes
    from ctypes import wintypes

    advapi32, kernel32 = _windows_security_api()
    dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    # SE_FILE_OBJECT (1), DACL_SECURITY_INFORMATION (4).
    status = advapi32.GetNamedSecurityInfoW(
        str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
    )
    if status in _WIN_NOT_THERE:
        return False
    if status:
        raise OSError(status, "cannot read the DACL", str(path))
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if control.value & _SE_DACL_PROTECTED:
            return True
        if not dacl.value:
            return False  # a NULL DACL holds no entry
        count = ctypes.cast(dacl, ctypes.POINTER(ctypes.c_ushort))[2]  # ACL.AceCount
        for index in range(count):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte))[1] & _INHERITED_ACE:
                return True  # ACE_HEADER.AceFlags without INHERITED_ACE
        return False
    finally:
        kernel32.LocalFree(descriptor)


# macOS: acl_get_link_np reads the extended (NFSv4-style) ACL without following a link.
_ACL_TYPE_EXTENDED = 0x100
_ACL_FIRST_ENTRY = 0


@functools.cache
def _macos_acl_api() -> Any:
    """``libc`` with the signatures :func:`_macos_acl` calls."""
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.acl_get_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int]
    libc.acl_get_link_np.restype = ctypes.c_void_p
    libc.acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    libc.acl_get_entry.restype = ctypes.c_int
    libc.acl_free.argtypes = [ctypes.c_void_p]
    libc.acl_free.restype = ctypes.c_int
    return libc


def _macos_acl(path: Path) -> bool:
    """Whether *path* itself (never a link's target) has an extended ACL entry (#5178).

    ``OSError`` when the ACL cannot be read, including any failure of the libc
    binding itself, so the caller keeps the unit; none, unsupported or nothing
    there is False.
    """
    try:
        return _read_macos_acl(path)
    except OSError:
        raise
    except Exception as exc:  # a broken binding is an unreadable ACL, never a crash
        raise OSError(errno.EIO, f"cannot read the ACL: {exc}", str(path)) from exc


def _read_macos_acl(path: Path) -> bool:
    import ctypes

    libc = _macos_acl_api()
    ctypes.set_errno(0)
    acl = libc.acl_get_link_np(os.fsencode(path), _ACL_TYPE_EXTENDED)
    if not acl:
        error = ctypes.get_errno()
        if error in _NO_XATTRS:
            return False
        raise OSError(error, os.strerror(error), str(path))
    try:
        entry = ctypes.c_void_p()
        ctypes.set_errno(0)
        if libc.acl_get_entry(acl, _ACL_FIRST_ENTRY, ctypes.byref(entry)) == 0:
            return True
        error = ctypes.get_errno()
        if error == errno.EINVAL:
            return False  # an ACL without entries
        raise OSError(error, os.strerror(error), str(path))
    finally:
        libc.acl_free(acl)


def _is_junction(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", None)  # Python 3.12+
    return bool(isjunction and isjunction(path))


def _posix_acl(path: Path) -> bool:
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in _NO_XATTRS:
            return False  # unsupported, none, or nothing there: no ACL
        raise
    return bool(_ACL_XATTRS.intersection(names))


def carries_acl(*paths: Path) -> bool:
    """Whether any of *paths* (or an entry below one) carries an ACL.

    A POSIX ACL on Linux; on macOS, an extended ACL entry; on Windows, a DACL
    with an ACE it did not inherit or protected from inheritance (#5178). Restored bytes are staged with
    permission bits only, so an ACL would be lost. A path whose ACL cannot be
    checked is never assumed to have none.
    """
    if os.name == "nt":
        has_acl = _windows_acl
    elif sys.platform == "darwin":
        has_acl = _macos_acl
    elif hasattr(os, "listxattr"):
        has_acl = _posix_acl
    else:
        return False
    for root in paths:
        for path in [root, *(_entries(root) if root.is_dir() and not root.is_symlink() else ())]:
            if os.name == "nt" and (path.is_symlink() or _is_junction(path)):
                continue  # a link has no ACL of its own to keep, as on Linux
            try:
                if has_acl(path):
                    return True
            except OSError:
                return True  # cannot be checked: never assumed absent
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
        raise OSError(f"{staged} would carry an inherited ACL")


# On Windows a 0o700 mkdir gives the directory an explicit, protected DACL (Python
# 3.12.4+): an ACL carries_acl reports. Windows modes carry nothing else.
_STAGE_DIR_MODE = 0o777 if os.name == "nt" else 0o700


def _stage_tree(copy: Path, staged: Path, read: Any) -> None:
    staged.mkdir(mode=_STAGE_DIR_MODE)
    for entry in sorted(copy.rglob("*")):
        target = staged / entry.relative_to(copy)
        if entry.is_symlink():
            release_held(target)
            os.symlink(os.readlink(entry), target)
        elif entry.is_dir():
            target.mkdir(mode=_STAGE_DIR_MODE)
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
    """Create *path* (and its parents) without group or other write, narrowing it if needed.

    APM_HOME, its parent, is created private too when APM creates it.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=True)
    if os.name != "nt":
        st = os.stat(path)
        if st.st_mode & 0o022 and st.st_uid == os.geteuid():
            os.chmod(path, stat.S_IMODE(st.st_mode) & ~0o022)


def open_private_append(path: Path) -> int:
    """Open the journal file *path* for appending, private to this user; return the fd."""
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if os.name != "nt" and os.fstat(fd).st_mode & 0o077:
            os.fchmod(fd, 0o600)  # set explicitly, never left to the umask
    except BaseException:
        os.close(fd)
        raise
    return fd


def append_private(path: Path, text: str) -> None:
    """Append *text* to the journal file *path*, which is only ever this user's to write."""
    fd = open_private_append(path)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def append_held(owner: Any, path: Path, text: str) -> None:
    """Append *text* to *path* through one private fd that *owner* holds (#4939).

    The same open as :func:`append_private`, once per journal file, then one
    ``write`` per line: record order and durability are unchanged. A new
    *path* (a renamed store) closes the old fd first. Windows opens per line:
    a held handle blocks the in-process replay's rename and delete of a store.
    """
    if os.name == "nt":
        append_private(path, text)
        return
    held = getattr(owner, "_held_journal", None)
    if held is None or held[0] != path:
        close_held(owner)
        fd = open_private_append(path)
        # Closed with its owner too: a journal dropped without discard() leaks nothing.
        held = owner._held_journal = (path, fd, weakref.finalize(owner, _close_quietly, fd))
    os.write(held[1], text.encode("utf-8"))


def close_held(owner: Any) -> None:
    """Close the journal fd *owner* holds, if any."""
    held = getattr(owner, "_held_journal", None)
    owner._held_journal = None
    if held is not None:
        held[2]()


def _close_quietly(fd: int) -> None:
    with contextlib.suppress(OSError):
        os.close(fd)


class HomeGroupWritable(ValueError):
    """Only APM_HOME's group write fails the privacy check (#5385).

    The store itself is private; once the user removes that group write
    (``chmod g-w``), the same store replays, so it is never retained.
    """

    def __init__(self, home: Path) -> None:
        super().__init__(HOME_GROUP_WRITABLE)
        self.home = home


HOME_GROUP_WRITABLE = (
    "APM_HOME is group writable and its group cannot be shown private to this user"
)


def require_private_store(store: Path, *, allow_group_home: bool = False) -> None:
    """Raise ``ValueError`` unless the store chain is private to this user.

    The install-snapshots directory and the store must each be a real directory and the
    journal a regular file (never a link), on POSIX owned by this user with no group or
    other write. APM_HOME may be a link (a dotfiles home): its target must be a directory
    this user owns, never other writable, and group writable only when its group is
    private to this user (:func:`_user_private_group`, #5313). When that group write is
    the only failure, :class:`HomeGroupWritable` is raised instead (#5385), unless
    *allow_group_home* (reading a store's paths to report them, never to restore).
    """
    home = (store.parent.parent, os.stat, stat.S_ISDIR, 0o002)
    dirs = [(path, os.lstat, stat.S_ISDIR, 0o022) for path in (store.parent, store)]
    journal = (store / _MANIFEST, os.lstat, stat.S_ISREG, 0o022)
    group_writable_home: Path | None = None
    for path, read, is_kind, forbidden in (home, *dirs, journal):
        try:
            st = read(path)
        except FileNotFoundError:
            continue
        if not is_kind(st.st_mode):
            raise ValueError(NOT_PRIVATE)
        if os.name != "nt" and (st.st_uid != os.geteuid() or st.st_mode & forbidden):
            raise ValueError(NOT_PRIVATE)
        if os.name != "nt" and st.st_mode & 0o020 and not _user_private_group(st.st_gid):
            group_writable_home = path  # only APM_HOME may get here group writable
    if group_writable_home is not None and not allow_group_home:
        raise HomeGroupWritable(group_writable_home)


def _user_private_group(gid: int) -> bool:
    """Whether group *gid* is verifiably private to this user (#5313, #5371).

    It is this user's primary group, lists no other member, and is no other
    account's primary group. Membership evidence must be complete: an account
    enumeration counts only when :func:`_local_account_databases` holds and it
    lists this user, since NSS may return an incomplete or empty list without an
    error. Any lookup that fails or is unavailable answers ``False`` (fail
    closed). Cost, paid only for a group-writable APM_HOME on replay: one small
    file read, one group lookup and one enumeration of local account files.
    """
    try:
        import grp
        import pwd

        me = pwd.getpwuid(os.geteuid())
        if me.pw_gid != gid or not _local_account_databases():
            return False
        if set(grp.getgrgid(gid).gr_mem) - {me.pw_name}:
            return False
        accounts = pwd.getpwall()
        if not any(account.pw_uid == me.pw_uid for account in accounts):
            return False  # the enumeration does not even list this user
        return not any(
            account.pw_gid == gid and account.pw_uid != me.pw_uid for account in accounts
        )
    except (ImportError, KeyError, OSError, UnicodeError):
        return False


# The one name service source whose account enumeration is complete: local files.
# systemd's iterator may end early on an error after returning entries (#5371).
_COMPLETE_SOURCES = frozenset({"files"})


def _local_account_databases() -> bool:
    """Whether account and group membership come only from local files.

    The passwd and group databases, and initgroups (supplementary memberships;
    glibc falls back to group when it is not named), must list only ``files``.
    A missing or unreadable name service switch configuration (macOS reads its
    directory service instead), a database it does not name, any other source
    (``systemd``, ``sss``, ``ldap``, ``nis``, ``compat``) or an action such as
    ``[NOTFOUND=return]`` answers ``False``.
    """
    from apm_cli.utils.name_service import database_sources

    sources = database_sources()
    if sources is None:
        return False
    databases = {
        "passwd": sources.get("passwd"),
        "group": sources.get("group"),
        "initgroups": sources.get("initgroups", sources.get("group")),
    }
    return all(listed and set(listed) <= _COMPLETE_SOURCES for listed in databases.values())


def read_private_journal(path: Path, *, first_line: bool = False) -> str:
    """Read the journal *path* without following a link or blocking on a FIFO.

    The opened file itself must be a regular file this user owns with no group or other
    write; anything else raises ``ValueError(NOT_PRIVATE)`` before a byte is read.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags | getattr(os, "O_BINARY", 0))
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):  # a link (EMLINK on FreeBSD)
            raise ValueError(NOT_PRIVATE) from None
        raise
    with os.fdopen(fd, "rb") as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode) or (
            os.name != "nt" and (st.st_uid != os.geteuid() or st.st_mode & 0o022)
        ):
            raise ValueError(NOT_PRIVATE)
        data = handle.readline() if first_line else handle.read()
    return data.decode("utf-8")


def for_destination(path: Path, mode: int | dict[str, int], copy: Path) -> int | dict[str, int]:
    """The mode to give a restore: each path recreated from absence is capped by the umask.

    A directory that still exists may have lost children (a removal killed partway);
    those children are recreated too, so each entry is judged on its own.
    """
    if os.name == "nt":
        return mode
    from apm_cli.cache.deploy_store import process_umask

    umask = process_umask()  # the process's umask, read once
    if isinstance(mode, int):
        return mode if os.path.lexists(path) else mode & _NEW_FILE & ~umask
    return {
        rel: bits
        if os.path.lexists(path / rel if rel else path)
        else bits & (_NEW_DIR if not rel or (copy / rel).is_dir() else _NEW_FILE) & ~umask
        for rel, bits in mode.items()
    }
