from pathlib import Path

# Lever 3: memo the tracked-link plan per (HEAD commit sha, paths) within a command.
p = Path("src/apm_cli/utils/git_sparse.py")
s = p.read_text()
old = '''def _plan_tracked_symlinks(
    git_exe: str,
    repo_dir: Path,
    paths: list[str] | None,
    *,
    env: dict[str, str] | None,
    timeout: int,
    extra_git_args: list[str] | None,
) -> tuple[dict[str, list[_LinkWrite]], dict[str, Exception]]:
    """Plan link materialization; the common no-link case costs one ``ls-tree``."""
'''
new = '''_LINK_PLANS: dict[tuple[str, tuple[str, ...] | None], tuple[dict, dict]] = {}


def _detached_head(repo_dir: Path) -> str | None:
    """The commit id a detached ``HEAD`` names (the file ``git ls-tree HEAD`` reads), or ``None``."""
    try:
        text = (Path(repo_dir) / ".git" / "HEAD").read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if len(text) in (40, 64) and all(c in "0123456789abcdef" for c in text):
        return text
    return None


def _plan_tracked_symlinks(
    git_exe: str,
    repo_dir: Path,
    paths: list[str] | None,
    *,
    env: dict[str, str] | None,
    timeout: int,
    extra_git_args: list[str] | None,
) -> tuple[dict[str, list[_LinkWrite]], dict[str, Exception]]:
    """Plan link materialization; the common no-link case costs one ``ls-tree``.

    PERF PROTOTYPE (lever 3): the plan is a pure function of the commit's tree,
    so within one command it is computed once per (commit id, paths).
    """
    head = _detached_head(repo_dir)
    key = (head, tuple(paths) if paths is not None else None) if head else None
    if key is not None and key in _LINK_PLANS:
        return _LINK_PLANS[key]
    result = _plan_tracked_symlinks_uncached(
        git_exe, repo_dir, paths, env=env, timeout=timeout, extra_git_args=extra_git_args
    )
    if key is not None:
        _LINK_PLANS[key] = result
    return result


def _plan_tracked_symlinks_uncached(
    git_exe: str,
    repo_dir: Path,
    paths: list[str] | None,
    *,
    env: dict[str, str] | None,
    timeout: int,
    extra_git_args: list[str] | None,
) -> tuple[dict[str, list[_LinkWrite]], dict[str, Exception]]:
    """One ``ls-tree`` (plus link blobs when links exist) for the HEAD tree."""
'''
assert old in s
s = s.replace(old, new)
p.write_text(s)

# Lever 2b: answer "is <sha> a commit in this repository" from pack .idx files.
p = Path("src/apm_cli/cache/git_cache.py")
s = p.read_text()
helper = '''

def _packed_commit(git_dir: Path, sha: str) -> bool | None:
    """PERF PROTOTYPE (lever 2b): whether a pack in *git_dir* holds *sha* as a commit.

    Reads only pack ``.idx`` v2 fanout + a bisect and the entry's type bits,
    the bytes ``git cat-file -t`` reads. ``True``/``False`` only for a plain
    SHA-1 repository whose object is a non-delta pack entry; anything else
    (loose object, alternates, delta entry, other index version, not found)
    returns ``None`` and the caller spawns git as before.
    """
    import bisect
    import struct

    if len(sha) != 40:
        return None
    objects = git_dir / "objects"
    try:
        want = bytes.fromhex(sha)
    except ValueError:
        return None
    if (objects / "info" / "alternates").exists() or (objects / sha[:2] / sha[2:]).exists():
        return None
    try:
        idx_files = sorted((objects / "pack").glob("pack-*.idx"))
    except OSError:
        return None
    for idx_path in idx_files:
        try:
            data = idx_path.read_bytes()
        except OSError:
            return None
        if data[:8] != b"\\xfftOc\\x00\\x00\\x00\\x02":
            return None
        fanout = struct.unpack(">256I", data[8 : 8 + 1024])
        total = fanout[255]
        lo = fanout[want[0] - 1] if want[0] else 0
        hi = fanout[want[0]]
        names = 8 + 1024
        keys = [data[names + 20 * i : names + 20 * i + 20] for i in range(lo, hi)]
        at = bisect.bisect_left(keys, want)
        if at == len(keys) or keys[at] != want:
            continue
        index = lo + at
        offsets = names + 20 * total + 4 * total
        (offset,) = struct.unpack(">I", data[offsets + 4 * index : offsets + 4 * index + 4])
        if offset & 0x80000000:
            return None
        pack_path = idx_path.with_suffix(".pack")
        try:
            with open(pack_path, "rb") as handle:
                handle.seek(offset)
                first = handle.read(1)
        except OSError:
            return None
        if not first:
            return None
        kind = (first[0] >> 4) & 7
        if kind in (6, 7):
            return None
        return kind == 1
    return None
'''
anchor = "\n\nclass GitCache"
assert anchor in s
s = s.replace(anchor, helper + anchor, 1)

old = '''        try:
            probe = run_git_text(
                [
                    get_git_executable(),
                    *_safe_git_args(),
                    *("--git-dir", str(staged), "cat-file", "-t", sha),
                ],'''
new = '''        if _packed_commit(staged, sha):
            return  # PERF PROTOTYPE (lever 2b): the pack already holds the commit
        try:
            probe = run_git_text(
                [
                    get_git_executable(),
                    *_safe_git_args(),
                    *("--git-dir", str(staged), "cat-file", "-t", sha),
                ],'''
assert old in s
s = s.replace(old, new)
old = '''        git_exe = get_git_executable()
        subprocess_env = git_subprocess_env(env)
        subprocess_env = cached_repo_env(subprocess_env)
        try:
            result = run_git_text(
                [git_exe, *_safe_git_args(), "--git-dir", str(bare_dir), "cat-file", "-t", sha],'''
new = '''        known = _packed_commit(bare_dir, sha)
        if known is not None:
            return known  # PERF PROTOTYPE (lever 2b)
        git_exe = get_git_executable()
        subprocess_env = git_subprocess_env(env)
        subprocess_env = cached_repo_env(subprocess_env)
        try:
            result = run_git_text(
                [git_exe, *_safe_git_args(), "--git-dir", str(bare_dir), "cat-file", "-t", sha],'''
assert old in s
s = s.replace(old, new)
p.write_text(s)
print("patched")
