"""Exact ``Path.resolve()`` on Windows with fewer kernel opens.

``ntpath.realpath`` opens every path twice (``_getfinalpathname`` on the
path, then again on its ``\\\\?\\``-stripped form) and ``Path.resolve`` adds
a ``stat``. On a cold install that is about a third of the CPU. Two exact
forms replace it; anything they cannot prove falls back to ``Path.resolve``:

* Form B: one ``_getfinalpathname`` call, the one CPython makes first. The
  second call only decides whether the prefix may be stripped, and for a
  plain Win32 path (drive root, under ``MAX_PATH``, no 8.3 name, trailing dot
  or space, ADS, device name or non-ASCII text) the stripped form names the
  same object by construction.
* Form A: a walk down from a HELD directory. Inside one CLI invocation the
  project root and the package directories that activation placed are held
  open without ``FILE_SHARE_DELETE``, so neither they nor their ancestors can
  be renamed or deleted while held; their final paths are therefore fixed.
  Each component below the held directory is proven with one
  ``FindFirstFileExW``: it must exist under exactly the given long name and
  must not be a reparse point. Directories proven inside a registered
  package are held in turn, so later lookups cost one ``FindFirstFileExW``.

Only the invocation scope (``core.invocation_memo``) enables holds, and its
exit closes them. APM releases the holds under a path before it renames or
deletes there (:func:`release_held`), and before user code runs.

Paths with a volatile component (``.git``, staging, ``.inc.``, ``.tmp``,
backups) always take ``Path.resolve``: APM deletes and renames there, and a
delete-pending entry is still listed by ``FindFirstFileExW`` but cannot be
opened, which changes ``Path.resolve``'s answer.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path, WindowsPath

ENABLED = sys.platform == "win32"
# Compute Path.resolve() too and raise on any difference (CI differential only).
_DIFFERENTIAL = True

_STOCK_RESOLVE = WindowsPath.resolve
_PREFIX = "\\\\?\\"
_UNC_PREFIX = _PREFIX + "UNC\\"
_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
    + ["COM\u00b9", "COM\u00b2", "COM\u00b3", "LPT\u00b9", "LPT\u00b2", "LPT\u00b3"]
)
_BAD_CHARS = frozenset('<>"|?*:/') | frozenset(chr(c) for c in range(32))
_SHORT_NAME = re.compile(r"~[0-9]")
_VOLATILE_MARKERS = ("staging", ".inc.", ".tmp", "backup", "migration", "refresh")
_MAX_PATH = 260
_MAX_MISSING_DEPTH = 32

_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_READ_ATTRIBUTES = 0x80
_FILE_SHARE_READ_WRITE = 0x1 | 0x2  # never FILE_SHARE_DELETE: the hold pins the name
_OPEN_EXISTING = 3
_FLAGS_DIR_NO_FOLLOW = 0x02000000 | 0x00200000  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
_ERROR_NOT_FOUND = (2, 3)  # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND


def _plain_component(part: str) -> bool:
    """A name Win32 maps to itself: no 8.3 alias, trailing dot/space, ADS or device."""
    if not part or part[-1] in ". " or not part.isascii():
        return False
    if any(ch in _BAD_CHARS for ch in part):
        return False
    if part.split(".", 1)[0].rstrip(" ").upper() in _RESERVED:
        return False
    return _SHORT_NAME.search(part) is None


def plain_win32(path: str) -> bool:
    """Whether Win32 parses *path* to the same NT path as its ``\\\\?\\`` form."""
    if len(path) >= _MAX_PATH or len(path) < 3 or path[1:3] != ":\\" or not path[0].isalpha():
        return False
    rest = path[3:]
    return all(_plain_component(part) for part in rest.split("\\")) if rest else True


def _volatile(path: str) -> bool:
    for part in path.lower().split("\\"):
        if part == ".git" or any(marker in part for marker in _VOLATILE_MARKERS):
            return True
    return False


def _strip_prefix(final: str) -> str | None:
    if not final.startswith(_PREFIX) or final.startswith(_UNC_PREFIX):
        return None
    stripped = final[len(_PREFIX) :]
    return stripped if plain_win32(stripped) else None


_DEVICE: list[tuple[str, str]] = []  # (NT device name, drive) of the held root's volume


def _final_path(text: str) -> str:
    """``nt._getfinalpathname(text)``, naming the volume by its NT device when known.

    Same open as CPython (no access, no sharing, backup semantics). The DOS
    volume name costs a mount-manager query per call; the NT device name
    does not. On the held root's device the DOS name is the root's drive
    (learned once from the root's own DOS final path); any other device
    takes CPython's call.
    """
    import nt  # type: ignore[import-not-found]

    if not _DEVICE:
        return nt._getfinalpathname(text)
    device, drive = _DEVICE[0]
    ctypes, k32, _find, _info, invalid = _kernel32()
    handle = k32.CreateFileW(text, 0, 0, None, _OPEN_EXISTING, 0x02000000, None)
    if not handle or handle == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(1024)
    try:
        size = k32.GetFinalPathNameByHandleW(handle, buffer, 1024, 0x2)  # VOLUME_NAME_NT
    finally:
        k32.CloseHandle(handle)
    value = buffer.value
    if 0 < size < 1024 and value.startswith(device + "\\"):
        return f"{_PREFIX}{drive}{value[len(device) :]}"
    return nt._getfinalpathname(text)


def _learn_device(handle: int, final: str) -> None:
    """Record the held root's NT device name and drive (``final`` is its DOS path)."""
    ctypes, k32 = _kernel32()[:2]
    buffer = ctypes.create_unicode_buffer(1024)
    size = k32.GetFinalPathNameByHandleW(handle, buffer, 1024, 0x2)  # VOLUME_NAME_NT
    rest = final[2:]
    value = buffer.value
    if 0 < size < 1024 and len(rest) > 1 and value.endswith(rest):
        device = value[: -len(rest)]
        if device.startswith("\\Device\\") and "\\" not in device[len("\\Device\\") :]:
            _DEVICE[:] = [(device, final[:2])]


def _form_b(text: str) -> str | None:
    """``ntpath.realpath(text)`` with one ``_getfinalpathname``, or ``None``."""

    try:
        final = _final_path(text)
    except OSError as exc:
        if getattr(exc, "winerror", None) in _ERROR_NOT_FOUND:
            return _form_b_missing(text)
        return None  # delete-pending, access errors: Path.resolve decides
    except ValueError:
        return None
    return _strip_prefix(final)


def _form_b_missing(text: str) -> str | None:
    """``ntpath.realpath`` of a path whose tail does not exist: final ancestor + tail.

    CPython's non-strict walk splits off names it cannot open and joins them
    to the first ancestor ``_getfinalpathname`` resolves; the strip check then
    fails with the same error as the first call, so the prefix goes. It also
    follows a name that exists but cannot be opened (a dangling link): each
    split-off name must be absent under ``lstat`` here, else ``None``.
    """

    tail: list[str] = []
    current = text
    for _ in range(_MAX_MISSING_DEPTH):
        try:
            os.lstat(current)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in _ERROR_NOT_FOUND:
                return None
        else:
            return None  # it exists but could not be opened: Path.resolve decides
        parent, _, name = current.rpartition("\\")
        if len(parent) < 2 or not name:
            return None
        if len(parent) == 2:
            parent += "\\"  # "C:" -> the drive root
        tail.append(name)
        try:
            final = _final_path(parent)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in _ERROR_NOT_FOUND or len(parent) == 3:
                return None
            current = parent
            continue
        except ValueError:
            return None
        base = _strip_prefix(final)
        if base is None:
            return None
        joined = "\\".join([base.rstrip("\\"), *reversed(tail)])
        return joined if plain_win32(joined) else None
    return None


# --------------------------------------------------------------------------
# Held directories (form A)
# --------------------------------------------------------------------------

_lock = threading.RLock()
_held: dict[str, tuple[str, int]] = {}  # exact text -> (final path, handle)
_registered: set[str] = set()  # lower-cased package roots placed by activation
_root: list[str | None] = []  # [] not tried yet; [None] unavailable; [text] held
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

        class _HandleInfo(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("created", wintypes.FILETIME),
                ("accessed", wintypes.FILETIME),
                ("written", wintypes.FILETIME),
                ("volume", wintypes.DWORD),
                ("size_hi", wintypes.DWORD),
                ("size_lo", wintypes.DWORD),
                ("links", wintypes.DWORD),
                ("index_hi", wintypes.DWORD),
                ("index_lo", wintypes.DWORD),
            ]

        k32.GetFileInformationByHandle.restype = wintypes.BOOL
        k32.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(_HandleInfo))

        class _Find(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("created", wintypes.FILETIME),
                ("accessed", wintypes.FILETIME),
                ("written", wintypes.FILETIME),
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
        _K32 = (ctypes, k32, _Find, _HandleInfo, ctypes.c_void_p(-1).value)
    return _K32


_MISSING = object()


def _entry(path: str):
    """``(attributes, on-disk long name)`` of *path*, ``_MISSING``, or ``None``."""
    ctypes, k32, find_type, _info, invalid = _kernel32()
    data = find_type()
    handle = k32.FindFirstFileExW(path, 1, ctypes.byref(data), 0, None, 0)  # FindExInfoBasic
    if not handle or handle == invalid:
        return _MISSING if ctypes.get_last_error() in _ERROR_NOT_FOUND else None
    k32.FindClose(handle)
    return data.attributes, data.name


def _close(handle: int) -> None:
    _kernel32()[1].CloseHandle(handle)


def _hold(directory: str) -> tuple[str, int] | None:
    """Open *directory* itself (no reparse follow, no FILE_SHARE_DELETE).

    Returns ``(final path, handle)`` when the object is a plain directory and
    its final path passes the same strip rules as form B; else ``None``.
    """
    ctypes, k32, _find, info_type, invalid = _kernel32()
    handle = k32.CreateFileW(
        directory,
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ_WRITE,
        None,
        _OPEN_EXISTING,
        _FLAGS_DIR_NO_FOLLOW,
        None,
    )
    if not handle or handle == invalid:
        return None
    info = info_type()
    buffer = ctypes.create_unicode_buffer(1024)
    if (
        not k32.GetFileInformationByHandle(handle, ctypes.byref(info))
        or not info.attributes & _FILE_ATTRIBUTE_DIRECTORY
        or info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        or not 0 < k32.GetFinalPathNameByHandleW(handle, buffer, 1024, 0) < 1024
    ):
        k32.CloseHandle(handle)
        return None
    final = _strip_prefix(buffer.value)
    if final is None:
        k32.CloseHandle(handle)
        return None
    return final, handle


def _holds_allowed() -> bool:
    from apm_cli.core.invocation_memo import invocation_active

    return invocation_active()


def _registered_cover(text: str) -> bool:
    """Whether *text* is at or below a package root that activation registered."""
    if not _registered:
        return False
    lowered = text.lower()
    index = lowered.find("\\", 3)
    while index != -1:
        if lowered[:index] in _registered:
            return True
        index = lowered.find("\\", index + 1)
    return lowered in _registered


def _root_text() -> str | None:
    """The held project root (cwd), or ``None``.

    The root is held only when its final path equals its text: no reparse
    point or 8.3 alias above it, so every text below it names the object the
    walk proves.
    """
    with _lock:
        if _root:
            return _root[0]
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = ""
        held = _hold(cwd) if plain_win32(cwd) and not _volatile(cwd) else None
        if held is not None and held[0] != cwd:
            _close(held[1])
            held = None
        if held is None:
            _root.append(None)
            return None
        _held[cwd] = held
        _root.append(cwd)
        _learn_device(held[1], held[0])
        return cwd


def _deepest_held(text: str, root: str) -> tuple[str, str]:
    with _lock:
        held = _held.get(text)
        if held is not None:
            return text, held[0]
        index = len(text)
        while True:
            index = text.rfind("\\", len(root), index)
            if index <= len(root):
                return root, _held[root][0]
            held = _held.get(text[:index])
            if held is not None:
                return text[:index], held[0]


def _adopt(directory: str, proven: str) -> None:
    """Hold *directory* when it is inside a registered package and still proven."""
    held = _hold(directory)
    if held is None:
        return
    with _lock:
        keep = (
            held[0] == proven
            and directory not in _held
            and _registered_cover(directory)
            and _holds_allowed()
        )
        if keep:
            _held[directory] = held
    if not keep:
        _close(held[1])


def _held_realpath(text: str) -> str | None:
    """Form A, or ``None`` when *text* is outside the held tree or unproven."""
    if not _holds_allowed():
        return None
    root = _root_text()
    if root is None or not text.startswith(root):
        return None
    if len(text) > len(root) and text[len(root)] != "\\":
        return None
    try:
        anchor, resolved = _deepest_held(text, root)
    except KeyError:
        return None  # the root was released meanwhile
    if anchor == text:
        return resolved
    parts = text[len(anchor) + 1 :].split("\\")
    inside_package = _registered_cover(text)
    if len(parts) > 1 and not inside_package:
        return None  # nothing below would be held: form B's one open is cheaper
    current = anchor
    for position, part in enumerate(parts):
        candidate = f"{current}\\{part}"
        entry = _entry(candidate)
        if entry is None:
            return None
        if entry is _MISSING:
            joined = "\\".join([resolved.rstrip("\\"), *parts[position:]])
            return joined if plain_win32(joined) else None
        attributes, name = entry
        if name != part or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            return None  # case or long-name drift, or a reparse point: Path.resolve decides
        resolved = f"{resolved.rstrip(chr(92))}\\{name}"
        current = candidate
        if inside_package and attributes & _FILE_ATTRIBUTE_DIRECTORY:
            _adopt(candidate, resolved)
    return resolved


def register_held_root(package_dir: Path, modules_dir: Path | None) -> None:
    """Let the walk hold *package_dir* (just placed by activation) for this invocation.

    Only a plain path strictly inside *modules_dir* registers; the walk still
    proves every component from the held project root before holding any.
    """
    if not ENABLED or modules_dir is None or not _holds_allowed():
        return
    text, modules = os.path.abspath(package_dir), os.path.abspath(modules_dir)
    if not plain_win32(text) or _volatile(text):
        return
    if not text.lower().startswith(modules.lower().rstrip("\\") + "\\"):
        return
    with _lock:
        _registered.add(text.lower())


def release_held(under: object = None) -> None:
    """Close the holds at or below *under* (``None``: all of them).

    Call before APM renames or deletes *under*. A registered package that
    contains *under* or lies below it is unregistered too, so no thread can
    re-hold that tree before the operation runs.
    """
    if not ENABLED or not (_held or _registered or _root):
        return
    with _lock:
        if under is None:
            taken = list(_held.values())
            _held.clear()
            _registered.clear()
            _root.clear()
            _DEVICE.clear()
        else:
            try:
                base = os.path.abspath(os.fspath(under)).lower().rstrip("\\")  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return
            dropped = {
                root
                for root in _registered
                if root == base or root.startswith(base + "\\") or base.startswith(root + "\\")
            }
            _registered.difference_update(dropped)
            prefixes = (base, *dropped)
            keys = [
                key
                for key in _held
                if any(key.lower() == p or key.lower().startswith(p + "\\") for p in prefixes)
            ]
            taken = [_held.pop(key) for key in keys]
            if _root and _root[0] is not None and _root[0] in keys:
                _root[:] = [None]  # APM is changing the root itself: never re-hold it
    for _final, handle in taken:
        _close(handle)


def held_count() -> int:
    """Open holds (cells assert this returns to 0 at command end)."""
    with _lock:
        return len(_held)


def resolve_windows(path: Path) -> Path | None:
    """``path.resolve()`` for a stock ``WindowsPath`` when provable, else ``None``."""
    if type(path) is not WindowsPath or WindowsPath.resolve is not _STOCK_RESOLVE:
        return None
    text = str(path)
    if not plain_win32(text) or _volatile(text):
        return None
    try:
        resolved = _held_realpath(text) or _form_b(text)
    except (OSError, ValueError):
        resolved = None
    if resolved is None:
        return None
    fast = WindowsPath(resolved)
    if _DIFFERENTIAL:
        stock = path.resolve()
        if str(stock) != str(fast):
            raise RuntimeError(f"windows realpath differential: {fast!s} != {stock!s}")
    return fast
