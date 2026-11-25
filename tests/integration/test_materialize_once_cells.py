"""M2 materialize-once: a package is written once, from objects, into its final tree.

W3d ``materialize-once`` MS1-MS7. ``materialize_verified_copy`` fills an empty
``apm_modules`` target from the verified checkout's objects: one write per
file, every object re-hashed against its id as it is read, each file created
exclusively without following links, with the mode of its tree entry. Anything
it cannot prove equal to git's checkout leaves the target empty and takes the
copy + verify path unchanged (MS5).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.cache import checkout_verify
from apm_cli.cache.checkout_verify import CheckoutVerificationError, materialize_verified_copy
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
    monkeypatch.delenv("APM_NO_INPROCESS_CHECKOUT", raising=False)
    monkeypatch.delenv("APM_NO_INPROCESS_VERIFY", raising=False)
    reset_git_cache()
    checkout_verify.forget_verdicts()
    yield tmp_path
    checkout_verify.forget_verdicts()
    reset_git_cache()


_GIT = ("git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "user.name=t", "-c")
_GIT = (*_GIT, "user.email=t@t")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [*_GIT, *args],
        cwd=cwd,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")


def _checkout(root: Path, extra=None) -> tuple[Path, str]:
    """A verified-shape cache checkout: a clone of one commit, objects in its packs."""
    src = root / "src"
    src.mkdir()
    _git(src, "init", "-q")
    for rel, data in _FILES.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(data)
    _git(src, "add", "-A")
    _git(src, "update-index", "--chmod=+x", "dir/sub/c.sh")
    if extra:
        extra(src)
    _git(src, "commit", "-q", "-m", "c")
    sha = _git(src, "rev-parse", "HEAD").strip()
    checkout = root / "cache" / sha
    _git(root, "clone", "-q", "--no-local", str(src), str(checkout))
    _git(checkout, "checkout", "-q", "--detach", sha)
    return checkout, sha


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 -- git ids


def _materialize(checkout: Path, sha: str, dest: Path, subdir: str = "") -> None:
    dest.mkdir(parents=True)
    materialize_verified_copy(dest, checkout, sha, subdir)


def test_each_written_file_is_its_pinned_blob_and_nothing_is_copied(world) -> None:
    """MS1 + no second copy: bytes come from the objects, never from the checkout's files.

    A file deleted from the cache worktree (its blob intact) would make the copy
    path fail "missing from the copy"; the object path writes it.
    """
    checkout, sha = _checkout(world)
    (checkout / "a.txt").unlink()
    dest = world / "apm_modules" / "o" / "r"
    _materialize(checkout, sha, dest)
    _assert_pinned_tree(dest, checkout, sha)


def _assert_pinned_tree(dest: Path, checkout: Path, sha: str) -> None:
    """MS1/MS7: bytes equal to the commit and each file's hash is the pinned blob id."""
    pinned = {}
    for line in _git(checkout, "ls-tree", "-r", sha).splitlines():
        meta, _, path = line.partition("\t")
        mode, _kind, oid = meta.split()
        pinned[path] = (mode, oid)
    written = _tree(dest)
    assert written == _FILES
    assert {path: _blob_id(data) for path, data in written.items()} == {
        path: oid for path, (_mode, oid) in pinned.items()
    }
    assert not (dest / ".git").exists()
    if os.name != "nt":
        for path, (mode, _oid) in pinned.items():
            assert os.access(dest / path, os.X_OK) is (mode == "100755"), path


def test_a_subdirectory_is_written_once_from_its_subtree(world) -> None:
    checkout, sha = _checkout(world)
    (checkout / "dir" / "b.md").unlink()
    dest = world / "apm_modules" / "o" / "r" / "dir"
    _materialize(checkout, sha, dest, subdir="dir")
    _assert_subtree(dest)


def _assert_subtree(dest: Path) -> None:
    assert _tree(dest) == {"b.md": _FILES["dir/b.md"], "sub/c.sh": _FILES["dir/sub/c.sh"]}


def test_an_attributes_file_takes_the_copy_and_verify_path(world) -> None:
    """MS5: a tree the object path cannot prove (here ``.gitattributes``) goes the old way.

    The copy path reads the checkout's files, so a file missing from the cache
    worktree surfaces there; with the worktree intact it yields git's tree.
    """

    def attributes(src: Path) -> None:
        (src / ".gitattributes").write_bytes(b"*.md -text\n")
        _git(src, "add", "-A")

    checkout, sha = _checkout(world, attributes)
    _materialize(checkout, sha, world / "intact")
    _assert_copied_like_git(world / "intact")
    checkout_verify.forget_verdicts()
    (checkout / "a.txt").unlink()
    with pytest.raises(
        CheckoutVerificationError, match="does not match its commit|missing from the copy"
    ):
        _materialize(checkout, sha, world / "missing")


def _assert_copied_like_git(dest: Path) -> None:
    assert _tree(dest) == {**_FILES, ".gitattributes": b"*.md -text\n"}


def test_a_tampered_cache_object_is_never_materialized(world) -> None:
    """MS6: a cache object that does not hash to its id never yields a byte."""
    checkout, sha = _checkout(world)
    _git(checkout, "repack", "-q", "-a", "-d")
    (pack,) = (checkout / ".git" / "objects" / "pack").glob("*.pack")
    data = bytearray(pack.read_bytes())
    data[len(data) // 2] ^= 0xFF
    os.chmod(pack, 0o644)
    pack.write_bytes(bytes(data))
    dest = world / "apm_modules" / "o" / "r"
    with pytest.raises((CheckoutVerificationError, RuntimeError, OSError)):
        _materialize(checkout, sha, dest)
    _assert_no_unverified_bytes(dest)


def _assert_no_unverified_bytes(dest: Path) -> None:
    """Whatever stayed in *dest* matches the commit byte for byte, or nothing stayed."""
    left = _tree(dest) if dest.exists() else {}
    assert all(_FILES.get(path) == data for path, data in left.items())


def test_a_non_empty_target_is_never_merged_into(world) -> None:
    """The object path writes only into an empty target; a stray file fails verification."""
    checkout, sha = _checkout(world)
    dest = world / "apm_modules" / "o" / "r"
    dest.mkdir(parents=True)
    (dest / "stray.txt").write_bytes(b"left over\n")
    with pytest.raises(CheckoutVerificationError):
        materialize_verified_copy(dest, checkout, sha)
    assert (dest / "stray.txt").read_bytes() == b"left over\n"
