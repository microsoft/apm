"""Exact, kernel-backed ``Path.resolve()`` for hot containment checks.

CPython resolves a path in Python: one ``lstat`` per component plus the
string work around it, then one more ``stat``. On Linux the kernel can do
the whole walk in one ``open(O_PATH)`` and report the result through
``/proc/self/fd``. :func:`resolve_path` returns exactly what
``Path.resolve()`` returns, and falls back to it whenever the fast form
cannot prove that (another OS, no ``/proc``, a dangling link, a loop, an
access error). A path the kernel proves link-free in one ``openat2``
(``RESOLVE_NO_SYMLINKS``) is its own answer.

Where no kernel form applies (macOS and other POSIX), inside one command, a directory whose resolved form is its own exact text has
no link anywhere on its path (W3d ``containment-once`` K1-K9). Such a proof is
kept in the command's invocation memo, which user code (a lifecycle script)
clears, and lets a path directly under a proven directory resolve with one
``lstat`` of its leaf instead of a walk. A leaf that is a link or a reparse
point, a ``..`` anywhere, a volatile component (``.git``, staging, ``.inc.``,
``.tmp``, backups), or any doubt takes the full resolution. APM drops the proofs
under a path before it renames or deletes there (:func:`forget_proofs`, called by
``realpath_win.release_held``). Linux (the kernel forms) and Windows (the exact forms
in :mod:`apm_cli.utils.realpath_win`) keep no proofs: measured no faster with them.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import threading
from pathlib import Path, PosixPath
from typing import Any

_O_PATH = getattr(os, "O_PATH", 0)
_FAST = sys.platform.startswith("linux") and bool(_O_PATH)
_OPEN_FLAGS = _O_PATH | getattr(os, "O_CLOEXEC", 0)
_MISSING = (errno.ENOENT, errno.ENOTDIR)
_MAX_MISSING_DEPTH = 32
_PROC_FD = "/proc/self/fd/"
_STOCK_RESOLVE = Path.resolve
_PROOFS = "realpath-link-free-directories"
# Proof generation: forget_proofs bumps it under _proof_publish; a resolver captures
# it before its lstat or platform resolve and publishes a proof only if it is still
# current, so a release_held racing the resolve can never be undone by a late publish.
_proof_publish = threading.Lock()
_generation = [0]
# Test seam: called between a resolve's observation and its publish (None in production).
_before_publish: list = []
_WINDOWS = sys.platform == "win32"
# Any reparse point (symbolic link, junction, mount point) leaves the fast form.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_memo_getter: list = []


def _memo(name: str) -> dict | None:
    """The command's invocation memo *name*, or ``None`` outside a command."""
    if not _memo_getter:
        from ..core.invocation_memo import invocation_memo

        _memo_getter.append(invocation_memo)
    return _memo_getter[0](name)


def _leaf_is_link(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


_VOLATILE_MARKERS = ("staging", ".inc.", ".tmp", "backup", "migration", "refresh")


def _components(text: str) -> list[str]:
    return text.replace("\\", "/").split("/")


def _volatile(text: str) -> bool:
    """R3: APM renames and deletes under these components, so nothing there is proven."""
    for part in _components(text.lower()):
        if part == ".git" or any(marker in part for marker in _VOLATILE_MARKERS):
            return True
    return False


def _publish(proofs: dict, key: str, seen: int) -> None:
    """Record *key* as proven unless a forget ran since generation *seen* was captured."""
    for hook in _before_publish:
        hook()
    with _proof_publish:
        if _generation[0] == seen:
            proofs[key] = True


def _proven_answer(text: str, proofs: dict, seen: int) -> str | None:
    """The resolved form of *text* when its directory is proven link-free, else ``None``."""
    if ".." in _components(text):
        return None
    absolute = os.path.abspath(text)
    head, name = os.path.split(absolute)
    if not name or head not in proofs or _volatile(name):
        return None
    try:
        info = os.lstat(absolute)
    except FileNotFoundError:
        return absolute
    except OSError:
        return None
    if _leaf_is_link(info):
        return None
    if stat.S_ISDIR(info.st_mode):
        _publish(proofs, absolute, seen)  # an existing, non-link directory under a proven one
    return absolute


def proven_link_free(directory: Path) -> bool:
    """Whether this command proved *directory* has no link on its path (see module doc)."""
    proofs = _memo(_PROOFS)
    text = str(directory)
    # abspath() collapses ``..`` lexically, which a link before it would not.
    return proofs is not None and ".." not in _components(text) and os.path.abspath(text) in proofs


# Whether /proc names reach the objects this process opens (they would not
# across a chroot or mount-namespace boundary) is a property of the process,
# so it is proved once, on the first kernel answer, by fstat/stat identity.
# A failed proof turns the kernel form off for the process; a forked child
# proves again.
_proof_lock = threading.Lock()
_proc_state = {"proven": False, "refused": False}


def _reset_proof() -> None:
    global _proof_lock
    # A thread that held the lock at fork() does not exist in the child.
    _proof_lock = threading.Lock()
    _proc_state.update(proven=False, refused=False)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_proof)


