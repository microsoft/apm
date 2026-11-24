"""Lever C: the pinned worktree written from the bare's pack answers like ``git checkout`` or refuses.

W3d w3d:ruling:lever-c. C6: the checkout keeps a real ``.git`` (``clone --shared
--no-checkout``), a detached HEAD at the pin and an index, so every git reader
(ls-files, status, rev-parse) answers as after ``git checkout``. C8: a refusal or
a failed write leaves only ``.git`` behind, and git checks out instead. C11 tamper
cells: a flipped pack byte, a planted link, a replace ref, a swapped parent.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.cache.pack_objects import materialize_tree
from apm_cli.core.invocation_memo import invocation_scope
from apm_cli.utils.git_env import reset_git_cache

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-git-transport-selection")]

_FILES = {
    "a.txt": b"alpha\n",
    "dir/b.md": b"# bravo\n\nsome text\n",
    "dir/sub/c.sh": b"#!/bin/sh\necho charlie\n",
    "z/deep/e.json": b'{"e": 1}\n',
}


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


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        check=check,
    )
    return result.stdout.decode("utf-8")


def _source(root: Path, extra=None) -> tuple[Path, str]:
    src = root / "src"
    src.mkdir()
    _git(src, "init", "-q")
    for rel, data in _FILES.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(src, "add", "-A")
    _git(src, "update-index", "--chmod=+x", "dir/sub/c.sh")
    if extra:
        extra(src)
    _git(src, "commit", "-q", "-m", "c")
    return src, _git(src, "rev-parse", "HEAD").strip()


def _staged(root: Path, src: Path, name: str = "staged") -> tuple[Path, Path]:
    bare = root / "bare.git"
    if not bare.exists():
        _git(root, "clone", "-q", "--bare", "--no-local", str(src), str(bare))
    staged = root / name
    _git(root, "clone", "-q", "--local", "--shared", "--no-checkout", str(bare), str(staged))
    return bare, staged


def _worktree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def _materialize(staged: Path, sha: str, env=None, **options):
    with invocation_scope():
        return materialize_tree(staged, sha, env or dict(os.environ), **options)


def test_the_pack_checkout_answers_every_git_reader_like_git_checkout(world: Path) -> None:
    src, sha = _source(world)
    _bare, staged = _staged(world, src)
    _bare, reference = _staged(world, src, "reference")
    _git(reference, "checkout", "-q", sha)

    _assert_like_git_checkout(_materialize(staged, sha), staged, reference, sha)


def _assert_like_git_checkout(manifest, staged: Path, reference: Path, sha: str) -> None:
    """C6: every git reader answers on the pack checkout as on ``git checkout``."""
    assert manifest is not None
    assert sorted(manifest) == sorted(_FILES)
    assert _worktree(staged) == _worktree(reference) == _FILES
    assert _git(staged, "ls-files", "-s") == _git(reference, "ls-files", "-s")
    assert _git(staged, "status", "--porcelain") == _git(reference, "status", "--porcelain") == ""
    assert _git(staged, "rev-parse", "HEAD").strip() == sha
    assert _git(staged, "symbolic-ref", "-q", "HEAD", check=False) == ""
    assert _git(staged, "diff", "--name-only", sha) == ""
    if os.name != "nt":
        assert os.access(staged / "dir" / "sub" / "c.sh", os.X_OK)
        assert not os.access(staged / "a.txt", os.X_OK)


def _assert_left_to_git(staged: Path, sha: str, env=None, **options) -> None:
    """C8: a refusal writes nothing; only ``.git`` (no index) is left for git checkout."""
    assert _materialize(staged, sha, env, **options) is None
    assert sorted(os.listdir(staged)) == [".git"]
    assert not (staged / ".git" / "index").exists()


def test_a_planted_link_is_left_to_git(world: Path) -> None:
    def link(src: Path) -> None:
        blob = (
            subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=src,
                input=b"a.txt",
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        _git(src, "update-index", "--add", "--cacheinfo", f"120000,{blob},link")

    src, sha = _source(world, link)
    _bare, staged = _staged(world, src)
    _assert_left_to_git(staged, sha)


def test_a_commit_gitattributes_is_left_to_git(world: Path) -> None:
    def attributes(src: Path) -> None:
        (src / ".gitattributes").write_text("*.md text eol=crlf\n", encoding="utf-8")
        _git(src, "add", ".gitattributes")

    src, sha = _source(world, attributes)
    _bare, staged = _staged(world, src)
    _assert_left_to_git(staged, sha)


def test_a_replace_ref_is_left_to_git(world: Path) -> None:
    src, sha = _source(world)
    bare, staged = _staged(world, src)
    git_dir = f"--git-dir={bare}"  # no bare discovery: safe.bareRepository may be explicit
    blob = _git(world, git_dir, "rev-parse", f"{sha}:a.txt").strip()
    other = _git(world, git_dir, "rev-parse", f"{sha}:dir/b.md").strip()
    _git(world, git_dir, "replace", blob, other)
    _assert_left_to_git(staged, sha)


def test_a_flipped_pack_byte_writes_nothing(world: Path) -> None:
    src, sha = _source(world)
    bare, staged = _staged(world, src)
    (pack,) = sorted((bare / "objects" / "pack").glob("*.pack"))
    os.chmod(pack, 0o644)
    data = bytearray(pack.read_bytes())
    for offset in range(12, len(data) - 20, 7):
        data[offset] ^= 0x5A
    pack.write_bytes(bytes(data))
    _assert_left_to_git(staged, sha)


def test_a_swapped_parent_refuses_and_leaves_only_git(world: Path) -> None:
    src, sha = _source(world)
    _bare, staged = _staged(world, src)
    (staged / "dir").write_bytes(b"planted, not a directory\n")
    _assert_left_to_git(staged, sha)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_a_parent_swapped_for_a_link_is_never_followed(world: Path) -> None:
    src, sha = _source(world)
    _bare, staged = _staged(world, src)
    outside = world / "outside"
    outside.mkdir()
    os.symlink(outside, staged / "z")
    _assert_left_to_git(staged, sha)
    _assert_empty(outside)


def _assert_empty(directory: Path) -> None:
    assert list(directory.iterdir()) == []


def _bulk(src: Path) -> None:
    """Thirty distinct 641-byte files: the tree inflates to far more than any one object."""
    for i in range(30):
        (src / "bulk").mkdir(exist_ok=True)
        # Hash-chained text: no two files share content git could store as a delta.
        text = "".join(hashlib.sha256(f"{i}-{j}".encode()).hexdigest() for j in range(10))
        (src / "bulk" / f"f{i:02d}.txt").write_text(text + "\n", encoding="ascii")
    _git(src, "add", "bulk")


def _object_sizes(src: Path, sha: str) -> list[int]:
    listed = _git(src, "rev-list", "--objects", sha)
    oids = "\n".join(line.split(" ", 1)[0] for line in listed.splitlines()) + "\n"
    sizes = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectsize)"],
        cwd=src,
        input=oids.encode("ascii"),
        capture_output=True,
        check=True,
    ).stdout.split()
    return [int(size) for size in sizes]


def test_one_inflate_budget_covers_the_whole_tree(world: Path) -> None:
    """W3d R1: every blob is held until written, so the cap is per call, not per read.
    Half the tree's bytes is many times any one object, and still refuses."""
    src, sha = _source(world, _bulk)
    sizes = _object_sizes(src, sha)
    _assert_budget_splits_the_tree(sizes)
    _bare, staged = _staged(world, src)
    _assert_left_to_git(staged, sha, inflate_budget=sum(sizes) // 2)
    _bare, control = _staged(world, src, "control")
    _assert_materialized(_materialize(control, sha))


def test_attributes_are_read_from_the_checkout_env(world: Path) -> None:
    """W3d R2 (the #5351 fold-2 class): the command's isolated global config names no
    attributesFile, so git's checkout would read XDG's eol=crlf; the pack refuses."""
    src, sha = _source(world)
    _bare, staged = _staged(world, src)
    isolated = world / "isolated.gitconfig"
    isolated.write_text("", encoding="utf-8")
    xdg = world / "xdg"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "attributes").write_text("* eol=crlf\n", encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(isolated), "XDG_CONFIG_HOME": str(xdg)}
    _assert_left_to_git(staged, sha, env=env)


