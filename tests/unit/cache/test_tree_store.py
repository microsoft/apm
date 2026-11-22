"""Sealed cache entries: verify-by-hash on read, copy-on-write or linked materialization."""

from __future__ import annotations

import errno
import json
import os
import sys
from pathlib import Path

import pytest

from apm_cli.cache import tree_store
from apm_cli.cache.git_cache import GitCache
from apm_cli.cache.tree_store import (
    TreeIntegrityError,
    materialize_cached_checkout,
    read_seal,
    seal_path,
    verified_seal,
)
from apm_cli.utils import reflink
from apm_cli.utils.path_security import PathTraversalError, safe_rmtree
from apm_cli.utils.reflink import reflink_supported

pytestmark = [pytest.mark.component, pytest.mark.promise("P-LAW-source.ref_cache_coherent")]

_FILES = {
    "apm.yml": b"name: pkg\nversion: 1.0.0\n",
    ".apm/instructions/a.instructions.md": b"# A\n",
    ".apm/skills/s/SKILL.md": b"# S\n",
    "bin/run.sh": b"#!/bin/sh\necho hi\n",
}


def _entry(root: Path) -> Path:
    """A checkout-shaped cache entry: content plus a ``.git`` directory."""
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("0" * 40 + "\n", encoding="ascii")
    for rel, data in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "bin" / "run.sh").chmod(0o755)
    (root / "empty").mkdir()
    return root


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _materialize(entry: Path, dest: Path, calls: list[int] | None = None, **kwargs):
    dest.mkdir(parents=True, exist_ok=True)
    calls = calls if calls is not None else []
    return materialize_cached_checkout(
        entry, dest, validate_links=lambda: calls.append(1), **kwargs
    )


@pytest.mark.parametrize("strategy", ["clone", "link", "copy"])
def test_materialized_tree_matches_the_entry(tmp_path, monkeypatch, strategy):
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, strategy)
    entry = _entry(tmp_path / "cache" / "full")
    dest = tmp_path / "apm_modules" / "pkg"

    stats = _materialize(entry, dest)

    assert _tree(dest) == _tree(entry)
    if strategy == "link":
        assert stats.links == len(_FILES)
    elif strategy == "clone" and sys.platform == "darwin" and reflink_supported():
        assert stats.clones == len(read_seal(entry).top_level())
    assert (dest / "empty").is_dir()
    assert not (dest / ".git").exists()
    if sys.platform != "win32":
        assert os.stat(dest / "bin" / "run.sh").st_mode & 0o100
    assert read_seal(entry) is not None


@pytest.mark.parametrize("strategy", ["clone", "copy"])
def test_editing_materialized_file_does_not_change_cache(tmp_path, monkeypatch, strategy):
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, strategy)
    entry = _entry(tmp_path / "cache" / "full")
    dest = tmp_path / "apm_modules" / "pkg"
    _materialize(entry, dest)

    with open(dest / "apm.yml", "ab") as handle:  # in-place edit, same inode
        handle.write(b"tampered: true\n")

    assert (entry / "apm.yml").read_bytes() == _FILES["apm.yml"]
    assert verified_seal(entry) is not None
    again = tmp_path / "other" / "apm_modules" / "pkg"
    _materialize(entry, again)
    assert (again / "apm.yml").read_bytes() == _FILES["apm.yml"]


def test_edited_hardlink_is_detected_and_never_reused(tmp_path, monkeypatch):
    """A hard link shares the inode: the next read must refuse and evict the entry."""
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, "link")
    entry = _entry(tmp_path / "cache" / "full")
    dest = tmp_path / "apm_modules" / "pkg"
    _materialize(entry, dest)
    with open(dest / "apm.yml", "ab") as handle:
        handle.write(b"tampered: true\n")

    again = tmp_path / "other" / "apm_modules" / "pkg"
    with pytest.raises(TreeIntegrityError):
        _materialize(entry, again)

    assert not entry.exists()
    assert list(again.iterdir()) == []


def test_tampered_cache_entry_is_evicted_before_any_byte_is_materialized(tmp_path, monkeypatch):
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, "copy")
    entry = _entry(tmp_path / "cache" / "full")
    _materialize(entry, tmp_path / "first")
    (entry / ".apm" / "skills" / "s" / "SKILL.md").write_bytes(b"# evil\n")
    (entry / "extra.md").write_bytes(b"new\n")

    dest = tmp_path / "second"
    with pytest.raises(TreeIntegrityError):
        _materialize(entry, dest)

    assert not entry.exists()
    assert list(dest.iterdir()) == []


