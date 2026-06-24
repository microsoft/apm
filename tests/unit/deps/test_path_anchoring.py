"""Unit tests for :mod:`apm_cli.deps.path_anchoring`.

The drift-replay engine must resolve a locked LOCAL dependency's on-disk
source directory the SAME way the install resolver does (issue #857):

* a direct (root-declared) local dep stores a project-root-relative
  ``local_path`` (``./packages/foo``) and ``resolved_by = None``;
* a transitive local dep stores a path relative to the package that
  DECLARED it (``../sibling``) plus ``resolved_by`` = that parent's
  ``repo_url``.

These tests pin the parent-walk, absolute-path bypass, and the
corrupt-lockfile hard-failure modes (missing / ambiguous / cyclic
``resolved_by``). They deliberately do NOT exercise on-disk existence:
the helper is pure path math + corruption detection, and the caller
(``drift._materialize_install_path``) owns the existence check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.path_anchoring import LocalResolutionError, resolve_local_dep_dir


def _local(repo_url: str, local_path: str, resolved_by: str | None = None) -> LockedDependency:
    return LockedDependency(
        repo_url=repo_url,
        source="local",
        local_path=local_path,
        resolved_by=resolved_by,
    )


def _lockfile(*deps: LockedDependency) -> LockFile:
    lf = LockFile()
    for dep in deps:
        lf.add_dependency(dep)
    return lf


class TestDirectDep:
    def test_root_dep_anchors_on_project_root(self, tmp_path: Path) -> None:
        dep = _local("_local/foo", "./packages/foo")
        result = resolve_local_dep_dir(dep, _lockfile(dep), tmp_path)
        assert result == (tmp_path / "packages" / "foo").resolve()

    def test_lockfile_none_ok_for_root_dep(self, tmp_path: Path) -> None:
        dep = _local("_local/foo", "./packages/foo")
        result = resolve_local_dep_dir(dep, None, tmp_path)
        assert result == (tmp_path / "packages" / "foo").resolve()


class TestParentWalk:
    def test_single_hop(self, tmp_path: Path) -> None:
        parent = _local("_local/parent", "./packages/parent")
        child = _local("_local/sibling", "../sibling", resolved_by="_local/parent")
        result = resolve_local_dep_dir(child, _lockfile(parent, child), tmp_path)
        assert result == (tmp_path / "packages" / "sibling").resolve()

    def test_multi_hop_depth_three(self, tmp_path: Path) -> None:
        a = _local("_local/a", "./packages/a")
        b = _local("_local/b", "../b", resolved_by="_local/a")
        c = _local("_local/c", "../c", resolved_by="_local/b")
        result = resolve_local_dep_dir(c, _lockfile(a, b, c), tmp_path)
        assert result == (tmp_path / "packages" / "c").resolve()


class TestAbsoluteBypass:
    def test_absolute_local_path_bypasses_anchor(self, tmp_path: Path) -> None:
        abs_dir = tmp_path / "elsewhere" / "pkg"
        parent = _local("_local/parent", "./packages/parent")
        child = _local("_local/abs", str(abs_dir), resolved_by="_local/parent")
        result = resolve_local_dep_dir(child, _lockfile(parent, child), tmp_path)
        assert result == abs_dir.resolve()


class TestCorruptLockfile:
    def test_missing_parent_raises(self, tmp_path: Path) -> None:
        child = _local("_local/orphan", "../orphan", resolved_by="_local/ghost")
        with pytest.raises(LocalResolutionError, match="ghost"):
            resolve_local_dep_dir(child, _lockfile(child), tmp_path)

    def test_ambiguous_parent_raises(self, tmp_path: Path) -> None:
        dup_a = _local("_local/dup", "./packages/dup-a")
        dup_b = _local("_local/dup", "./packages/dup-b")
        child = _local("_local/child", "../child", resolved_by="_local/dup")
        with pytest.raises(LocalResolutionError, match="ambiguous"):
            resolve_local_dep_dir(child, _lockfile(dup_a, dup_b, child), tmp_path)

    def test_cycle_raises(self, tmp_path: Path) -> None:
        a = _local("_local/a", "../a", resolved_by="_local/b")
        b = _local("_local/b", "../b", resolved_by="_local/a")
        with pytest.raises(LocalResolutionError, match="cycle"):
            resolve_local_dep_dir(a, _lockfile(a, b), tmp_path)

    def test_resolved_by_set_but_lockfile_none_raises(self, tmp_path: Path) -> None:
        child = _local("_local/child", "../child", resolved_by="_local/parent")
        with pytest.raises(LocalResolutionError):
            resolve_local_dep_dir(child, None, tmp_path)

    def test_remote_parent_cannot_anchor(self, tmp_path: Path) -> None:
        remote_parent = LockedDependency(repo_url="owner/repo", resolved_commit="abc")
        child = _local("_local/child", "../child", resolved_by="owner/repo")
        with pytest.raises(LocalResolutionError):
            resolve_local_dep_dir(child, _lockfile(remote_parent, child), tmp_path)


class TestGuards:
    def test_non_local_dep_raises(self, tmp_path: Path) -> None:
        dep = LockedDependency(repo_url="owner/repo", resolved_commit="abc")
        with pytest.raises(LocalResolutionError):
            resolve_local_dep_dir(dep, _lockfile(dep), tmp_path)

    def test_local_dep_without_local_path_raises(self, tmp_path: Path) -> None:
        dep = LockedDependency(repo_url="_local/foo", source="local", local_path=None)
        with pytest.raises(LocalResolutionError):
            resolve_local_dep_dir(dep, _lockfile(dep), tmp_path)
