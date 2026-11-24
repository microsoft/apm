from pathlib import Path

# S1/S4: a reparse point (junction etc.) found in the listing uses the original
# per-item Path calls; its type data comes from find data (no syscall).
p = Path("src/apm_cli/utils/content_hash.py")
s = p.read_text()
old = '''            if entry.is_symlink():
                if native:'''
new = '''            try:
                attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
            except OSError:
                attributes = 0
            if attributes & 0x400:  # W3d S1: reparse point -> the original per-item probes
                item = package_path.joinpath(*parts)
                if item.is_symlink():
                    if native:
                        raise PathTraversalError(f"Native bundle integrity refuses symlink: {item}")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, parts))  # Path.walk descends it, as rglob did
                if item.is_file() and not (len(parts) == 1 and entry.name in _EXCLUDED_ROOT_FILES):
                    regular_files.append(Path(*parts))
                continue
            if entry.is_symlink():
                if native:'''
assert old in s
s = s.replace(old, new, 1)

# S3: the hashing read checks the opened object is a regular file.
old = '''        payload = (package_path / rel_path).read_bytes()'''
new = '''        payload = _read_regular_bytes(package_path / rel_path)'''
assert old in s
s = s.replace(old, new, 1)
old = '''def _package_files(
    package_path: Path, *, native: bool = False, skip_roots: tuple[str, ...] = ()
) -> list[Path]:
    """Select canonical payload paths without following publisher symlinks."""
    fast ='''
new = '''def _read_regular_bytes(path: Path) -> bytes:
    """W3d S3: the bytes of *path*, refusing anything that is not a regular file once opened."""
    with open(path, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise IsADirectoryError(f"not a regular file: {path}")
        return handle.read()


def _package_files(
    package_path: Path, *, native: bool = False, skip_roots: tuple[str, ...] = ()
) -> list[Path]:
    """Select canonical payload paths without following publisher symlinks."""
    fast ='''
assert old in s
s = s.replace(old, new, 1)
p.write_text(s)

# S5: hash through the opened fd; its (dev, ino) must equal the lstat's.
p = Path("src/apm_cli/install/deploy_state.py")
s = p.read_text()
old = '''    if stat.S_ISREG(info.st_mode):
        return f"{compute_exact_file_hash(path)}:{stat.S_IMODE(info.st_mode)}"'''
new = '''    if stat.S_ISREG(info.st_mode):
        if not info.st_ino:
            return None  # W3d S5: no file identity -> the original sequence
        try:
            with open(path, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    return None  # replaced between lstat and open -> re-take
                data = handle.read()
        except OSError:
            return None
        return f"{exact_hash_of_bytes(data)}:{stat.S_IMODE(info.st_mode)}"'''
assert old in s
s = s.replace(old, new, 1)
s = s.replace(
    "from apm_cli.utils.content_hash import compute_exact_file_hash, compute_file_hash",
    "from apm_cli.utils.content_hash import (\n    compute_exact_file_hash,\n    compute_file_hash,\n    exact_hash_of_bytes,\n)",
    1,
)
p.write_text(s)

# S6: mkdir without exist_ok; EEXIST re-proves.
p = Path("src/apm_cli/cache/deploy_store.py")
s = p.read_text()
old = '''            if not self._real_dir(directory):
                if os.path.lexists(directory):
                    return None
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not self._real_dir(directory):
                    return None'''
new = '''            if not self._real_dir(directory):
                try:
                    directory.mkdir(mode=0o700, parents=True)
                except FileExistsError:
                    pass  # W3d S6: someone else's entry -> proven below like any other
                if not self._real_dir(directory):
                    return None'''
assert old in s
s = s.replace(old, new, 1)
p.write_text(s)
print("patched")