@pytest.mark.parametrize("bad_path", ["../escape", "/abs", "a/../../b", "a\\..\\b"])
def test_seal_naming_a_path_outside_the_entry_is_rejected(tmp_path, monkeypatch, bad_path):
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, "link")
    entry = _entry(tmp_path / "cache" / "full")
    _materialize(entry, tmp_path / "first")
    data = json.loads(seal_path(entry).read_text(encoding="utf-8"))
    data["files"][0][0] = bad_path
    seal_path(entry).write_text(json.dumps(data), encoding="utf-8")

    dest = tmp_path / "second"
    with pytest.raises(TreeIntegrityError):
        _materialize(entry, dest)
    assert not (tmp_path / "escape").exists()
    assert list(dest.iterdir()) == []


def test_hardlink_unavailable_falls_back_to_copy(tmp_path, monkeypatch):
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, "link")

    def _no_link(src, dst, **_):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(tree_store.os, "link", _no_link)
    entry = _entry(tmp_path / "cache" / "full")
    dest = tmp_path / "apm_modules" / "pkg"

    stats = _materialize(entry, dest)

    assert (stats.links, stats.copies) == (0, len(_FILES))
    for rel, data in _FILES.items():
        assert (dest / rel).read_bytes() == data
        assert not os.path.samefile(entry / rel, dest / rel)


def test_uninstall_and_prune_never_remove_content_another_tree_uses(tmp_path, monkeypatch):
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, "link")
    cache = GitCache(tmp_path / "cache_root")
    entry = _entry(cache._checkouts_root / "shard" / ("a" * 40) / "full")
    modules_a = tmp_path / "a" / "apm_modules"
    modules_b = tmp_path / "b" / "apm_modules"
    _materialize(entry, modules_a / "pkg")
    _materialize(entry, modules_b / "pkg")

    safe_rmtree(modules_a / "pkg", modules_a)  # what `apm uninstall` runs
    assert verified_seal(entry) is not None
    assert _tree(modules_b / "pkg") == _tree(entry)

    expected = _tree(entry)
    os.utime(entry.parent, (0, 0))
    assert cache.prune(max_age_days=1) == 1  # what `apm cache prune` runs
    assert not entry.exists()
    assert _tree(modules_b / "pkg") == expected


def test_link_verdict_is_recorded_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, "copy")
    entry = _entry(tmp_path / "cache" / "full")
    calls: list[int] = []

    _materialize(entry, tmp_path / "one", calls)
    _materialize(entry, tmp_path / "two", calls)

    assert calls == [1]
    assert read_seal(entry).link_failures == ()
    assert (tmp_path / "two" / "apm.yml").read_bytes() == _FILES["apm.yml"]
    assert json.loads(seal_path(entry).read_text(encoding="utf-8"))["link_failures"] == []


@pytest.mark.parametrize("recorded", [None, ["docs/broken-link"]])
def test_unknown_or_failing_link_verdict_still_raises_the_strict_diagnostic(
    tmp_path, monkeypatch, recorded
):
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, "copy")
    entry = _entry(tmp_path / "cache" / "full")
    _materialize(entry, tmp_path / "seed")
    data = json.loads(seal_path(entry).read_text(encoding="utf-8"))
    data["link_failures"] = recorded
    seal_path(entry).write_text(json.dumps(data), encoding="utf-8")

    def _strict() -> None:
        raise RuntimeError("symlink escapes the repository")

    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(RuntimeError, match="escapes"):
        materialize_cached_checkout(entry, dest, validate_links=_strict)
    assert list(dest.iterdir()) == []


@pytest.mark.parametrize("read_only", [False, True])
def test_cache_entry_link_pointing_outside_is_refused_before_any_write(
    tmp_path, monkeypatch, read_only
):
    if sys.platform == "win32":
        pytest.skip("symlink creation needs privileges on Windows")
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, "copy")
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir()
    secret.write_bytes(b"host secret\n")
    entry = _entry(tmp_path / "cache" / "full")
    (entry / ".apm" / "leak.md").symlink_to(secret)
    dest = tmp_path / "apm_modules" / "pkg"

    with pytest.raises(TreeIntegrityError, match="symlink"):
        _materialize(entry, dest, read_only=read_only)

    assert list(dest.iterdir()) == []
    assert entry.exists() is read_only
    assert secret.read_bytes() == b"host secret\n"


