from pathlib import Path

p = Path("src/apm_cli/utils/realpath.py")
s = p.read_text()

old = s[s.index("# PERF RESEARCH (form A cost model"): s.index("def _resolve_windows(path: Path) -> Path | None:")]
new = '''# PERF PROTOTYPE (form A, W3d A1-A3): a parent directory under the project root
# is opened ONCE per command and its handle HELD without FILE_SHARE_DELETE, so
# neither it nor any ancestor can be renamed or replaced while APM runs (NTFS
# refuses to rename a directory with an open handle in its subtree). Its final
# path is read from that handle; every later path under it costs one lstat of
# the leaf plus a reparse-point check. Directories APM itself renames or deletes
# (apm_modules staging, caches outside the project, .git) are never held (A7)
# and keep form (B).
_HELD: dict[str, str] = {}
_HANDLES: list[int] = []
_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
_NOT_HELD = ("\\\\apm_modules\\\\", "\\\\.git\\\\", "\\\\node_modules\\\\")
_PROJECT: list[str] = []
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

        class _Info(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("c", wintypes.FILETIME),
                ("a", wintypes.FILETIME),
                ("w", wintypes.FILETIME),
                ("serial", wintypes.DWORD),
                ("size_hi", wintypes.DWORD),
                ("size_lo", wintypes.DWORD),
                ("links", wintypes.DWORD),
                ("index_hi", wintypes.DWORD),
                ("index_lo", wintypes.DWORD),
            ]

        k32.GetFileInformationByHandle.restype = wintypes.BOOL
        k32.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Info))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        _K32 = (ctypes, k32, _Info)
    return _K32


def _hold(directory: str) -> str | None:
    """Open and hold *directory* (no FILE_SHARE_DELETE); its final path, or ``None``."""
    ctypes, k32, info_type = _kernel32()
    handle = k32.CreateFileW(
        directory,
        0x80,  # FILE_READ_ATTRIBUTES
        0x1 | 0x2,  # FILE_SHARE_READ | FILE_SHARE_WRITE: no FILE_SHARE_DELETE (A1/A2)
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT (A3)
        None,
    )
    if not handle or handle == ctypes.c_void_p(-1).value:
        return None
    info = info_type()
    buffer = ctypes.create_unicode_buffer(1024)
    if (
        not k32.GetFileInformationByHandle(handle, ctypes.byref(info))
        or info.attributes & _REPARSE
        or not info.attributes & 0x10  # FILE_ATTRIBUTE_DIRECTORY
        or not 0 < k32.GetFinalPathNameByHandleW(handle, buffer, 1024, 0) < 1024
    ):
        k32.CloseHandle(handle)
        return None
    final = buffer.value
    if not final.startswith(_WIN_PREFIX) or final.startswith(_WIN_PREFIX + "UNC\\\\"):
        k32.CloseHandle(handle)
        return None
    stripped = final[len(_WIN_PREFIX) :]
    if not _plain_win32(stripped):
        k32.CloseHandle(handle)
        return None
    _HANDLES.append(handle)  # held for the rest of the command
    return stripped


def _holdable(parent: str) -> bool:
    if not _PROJECT:
        _PROJECT.append(os.getcwd().lower().rstrip("\\\\") + "\\\\")
    lowered = parent.lower() + "\\\\"
    return lowered.startswith(_PROJECT[0]) and not any(part in lowered for part in _NOT_HELD)


def _held_realpath(text: str) -> str | None:
    parent, _, name = text.rpartition("\\\\")
    if not name or len(parent) < 3 or not _plain_win32(text) or not _holdable(parent):
        return None
    key = parent.lower()
    held = _HELD.get(key)
    if held is None:
        held = _hold(parent)
        if held is None:
            return None
        _HELD[key] = held
    try:
        info = os.lstat(text)
    except FileNotFoundError:
        pass  # a missing leaf joins its held parent, as realpath(strict=False)
    except OSError:
        return None
    else:
        if getattr(info, "st_file_attributes", 0) & _REPARSE:
            return None
    joined = f"{held.rstrip(chr(92))}\\\\{name}"
    return joined if _plain_win32(joined) else None


'''
s = s.replace(old, new, 1)
p.write_text(s)
print("patched")
