"""The in-process pinned fetch writes only bytes whose ids it computed, or refuses (#4939).

Packs are built here byte by byte, so each refusal is exercised without a
network or a git child. The killed control shows that keying objects by the
id the parser computes, not by any id the pack claims, is what stops a
tampered blob.
"""

from __future__ import annotations

import hashlib
import os
import zlib
from pathlib import Path

import pytest

from apm_cli.cache import inprocess_fetch as fetch

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-git-transport-selection")]

_TYPE = {"commit": 1, "tree": 2, "blob": 3}


def _header(kind: int, size: int) -> bytes:
    out = bytearray()
    byte = (kind << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(byte | 0x80)
        byte = size & 0x7F
        size >>= 7
    out.append(byte)
    return bytes(out)


def _pack(entries: list[bytes]) -> bytes:
    count = len(entries).to_bytes(4, "big")
    body = b"PACK" + (2).to_bytes(4, "big") + count + b"".join(entries)
    return body + hashlib.sha1(body).digest()


def _obj(kind: str, data: bytes, *, declared: int | None = None) -> bytes:
    size = len(data) if declared is None else declared
    return _header(_TYPE[kind], size) + zlib.compress(data)


def _oid(kind: str, data: bytes) -> str:
    return hashlib.sha1(b"%s %d\x00" % (kind.encode(), len(data)) + data).hexdigest()


def _repo(blob: bytes = b"# skill\n", name: bytes = b"SKILL.md"):
    """``(commit id, tree id, blob id, objects)`` of a one-file commit."""
    blob_id = _oid("blob", blob)
    tree = b"100644 " + name + b"\x00" + bytes.fromhex(blob_id)
    tree_id = _oid("tree", tree)
    commit = f"tree {tree_id}\nauthor a <a> 0 +0000\ncommitter a <a> 0 +0000\n\nm\n".encode()
    objects = [("commit", commit), ("tree", tree), ("blob", blob)]
    return _oid("commit", commit), tree_id, blob_id, objects


def test_a_clean_pack_plans_the_pinned_worktree() -> None:
    sha, _tree, blob_id, objects = _repo()
    parsed = fetch.parse_pack(_pack([_obj(k, d) for k, d in objects]))
    files, reached = fetch._plan(parsed, sha)
    assert files == [("SKILL.md", blob_id, False)]
    assert blob_id in reached and sha in reached


def test_a_tampered_blob_byte_is_refused() -> None:
    sha, _tree, _blob, objects = _repo()
    tampered = [(k, (d[:-2] + b"X\n") if k == "blob" else d) for k, d in objects]
    parsed = fetch.parse_pack(_pack([_obj(k, d) for k, d in tampered]))
    with pytest.raises(fetch._Refuse, match="blob missing"):
        fetch._plan(parsed, sha)


def test_killed_control_trusting_a_claimed_id_would_admit_the_tampered_blob() -> None:
    """Mutation control: an object table keyed by the pack's claimed ids plans the tamper."""
    sha, _tree, blob_id, objects = _repo()
    claimed = {_oid(k, d): (k, d) for k, d in objects}
    claimed[blob_id] = ("blob", b"# evil!\n")  # what a parser trusting claimed ids would hold
    files, _reached = fetch._plan(claimed, sha)
    assert files == [("SKILL.md", blob_id, False)]
    assert claimed[blob_id][1] != objects[2][1]


def test_an_extra_unreachable_commit_is_never_written(tmp_path: Path) -> None:
    sha, _tree, _blob, objects = _repo()
    stray = b"tree " + b"0" * 40 + b"\nauthor a <a> 0 +0000\ncommitter a <a> 0 +0000\n\nstray\n"
    parsed = fetch.parse_pack(_pack([_obj(k, d) for k, d in [*objects, ("commit", stray)]]))
    _files, reached = fetch._plan(parsed, sha)
    staged = tmp_path / "staged"
    staged.mkdir()
    fetch._write_git_dir(staged, "https://github.com/o/r", sha, parsed, reached, ())
    written = {
        p.parent.name + p.name for p in (staged / ".git" / "objects").rglob("*") if p.is_file()
    }
    assert _oid("commit", stray) not in written
    assert sha in written


def test_a_small_pack_declaring_a_huge_object_is_refused() -> None:
    entry = _header(_TYPE["blob"], fetch._MAX_OBJECT + 1) + zlib.compress(b"x")
    with pytest.raises(fetch._Refuse, match="size cap"):
        fetch.parse_pack(_pack([entry]))


def test_an_object_inflating_past_its_declared_size_is_refused() -> None:
    bomb = b"\0" * 64 * 1024  # compresses to well under 1 KB
    with pytest.raises(fetch._Refuse, match="declared size"):
        fetch.parse_pack(_pack([_obj("blob", bomb, declared=16)]))


def _ofs_delta(base_size: int, start: int, base_start: int) -> bytes:
    delta = bytes([base_size, base_size, 0x80 | 0x10, base_size])  # copy the whole base
    offset = start - base_start
    encoded = bytearray([offset & 0x7F])
    offset >>= 7
    while offset:
        offset -= 1
        encoded.insert(0, 0x80 | (offset & 0x7F))
        offset >>= 7
    return _header(6, len(delta)) + bytes(encoded) + zlib.compress(delta)


def test_a_delta_chain_deeper_than_the_cap_is_refused() -> None:
    base = b"abc"
    entries = [_obj("blob", base)]
    starts = [12]
    for _ in range(fetch._MAX_DELTA_DEPTH + 1):
        start = starts[-1] + len(entries[-1])
        entries.append(_ofs_delta(len(base), start, starts[-1]))
        starts.append(start)
    with pytest.raises(fetch._Refuse, match="depth cap"):
        fetch.parse_pack(_pack(entries))


@pytest.mark.parametrize(
    "name",
    [".git", "GIT~2", ".g\u200cit".encode().decode(), "con.txt", "a:b", "x.", ".gitmodules"],
)
def test_a_name_git_must_handle_is_refused(name: str) -> None:
    with pytest.raises(fetch._Refuse):
        fetch._safe_name(name.encode("utf-8"))


def test_a_symlink_or_submodule_entry_falls_back_to_git() -> None:
    blob = b"target"
    blob_id = _oid("blob", blob)
    tree = b"120000 link\x00" + bytes.fromhex(blob_id)
    tree_id = _oid("tree", tree)
    commit = f"tree {tree_id}\n\nm\n".encode()
    parsed = fetch.parse_pack(
        _pack([_obj("commit", commit), _obj("tree", tree), _obj("blob", blob)])
    )
    with pytest.raises(fetch._Refuse, match="mode 120000"):
        fetch._plan(parsed, _oid("commit", commit))


def test_a_non_github_or_non_sha1_pin_never_fetches(tmp_path: Path) -> None:
    pin = "ab" * 32  # a SHA-256 object id: never served in process
    assert fetch.fetch_worktree("https://github.com/o/r", pin, tmp_path, os.environ, ()) is None
