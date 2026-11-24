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
_DIFFERENTIAL = False

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
# Share access only counts opens with read, write or delete access: list
# access makes the hold refuse every later open that asks for DELETE.
_FILE_LIST_DIRECTORY = 0x1
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


def _form_b(text: str) -> str | None:
    """``ntpath.realpath(text)`` with one ``_getfinalpathname``, or ``None``."""
    import nt  # type: ignore[import-not-found]

    try:
        final = nt._getfinalpathname(text)
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
    import nt  # type: ignore[import-not-found]

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
            final = nt._getfinalpathname(parent)
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

        class _TagInfo(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

        k32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        k32.GetFileInformationByHandleEx.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        _TAG_INFO.append(_TagInfo)

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
_TAG_INFO: list = []  # the FILE_ATTRIBUTE_TAG_INFO structure, built with the kernel32 bindings
_FILE_ATTRIBUTE_TAG_INFO = 9


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
        _FILE_READ_ATTRIBUTES | _FILE_LIST_DIRECTORY,
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
        return cwd


_Held = tuple[str, int]  # (final path, handle); identity marks one hold's lifetime


def _deepest_held(text: str, root: str) -> tuple[str, _Held]:
    with _lock:
        held = _held.get(text)
        if held is not None:
            return text, held
        index = len(text)
        while True:
            index = text.rfind("\\", len(root), index)
            if index <= len(root):
                return root, _held[root]
            held = _held.get(text[:index])
            if held is not None:
                return text[:index], held


def _adopt(directory: str, proven: str) -> _Held | None:
    """Hold *directory* (inside a registered root, still the proven object), or ``None``.

    The open happens under the table lock, so :func:`release_held` never
    misses a handle that is being opened.
    """
    with _lock:
        existing = _held.get(directory)
        if existing is not None:
            return existing
        if not (_registered_cover(directory) and _holds_allowed()):
            return None  # released meanwhile: APM is about to move it
        held = _hold(directory)
        if held is None:
            return None
        if held[0] != proven:
            _close(held[1])
            return None
        _held[directory] = held
        return held


def _intact(held: _Held) -> bool:
    """Whether the held object is still a plain directory (call under ``_lock``).

    A hold pins the name, not the object's kind: an empty held directory can
    be turned into a junction in place. The attribute is read on the held
    handle itself; a reparse point or a failed read voids the hold.
    """
    ctypes, k32 = _kernel32()[:2]
    info = _TAG_INFO[0]()
    if not k32.GetFileInformationByHandleEx(
        held[1], _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info)
    ):
        return False
    return not info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT


def _still_held(used: list[tuple[str, _Held]]) -> bool:
    """Whether every hold an answer rests on is the same, intact hold now.

    A hold found changed in kind is dropped (closed), so only :func:`_hold`,
    which refuses reparse points, can hold that path again.
    """
    with _lock:
        for key, held in used:
            if _held.get(key) is not held:
                return False
            if not _intact(held):
                del _held[key]
                if _root and _root[0] == key:
                    _root[:] = [None]
                _close(held[1])
                return False
    return True


def _held_anchor(text: str) -> tuple[str, _Held] | None:
    """``(deepest held prefix of text, its hold)``, or ``None`` outside the held tree."""
    if not _holds_allowed():
        return None
    root = _root_text()
    if root is None or not text.startswith(root):
        return None
    if len(text) > len(root) and text[len(root)] != "\\":
        return None
    try:
        return _deepest_held(text, root)
    except KeyError:
        return None  # the root was released meanwhile


def _held_realpath(text: str) -> str | None:
    """Form A, or ``None`` when *text* is outside the held tree or unproven."""
    found = _held_anchor(text)
    if found is None:
        return None
    anchor, held = found
    if anchor == text:
        return held[0] if _still_held([(anchor, held)]) else None
    parts = text[len(anchor) + 1 :].split("\\")
    inside_package = _registered_cover(text)
    if len(parts) > 1 and not inside_package:
        return None  # nothing below would be held: form B's one open is cheaper
    used = [(anchor, held)]
    resolved = _walk_below(anchor, held[0], parts, used, adopt=inside_package)
    # A hold released (and its directory moved) mid-walk voids the proof.
    return resolved if resolved is not None and _still_held(used) else None


def _walk_below(
    anchor: str, resolved: str, parts: list[str], used: list[tuple[str, _Held]], *, adopt: bool
) -> str | None:
    """Prove *parts* below the held *anchor* one component at a time."""
    current = anchor
    for position, part in enumerate(parts):
        candidate = f"{current}\\{part}"
        entry = _entry(candidate)
        if entry is _MISSING:
            joined = "\\".join([resolved.rstrip("\\"), *parts[position:]])
            return joined if plain_win32(joined) else None
        if entry is None or entry[1] != part or entry[0] & _FILE_ATTRIBUTE_REPARSE_POINT:
            return None  # unproven, case or long-name drift, or a reparse point
        attributes, name = entry
        resolved = f"{resolved.rstrip(chr(92))}\\{name}"
        current = candidate
        if adopt and attributes & _FILE_ATTRIBUTE_DIRECTORY and _registered_cover(candidate):
            held = _adopt(candidate, resolved)
            if held is not None:
                used.append((candidate, held))
            elif position < len(parts) - 1:
                return None  # a registered directory that cannot be pinned: below is unproven
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
    re-hold that tree before the operation runs. On every platform it first drops
    this command's link-free proofs there (:func:`realpath.forget_proofs`).
    """
    from apm_cli.utils.realpath import forget_proofs

    forget_proofs(under)
    if not ENABLED or not (_held or _registered or _root):
        return
    base = None
    if under is not None:
        try:
            base = os.path.abspath(os.fspath(under)).rstrip("\\")  # type: ignore[arg-type]
        except (TypeError, ValueError):
            base = None
        if base is not None and not plain_win32(base):
            base = None  # another spelling (\\?\, UNC, 8.3) cannot be matched: release all
        base = base.lower() if base is not None else None
    with _lock:
        if base is None:
            taken = list(_held.values())
            _held.clear()
            _registered.clear()
            _root.clear()
        else:
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
        # Closed under the lock: a handle value is never queried after its close.
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
