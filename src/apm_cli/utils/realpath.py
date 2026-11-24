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
_WIN_DIFFERENTIAL = True
_WIN_PREFIX = "\\\\?\\"
_WIN_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
    + ["COM\u00b9", "COM\u00b2", "COM\u00b3", "LPT\u00b9", "LPT\u00b2", "LPT\u00b3"]
)
_WIN_BAD_CHARS = frozenset('<>"|?*:/') | frozenset(chr(c) for c in range(32))
_WIN_SHORT_NAME = re.compile(r"~[0-9]")


def _plain_component(part: str) -> bool:
    """W3d B2/R2: a name Win32 maps to itself (no 8.3, trailing dot/space, ADS, device)."""
    if not part or part[-1] in ". " or any(ch in _WIN_BAD_CHARS for ch in part):
        return False
    if not part.isascii():
        return False  # R2: odd Unicode (normalization/case folding) falls back
    if part.split(".", 1)[0].rstrip(" ").upper() in _WIN_RESERVED:
        return False
    return _WIN_SHORT_NAME.search(part) is None


def _plain_win32(path: str) -> bool:
    if len(path) >= 260 or len(path) < 3 or path[1:3] != ":\\" or not path[0].isalpha():
        return False
    return all(_plain_component(part) for part in path[3:].split("\\")) if path[3:] else True


def _win_realpath(text: str) -> str | None:
    """Form B: ``ntpath.realpath(text)`` with one ``_getfinalpathname``, or ``None``."""
    import nt  # type: ignore[import-not-found]

    if not _plain_win32(text):
        return None
    try:
        final = nt._getfinalpathname(text)
    except (OSError, ValueError):
        return None  # W3d B3
    if not final.startswith(_WIN_PREFIX) or final.startswith(_WIN_PREFIX + "UNC\\"):
        return None
    stripped = final[len(_WIN_PREFIX) :]
    return stripped if _plain_win32(stripped) else None


# Form A (W3d winpaths-reopen R1-R12): the project root and every directory
# verified below it are HELD (no FILE_SHARE_DELETE) for the command; a path is
# answered by walking down from its deepest held ancestor with one
# FindFirstFileExW per remaining component (on-disk long name must equal the
# text, R2; no reparse point on any component, R1). APM's own deletes/renames
# release the holds under the target first (A7); every hold is released before
# a child process (R4) and at command end (R5).
_HELD: dict[str, tuple[str, int]] = {}
_REPARSE = 0x400
_DIRECTORY = 0x10
_ROOT: list[str] = []
_NEVER_HOLD = (".git", ".inc.", ".tmp", "staging")
_K32 = None


