from pathlib import Path

p = Path("src/apm_cli/cache/git_cache.py")
s = p.read_text()

helper = '''

# PERF PROTOTYPE (lever 4): write the checkout's .git in process instead of
# ``git clone --local --shared --no-checkout``; differential vs a real clone.
_GITDIR_DIFFERENTIAL = True
_INIT_CONFIG: dict[tuple[str, int], str] = {}
_GITDIR_WRITE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
)


def _init_config_text(git_exe: str, root: Path, env: dict[str, str]) -> str:
    """W3d S2: core.* exactly as ONE real ``git init`` writes it, per (git, device)."""
    key = (git_exe, os.stat(root).st_dev)
    cached = _INIT_CONFIG.get(key)
    if cached is not None:
        return cached
    import tempfile

    from ..utils.file_ops import robust_rmtree
    from ..utils.git_env import git_no_templates_args

    probe = Path(tempfile.mkdtemp(prefix=".apm-init-", dir=root))
    try:
        run_git_text(
            [git_exe, *_safe_git_args(), "init", *git_no_templates_args(), "--quiet", str(probe)],
            capture_output=True,
            timeout=30,
            env=env,
            stdin=subprocess.DEVNULL,
            check=True,
        )
        text = (probe / ".git" / "config").read_text(encoding="utf-8")
    finally:
        robust_rmtree(probe, ignore_errors=True)
    _INIT_CONFIG[key] = text
    return text


def _with_config_values(text: str, values: list[tuple[str, str]]) -> str:
    """Set ``section.key = value`` in a ``git init`` config, as ``git config`` would."""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("["):
            sections.append((stripped, []))
        elif "=" in stripped and sections:
            name, value = (part.strip() for part in stripped.split("=", 1))
            sections[-1][1].append((name, value))
        else:
            raise ValueError("unexpected git init config line")
    for dotted, value in values:
        section, name = dotted.rsplit(".", 1)
        header = f"[{section}]" if "." not in section else None
        if header is None:
            first, sub = section.split(".", 1)
            header = f'[{first} "{sub}"]'
        for existing_header, entries in sections:
            if existing_header == header:
                for index, (existing, _old) in enumerate(entries):
                    if existing.lower() == name.lower():
                        entries[index] = (existing, value)
                        break
                else:
                    entries.append((name, value))
                break
        else:
            sections.append((header, [(name, value)]))
    out = []
    for header, entries in sections:
        out.append(header)
        out.extend(f"\\t{name} = {value}" for name, value in entries)
    return "\\n".join(out) + "\\n"


def _write_new(path: Path, data: str) -> None:
    fd = os.open(path, _GITDIR_WRITE_FLAGS, 0o644)
    try:
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)


def _write_shared_gitdir(git_exe: str, staged: Path, bare_dir: Path, env: dict[str, str]) -> None:
    """W3d S1-S4: the .git ``clone --local --shared --no-checkout`` would write, minus refs.

    Later readers of a cache checkout's .git (S6): cached_repo trust (config
    allowlist, remote.origin.url, alternates) and integrity (HEAD, after the
    detached checkout). refs/remotes/* and branch.* are only read in temp clones.
    """
    objects = os.path.abspath(bare_dir / "objects")
    if any(ch in objects for ch in "\\n\\r\\0"):
        raise ValueError("alternates path holds a line break or NUL")  # S3
    if any(staged.iterdir()):
        raise ValueError("checkout staging directory is not empty")
    text = _init_config_text(git_exe, staged.parent, env)
    config = _with_config_values(
        text,
        [
            *_checkout_materialization_pins(),
            ("remote.origin.url", str(bare_dir)),
            ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
        ],
    )
    git_dir = staged / ".git"
    for directory in (
        git_dir,
        git_dir / "objects",
        git_dir / "objects" / "info",
        git_dir / "objects" / "pack",
        git_dir / "refs",
        git_dir / "refs" / "heads",
        git_dir / "refs" / "tags",
    ):
        os.mkdir(directory, 0o755)  # S4: fresh entries only; no hooks dir
    _write_new(git_dir / "HEAD", "ref: refs/heads/main\\n")
    _write_new(git_dir / "config", config)
    _write_new(git_dir / "objects" / "info" / "alternates", objects + "\\n")


def _gitdir_differential(
    git_exe: str, clone_args: list[str], staged: Path, bare_dir: Path, env: dict[str, str]
) -> None:
    """S8: compare the in-process .git with a real ``clone --shared`` of the same bare."""
    from ..utils.file_ops import robust_rmtree
    from .cached_repo import _config_entries

    twin = staged.parent / (staged.name + ".diff")
    try:
        twin.mkdir()
        run_git_text(
            [*clone_args, str(bare_dir), str(twin)],
            capture_output=True,
            timeout=60,
            env=env,
            stdin=subprocess.DEVNULL,
            check=True,
        )

        def entries(root: Path) -> list:
            return sorted(
                e for e in _config_entries(root / ".git" / "config") if e[0] != "branch"
            )

        mine, theirs = entries(staged), entries(twin)
        alt = "objects/info/alternates"
        a = (staged / ".git" / alt).read_text(encoding="utf-8")
        b = (twin / ".git" / alt).read_text(encoding="utf-8")
        if mine != theirs or os.path.normcase(a.strip()) != os.path.normcase(b.strip()):
            raise RuntimeError(
                f"PERF lever 4 differential: in-process .git != clone --shared: "
                f"{sorted(set(mine) ^ set(theirs))} | {a.strip()!r} vs {b.strip()!r}"
            )
    finally:
        robust_rmtree(twin, ignore_errors=True)
'''
anchor = "\n\nclass GitCache"
assert anchor in s
s = s.replace(anchor, helper + anchor, 1)