@pytest.mark.parametrize("strategy", ["clone", "link", "copy"])
def test_destination_leaving_the_deploy_root_is_refused_before_any_write(
    tmp_path, monkeypatch, strategy
):
    if sys.platform == "win32":
        pytest.skip("symlink creation needs privileges on Windows")
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, strategy)
    entry = _entry(tmp_path / "cache" / "full")
    outside = tmp_path / "outside"
    outside.mkdir()
    modules = tmp_path / "apm_modules"
    modules.mkdir()
    (modules / "pkg").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathTraversalError):
        materialize_cached_checkout(entry, modules / "pkg", validate_links=lambda: None)

    assert list(outside.iterdir()) == []
    assert (entry / "apm.yml").read_bytes() == _FILES["apm.yml"]


def test_read_only_reader_never_writes_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, "copy")
    entry = _entry(tmp_path / "cache" / "full")
    before = sorted(p.name for p in entry.parent.iterdir())
    calls: list[int] = []

    _materialize(entry, tmp_path / "dest", calls, read_only=True)

    assert calls == [1]
    assert read_seal(entry) is None
    assert sorted(p.name for p in entry.parent.iterdir()) == before
    assert _tree(tmp_path / "dest") == _tree(entry)


def test_pipeline_rewrite_of_a_linked_file_does_not_reach_the_cache(tmp_path, monkeypatch):
    """stamp_plugin_version and plugin normalization rewrite apm.yml with write_text_lf."""
    from apm_cli.utils.atomic_io import write_text_lf

    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, "link")
    entry = _entry(tmp_path / "cache" / "full")
    dest = tmp_path / "apm_modules" / "pkg"
    _materialize(entry, dest)

    write_text_lf(dest / "apm.yml", "name: pkg\nversion: abc1234\n")
    write_text_lf(dest / "bin" / "run.sh", "#!/bin/sh\necho stamped\n")

    assert (dest / "apm.yml").read_text(encoding="utf-8") == "name: pkg\nversion: abc1234\n"
    assert (entry / "apm.yml").read_bytes() == _FILES["apm.yml"]
    assert (entry / "bin" / "run.sh").read_bytes() == _FILES["bin/run.sh"]
    if sys.platform != "win32":
        assert os.stat(dest / "bin" / "run.sh").st_mode & 0o100
    assert verified_seal(entry) is not None


def test_linux_clone_keeps_the_source_mode(tmp_path, monkeypatch):
    """FICLONE copies data only; a cloned root-level executable must stay executable."""
    if sys.platform == "win32":
        pytest.skip("POSIX mode bits")
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, "clone")
    fcntl = pytest.importorskip("fcntl")
    monkeypatch.delenv("APM_NO_REFLINK", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    reflink._reset_capability_cache()

    def _ficlone(dst_fd, _request, src_fd):
        """The kernel clones data only: the new file keeps its 0600 creation mode."""
        os.lseek(src_fd, 0, os.SEEK_SET)
        while chunk := os.read(src_fd, 1 << 16):
            os.write(dst_fd, chunk)

    monkeypatch.setattr(fcntl, "ioctl", _ficlone)
    entry = _entry(tmp_path / "cache" / "full")
    (entry / "tool.sh").write_bytes(b"#!/bin/sh\n")
    (entry / "tool.sh").chmod(0o755)
    dest = tmp_path / "apm_modules" / "pkg"

    _materialize(entry, dest)

    assert os.stat(dest / "tool.sh").st_mode & 0o777 == 0o755
    assert (dest / "tool.sh").read_bytes() == b"#!/bin/sh\n"


def test_linked_git_dir_never_receives_the_seal(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("symlink creation needs privileges on Windows")
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, "copy")
    entry = _entry(tmp_path / "cache" / "full")
    outside = tmp_path / "outside-git"
    (entry / ".git").rename(outside)
    (entry / ".git").symlink_to(outside, target_is_directory=True)
    dest = tmp_path / "dest"

    with pytest.raises(TreeIntegrityError, match="linked seal path"):
        _materialize(entry, dest)

    assert not (outside / tree_store.SEAL_NAME).exists()
    assert list(dest.iterdir()) == []
    assert not entry.exists()


@pytest.mark.parametrize("bad", ["", {}, "docs/x"])
def test_seal_with_a_malformed_link_verdict_is_evicted(tmp_path, monkeypatch, bad):
    monkeypatch.setenv(tree_store.MATERIALIZE_ENV, "copy")
    entry = _entry(tmp_path / "cache" / "full")
    _materialize(entry, tmp_path / "seed")
    data = json.loads(seal_path(entry).read_text(encoding="utf-8"))
    data["link_failures"] = bad
    seal_path(entry).write_text(json.dumps(data), encoding="utf-8")
    calls: list[int] = []

    with pytest.raises(TreeIntegrityError, match="malformed seal"):
        _materialize(entry, tmp_path / "dest", calls)

    assert calls == []
    assert not entry.exists()
    assert list((tmp_path / "dest").iterdir()) == []