def _proc_names_agree(fd: int, resolved: str) -> bool:
    """Prove once per process that the /proc name of *fd* reaches *fd* itself."""
    with _proof_lock:
        if _proc_state["proven"]:
            return True
        if _proc_state["refused"]:
            return False
        try:
            agree = os.path.samestat(os.fstat(fd), os.stat(resolved))
        except OSError:
            agree = False
        _proc_state["proven" if agree else "refused"] = True
        return agree


def _kernel_realpath(text: str, depth: int = 0) -> str | None:
    """Resolved form of an absolute *text*, or ``None`` when unproven."""
    try:
        fd = os.open(text, _OPEN_FLAGS)
    except OSError as exc:
        if exc.errno not in _MISSING:
            return None
        return _missing_realpath(text, depth)
    try:
        resolved = os.readlink(f"{_PROC_FD}{fd}")
        if (
            not resolved.startswith("/")
            or resolved.endswith(" (deleted)")
            or "\0" in resolved
            or "\n" in resolved
            or not (_proc_state["proven"] or _proc_names_agree(fd, resolved))
        ):
            return None
    finally:
        os.close(fd)
    return resolved


def _missing_realpath(text: str, depth: int) -> str | None:
    """Resolve a path whose leaf is absent: resolved parent plus the name.

    Matches ``realpath(strict=False)``, which joins a name it cannot
    ``lstat`` without following it. A dangling link does exist under
    ``lstat``, so it is left to the slow path, which follows its target.
    """
    if depth >= _MAX_MISSING_DEPTH:
        return None
    try:
        os.lstat(text)
    except OSError as exc:
        if exc.errno not in _MISSING:
            return None
    else:
        return None
    parent, _, name = text.rpartition("/")
    if name in ("", ".", "..") or not parent:
        return None
    resolved_parent = _kernel_realpath(parent, depth + 1)
    if resolved_parent is None:
        return None
    return f"{resolved_parent.rstrip('/')}/{name}"


# openat2(2) with RESOLVE_NO_SYMLINKS (Linux 5.6+) refuses any symlink in the
# walk. When it opens a normalized absolute path (no "." or ".." parts), no
# component is a link, so the path is already its own realpath: one open and
# one close, no /proc name lookup. A link anywhere (ELOOP) or any doubt goes
# to the /proc form below. A missing tail (ENOENT/ENOTDIR) is also exact:
# every existing prefix component was walked link-free, and realpath joins
# the rest as written.
_SYS_OPENAT2 = {"x86_64": 437, "aarch64": 437, "arm64": 437}.get(
    os.uname().machine if hasattr(os, "uname") else ""
)
_RESOLVE_NO_SYMLINKS = 0x04
_AT_FDCWD = -100
_openat2_state = {"usable": _FAST and _SYS_OPENAT2 is not None}
_openat2_call: list = []


def _openat2_bind() -> Any:
    import ctypes

    class _OpenHow(ctypes.Structure):
        _fields_ = (
            ("flags", ctypes.c_uint64),
            ("mode", ctypes.c_uint64),
            ("resolve", ctypes.c_uint64),
        )

    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    how = _OpenHow(_OPEN_FLAGS, 0, _RESOLVE_NO_SYMLINKS)
    size = ctypes.sizeof(how)
    ref = ctypes.byref(how)

    def call(raw: bytes) -> tuple[int, int]:
        fd = syscall(
            ctypes.c_long(_SYS_OPENAT2), ctypes.c_int(_AT_FDCWD), raw, ref, ctypes.c_size_t(size)
        )
        return (fd, 0) if fd >= 0 else (-1, ctypes.get_errno())

    return call


