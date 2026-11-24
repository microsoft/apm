"""Lever S: the cone in process is exactly what ``git sparse-checkout --cone`` checks out, or git.

W3d w3d:ruling:lever-s (S1-S12) and w3d:ruling:lever-s6prime (T1-T7). Packs come
from ``git pack-objects`` on a local repository, so the plan, the cone file and
the attribute rule are compared byte for byte with git itself, with no network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.cache import inprocess_fetch as fetch

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-git-transport-selection")]

_GIT = [
    "git",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.eol=lf",
    "-c",
    "core.symlinks=true",
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@t",
    "-c",
    "init.defaultBranch=main",
]

_TREE = {
    "README.md": b"root readme\n",
    "packages/guide/apm.yml": b"name: guide\n",
    "packages/guide/skills/a/SKILL.md": b"# a\n",
    "packages/guide/deep/x/y.md": b"y\n",
    "packages/other/apm.yml": b"name: other\n",
    "packages/top.md": b"ancestor file\n",
    "docs/index.md": b"not in the cone\n",
    "agents/one.agent.md": b"one\n",
    "agents/two.agent.md": b"two\n",
}


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        [*_GIT, *args], cwd=cwd, input=stdin, capture_output=True, check=True
    ).stdout


def _repo(tmp_path: Path, extra: dict[str, bytes] | None = None) -> tuple[Path, str]:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q")
    for rel, data in {**_TREE, **(extra or {})}.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "c")
    return src, _git(src, "rev-parse", "HEAD").decode().strip()


def _objects(src: Path, sha: str) -> dict[str, tuple[str, bytes]]:
    pack = _git(src, "pack-objects", "--stdout", "--revs", stdin=f"{sha}\n".encode())
    return fetch.parse_pack(pack)


def _git_sparse(src: Path, tmp_path: Path, cone: str) -> tuple[dict[str, bytes], bytes]:
    work = tmp_path / "git-sparse"
    _git(tmp_path, "clone", "-q", "--no-checkout", str(src), str(work))
    _git(work, "sparse-checkout", "set", "--cone", cone)
    _git(work, "checkout", "-q", "main")
    files = {
        p.relative_to(work).as_posix(): p.read_bytes()
        for p in sorted(work.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(work).parts
    }
    return files, (work / ".git" / "info" / "sparse-checkout").read_bytes()


@pytest.mark.parametrize("cone", ["packages/guide", "agents/one.agent.md", "agents"])
def test_the_cone_and_its_file_equal_git_sparse_checkout(tmp_path: Path, cone: str) -> None:
    src, sha = _repo(tmp_path)
    objects = _objects(src, sha)
    files, trees = fetch._sparse_plan(objects, sha, (cone,))
    ours = {rel: objects[oid][1] for rel, oid, _ in files}
    theirs, cone_file = _git_sparse(src, tmp_path, cone)
    assert ours == theirs
    assert fetch.sparse_checkout_file((cone,)).encode() == cone_file
    assert all(objects[t][0] in ("tree", "commit") for t in trees)


def _plan_with(tmp_path: Path, extra: dict[str, bytes], cone: str = "packages/guide"):
    src, sha = _repo(tmp_path, extra)
    objects = _objects(src, sha)
    files, _ = fetch._sparse_plan(objects, sha, (cone,))
    return src, sha, objects, files


def _attributes_inert(objects, files) -> bool:
    return all(
        fetch._cone_attributes_inert(objects[oid][1])
        for rel, oid, _ in files
        if rel.rpartition("/")[2] == ".gitattributes"
    )


def test_a_crlf_blob_under_text_auto_eol_lf_is_written_as_git_writes_it(tmp_path: Path) -> None:
    extra = {
        ".gitattributes": b"* text=auto eol=lf\n*.md text eol=lf\n*.bin -text\n",
        "packages/guide/crlf.md": b"line one\r\nline two\r\n",
    }
    src, _sha, objects, files = _plan_with(tmp_path, extra)
    assert _attributes_inert(objects, files)
    ours = {rel: objects[oid][1] for rel, oid, _ in files}
    theirs, _ = _git_sparse(src, tmp_path, "packages/guide")
    assert ours == theirs


@pytest.mark.parametrize(
    "attributes",
    [
        b"* text eol=crlf\n",
        b"*.md eol=crlf\n",
        b"* crlf\n",
        b"*.bin filter=lfs diff=lfs merge=lfs -text\n",
        b"*.md ident\n",
        b"*.utf16 working-tree-encoding=UTF-16\n",
        b"[attr]mine text eol=crlf\n",
        b'"*.md" text\n',
        b"*.md unknown-token\n",
    ],
)
def test_a_byte_changing_attribute_sends_the_dep_to_git(tmp_path: Path, attributes: bytes) -> None:
    _src, _sha, objects, files = _plan_with(tmp_path, {".gitattributes": attributes})
    assert not _attributes_inert(objects, files)


def test_a_nested_in_cone_eol_crlf_sends_the_dep_to_git(tmp_path: Path) -> None:
    extra = {
        ".gitattributes": b"* text=auto eol=lf\n",
        "packages/guide/.gitattributes": b"*.md eol=crlf\n",
    }
    _src, _sha, objects, files = _plan_with(tmp_path, extra)
    assert not _attributes_inert(objects, files)


def test_an_out_of_cone_attributes_file_does_not_apply(tmp_path: Path) -> None:
    _src, _sha, objects, files = _plan_with(tmp_path, {"docs/.gitattributes": b"* eol=crlf\n"})
    assert "docs/.gitattributes" not in {rel for rel, _, _ in files}
    assert _attributes_inert(objects, files)


@pytest.mark.parametrize(
    "alias", [".GitAttributes", ".gitattributes.", ".gitattributes ", ".gitattr\u200cibutes"]
)
def test_an_alias_of_gitattributes_refuses(tmp_path: Path, alias: str) -> None:
    if alias != ".gitattr\u200cibutes" and os.name == "nt":
        pytest.skip("the filesystem folds this name")
    src, sha = _repo(tmp_path)
    blob = _git(src, "hash-object", "-w", "--stdin", stdin=b"* eol=crlf\n").decode().strip()
    name = alias if alias != ".gitattr\u200cibutes" else ".git\u200cattributes"
    _git(src, "update-index", "--add", "--cacheinfo", f"100644,{blob},packages/guide/{name}")
    _git(src, "commit", "-q", "-m", "alias")
    sha = _git(src, "rev-parse", "HEAD").decode().strip()
    with pytest.raises(fetch._Refuse):
        fetch._sparse_plan(_objects(src, sha), sha, ("packages/guide",))


def test_a_linked_gitattributes_refuses(tmp_path: Path) -> None:
    src, sha = _repo(tmp_path)
    blob = _git(src, "hash-object", "-w", "--stdin", stdin=b"README.md").decode().strip()
    _git(src, "update-index", "--add", "--cacheinfo", f"120000,{blob},.gitattributes")
    _git(src, "commit", "-q", "-m", "link")
    sha = _git(src, "rev-parse", "HEAD").decode().strip()
    with pytest.raises(fetch._Refuse):
        fetch._sparse_plan(_objects(src, sha), sha, ("packages/guide",))


def test_a_link_in_the_cone_refuses_and_one_outside_is_ignored(tmp_path: Path) -> None:
    src, sha = _repo(tmp_path)
    blob = _git(src, "hash-object", "-w", "--stdin", stdin=b"../README.md").decode().strip()
    _git(src, "update-index", "--add", "--cacheinfo", f"120000,{blob},docs/link")
    _git(src, "commit", "-q", "-m", "out")
    sha = _git(src, "rev-parse", "HEAD").decode().strip()
    fetch._sparse_plan(_objects(src, sha), sha, ("packages/guide",))
    _git(src, "update-index", "--add", "--cacheinfo", f"120000,{blob},packages/guide/link")
    _git(src, "commit", "-q", "-m", "in")
    sha = _git(src, "rev-parse", "HEAD").decode().strip()
    with pytest.raises(fetch._Refuse):
        fetch._sparse_plan(_objects(src, sha), sha, ("packages/guide",))
