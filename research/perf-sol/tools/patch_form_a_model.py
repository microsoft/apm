from pathlib import Path

p = Path("src/apm_cli/utils/realpath.py")
s = p.read_text()

old = '''def _resolve_windows(path: Path) -> Path | None:'''
new = '''# PERF RESEARCH (form A cost model, never ships as is): each parent directory is
# resolved once per command (the cost of opening and holding its handle, W3d
# A1-A3) and every later path under it costs one lstat of the leaf + a
# reparse-point check. The faithful form holds the parent handle without
# FILE_SHARE_DELETE so the cached parent cannot be swapped; this model does not.
_HELD: dict[str, str] = {}
_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def _held_realpath(text: str) -> str | None:
    parent, _, name = text.rpartition("\\\\")
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
    joined = f"{held.rstrip(chr(92))}\\\\{name}"
    return joined if _plain_win32(joined) else None


def _resolve_windows(path: Path) -> Path | None:'''
assert old in s
s = s.replace(old, new, 1)

old = '''    text = str(path)
    try:
        resolved = _win_realpath(text)
    except (OSError, ValueError):
        resolved = None'''
new = '''    text = str(path)
    try:
        resolved = _held_realpath(text) or _win_realpath(text)
    except (OSError, ValueError):
        resolved = None'''
assert old in s
s = s.replace(old, new, 1)
p.write_text(s)
print("patched")
