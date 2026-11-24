"""Exact, kernel-backed ``Path.resolve()`` for hot containment checks.

CPython resolves a path in Python: one ``lstat`` per component plus the
string work around it, then one more ``stat``. On Linux the kernel can do
the whole walk in one ``open(O_PATH)`` and report the result through
``/proc/self/fd``. :func:`resolve_path` returns exactly what
``Path.resolve()`` returns, and falls back to it whenever the fast form
cannot prove that (another OS, no ``/proc``, a dangling link, a loop, an
access error).

Inside one command, a directory whose resolved form is its own exact text has
no link anywhere on its path (W3d ``containment-once`` K1-K9). Such a proof is
kept in the command's invocation memo, which user code (a lifecycle script)
clears, and lets a path directly under a proven directory resolve with one
``lstat`` of its leaf instead of a walk. A leaf that is a link or a reparse
point, a ``..`` anywhere, a volatile component (``.git``, staging, ``.inc.``,
``.tmp``, backups), a case-variant name on Windows, or any doubt takes the full
resolution. APM drops the proofs under a path before it renames or deletes there
(:func:`forget_proofs`, called by ``realpath_win.release_held``). On Windows the
exact forms live in :mod:`apm_cli.utils.realpath_win`.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path, PosixPath, WindowsPath

_O_PATH = getattr(os, "O_PATH", 0)
_FAST = sys.platform.startswith("linux") and bool(_O_PATH)
_OPEN_FLAGS = _O_PATH | getattr(os, "O_CLOEXEC", 0)
_MISSING = (errno.ENOENT, errno.ENOTDIR)
_MAX_MISSING_DEPTH = 32
_PROC_FD = "/proc/self/fd/"
_STOCK_RESOLVE = Path.resolve
_PROOFS = "realpath-link-free-directories"
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


def _windows_leaf(absolute: str, name: str) -> tuple[bool, bool] | None:
    """Windows: ``(exists, is_directory)`` of the exact leaf name, ``None`` to resolve fully.

    One ``FindFirstFileExW`` (E1 F2): the on-disk long name must equal *name*
    exactly (``resolve()`` reports the disk case) and carry no reparse point.
    Nothing is cached, so APM's own case-only renames are always seen.
    """
    from apm_cli.utils import realpath_win

    entry = realpath_win._entry(absolute)
    if entry is realpath_win._MISSING:
        return False, False
    if entry is None:
        return None
    attributes, disk_name = entry
    if disk_name != name or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return None
    return True, bool(attributes & 0x10)


_VOLATILE_MARKERS = ("staging", ".inc.", ".tmp", "backup", "migration", "refresh")


def _components(text: str) -> list[str]:
    return text.replace("\\", "/").split("/")


def _volatile(text: str) -> bool:
    """R3: APM renames and deletes under these components, so nothing there is proven."""
    for part in _components(text.lower()):
        if part == ".git" or any(marker in part for marker in _VOLATILE_MARKERS):
            return True
    return False


def _proven_answer(text: str, proofs: dict) -> str | None:
    """The resolved form of *text* when its directory is proven link-free, else ``None``."""
    if ".." in _components(text):
        return None
    absolute = os.path.abspath(text)
    if _WINDOWS and not _plain_windows(text, absolute):
        return None
    head, name = os.path.split(absolute)
    if not name or head not in proofs or _volatile(name):
        return None
    if _WINDOWS:
        leaf = _windows_leaf(absolute, name)
        if leaf is None:
            return None
        exists, is_dir = leaf
    else:
        try:
            info = os.lstat(absolute)
        except FileNotFoundError:
            return absolute
        except OSError:
            return None
        if _leaf_is_link(info):
            return None
        exists, is_dir = True, stat.S_ISDIR(info.st_mode)
    if exists and is_dir:
        proofs[absolute] = True  # an existing, non-link directory under a proven one
    return absolute


def _plain_windows(text: str, absolute: str) -> bool:
    """E1 F1: GetFullPathNameW must not have rewritten *text* (trailing dots or
    spaces, device names, short names): absolute, plain Win32, normpath-equal."""
    import ntpath

    from apm_cli.utils.realpath_win import plain_win32

    return plain_win32(absolute) and absolute == ntpath.normpath(text)


def proven_link_free(directory: Path) -> bool:
    """Whether this command proved *directory* has no link on its path (see module doc)."""
    proofs = _memo(_PROOFS)
    return proofs is not None and os.path.abspath(str(directory)) in proofs


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
            # The kernel name must reach the very object the fd holds (it
            # would not across a chroot or mount namespace boundary).
            or not os.path.samestat(os.fstat(fd), os.stat(resolved))
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


def resolve_path(path: Path) -> Path:
    """Return ``path.resolve()``, through an exact fast form when one applies.

    Only a stock ``PosixPath`` or ``WindowsPath`` takes a fast form: a subclass
    (which may override ``resolve`` or ``__fspath__``) keeps its own behavior,
    and any failure of a fast form leaves the answer (or the error) to it.
    """
    stock = type(path) in (PosixPath, WindowsPath) and type(path).resolve is _STOCK_RESOLVE
    proofs = _memo(_PROOFS) if stock else None
    text = str(path)
    if proofs is not None:
        answer = _proven_answer(text, proofs)
        if answer is not None:
            return type(path)(answer)
    resolved = _platform_resolve(path, text)
    if proofs is not None:
        _record_proof(text, str(resolved), proofs)
    return resolved


def _record_proof(text: str, resolved: str, proofs: dict) -> None:
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
        proofs[head] = True


def forget_proofs(under: object = None) -> None:
    """Drop the proofs at or below *under* (``None``: all of them).

    APM calls this (through ``realpath_win.release_held``) before it renames,
    deletes or recreates a tree, so a proof never outlives the directory it saw.
    """
    for name in (_PROOFS,):
        memo = _memo(name)
        if not memo:
            continue
        if under is None:
            memo.clear()
            continue
        try:
            base = os.path.abspath(os.fspath(under))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            memo.clear()
            continue
        prefix = base if base.endswith(os.sep) else base + os.sep
        for key in [k for k in list(memo) if k == base or k.startswith(prefix)]:
            memo.pop(key, None)


def _platform_resolve(path: Path, text: str) -> Path:
    if _FAST and type(path) is PosixPath and PosixPath.resolve is _STOCK_RESOLVE:
        if text.startswith("/") and not text.startswith("//"):
            try:
                resolved = _kernel_realpath(text)
            except (OSError, ValueError):
                resolved = None
            if resolved is not None:
                return type(path)(resolved)
    return path.resolve()


if _WINDOWS:
    # Bound once at import (E1 F3): other platforms never load the Windows forms,
    # and the proofs above run first on every platform.
    from apm_cli.utils.realpath_win import resolve_windows as _resolve_windows

    def _platform_resolve(path: Path, text: str) -> Path:
        fast = _resolve_windows(path)
        return fast if fast is not None else path.resolve()