@pytest.mark.parametrize("swap", ["dir", "link"])
def test_a_parent_swapped_during_the_writes_is_discarded(
    world: Path, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    """W3d R3 (C8 seal): a directory replaced after mkdir, by a new directory or by a
    link out of the staged root, while files are written, leaves only .git."""
    if swap == "link" and os.name == "nt":
        pytest.skip("symlink creation needs privileges on Windows")
    src, sha = _source(world)
    _bare, staged = _staged(world, src)
    real_open = os.open
    swapped: list[str] = []

    def racing_open(path, flags, mode=0o777, **kwargs):
        if not swapped and os.path.dirname(os.fspath(path)) == str(staged / "dir"):
            os.rename(staged / "dir", world / "moved")
            if swap == "link":
                os.symlink(world / "moved", staged / "dir")
            else:
                (staged / "dir" / "sub").mkdir(parents=True)
            swapped.append(os.fspath(path))
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    _assert_left_to_git(staged, sha)
    monkeypatch.undo()
    _assert_swapped(swapped)


def _assert_budget_splits_the_tree(sizes: list[int]) -> None:
    assert sum(sizes) // 2 > 4 * max(sizes)


def _assert_materialized(manifest) -> None:
    assert manifest is not None and "bulk/f00.txt" in manifest


def _assert_swapped(swapped: list[str]) -> None:
    assert len(swapped) == 1
