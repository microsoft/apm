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
point, a ``..`` anywhere, a case-variant name on Windows, or any doubt takes
the full resolution.
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
_LISTINGS = "realpath-directory-names"
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


def _exact_name(head: str, name: str) -> bool:
    """Windows: whether *head* holds *name* in exactly this case (resolve reports disk case)."""
    listings = _memo(_LISTINGS)
    if listings is None:
        return False
    names = listings.get(head)
    if names is None:
        try:
            names = frozenset(os.listdir(head))
        except OSError:
            return False
        listings[head] = names
    return name in names


def _proven_answer(text: str, proofs: dict) -> str | None:
    """The resolved form of *text* when its directory is proven link-free, else ``None``."""
    if ".." in text.replace("\\", "/").split("/"):
        return None
    absolute = os.path.abspath(text)
    head, name = os.path.split(absolute)
    if not name or head not in proofs:
        return None
    try:
        info = os.lstat(absolute)
    except FileNotFoundError:
        return absolute
    except OSError:
        return None
    if _leaf_is_link(info) or (_WINDOWS and not _exact_name(head, name)):
        return None
    if stat.S_ISDIR(info.st_mode):
        proofs[absolute] = True
    return absolute


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
    """Return ``path.resolve()``, computed in the kernel when that is exact.

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
    resolved = _resolve(path, text)
    if proofs is not None and ".." not in text.replace("\\", "/").split("/"):
        absolute = os.path.abspath(text)
        if str(resolved) == absolute:
            # Resolution kept the exact text: no component up to the leaf is a link.
            head = os.path.dirname(absolute)
            if head and head != absolute:
                proofs[head] = True
    return resolved


def _resolve(path: Path, text: str) -> Path:
    if _FAST and type(path) is PosixPath and PosixPath.resolve is _STOCK_RESOLVE:
        if text.startswith("/") and not text.startswith("//"):
            try:
                resolved = _kernel_realpath(text)
            except (OSError, ValueError):
                resolved = None
            if resolved is not None:
                return type(path)(resolved)
    return path.resolve()
