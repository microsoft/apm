"""In-process git answers equal git's, or defer to git (git-path levers, W3d cells).

``cache/pack_index.packed_commit`` answers ``git cat-file -t <sha>`` from the
pack index only for a plain SHA-1 repository and a non-delta entry in a pack
that passes git's index checks; every other shape returns ``None`` (V1-V4).
``git_sparse`` memoizes a tracked-link plan per commit id within one command,
never across replace refs, grafts or a non-regular HEAD (L1-L3).
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from apm_cli.cache import pack_index
from apm_cli.core.invocation_memo import invocation_scope
from apm_cli.utils import git_sparse

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-git-transport-selection"),
]

SHA = "ab" * 20
_GIT = shutil.which("git")


def _craft(git_dir: Path, type_byte: int, *, sha: str = SHA, count: int = 1) -> Path:
    """A one-object pack and its v2 index, with git's header, size and trailer rules."""
    pack_dir = git_dir / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    body = b"PACK" + struct.pack(">II", 2, count) + bytes([type_byte << 4 | 5]) + b"x" * 8
    trailer = hashlib.sha1(body).digest()  # noqa: S324 -- git's pack trailer
    (pack_dir / "pack-1.pack").write_bytes(body + trailer)
    want = bytes.fromhex(sha)
    fanout = [0] * 256
    for byte in range(want[0], 256):
        fanout[byte] = 1
    idx = (
        b"\xfftOc\x00\x00\x00\x02"
        + struct.pack(">256I", *fanout)
        + want
        + b"\x00" * 4
        + struct.pack(">I", 12)
        + trailer
        + b"\x00" * 20
    )
    (pack_dir / "pack-1.idx").write_bytes(idx)
    return pack_dir


@pytest.mark.parametrize(("type_byte", "expected"), [(1, True), (2, False), (3, False)])
def test_a_plain_pack_entry_is_answered(tmp_path: Path, type_byte: int, expected: bool) -> None:
    _craft(tmp_path, type_byte)
    _assert_answer(tmp_path, expected)


@pytest.mark.parametrize(
    "shape",
    [
        "ofs-delta",
        "ref-delta",
        "truncated-idx",
        "wrong-pack-trailer",
        "count-mismatch",
        "large-offset",
        "alternates",
        "http-alternates",
        "grafts",
        "multi-pack-index",
        "loose-object",
        "replace-ref",
        "packed-replace-ref",
        "not-found",
        "empty-idx",
    ],
)
def test_shapes_git_must_decide_are_left_to_git(tmp_path: Path, shape: str) -> None:
    """W3d V1-V4: any of these returns None and the caller spawns git."""
    type_byte = {"ofs-delta": 6, "ref-delta": 7}.get(shape, 1)
    pack_dir = _craft(tmp_path, type_byte, count=2 if shape == "count-mismatch" else 1)
    idx = pack_dir / "pack-1.idx"
    if shape == "truncated-idx":
        idx.write_bytes(idx.read_bytes()[:-7])
    elif shape == "empty-idx":
        idx.write_bytes(b"")
    elif shape == "wrong-pack-trailer":
        pack = pack_dir / "pack-1.pack"
        pack.write_bytes(pack.read_bytes()[:-1] + b"\x00")
    elif shape == "large-offset":
        data = bytearray(idx.read_bytes())
        data[-44:-40] = struct.pack(">I", 0x80000000)
        idx.write_bytes(bytes(data))
    elif shape in ("alternates", "http-alternates"):
        (tmp_path / "objects" / "info").mkdir(parents=True)
        (tmp_path / "objects" / "info" / shape).write_text("/elsewhere\n", encoding="ascii")
    elif shape == "grafts":
        (tmp_path / "info").mkdir()
        (tmp_path / "info" / "grafts").write_text(SHA + "\n", encoding="ascii")
    elif shape == "multi-pack-index":
        (pack_dir / "multi-pack-index").write_bytes(b"MIDX")
    elif shape == "loose-object":
        (tmp_path / "objects" / SHA[:2]).mkdir()
        (tmp_path / "objects" / SHA[:2] / SHA[2:]).write_bytes(b"loose")
    elif shape == "replace-ref":
        (tmp_path / "refs" / "replace").mkdir(parents=True)
        (tmp_path / "refs" / "replace" / SHA).write_text(SHA + "\n", encoding="ascii")
    elif shape == "packed-replace-ref":
        (tmp_path / "packed-refs").write_text(f"{SHA} refs/replace/{SHA}\n", encoding="ascii")
    elif shape == "not-found":
        _assert_answer(tmp_path, None, sha="cd" * 20)
        return
    _assert_answer(tmp_path, None)