old = '''                run_git_text(
                    [
                        git_exe,
                        *_safe_git_args(),
                        "clone",
                        *git_no_templates_args(),
                        "--local",
                        "--shared",
                        "--no-checkout",
                        "--no-recurse-submodules",
                        *(
                            arg
                            for key, value in _checkout_materialization_pins()
                            for arg in ("-c", f"{key}={value}")
                        ),
                        *(
                            [
                                "-c",
                                "remote.origin.promisor=true",
                                "-c",
                                "remote.origin.partialclonefilter=blob:none",
                            ]
                            if promisor_url
                            else []
                        ),
                        str(bare_dir),
                        str(staged),
                    ],
                    capture_output=True,
                    timeout=60,
                    env=subprocess_env,
                    stdin=subprocess.DEVNULL,
                    check=True,
                )
'''
new = '''                clone_args = [
                    git_exe,
                    *_safe_git_args(),
                    "clone",
                    *git_no_templates_args(),
                    "--local",
                    "--shared",
                    "--no-checkout",
                    "--no-recurse-submodules",
                    *(
                        arg
                        for key, value in _checkout_materialization_pins()
                        for arg in ("-c", f"{key}={value}")
                    ),
                    *(
                        [
                            "-c",
                            "remote.origin.promisor=true",
                            "-c",
                            "remote.origin.partialclonefilter=blob:none",
                        ]
                        if promisor_url
                        else []
                    ),
                ]
                in_process = False
                if not sparse_paths and not promisor_url and len(sha) == 40:  # S1
                    try:
                        _write_shared_gitdir(git_exe, staged, bare_dir, subprocess_env)
                        in_process = True
                    except (OSError, ValueError, subprocess.SubprocessError):
                        from ..utils.file_ops import robust_rmtree

                        robust_rmtree(staged / ".git", ignore_errors=True)  # S5: fall back
                    if in_process and _GITDIR_DIFFERENTIAL:
                        _gitdir_differential(git_exe, clone_args, staged, bare_dir, subprocess_env)
                if not in_process:
                    run_git_text(
                        [*clone_args, str(bare_dir), str(staged)],
                        capture_output=True,
                        timeout=60,
                        env=subprocess_env,
                        stdin=subprocess.DEVNULL,
                        check=True,
                    )
'''
assert old in s
s = s.replace(old, new)
p.write_text(s)
print("patched")
