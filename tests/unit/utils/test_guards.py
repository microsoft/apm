"""Tests for ``utils.guards._ReadOnlyProjectGuard``."""

from __future__ import annotations

import os
import time

import pytest

from apm_cli.utils.guards import (
    ProtectedPathMutationError,
    _ReadOnlyProjectGuard,
)


def _bump_mtime(p):
    now_ns = time.time_ns()
    os.utime(p, ns=(now_ns, now_ns))


def test_no_mutation_passes(tmp_path):
    apm_dir = tmp_path / ".apm"
    apm_dir.mkdir()
    (apm_dir / "x.md").write_text("x")
    with _ReadOnlyProjectGuard(tmp_path, [".apm"]):
        pass


def test_modification_raises(tmp_path):
    apm_dir = tmp_path / ".apm"
    apm_dir.mkdir()
    target = apm_dir / "x.md"
    target.write_bytes(b"original")

    with pytest.raises(ProtectedPathMutationError, match=r"modified"):
        with _ReadOnlyProjectGuard(tmp_path, [".apm"]):
            time.sleep(0.01)
            target.write_bytes(b"changed-content-different-size")
            _bump_mtime(target)


def test_creation_under_protected_root_raises(tmp_path):
    apm_dir = tmp_path / ".apm"
    apm_dir.mkdir()
    with pytest.raises(ProtectedPathMutationError, match=r"created"):
        with _ReadOnlyProjectGuard(tmp_path, [".apm"]):
            (apm_dir / "new.md").write_text("new")


def test_deletion_raises(tmp_path):
    apm_dir = tmp_path / ".apm"
    apm_dir.mkdir()
    target = apm_dir / "x.md"
    target.write_text("x")
    with pytest.raises(ProtectedPathMutationError, match=r"deleted"):
        with _ReadOnlyProjectGuard(tmp_path, [".apm"]):
            target.unlink()


def test_missing_path_tolerated(tmp_path):
    with _ReadOnlyProjectGuard(tmp_path, [".apm", "apm.lock.yaml"]):
        pass


def test_existing_exception_not_masked(tmp_path):
    apm_dir = tmp_path / ".apm"
    apm_dir.mkdir()
    (apm_dir / "x.md").write_text("x")
    with pytest.raises(ValueError, match="boom"):
        with _ReadOnlyProjectGuard(tmp_path, [".apm"]):
            (apm_dir / "x.md").write_text("changed")
            raise ValueError("boom")


def test_single_file_protected_path(tmp_path):
    lock = tmp_path / "apm.lock.yaml"
    lock.write_text("locked: true\n")
    with _ReadOnlyProjectGuard(tmp_path, ["apm.lock.yaml"]):
        pass
    with pytest.raises(ProtectedPathMutationError, match=r"modified"):
        with _ReadOnlyProjectGuard(tmp_path, ["apm.lock.yaml"]):
            time.sleep(0.01)
            lock.write_bytes(b"locked: false-and-longer\n")
            _bump_mtime(lock)
