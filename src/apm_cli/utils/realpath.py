"""Exact, kernel-backed ``Path.resolve()`` for hot containment checks.

CPython resolves a path in Python: one ``lstat`` per component plus the
string work around it, then one more ``stat``. On Linux the kernel can do
the whole walk in one ``open(O_PATH)`` and report the result through
``/proc/self/fd``. :func:`resolve_path` returns exactly what
``Path.resolve()`` returns, and falls back to it whenever the fast form
cannot prove that (another OS, no ``/proc``, a dangling link, a loop, an
access error). Nothing is cached: every call observes the filesystem as
it is now, the same as ``Path.resolve()``.
"""

from __future__ import annotations

import errno
import os
import re
import sys
from pathlib import Path, PosixPath

_O_PATH = getattr(os, "O_PATH", 0)
_FAST = sys.platform.startswith("linux") and bool(_O_PATH)
_OPEN_FLAGS = _O_PATH | getattr(os, "O_CLOEXEC", 0)
_MISSING = (errno.ENOENT, errno.ENOTDIR)
_MAX_MISSING_DEPTH = 32
_PROC_FD = "/proc/self/fd/"
_STOCK_RESOLVE = Path.resolve


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


_WIN_FAST = sys.platform == "win32"
# PERF PROTOTYPE: compute Path.resolve() too and fail loudly on any difference.
_WIN_DIFFERENTIAL = False
_WIN_PREFIX = "\\\\?\\"
_WIN_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
    + ["COM\u00b9", "COM\u00b2", "COM\u00b3", "LPT\u00b9", "LPT\u00b2", "LPT\u00b3"]
)
_WIN_BAD_CHARS = frozenset('<>"|?*:/') | frozenset(chr(c) for c in range(32))
_WIN_SHORT_NAME = re.compile(r"~[0-9]")


def _plain_win32(path: str) -> bool:
    """Whether Win32 parses *path* (``X:\\a\\b``) to the same NT path as ``\\\\?\\`` + path.

    Then ``ntpath.realpath``'s second ``_getfinalpathname`` (the check that
    stripping the prefix is safe) is proven by construction.
    """
    if len(path) >= 260 or len(path) < 3 or path[1:3] != ":\\" or not path[0].isalpha():
        return False
    for part in path[3:].split("\\"):
        if not part or part[-1] in ". " or any(ch in _WIN_BAD_CHARS for ch in part):
            return False
        if part.split(".", 1)[0].rstrip(" ").upper() in _WIN_RESERVED:
            return False
        if _WIN_SHORT_NAME.search(part):
            return False  # W3d B2: an 8.3 short name may alias another entry
    return True


def _win_realpath(text: str, depth: int = 0) -> str | None:
    """``ntpath.realpath(text)`` for a plain absolute *text*, or ``None`` when unproven."""
    import nt  # type: ignore[import-not-found]

    if depth >= _MAX_MISSING_DEPTH or not _plain_win32(text):
        return None
    try:
        final = nt._getfinalpathname(text)
    except ValueError:
        return None
    except OSError:
        return None  # W3d B3: a missing leaf or any error -> Path.resolve()
    if not final.startswith(_WIN_PREFIX) or final.startswith(_WIN_PREFIX + "UNC\\"):
        return None
    stripped = final[len(_WIN_PREFIX) :]
    return stripped if _plain_win32(stripped) else None


# PERF RESEARCH (form A cost model, never ships as is): each parent directory is
# resolved once per command (the cost of opening and holding its handle, W3d
# A1-A3) and every later path under it costs one lstat of the leaf + a
# reparse-point check. The faithful form holds the parent handle without
# FILE_SHARE_DELETE so the cached parent cannot be swapped; this model does not.
_HELD: dict[str, str] = {}
_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def _held_realpath(text: str) -> str | None:
    parent, _, name = text.rpartition("\\")
    if not name or len(parent) < 3 or not _plain_win32(text):
        return None
    key = parent.lower()
    held = _HELD.get(key)
    if held is None:
        held = _win_realpath(parent)
        if held is None:
            return None
        _HELD[key] = held
    try:
        info = os.lstat(text)
    except FileNotFoundError:
        pass  # a missing leaf joins its (held) parent, as realpath(strict=False)
    except OSError:
        return None
    else:
        if getattr(info, "st_file_attributes", 0) & _REPARSE:
            return None
    joined = f"{held.rstrip(chr(92))}\\{name}"
    return joined if _plain_win32(joined) else None


def _resolve_windows(path: Path) -> Path | None:
    from pathlib import WindowsPath

    if type(path) is not WindowsPath or WindowsPath.resolve is not _STOCK_RESOLVE:
        return None
    text = str(path)
    try:
        resolved = _held_realpath(text) or _win_realpath(text)
    except (OSError, ValueError):
        resolved = None
    if resolved is None:
        return None
    fast = type(path)(resolved)
    if _WIN_DIFFERENTIAL:
        stock = path.resolve()
        if str(stock) != str(fast):
            raise RuntimeError(f"PERF windows realpath differential: {fast!s} != {stock!s}")
    return fast


def resolve_path(path: Path) -> Path:
    """Return ``path.resolve()``, computed in the kernel when that is exact.

    Only a stock ``PosixPath`` takes the fast form: a subclass (which may
    override ``resolve`` or ``__fspath__``) keeps its own behavior, and any
    failure of the fast form leaves the answer (or the error) to it.
    """
    if _FAST and type(path) is PosixPath and PosixPath.resolve is _STOCK_RESOLVE:
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
