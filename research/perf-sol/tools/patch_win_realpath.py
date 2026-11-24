from pathlib import Path

p = Path("src/apm_cli/utils/realpath.py")
s = p.read_text()
# drop the ceiling hack (research-only) if present
marker = "\n\n# PERF RESEARCH CEILING (never ships)"
if marker in s:
    s = s[: s.index(marker)] + "\n"

old = '''def resolve_path(path: Path) -> Path:'''
new = '''_WIN_FAST = sys.platform == "win32"
# PERF PROTOTYPE: compute Path.resolve() too and fail loudly on any difference.
_WIN_DIFFERENTIAL = True
_WIN_PREFIX = "\\\\\\\\?\\\\"
_WIN_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
    + ["COM\\u00b9", "COM\\u00b2", "COM\\u00b3", "LPT\\u00b9", "LPT\\u00b2", "LPT\\u00b3"]
)
_WIN_BAD_CHARS = frozenset('<>"|?*:/') | frozenset(chr(c) for c in range(32))
_WIN_MISSING = (2, 3)  # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND


def _plain_win32(path: str) -> bool:
    """Whether Win32 parses *path* (``X:\\\\a\\\\b``) to the same NT path as ``\\\\\\\\?\\\\`` + path.

    Then ``ntpath.realpath``'s second ``_getfinalpathname`` (the check that
    stripping the prefix is safe) is proven by construction.
    """
    if len(path) >= 260 or len(path) < 3 or path[1:3] != ":\\\\" or not path[0].isalpha():
        return False
    for part in path[3:].split("\\\\"):
        if not part or part[-1] in ". " or any(ch in _WIN_BAD_CHARS for ch in part):
            return False
        if part.split(".", 1)[0].rstrip(" ").upper() in _WIN_RESERVED:
            return False
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
    except OSError as exc:
        if getattr(exc, "winerror", None) not in _WIN_MISSING:
            return None
        try:
            os.lstat(text)  # a dangling link exists under lstat: slow path
        except FileNotFoundError:
            pass
        except OSError:
            return None
        else:
            return None
        parent, _, name = text.rpartition("\\\\")
        if not name or len(parent) < 3:
            return None
        resolved_parent = _win_realpath(parent, depth + 1)
        if resolved_parent is None:
            return None
        return f"{resolved_parent.rstrip(chr(92))}\\\\{name}"
    if not final.startswith(_WIN_PREFIX) or final.startswith(_WIN_PREFIX + "UNC\\\\"):
        return None
    stripped = final[len(_WIN_PREFIX) :]
    return stripped if _plain_win32(stripped) else None


def _resolve_windows(path: Path) -> Path | None:
    from pathlib import WindowsPath

    if type(path) is not WindowsPath or WindowsPath.resolve is not _STOCK_RESOLVE:
        return None
    text = str(path)
    try:
        resolved = _win_realpath(text)
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


def resolve_path(path: Path) -> Path:'''
assert old in s
s = s.replace(old, new, 1)

old = '''            if resolved is not None:
                return type(path)(resolved)
    return path.resolve()'''
new = '''            if resolved is not None:
                return type(path)(resolved)
    if _WIN_FAST:
        fast = _resolve_windows(path)
        if fast is not None:
            return fast
    return path.resolve()'''
assert old in s
s = s.replace(old, new, 1)
p.write_text(s)
print("patched")
