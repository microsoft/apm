"""Lever 2a: ``verify_checkout`` reads objects from the pack and gives git's verdict.

W3d 2a (P1-P7). The pack is a byte source, never a trust root: every object is
re-hashed against its id, and any object the pack cannot serve sends the whole
package back to ``git cat-file --batch``. Each cell compares the verdict with
the pack on against ``APM_NO_INPROCESS_VERIFY=1`` (git only), and counts the
``cat-file`` spawns so an equal verdict cannot come from the pack never engaging.
"""

from __future__ import annotations

import os
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from apm_cli.cache import checkout_verify
from apm_cli.utils.git_env import reset_git_cache

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-git-transport-selection")]

_FILES = {
    "a.txt": b"alpha\n",
    "dir/b.md": b"# bravo\n\nsome text\n",
    "dir/sub/c.sh": b"#!/bin/sh\necho charlie\n",
}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("APM_NO_INPROCESS_VERIFY", raising=False)
    reset_git_cache()
    yield tmp_path
    reset_git_cache()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")


def _checkout(root: Path) -> tuple[Path, Path, str]:
    """A packed bare and a ``clone --shared`` checkout of its one commit."""
    src = root / "src"
    src.mkdir()
    _git(src, "init", "-q")
    for rel, data in _FILES.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(data)
    _git(src, "add", "-A")
    _git(src, "update-index", "--chmod=+x", "dir/sub/c.sh")
    _git(src, "commit", "-q", "-m", "c")
    sha = _git(src, "rev-parse", "HEAD").strip()
    bare = root / "bare.git"
    _git(root, "clone", "-q", "--bare", "--no-local", str(src), str(bare))
    checkout = root / "checkout"
    _git(root, "clone", "-q", "--local", "--shared", str(bare), str(checkout))
    _git(checkout, "checkout", "-q", "--detach", sha)
    return bare, checkout, sha


def _verdict(checkout: Path, sha: str, monkeypatch, *, git_only: bool) -> tuple[bool, int]:
    """``(verify_checkout, cat-file spawns)`` with the pack on, or git only."""
    real = subprocess.Popen
    spawns: list[list[str]] = []

    def popen(args, *pargs, **kwargs):
        spawns.append(list(args))
        return real(args, *pargs, **kwargs)

    checkout_verify.forget_verdicts()
    with monkeypatch.context() as patch:
        if git_only:
            patch.setenv("APM_NO_INPROCESS_VERIFY", "1")
        patch.setattr(checkout_verify.subprocess, "Popen", popen)
        verdict = checkout_verify.verify_checkout(checkout, sha)
    return verdict, sum("cat-file" in args for args in spawns)


def _tamper_file(checkout: Path) -> None:
    (checkout / "dir" / "b.md").write_bytes(b"# bravo\n\nother text\n")


def _plant_file(checkout: Path) -> None:
    (checkout / "dir" / "planted.md").write_bytes(b"not in the commit\n")


def _delete_file(checkout: Path) -> None:
    (checkout / "a.txt").unlink()


def _flip_exec(checkout: Path) -> None:
    os.chmod(checkout / "dir" / "sub" / "c.sh", 0o644)


_SHAPES = {
    "clean": None,
    "tampered-file": _tamper_file,
    "planted-file": _plant_file,
    "deleted-file": _delete_file,
}
if sys.platform != "win32":
    _SHAPES["exec-bit"] = _flip_exec


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_the_pack_verdict_equals_git(world: Path, monkeypatch, shape: str) -> None:
    """P1/P2: the same verdict as git, answered without a ``cat-file`` spawn."""
    _bare, checkout, sha = _checkout(world)
    if _SHAPES[shape] is not None:
        _SHAPES[shape](checkout)
    pack = _verdict(checkout, sha, monkeypatch, git_only=False)
    git = _verdict(checkout, sha, monkeypatch, git_only=True)
    _assert_verdicts(pack, git, expected=shape == "clean", pack_spawns=0)


