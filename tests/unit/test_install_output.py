"""Tests for install command output formatting: resolved refs and pinning hints."""

from unittest.mock import MagicMock

import pytest  # noqa: F401

from apm_cli.models.dependency import (
    DependencyReference,
    GitReferenceType,
    ResolvedReference,
)


class TestInstallOutputFormatting:
    """Test the formatting logic used in install output messages."""

    def test_resolved_reference_str_tag(self):
        """ResolvedReference for a tag shows ref name and short SHA."""
        ref = ResolvedReference(
            original_ref="v1.0.0",
            ref_type=GitReferenceType.TAG,
            resolved_commit="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
            ref_name="v1.0.0",
        )
        assert str(ref) == "v1.0.0 (a1b2c3d4)"

    def test_resolved_reference_str_branch(self):
        """ResolvedReference for a branch shows ref name and short SHA."""
        ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="deadbeef12345678deadbeef12345678deadbeef",
            ref_name="main",
        )
        assert str(ref) == "main (deadbeef)"

    def test_resolved_reference_str_commit(self):
        """ResolvedReference for a commit shows only short SHA."""
        ref = ResolvedReference(
            original_ref="a1b2c3d4",
            ref_type=GitReferenceType.COMMIT,
            resolved_commit="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
            ref_name="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        )
        assert str(ref) == "a1b2c3d4"


class TestCachedRefFormatting:
    """Test the cached-path ref formatting logic (mirrors install.py inline code)."""

    @staticmethod
    def _format_cached_ref(dep_ref, locked_dep):
        """Reproduce the cached-path ref formatting from install.py."""
        ref_str = ""
        if locked_dep and locked_dep.resolved_commit and locked_dep.resolved_commit != "cached":
            short_sha = locked_dep.resolved_commit[:8]
            if dep_ref.reference:
                ref_str = f"#{dep_ref.reference} ({short_sha})"
            else:
                ref_str = f"#{short_sha}"
        elif dep_ref.reference:
            ref_str = f"#{dep_ref.reference}"
        return ref_str

    def test_cached_with_lockfile_and_ref(self):
        """Cached dep with lockfile SHA and user ref shows both."""
        dep = DependencyReference.parse("owner/repo#v1.0.0")
        locked = MagicMock(resolved_commit="a1b2c3d4e5f6a1b2")
        result = self._format_cached_ref(dep, locked)
        assert result == "#v1.0.0 (a1b2c3d4)"

    def test_cached_with_lockfile_no_ref(self):
        """Cached dep with lockfile SHA but no user ref shows SHA only."""
        dep = DependencyReference.parse("owner/repo")
        locked = MagicMock(resolved_commit="deadbeef12345678")
        result = self._format_cached_ref(dep, locked)
        assert result == "#deadbeef"

    def test_cached_no_lockfile_with_ref(self):
        """Cached dep without lockfile shows user ref only."""
        dep = DependencyReference.parse("owner/repo#main")
        result = self._format_cached_ref(dep, None)
        assert result == "#main"

    def test_cached_no_lockfile_no_ref(self):
        """Cached dep without lockfile and no ref shows nothing."""
        dep = DependencyReference.parse("owner/repo")
        result = self._format_cached_ref(dep, None)
        assert result == ""

    def test_cached_lockfile_marked_as_cached(self):
        """Lockfile with resolved_commit='cached' falls through to user ref."""
        dep = DependencyReference.parse("owner/repo#v2.0")
        locked = MagicMock(resolved_commit="cached")
        result = self._format_cached_ref(dep, locked)
        assert result == "#v2.0"