def _link_free(text: str) -> bool | None:
    """True when no component of *text* is a symlink (so it is its own realpath).

    ``None`` when unproven: a link somewhere, openat2 unavailable, or an error.
    """
    if (
        not _openat2_state["usable"]
        or not text.startswith("/")
        or text.startswith("//")
        or "\0" in text
        or any(part in ("", ".", "..") for part in text[1:].split("/"))
    ):
        return None
    if not _openat2_call:
        try:
            _openat2_call.append(_openat2_bind())
        except (OSError, AttributeError, ImportError):
            _openat2_state["usable"] = False
            return None
    try:
        fd, err = _openat2_call[0](os.fsencode(text))
    except (OSError, ValueError, TypeError):
        return None
    if fd >= 0:
        try:
            return True
        finally:
            os.close(fd)
    if err in (errno.ENOSYS, errno.EPERM, errno.EINVAL, errno.E2BIG):
        _openat2_state["usable"] = False  # not offered here (old kernel, seccomp)
    return True if err in _MISSING else None


def kernel_resolves() -> bool:
    """Whether :func:`resolve_path` can take the kernel form in this process."""
    return _FAST and not _proc_state["refused"]


def resolve_path(path: Path) -> Path:
    """Return ``path.resolve()``, through an exact fast form when one applies.

    Only a stock ``PosixPath`` or ``WindowsPath`` takes a fast form: a subclass
    (which may override ``resolve`` or ``__fspath__``) keeps its own behavior,
    and any failure of a fast form leaves the answer (or the error) to it.
    """
    stock = type(path) is PosixPath and PosixPath.resolve is _STOCK_RESOLVE
    proofs = None if _WINDOWS or not stock or kernel_resolves() else _memo(_PROOFS)
    text = str(path)
    seen = _generation[0]
    if proofs is not None:
        answer = _proven_answer(text, proofs, seen)
        if answer is not None:
            return type(path)(answer)
    resolved = _platform_resolve(path, text)
    if proofs is not None:
        _record_proof(text, str(resolved), proofs, seen)
    return resolved


def _record_proof(text: str, resolved: str, proofs: dict, seen: int) -> None:
    """Prove the leaf's directory when resolution kept the exact text (R1: it must exist now)."""
    if ".." in _components(text):
        return
    absolute = os.path.abspath(text)
    head = os.path.dirname(absolute)
    if resolved != absolute or not head or head == absolute or _volatile(head):
        return
    try:
        info = os.lstat(head)
    except OSError:
        return  # a missing directory is never proven before it exists
    if stat.S_ISDIR(info.st_mode) and not _leaf_is_link(info):
        _publish(proofs, head, seen)


def forget_proofs(under: object = None) -> None:
    """Drop every proof of this command (*under* names the path APM is about to change).

    APM calls this (through ``realpath_win.release_held``) before it renames,
    deletes or recreates a tree, so a proof never outlives the directory it saw.
    All proofs go, not only those spelled below *under*: a link or case alias
    (``/alias/sub`` for ``/real/sub``) would escape a prefix match, and a dropped
    proof only costs the next resolve its full walk.
    """
    del under
    memo = _memo(_PROOFS)
    with _proof_publish:
        _generation[0] += 1
        if memo:
            memo.clear()


def _platform_resolve(path: Path, text: str) -> Path:
    if (
        _FAST
        and not _proc_state["refused"]
        and type(path) is PosixPath
        and PosixPath.resolve is _STOCK_RESOLVE
    ):
        if text.startswith("/") and not text.startswith("//"):
            if _link_free(text):
                return path
            try:
                resolved = _kernel_realpath(text)
            except (OSError, ValueError):
                resolved = None
            if resolved is not None:
                return type(path)(resolved)
    return path.resolve()


if _WINDOWS:
    # Bound once at import (E1 F3): other platforms never load the Windows forms.
    # Windows keeps no proofs (resolve_path), so every resolve lands here.
    from apm_cli.utils.realpath_win import resolve_windows as _resolve_windows

    def _platform_resolve(path: Path, text: str) -> Path:
        fast = _resolve_windows(path)
        return fast if fast is not None else path.resolve()
