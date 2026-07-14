from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path, PureWindowsPath

import pytest

from tests.utils.artifact_snapshot import (
    ArtifactSnapshot,
    _portable_path,
    assert_only_paths_changed,
    assert_paths_absent,
    assert_paths_created,
    assert_paths_present,
    assert_unchanged,
)


def test_capture_is_read_only_and_portable(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    source = nested / "source.bin"
    source_bytes = b"\x00before\r\n\xff"
    source.write_bytes(source_bytes)
    before_stat = source.stat()

    snapshot = ArtifactSnapshot.capture(tmp_path)

    after_stat = source.stat()
    assert source.read_bytes() == source_bytes
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert snapshot.paths == frozenset({"nested", "nested/source.bin"})
    source_entry = snapshot.entries[1]
    assert source_entry.relative_path == "nested/source.bin"
    assert source_entry.kind == "file"
    assert source_entry.fingerprint == hashlib.sha256(source_bytes).hexdigest()
    assert _portable_path(PureWindowsPath(r"nested\source.bin")) == "nested/source.bin"
    with pytest.raises(FrozenInstanceError):
        source_entry.__setattr__("kind", "directory")


def test_diff_observes_created_changed_and_removed_paths(tmp_path: Path) -> None:
    changed = tmp_path / "changed.txt"
    removed = tmp_path / "removed.txt"
    changed.write_bytes(b"before")
    removed.write_bytes(b"remove")
    before = ArtifactSnapshot.capture(tmp_path)

    changed.write_bytes(b"after")
    removed.unlink()
    (tmp_path / "created.txt").write_bytes(b"create")
    after = ArtifactSnapshot.capture(tmp_path)

    difference = before.diff(after)
    assert difference.added == frozenset({"created.txt"})
    assert difference.removed == frozenset({"removed.txt"})
    assert difference.changed == frozenset({"changed.txt"})
    with pytest.raises(FrozenInstanceError):
        difference.__setattr__("changed", frozenset())


def test_assertion_helpers_compare_captured_state(tmp_path: Path) -> None:
    before = ArtifactSnapshot.capture(tmp_path)
    lockfile = tmp_path / "apm.lock.yaml"
    lockfile.write_bytes(b"lock-version: 2\n")
    after = ArtifactSnapshot.capture(tmp_path)

    assert_paths_absent(before, {"apm.lock.yaml"})
    assert_paths_present(after, {"apm.lock.yaml"})
    assert_paths_created(before, after, {"apm.lock.yaml"})
    assert_only_paths_changed(before, after, {"apm.lock.yaml"})
    assert_unchanged(after, ArtifactSnapshot.capture(tmp_path))

    with pytest.raises(AssertionError, match="Missing artifact paths"):
        assert_paths_present(before, {"apm.lock.yaml"})
    with pytest.raises(AssertionError, match="Unexpected artifact paths"):
        assert_paths_absent(after, {"apm.lock.yaml"})
    with pytest.raises(AssertionError, match="Paths were not created"):
        assert_paths_created(after, after, {"apm.lock.yaml"})
    with pytest.raises(AssertionError, match="Unexpected artifact changes"):
        assert_unchanged(before, after)
    with pytest.raises(AssertionError, match="Unexpected changed paths"):
        assert_only_paths_changed(before, after, set())

    empty_root = tmp_path / "empty-root"
    empty_root.mkdir()
    present_root = ArtifactSnapshot.capture(empty_root)
    empty_root.rmdir()
    deleted_root = ArtifactSnapshot.capture(empty_root)
    assert present_root.root_existed is True
    assert deleted_root.root_existed is False
    with pytest.raises(AssertionError, match="Root existence changed"):
        assert_unchanged(present_root, deleted_root)

    empty_root.mkdir()
    recreated_root = ArtifactSnapshot.capture(empty_root)
    assert recreated_root.root_existed is True
    with pytest.raises(AssertionError, match="Root existence changed"):
        assert_unchanged(deleted_root, recreated_root)


def test_assertions_reject_authored_expected_mappings(tmp_path: Path) -> None:
    snapshot = ArtifactSnapshot.capture(tmp_path)

    with pytest.raises(TypeError, match="ArtifactSnapshot"):
        assert_unchanged({}, snapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.__setattr__("root_existed", False)
