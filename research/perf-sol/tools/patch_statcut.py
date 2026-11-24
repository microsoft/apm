from pathlib import Path

DIFF = "# PERF PROTOTYPE (stat-cut): compute the original too and fail loudly on any difference.\n_STAT_DIFFERENTIAL = True\n"

# 1) content_hash._package_files: one scandir walk; DirEntry type data instead of
#    Path.is_symlink() (lstat) + Path.is_file() (stat) per item. On Windows the
#    DirEntry answers come from the directory listing itself (no syscall).
p = Path("src/apm_cli/utils/content_hash.py")
s = p.read_text()
old = '''def _package_files(
    package_path: Path, *, native: bool = False, skip_roots: tuple[str, ...] = ()
) -> list[Path]:
    """Select canonical payload paths without following publisher symlinks."""
    regular_files: list[Path] = []'''
new = DIFF + '''

def _package_files(
    package_path: Path, *, native: bool = False, skip_roots: tuple[str, ...] = ()
) -> list[Path]:
    """Select canonical payload paths without following publisher symlinks."""
    fast = _package_files_scandir(package_path, native=native, skip_roots=skip_roots)
    if _STAT_DIFFERENTIAL:
        slow = _package_files_walk(package_path, native=native, skip_roots=skip_roots)
        if slow != fast:
            raise RuntimeError(f"PERF stat-cut differential: _package_files at {package_path}")
    return fast


def _package_files_scandir(
    package_path: Path, *, native: bool, skip_roots: tuple[str, ...]
) -> list[Path]:
    """``_package_files_walk`` from one scandir walk (same order rules, same refusals).

    Pruning an excluded directory (or a skipped root) yields the same set, since
    every path below it is skipped anyway. Like ``Path.rglob`` (3.12
    ``Path.walk(follow_symlinks=False)``), a directory link is not descended.
    """
    excluded = {".git"} if native else _EXCLUDED_DIRS
    regular_files: list[Path] = []
    stack: list[tuple[str, tuple[str, ...]]] = [(os.fspath(package_path), ())]
    while stack:
        directory, rel_parts = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue  # rglob ignores unreadable directories
        for entry in entries:
            parts = (*rel_parts, entry.name)
            if entry.name in excluded or (not rel_parts and entry.name in skip_roots):
                continue
            if entry.is_symlink():
                if native:
                    raise PathTraversalError(
                        f"Native bundle integrity refuses symlink: {package_path.joinpath(*parts)}"
                    )
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                stack.append((entry.path, parts))
                continue
            if entry.is_file() and not (len(parts) == 1 and entry.name in _EXCLUDED_ROOT_FILES):
                regular_files.append(Path(*parts))
    return sorted(regular_files, key=lambda path: path.as_posix())


def _package_files_walk(
    package_path: Path, *, native: bool = False, skip_roots: tuple[str, ...] = ()
) -> list[Path]:
    """The original walk (kept for the differential)."""
    regular_files: list[Path] = []'''
assert old in s
s = s.replace(old, new, 1)
p.write_text(s)

# 2) deploy_state.entry_state: one lstat instead of up to four (is_symlink,
#    is_file, is_dir, lexists) plus the mode lstat. Reparse points (junctions)
#    and anything unusual keep the original path.
p = Path("src/apm_cli/install/deploy_state.py")
s = p.read_text()
old = '''def entry_state(path: Path) -> str:
    """One entry below a journaled directory, as a seal and a fingerprint record it."""
    if path.is_symlink():'''
new = DIFF + '''_REPARSE_POINT = 0x400


def entry_state(path: Path) -> str:
    """One entry below a journaled directory, as a seal and a fingerprint record it."""
    fast = _entry_state_lstat(path)
    if fast is None or _STAT_DIFFERENTIAL:
        slow = _entry_state_walk(path)
        if fast is not None and fast != slow:
            raise RuntimeError(f"PERF stat-cut differential: entry_state at {path}")
        return slow
    return fast


def _entry_state_lstat(path: Path) -> str | None:
    """``entry_state`` from one ``lstat``; ``None`` when only the original can answer."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        return None  # links and junctions: the original follows them as before
    if stat.S_ISREG(info.st_mode):
        return f"{compute_exact_file_hash(path)}:{stat.S_IMODE(info.st_mode)}"
    if stat.S_ISDIR(info.st_mode):
        return f"dir:{stat.S_IMODE(info.st_mode)}"
    return "other"


def _entry_state_walk(path: Path) -> str:
    """The original probe sequence (kept for links and the differential)."""
    if path.is_symlink():'''
assert old in s
s = s.replace(old, new, 1)
p.write_text(s)

# 3) deploy_store._prepare: ``lexists`` then ``_real_dir`` (two lstats) per
#    directory -> one lstat; a missing directory is created and then proven.
p = Path("src/apm_cli/cache/deploy_store.py")
s = p.read_text()
old = '''        for directory in (self._root, entry.parent):
            if not os.path.lexists(directory):
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not self._real_dir(directory):
                return None'''
new = '''        for directory in (self._root, entry.parent):
            # PERF PROTOTYPE (stat-cut): one lstat proves an existing directory;
            # only a missing one is created and then proven.
            if not self._real_dir(directory):
                if os.path.lexists(directory):
                    return None
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not self._real_dir(directory):
                    return None'''
assert old in s
s = s.replace(old, new, 1)
p.write_text(s)
print("patched")
