"""Unit tests for apm_cli.commands.uninstall.engine helper functions.

Focuses on the pure/near-pure helpers that are currently untested:
  - _build_children_index
  - _parse_dependency_entry
  - _validate_uninstall_packages
  - _remove_packages_from_disk (filesystem interaction)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.uninstall.engine import (
    _build_children_index,
    _parse_dependency_entry,
    _remove_packages_from_disk,
    _validate_uninstall_packages,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.reference import DependencyReference

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_locked_dep(repo_url: str, resolved_by: str | None = None) -> LockedDependency:
    return LockedDependency(
        repo_url=repo_url,
        resolved_commit="abc123",
        depth=0 if resolved_by is None else 1,
        resolved_by=resolved_by,
    )


def _make_lockfile(*deps: LockedDependency) -> LockFile:
    lf = LockFile()
    for dep in deps:
        lf.add_dependency(dep)
    return lf


def _make_logger() -> MagicMock:
    logger = MagicMock()
    logger.error = MagicMock()
    logger.warning = MagicMock()
    logger.progress = MagicMock()
    return logger


# ---------------------------------------------------------------------------
# _build_children_index
# ---------------------------------------------------------------------------


class TestBuildChildrenIndex:
    def test_empty_lockfile_returns_empty_dict(self):
        lf = _make_lockfile()
        assert _build_children_index(lf) == {}

    def test_direct_deps_with_no_parent_not_indexed(self):
        lf = _make_lockfile(_make_locked_dep("owner/direct"))
        assert _build_children_index(lf) == {}

    def test_transitive_dep_indexed_under_parent(self):
        parent = _make_locked_dep("owner/parent")
        child = _make_locked_dep("owner/child", resolved_by="owner/parent")
        lf = _make_lockfile(parent, child)
        index = _build_children_index(lf)
        assert "owner/parent" in index
        assert len(index["owner/parent"]) == 1
        assert index["owner/parent"][0].repo_url == "owner/child"

    def test_multiple_children_under_same_parent(self):
        parent = _make_locked_dep("org/a")
        child1 = _make_locked_dep("org/b", resolved_by="org/a")
        child2 = _make_locked_dep("org/c", resolved_by="org/a")
        lf = _make_lockfile(parent, child1, child2)
        index = _build_children_index(lf)
        assert len(index["org/a"]) == 2
        child_urls = {d.repo_url for d in index["org/a"]}
        assert child_urls == {"org/b", "org/c"}

    def test_chained_deps_each_parent_indexed(self):
        a = _make_locked_dep("org/a")
        b = _make_locked_dep("org/b", resolved_by="org/a")
        c = _make_locked_dep("org/c", resolved_by="org/b")
        lf = _make_lockfile(a, b, c)
        index = _build_children_index(lf)
        assert "org/a" in index
        assert "org/b" in index
        assert "org/c" not in index


# ---------------------------------------------------------------------------
# _parse_dependency_entry
# ---------------------------------------------------------------------------


class TestParseDependencyEntry:
    def test_string_input_returns_dependency_reference(self):
        result = _parse_dependency_entry("owner/repo")
        assert isinstance(result, DependencyReference)
        assert result.repo_url == "owner/repo"

    def test_dependency_reference_passthrough(self):
        ref = DependencyReference.parse("owner/repo2")
        result = _parse_dependency_entry(ref)
        assert result is ref

    def test_dict_input_parsed(self):
        entry = {"git": "https://github.com/owner/repo3"}
        result = _parse_dependency_entry(entry)
        assert isinstance(result, DependencyReference)
        assert "owner/repo3" in result.repo_url

    def test_unsupported_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported dependency entry type"):
            _parse_dependency_entry(42)

    def test_none_raises_value_error(self):
        with pytest.raises((ValueError, TypeError, AttributeError)):
            _parse_dependency_entry(None)


# ---------------------------------------------------------------------------
# _validate_uninstall_packages
# ---------------------------------------------------------------------------


class TestValidateUninstallPackages:
    def test_matching_package_moved_to_remove_list(self):
        deps = ["owner/pkg"]
        logger = _make_logger()
        to_remove, not_found = _validate_uninstall_packages(["owner/pkg"], deps, logger)
        assert "owner/pkg" in to_remove
        assert not_found == []

    def test_missing_package_moved_to_not_found(self):
        deps = ["owner/other"]
        logger = _make_logger()
        to_remove, not_found = _validate_uninstall_packages(
            ["owner/missing"], deps, logger
        )
        assert to_remove == []
        assert "owner/missing" in not_found

    def test_invalid_format_no_slash_logs_error(self):
        logger = _make_logger()
        to_remove, not_found = _validate_uninstall_packages(["noslash"], [], logger)
        assert to_remove == []
        assert not_found == []
        logger.error.assert_called_once()

    def test_multiple_packages_mixed_results(self):
        deps = ["owner/a", "owner/b"]
        logger = _make_logger()
        to_remove, not_found = _validate_uninstall_packages(
            ["owner/a", "owner/missing"], deps, logger
        )
        assert "owner/a" in to_remove
        assert "owner/missing" in not_found

    def test_empty_packages_list_returns_empty(self):
        logger = _make_logger()
        to_remove, not_found = _validate_uninstall_packages([], ["owner/pkg"], logger)
        assert to_remove == []
        assert not_found == []

    def test_empty_deps_means_all_not_found(self):
        logger = _make_logger()
        to_remove, not_found = _validate_uninstall_packages(["owner/x"], [], logger)
        assert to_remove == []
        assert "owner/x" in not_found

    def test_dependency_reference_object_in_deps_matches(self):
        ref = DependencyReference.parse("owner/repox")
        logger = _make_logger()
        to_remove, not_found = _validate_uninstall_packages(
            ["owner/repox"], [ref], logger
        )
        assert ref in to_remove
        assert not_found == []


# ---------------------------------------------------------------------------
# _remove_packages_from_disk
# ---------------------------------------------------------------------------


class TestRemovePackagesFromDisk:
    def test_nonexistent_modules_dir_returns_zero(self, tmp_path):
        logger = _make_logger()
        absent_dir = tmp_path / "no_such_dir"
        result = _remove_packages_from_disk(["owner/pkg"], absent_dir, logger)
        assert result == 0

    def test_package_on_disk_is_removed(self, tmp_path):
        logger = _make_logger()
        pkg_dir = tmp_path / "owner" / "pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "file.txt").write_text("hi")

        result = _remove_packages_from_disk(["owner/pkg"], tmp_path, logger)
        assert result == 1
        assert not pkg_dir.exists()

    def test_package_not_on_disk_counts_as_zero(self, tmp_path):
        logger = _make_logger()
        result = _remove_packages_from_disk(["owner/missing"], tmp_path, logger)
        assert result == 0
        logger.warning.assert_called_once()

    def test_path_traversal_in_dep_is_rejected(self, tmp_path):
        """A dep_entry whose string fallback path has no slash is safely skipped."""
        logger = _make_logger()
        # "noslash" has no "/" so _parse_dependency_entry raises; the fallback
        # constructs a path that does not exist, so removal is skipped.
        result = _remove_packages_from_disk(["noslash"], tmp_path, logger)
        assert result == 0