def test_every_flipped_pack_byte_gives_git_verdict_or_refuses(world: Path, monkeypatch) -> None:
    """P1/P5: a flipped pack byte, header version and object count and trailer
    included, never turns a refusal into a pass."""
    bare, checkout, sha = _checkout(world)
    (pack,) = (bare / "objects" / "pack").glob("*.pack")
    original = pack.read_bytes()
    os.chmod(pack, 0o644)
    disagreements = []
    trailer = range(len(original) - 20, len(original))
    for position in [*range(4, 12), *range(12, len(original) - 20, 3), *trailer]:
        flipped = bytearray(original)
        flipped[position] ^= 0x40
        pack.write_bytes(bytes(flipped))
        pack_verdict, _ = _verdict(checkout, sha, monkeypatch, git_only=False)
        git_verdict, _ = _verdict(checkout, sha, monkeypatch, git_only=True)
        if pack_verdict and not git_verdict:
            disagreements.append(position)
    pack.write_bytes(original)
    _assert_no_disagreement(disagreements)


def test_an_index_that_names_the_wrong_bytes_is_refused(world: Path, monkeypatch) -> None:
    """P1: the pack is a byte source, not a trust root. The index points a.txt's
    id at b.md's entry and the worktree holds b.md's bytes: only the re-hash
    refuses it."""
    bare, checkout, sha = _checkout(world)
    a_oid = _git(checkout, "rev-parse", f"{sha}:a.txt").strip()
    b_oid = _git(checkout, "rev-parse", f"{sha}:dir/b.md").strip()
    (idx,) = (bare / "objects" / "pack").glob("*.idx")
    os.chmod(idx, 0o644)
    idx.write_bytes(_swap_offsets(idx.read_bytes(), a_oid, b_oid))
    (checkout / "a.txt").write_bytes(_FILES["dir/b.md"])
    pack = _verdict(checkout, sha, monkeypatch, git_only=False)
    git = _verdict(checkout, sha, monkeypatch, git_only=True)
    _assert_verdicts(pack, git, expected=False, pack_spawns=0)


def _swap_offsets(idx: bytes, first: str, second: str) -> bytes:
    """A v2 index with the 32-bit pack offsets of two object ids exchanged."""
    count = int.from_bytes(idx[8 + 4 * 255 : 8 + 1024], "big")
    names = 8 + 1024
    offsets = names + 24 * count
    ids = [idx[names + 20 * i : names + 20 * i + 20].hex() for i in range(count)]
    i, j = ids.index(first), ids.index(second)
    out = bytearray(idx)
    a, b = offsets + 4 * i, offsets + 4 * j
    out[a : a + 4], out[b : b + 4] = idx[b : b + 4], idx[a : a + 4]
    return bytes(out)


def test_a_loose_commit_on_top_of_the_pack_verifies_in_process(world: Path, monkeypatch) -> None:
    """P3: git reads loose objects from the same object directories; so does the pack source."""
    _bare, checkout, _sha = _checkout(world)
    (checkout / "a.txt").write_bytes(b"alpha two\n")
    _git(checkout, "commit", "-q", "-am", "c2")
    loose = _git(checkout, "rev-parse", "HEAD").strip()
    _assert_loose(checkout, loose)
    pack = _verdict(checkout, loose, monkeypatch, git_only=False)
    git = _verdict(checkout, loose, monkeypatch, git_only=True)
    _assert_verdicts(pack, git, expected=True, pack_spawns=0)


def test_a_corrupt_loose_commit_on_top_of_the_pack_goes_to_git(world: Path, monkeypatch) -> None:
    """Mixed case: tree objects in the pack, a loose commit that does not inflate exactly."""
    _bare, checkout, _sha = _checkout(world)
    (checkout / "a.txt").write_bytes(b"alpha two\n")
    _git(checkout, "commit", "-q", "-am", "c2")
    loose = _git(checkout, "rev-parse", "HEAD").strip()
    path = checkout / ".git" / "objects" / loose[:2] / loose[2:]
    original = path.read_bytes()
    os.chmod(path, 0o644)
    path.write_bytes(original + b"x")
    pack = _verdict(checkout, loose, monkeypatch, git_only=False)
    git = _verdict(checkout, loose, monkeypatch, git_only=True)
    _assert_verdicts(pack, git, expected=git[0], pack_spawns=1)


