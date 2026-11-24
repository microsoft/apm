"""Exact, kernel-backed ``Path.resolve()`` for hot containment checks.

CPython resolves a path in Python: one ``lstat`` per component plus the
string work around it, then one more ``stat``. On Linux the kernel can do
the whole walk in one ``open(O_PATH)`` and report the result through
``/proc/self/fd``. :func:`resolve_path` returns exactly what
``Path.resolve()`` returns, and falls back to it whenever the fast form
cannot prove that (another OS, no ``/proc``, a dangling link, a loop, an
access error). Nothing is cached: every call observes the filesystem as
it is now, the same as ``Path.resolve()``. On Windows the exact forms live
in :mod:`apm_cli.utils.realpath_win`.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
from pathlib import Path, PosixPath

from apm_cli.utils.realpath_win import ENABLED as _WIN_FAST
from apm_cli.utils.realpath_win import resolve_windows as _resolve_windows

_O_PATH = getattr(os, "O_PATH", 0)
_FAST = sys.platform.startswith("linux") and bool(_O_PATH)
_OPEN_FLAGS = _O_PATH | getattr(os, "O_CLOEXEC", 0)
_MISSING = (errno.ENOENT, errno.ENOTDIR)
_MAX_MISSING_DEPTH = 32
_PROC_FD = "/proc/self/fd/"
_STOCK_RESOLVE = Path.resolve

# Whether /proc names reach the objects this process opens (they would not
# across a chroot or mount-namespace boundary) is a property of the process,
# so it is proved once, on the first kernel answer, by fstat/stat identity.
# A failed proof turns the kernel form off for the process; a forked child
# proves again.
_proof_lock = threading.Lock()
_proc_state = {"proven": False, "refused": False}


def _reset_proof() -> None:
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


# RESEARCH CEILING ONLY (perf-research/arch-containment-ceiling): lexical resolution.
_RESEARCH_LEXICAL = True


def resolve_path(path: Path) -> Path:
    """Return ``path.resolve()``, computed in the kernel when that is exact.

    Only a stock ``PosixPath`` (or ``WindowsPath``) takes a fast form: a subclass (which may
    override ``resolve`` or ``__fspath__``) keeps its own behavior, and any
    failure of the fast form leaves the answer (or the error) to it.
    """
    if _RESEARCH_LEXICAL:
        return type(path)(os.path.abspath(path))
    if (
        _FAST
        and not _proc_state["refused"]
        and type(path) is PosixPath
        and PosixPath.resolve is _STOCK_RESOLVE
    ):
        text = str(path)
        if text.startswith("/") and not text.startswith("//"):
            try:
                resolved = _kernel_realpath(text)
            except (OSError, ValueError):
                resolved = None
            if resolved is not None:
                return type(path)(resolved)
    if _WIN_FAST:
        fast = _resolve_windows(path)
        if fast is not None:
            return fast
    return path.resolve()