def _kernel32():
    global _K32
    if _K32 is None:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        k32.GetFinalPathNameByHandleW.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)

        class _Find(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("c", wintypes.FILETIME),
                ("a", wintypes.FILETIME),
                ("w", wintypes.FILETIME),
                ("size_hi", wintypes.DWORD),
                ("size_lo", wintypes.DWORD),
                ("reserved0", wintypes.DWORD),
                ("reserved1", wintypes.DWORD),
                ("name", wintypes.WCHAR * 260),
                ("alt", wintypes.WCHAR * 14),
            ]

        k32.FindFirstFileExW.restype = wintypes.HANDLE
        k32.FindFirstFileExW.argtypes = (
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.POINTER(_Find),
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        k32.FindClose.argtypes = (wintypes.HANDLE,)
        _K32 = (ctypes, k32, _Find, ctypes.c_void_p(-1).value)
    return _K32


_MISSING_ENTRY = object()


def _entry(path: str):
    """``(attributes, on-disk long name)``, ``_MISSING_ENTRY``, or ``None`` (unproven)."""
    ctypes, k32, find_type, invalid = _kernel32()
    data = find_type()
    handle = k32.FindFirstFileExW(path, 1, ctypes.byref(data), 0, None, 0)  # FindExInfoBasic
    if not handle or handle == invalid:
        return _MISSING_ENTRY if ctypes.get_last_error() in (2, 3) else None
    k32.FindClose(handle)
    return data.attributes, data.name


def _hold(directory: str) -> tuple[str, int] | None:
    """Open *directory* without FILE_SHARE_DELETE; ``(final path, handle)`` or ``None``."""
    ctypes, k32, _find, invalid = _kernel32()
    handle = k32.CreateFileW(directory, 0x80, 0x1 | 0x2, None, 3, 0x02000000 | 0x00200000, None)
    if not handle or handle == invalid:
        return None
    buffer = ctypes.create_unicode_buffer(1024)
    size = k32.GetFinalPathNameByHandleW(handle, buffer, 1024, 0)
    final = buffer.value
    if (
        not 0 < size < 1024
        or not final.startswith(_WIN_PREFIX)
        or final.startswith(_WIN_PREFIX + "UNC\\")
        or not _plain_win32(final[len(_WIN_PREFIX) :])
    ):
        k32.CloseHandle(handle)
        return None
    return final[len(_WIN_PREFIX) :], handle


def _holdable(path: str) -> bool:
    """A7: nothing at or below a component APM stages, renames or deletes is held."""
    lowered = path.lower()
    return not any(marker in lowered for marker in _NEVER_HOLD)


def _root() -> tuple[str, str] | None:
    """The project root (cwd), held once (R3: its final path is the root of every proof)."""
    if not _ROOT:
        cwd = os.getcwd()
        if not _plain_win32(cwd):
            _ROOT.append("")
            return None
        held = _hold(cwd)
        if held is None:
            _ROOT.append("")
            return None
        _HELD[cwd.lower()] = held
        _ROOT.append(cwd)
    root = _ROOT[0]
    if not root or root.lower() not in _HELD:
        return None
    return root, _HELD[root.lower()][0]


def _held_realpath(text: str) -> str | None:
    if not _plain_win32(text):
        return None
    rooted = _root()
    if rooted is None:
        return None
    root, resolved = rooted
    lowered = text.lower()
    if lowered == root.lower():
        return resolved
    if not lowered.startswith(root.lower() + "\\"):
        return None  # R8: outside the registry -> form B
    parts = text[len(root) + 1 :].split("\\")
    current = root
    index = 0
    while index < len(parts):
        candidate = f"{current}\\{parts[index]}"
        held = _HELD.get(candidate.lower())
        if held is None:
            break
        current, resolved = candidate, held[0]
        index += 1
    for position in range(index, len(parts)):
        part = parts[position]
        path = f"{current}\\{part}"
        entry = _entry(path)
        if entry is None:
            return None
        if entry is _MISSING_ENTRY:
            joined = "\\".join([resolved.rstrip("\\"), *parts[position:]])
            return joined if _plain_win32(joined) else None  # R10
        attributes, name = entry
        if name != part or attributes & _REPARSE:
            return None  # R2 case/long-name drift, R1 reparse point
        resolved = f"{resolved.rstrip(chr(92))}\\{name}"
        current = path
        if attributes & _DIRECTORY and _holdable(path):
            held = _hold(path)
            if held is None:
                return None
            if held[0] != resolved:  # R3: the held object is the one proven by name
                _kernel32()[1].CloseHandle(held[1])
                return None
            _HELD[path.lower()] = held
    return resolved


def release_held(under: object = None) -> None:
    """Close the holds at or below *under* (``None``: all) before APM renames/deletes there."""
    if not _HELD:
        return
    k32 = _kernel32()[1]
    if under is None:
        keys = list(_HELD)
        _ROOT.clear()
    else:
        try:
            base = os.path.abspath(os.fspath(under)).lower().rstrip("\\")
        except TypeError:
            return
        keys = [k for k in _HELD if k == base or k.startswith(base + "\\")]
        if _ROOT and _ROOT[0].lower() in keys:
            _ROOT.clear()
    for key in keys:
        _final, handle = _HELD.pop(key)
        k32.CloseHandle(handle)


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


if _WIN_FAST:
    import atexit

    atexit.register(release_held)


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