def _loose_only_checkout(root: Path) -> tuple[Path, str]:
    """A checkout whose objects are all loose, as the in-process fetch writes them."""
    _bare, checkout, sha = _checkout(root)
    alternates = checkout / ".git" / "objects" / "info" / "alternates"
    source = Path(alternates.read_text(encoding="utf-8").strip())
    alternates.unlink()
    for pack in sorted((source / "pack").glob("*.pack")):
        with pack.open("rb") as handle:
            subprocess.run(
                ["git", "-C", str(checkout), "unpack-objects", "-q"], stdin=handle, check=True
            )
    return checkout, sha


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_a_loose_only_checkout_gives_git_verdict_without_git(
    world: Path, monkeypatch, shape: str
) -> None:
    """The in-process fetch writes loose objects: verify reads them with no cat-file spawn."""
    checkout, sha = _loose_only_checkout(world)
    _assert_loose(checkout, sha)
    if _SHAPES[shape] is not None:
        _SHAPES[shape](checkout)
    pack = _verdict(checkout, sha, monkeypatch, git_only=False)
    git = _verdict(checkout, sha, monkeypatch, git_only=True)
    _assert_verdicts(pack, git, expected=shape == "clean", pack_spawns=0)


@pytest.mark.parametrize("damage", ["truncate", "garbage", "header", "link"])
def test_a_damaged_loose_object_never_passes(world: Path, monkeypatch, damage: str) -> None:
    """A loose commit the reader cannot take exactly sends the package to git."""
    checkout, sha = _loose_only_checkout(world)
    path = checkout / ".git" / "objects" / sha[:2] / sha[2:]
    original = path.read_bytes()
    os.chmod(path, 0o644)
    if damage == "truncate":
        path.write_bytes(original[:-4])
    elif damage == "garbage":
        path.write_bytes(original + b"x")
    elif damage == "header":
        path.write_bytes(
            zlib.compress(b"commit 99999\0" + zlib.decompress(original).partition(b"\0")[2])
        )
    else:
        if sys.platform == "win32":
            pytest.skip("symlink creation needs a privilege on Windows")
        target = path.with_name("elsewhere")
        target.write_bytes(original)
        path.unlink()
        path.symlink_to(target)
    pack = _verdict(checkout, sha, monkeypatch, git_only=False)
    git = _verdict(checkout, sha, monkeypatch, git_only=True)
    # The reader refused (git answered the whole package) and the verdict is git's.
    _assert_verdicts(pack, git, expected=git[0], pack_spawns=1)


def test_a_replace_ref_sends_the_package_to_git(world: Path, monkeypatch) -> None:
    """P4: a replace ref can change what an id names, so git answers."""
    bare, checkout, sha = _checkout(world)
    (bare / "refs" / "replace").mkdir(parents=True)
    pack = _verdict(checkout, sha, monkeypatch, git_only=False)
    git = _verdict(checkout, sha, monkeypatch, git_only=True)
    _assert_verdicts(pack, git, expected=True, pack_spawns=1)


@pytest.mark.parametrize("value", ["0", "false", ""])
def test_a_switch_that_is_not_on_keeps_the_pack(world: Path, monkeypatch, value: str) -> None:
    """The kill switch reads like APM_NO_INPROCESS_FETCH: only 1, true, yes or on."""
    _bare, checkout, sha = _checkout(world)
    monkeypatch.setenv("APM_NO_INPROCESS_VERIFY", value)
    verdict = _verdict(checkout, sha, monkeypatch, git_only=False)
    _assert_verdicts(verdict, (True, 0), expected=True, pack_spawns=0)


def _assert_verdicts(pack, git, *, expected: bool, pack_spawns: int) -> None:
    assert pack[0] is git[0] is expected
    assert pack[1] == pack_spawns


def _assert_no_disagreement(positions: list[int]) -> None:
    assert positions == []


def _assert_loose(checkout: Path, oid: str) -> None:
    assert (checkout / ".git" / "objects" / oid[:2] / oid[2:]).is_file()
