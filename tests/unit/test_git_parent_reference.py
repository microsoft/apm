"""Unit tests for ``git: parent`` object-form dependency parsing."""

import pytest

from apm_cli.models.dependency import DependencyReference
from apm_cli.utils.path_security import PathTraversalError


class TestGitParentParse:
    def test_basic_parent_decl(self):
        dep = DependencyReference.parse_from_dict({"git": "parent", "path": "skills/shared"})
        assert dep.is_parent_repo_inheritance is True
        assert dep.virtual_path == "skills/shared"
        assert dep.reference is None
        assert dep.alias is None
        assert dep.is_virtual is True
        assert dep.repo_url == "_parent"

    def test_parent_with_ref(self):
        dep = DependencyReference.parse_from_dict(
            {
                "git": "parent",
                "path": "skills/shared",
                "ref": "v1.0",
            }
        )
        assert dep.is_parent_repo_inheritance is True
        assert dep.virtual_path == "skills/shared"
        assert dep.reference == "v1.0"

    def test_parent_with_alias(self):
        dep = DependencyReference.parse_from_dict(
            {
                "git": "parent",
                "path": "pkg/a",
                "alias": "shared-pkg",
            }
        )
        assert dep.alias == "shared-pkg"
        assert dep.is_parent_repo_inheritance is True

    def test_parent_ref_and_alias(self):
        dep = DependencyReference.parse_from_dict(
            {
                "git": "parent",
                "path": "x/y",
                "ref": "main",
                "alias": "xy",
            }
        )
        assert dep.reference == "main"
        assert dep.alias == "xy"

    def test_normalize_backslashes(self):
        dep = DependencyReference.parse_from_dict({"git": "parent", "path": "\\skills\\shared"})
        assert dep.virtual_path == "skills/shared"

    def test_normalize_whitespace_and_slashes(self):
        dep = DependencyReference.parse_from_dict({"git": "parent", "path": "  /skills/shared/  "})
        assert dep.virtual_path == "skills/shared"

    def test_normalize_collapses_duplicate_slashes(self):
        dep = DependencyReference.parse_from_dict({"git": "parent", "path": "skills//shared//z"})
        assert dep.virtual_path == "skills/shared/z"

    def test_rejects_parent_case_variants(self):
        with pytest.raises(ValueError):
            DependencyReference.parse_from_dict({"git": "Parent", "path": "x"})

    def test_rejects_missing_path(self):
        with pytest.raises(ValueError, match="path"):
            DependencyReference.parse_from_dict({"git": "parent"})

    def test_rejects_empty_path(self):
        with pytest.raises(ValueError, match="non-empty"):
            DependencyReference.parse_from_dict({"git": "parent", "path": ""})

    def test_rejects_whitespace_only_path(self):
        with pytest.raises(ValueError, match="non-empty"):
            DependencyReference.parse_from_dict({"git": "parent", "path": "   "})

    def test_rejects_path_only_slashes(self):
        with pytest.raises(ValueError, match="non-empty"):
            DependencyReference.parse_from_dict({"git": "parent", "path": "///"})

    def test_rejects_dotdot_segment(self):
        with pytest.raises((ValueError, PathTraversalError)):
            DependencyReference.parse_from_dict({"git": "parent", "path": "../skills"})

    def test_rejects_dot_segment(self):
        with pytest.raises((ValueError, PathTraversalError)):
            DependencyReference.parse_from_dict({"git": "parent", "path": "./hidden"})

    def test_rejects_nested_dotdot(self):
        with pytest.raises((ValueError, PathTraversalError)):
            DependencyReference.parse_from_dict({"git": "parent", "path": "a/../../b"})

    def test_rejects_backslash_traversal(self):
        with pytest.raises((ValueError, PathTraversalError)):
            DependencyReference.parse_from_dict({"git": "parent", "path": r"sub\..\..\\esc"})

    def test_rejects_empty_ref(self):
        with pytest.raises(ValueError, match="ref"):
            DependencyReference.parse_from_dict({"git": "parent", "path": "a", "ref": ""})

    def test_rejects_empty_alias(self):
        with pytest.raises(ValueError, match="alias"):
            DependencyReference.parse_from_dict({"git": "parent", "path": "a", "alias": ""})

    def test_rejects_invalid_alias_characters(self):
        with pytest.raises(ValueError, match="Invalid alias"):
            DependencyReference.parse_from_dict(
                {"git": "parent", "path": "a", "alias": "bad alias"}
            )

    def test_does_not_expand_repo_coordinates(self):
        """Parsing must not set real host/repo; resolver expands later."""
        dep = DependencyReference.parse_from_dict({"git": "parent", "path": "skills/shared"})
        assert dep.host is None
        assert dep.repo_url == "_parent"
