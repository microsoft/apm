"""RT5 red cells (PERF-Bets): crafted pack trees against the in-process writers.

Each hostile tree must either be left to git (nothing written, only ``.git``)
or produce exactly what ``git checkout`` writes. Nothing may land outside the
staged checkout.
"""

from __future__ import annotations

import os
import subprocess
import unicodedata
from pathlib import Path

import pytest

from apm_cli.cache.pack_objects import materialize_tree
from apm_cli.core.invocation_memo import invocation_scope
from apm_cli.utils.git_env import reset_git_cache

pytestmark = [pytest.mark.component]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = home / ".gitconfig"
    gitconfig.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    reset_git_cache()
    yield tmp_path
    reset_git_cache()


def _git(cwd: Path, *args: str, data: bytes | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
        cwd=cwd,
        input=data,
        capture_output=True,
        check=check,
    )
    return result.stdout.decode("utf-8", "replace")


def _blob(repo: Path, data: bytes) -> bytes:
    return bytes.fromhex(_git(repo, "hash-object", "-w", "--stdin", data=data).strip())


def _tree(repo: Path, entries: list[tuple[bytes, bytes, bytes]]) -> bytes:
    raw = b"".join(mode + b" " + name + b"\0" + oid for mode, name, oid in entries)
    out = _git(repo, "hash-object", "-t", "tree", "--literally", "-w", "--stdin", data=raw)
    return bytes.fromhex(out.strip())


def _commit(repo: Path, tree: bytes) -> str:
    return _git(repo, "commit-tree", tree.hex(), "-m", "c").strip()


def _hostile(root: Path, build) -> tuple[Path, str]:
    src = root / "src"
    src.mkdir()
    _git(src, "init", "-q")
    sha = _commit(src, build(src))
    _git(src, "update-ref", "refs/heads/main", sha)
    return src, sha


def _staged(root: Path, src: Path, name: str) -> Path:
    bare = root / "bare.git"
    if not bare.exists():
        _git(
            root,
            "-c",
            "transfer.fsckObjects=false",
            "clone",
            "-q",
            "--bare",
            "--no-local",
            str(src),
            str(bare),
        )
    staged = root / name
    _git(root, "clone", "-q", "--local", "--shared", "--no-checkout", str(bare), str(staged))
    return staged


def _snapshot(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            path = Path(dirpath) / name
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def _outside(root: Path) -> set[str]:
    return {p.name for p in root.iterdir()}


def _assert_like_git_or_left(world: Path, build) -> None:
    src, sha = _hostile(world, build)
    staged = _staged(world, src, "staged")
    before = _outside(world)
    with invocation_scope():
        manifest = materialize_tree(staged, sha, dict(os.environ))
    assert _outside(world) == before, "a write landed outside the staged checkout"
    if manifest is None:
        assert sorted(os.listdir(staged)) == [".git"]
        return
    reference = _staged(world, src, "reference")
    done = subprocess.run(["git", "checkout", "-q", sha], cwd=reference, capture_output=True)
    assert done.returncode == 0, "in-process wrote a tree git itself refuses to check out"
    assert _snapshot(staged) == _snapshot(reference)
    assert sorted(manifest) == sorted(_snapshot(staged))


def test_a_duplicate_file_entry(world: Path) -> None:
    def build(repo: Path) -> bytes:
        a, b = _blob(repo, b"first\n"), _blob(repo, b"second\n")
        return _tree(repo, [(b"100644", b"a.txt", a), (b"100644", b"a.txt", b)])

    _assert_like_git_or_left(world, build)


def test_a_file_and_a_directory_with_one_name(world: Path) -> None:
    def build(repo: Path) -> bytes:
        a = _blob(repo, b"file\n")
        sub = _tree(repo, [(b"100644", b"x.md", a)])
        return _tree(repo, [(b"100644", b"d", a), (b"40000", b"d", sub)])

    _assert_like_git_or_left(world, build)


def test_case_twins(world: Path) -> None:
    def build(repo: Path) -> bytes:
        a, b = _blob(repo, b"upper\n"), _blob(repo, b"lower\n")
        return _tree(repo, [(b"100644", b"README.md", a), (b"100644", b"readme.md", b)])

    _assert_like_git_or_left(world, build)


def test_case_twin_directories(world: Path) -> None:
    def build(repo: Path) -> bytes:
        a, b = _blob(repo, b"one\n"), _blob(repo, b"two\n")
        upper = _tree(repo, [(b"100644", b"a.md", a)])
        lower = _tree(repo, [(b"100644", b"b.md", b)])
        return _tree(repo, [(b"40000", b"Skills", upper), (b"40000", b"skills", lower)])

    _assert_like_git_or_left(world, build)


def test_nfc_nfd_twins(world: Path) -> None:
    nfc = unicodedata.normalize("NFC", "caf\u00e9.md").encode()
    nfd = unicodedata.normalize("NFD", "caf\u00e9.md").encode()

    def build(repo: Path) -> bytes:
        a, b = _blob(repo, b"nfc\n"), _blob(repo, b"nfd\n")
        return _tree(repo, sorted([(b"100644", nfc, a), (b"100644", nfd, b)], key=lambda e: e[1]))

    _assert_like_git_or_left(world, build)


def test_unsorted_entries(world: Path) -> None:
    def build(repo: Path) -> bytes:
        a, b = _blob(repo, b"a\n"), _blob(repo, b"b\n")
        return _tree(repo, [(b"100644", b"z.md", a), (b"100644", b"a.md", b)])

    _assert_like_git_or_left(world, build)


def test_a_dotdot_entry(world: Path) -> None:
    def build(repo: Path) -> bytes:
        a = _blob(repo, b"escape\n")
        return _tree(repo, [(b"100644", b"..", a), (b"100644", b"ok.md", a)])

    _assert_like_git_or_left(world, build)


def test_a_slash_in_an_entry_name(world: Path) -> None:
    def build(repo: Path) -> bytes:
        a = _blob(repo, b"escape\n")
        return _tree(repo, [(b"100644", b"../../escaped.md", a)])

    _assert_like_git_or_left(world, build)


def test_a_mode_git_never_writes(world: Path) -> None:
    def build(repo: Path) -> bytes:
        a = _blob(repo, b"odd mode\n")
        return _tree(repo, [(b"100664", b"odd.md", a), (b"0100644", b"zero.md", a)])

    _assert_like_git_or_left(world, build)