@pytest.mark.skipif(_GIT is None, reason="needs git")
def test_a_real_packed_repository_agrees_with_git(tmp_path: Path) -> None:
    """V6: commit, tree and blob ids of a real packed repository answer as git does."""
    repo = tmp_path / "repo"
    run = _git_runner(repo)
    run("init", "-q", str(repo))
    (repo / "a.txt").write_text("a\n", encoding="ascii")
    run("-C", str(repo), "add", "a.txt")
    run("-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "c")
    run("-C", str(repo), "repack", "-adq")
    git_dir = repo / ".git"
    for rev in ("HEAD", "HEAD^{tree}", "HEAD:a.txt"):
        sha = run("-C", str(repo), "rev-parse", rev).strip()
        kind = run("--git-dir", str(git_dir), "cat-file", "-t", sha).strip()
        _assert_answer(git_dir, kind == "commit", sha=sha)


def test_the_differential_raises_on_disagreement() -> None:
    with pytest.raises(pack_index.PackIndexMismatch):
        pack_index.check(True, False)


def _git_runner(repo: Path):
    def run(*args: str) -> str:
        return subprocess.run([str(_GIT), *args], check=True, capture_output=True, text=True).stdout

    return run


@pytest.mark.skipif(_GIT is None, reason="needs git")
def test_a_link_plan_is_memoized_per_commit_and_copied(tmp_path: Path) -> None:
    """L1/L4: one plan per commit id in a command; every caller gets its own copy."""
    repo = tmp_path / "repo"
    run = _git_runner(repo)
    run("init", "-q", str(repo))
    (repo / "a.txt").write_text("a\n", encoding="ascii")
    run("-C", str(repo), "add", "a.txt")
    run("-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "c")
    sha = run("-C", str(repo), "rev-parse", "HEAD").strip()
    run("-C", str(repo), "checkout", "-q", "--detach", sha)
    kwargs = {"env": None, "timeout": 30, "extra_git_args": None}
    with invocation_scope():
        first = git_sparse._plan_tracked_symlinks(str(_GIT), repo, None, **kwargs)
        first[0]["mutated"] = []
        second = git_sparse._plan_tracked_symlinks(str(_GIT), repo, None, **kwargs)
    _assert_plan(second, ({}, {}))


@pytest.mark.parametrize("shape", ["symlink-head", "replace-ref", "grafts", "symbolic-head"])
def test_no_link_plan_memo_where_the_tree_may_differ(tmp_path: Path, shape: str) -> None:
    """L2/L3: replace refs, grafts, or a HEAD that is not a regular commit-id file."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "real-head").write_text(SHA + "\n", encoding="ascii")
    if shape == "symlink-head":
        try:
            (git_dir / "HEAD").symlink_to(git_dir / "real-head")
        except OSError:
            pytest.skip("no symlink support")
    else:
        text = "ref: refs/heads/main\n" if shape == "symbolic-head" else SHA + "\n"
        (git_dir / "HEAD").write_text(text, encoding="ascii")
    if shape == "replace-ref":
        (git_dir / "refs" / "replace").mkdir(parents=True)
    elif shape == "grafts":
        (git_dir / "info").mkdir()
        (git_dir / "info" / "grafts").write_text("", encoding="ascii")
    _assert_no_memo(tmp_path)


def test_the_link_plan_key_names_the_object_format(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ab" * 32 + "\n", encoding="ascii")
    _assert_key_format(tmp_path, "sha256")


def _assert_answer(git_dir: Path, expected: bool | None, *, sha: str = SHA) -> None:
    """What the cache acts on: True/False skip the spawn, None asks git."""
    assert pack_index.packed_commit(git_dir, sha) is expected


def _assert_plan(plan, expected) -> None:
    assert plan == expected


def _assert_no_memo(repo: Path) -> None:
    assert git_sparse._plan_key(repo, None, None) is None


def _assert_key_format(repo: Path, object_format: str) -> None:
    key = git_sparse._plan_key(repo, None, None)
    assert key is not None and key[0] == object_format
