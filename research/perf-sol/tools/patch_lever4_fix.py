from pathlib import Path

p = Path("src/apm_cli/cache/git_cache.py")
s = p.read_text()

old = '''    objects = os.path.abspath(bare_dir / "objects")
'''
new = '''    # Git writes alternates with forward slashes on every OS.
    objects = os.path.abspath(bare_dir / "objects").replace(os.sep, "/")
'''
assert old in s
s = s.replace(old, new)

old = '''        if mine != theirs or os.path.normcase(a.strip()) != os.path.normcase(b.strip()):'''
new = '''        if mine != theirs or a != b:'''
assert old in s
s = s.replace(old, new)

old = '''def _gitdir_differential('''
new = '''class _Lever4Failure(BaseException):
    """PERF PROTOTYPE: differential mode aborts the command instead of a silent fallback."""


def _gitdir_differential('''
assert old in s
s = s.replace(old, new, 1)

old = '''                # Checkout the specific SHA.
                checkout = run_git_text(
                    [
                        git_exe,
                        *_safe_git_args(),
                        "-C",
                        str(staged),
                        "checkout",
                        sha,
                    ],
                    capture_output=True,
                    timeout=60,
                    env=subprocess_env,
                    stdin=subprocess.DEVNULL,
                    check=True,
                )'''
new = '''                # Checkout the specific SHA.
                try:
                    checkout = run_git_text(
                        [
                            git_exe,
                            *_safe_git_args(),
                            "-C",
                            str(staged),
                            "checkout",
                            sha,
                        ],
                        capture_output=True,
                        timeout=60,
                        env=subprocess_env,
                        stdin=subprocess.DEVNULL,
                        check=True,
                    )
                except subprocess.CalledProcessError as exc:
                    if in_process and _GITDIR_DIFFERENTIAL:
                        raise _Lever4Failure(
                            f"PERF lever 4: checkout from the in-process .git failed: {exc.stderr}"
                        ) from exc
                    raise'''
assert old in s
s = s.replace(old, new)
p.write_text(s)
print("patched")
