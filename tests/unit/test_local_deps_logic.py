"""Unit tests for local filesystem path dependency support (pure logic cases)."""

from pathlib import Path  # noqa: F401
from unittest.mock import Mock  # noqa: F401

import pytest

from apm_cli.models.apm_package import DependencyReference

pytestmark = pytest.mark.unit

# ===========================================================================
# DependencyReference.is_local_path()
# ===========================================================================


class TestIsLocalPath:
    """Test local path detection logic."""

    def test_relative_dot_slash(self):
        assert DependencyReference.is_local_path("./my-package") is True

    def test_relative_dot_dot_slash(self):
        assert DependencyReference.is_local_path("../sibling-pkg") is True

    def test_absolute_unix(self):
        assert DependencyReference.is_local_path("/home/user/my-pkg") is True

    def test_home_tilde(self):
        assert DependencyReference.is_local_path("~/repos/my-pkg") is True

    def test_windows_relative(self):
        assert DependencyReference.is_local_path(".\\packages\\my-pkg") is True

    def test_windows_parent(self):
        assert DependencyReference.is_local_path("..\\sibling-pkg") is True

    def test_windows_home(self):
        assert DependencyReference.is_local_path("~\\repos\\my-pkg") is True

    def test_windows_absolute_backslash(self):
        assert DependencyReference.is_local_path("C:\\Users\\runner\\my-pkg") is True

    def test_windows_absolute_forward_slash(self):
        assert DependencyReference.is_local_path("D:/repos/my-pkg") is True

    def test_windows_absolute_uppercase(self):
        assert DependencyReference.is_local_path("Z:\\some\\path") is True

    def test_windows_absolute_lowercase(self):
        assert DependencyReference.is_local_path("c:\\users\\me\\pkg") is True

    def test_remote_shorthand_not_local(self):
        assert DependencyReference.is_local_path("owner/repo") is False

    def test_https_url_not_local(self):
        assert DependencyReference.is_local_path("https://github.com/owner/repo") is False

    def test_ssh_url_not_local(self):
        assert DependencyReference.is_local_path("git@github.com:owner/repo.git") is False

    def test_protocol_relative_not_local(self):
        """Protocol-relative URLs (//...) must NOT be treated as local paths."""
        assert DependencyReference.is_local_path("//evil.com/owner/repo") is False

    def test_bare_name_not_local(self):
        assert DependencyReference.is_local_path("my-package") is False

    def test_whitespace_trimmed(self):
        assert DependencyReference.is_local_path("  ./my-pkg  ") is True

    def test_empty_string_not_local(self):
        assert DependencyReference.is_local_path("") is False


# ===========================================================================
# DependencyReference.parse() with local paths
# ===========================================================================


class TestParseLocalPath:
    """Test parsing local filesystem paths into DependencyReference."""

    def test_relative_path(self):
        dep = DependencyReference.parse("./packages/my-skills")
        assert dep.is_local is True
        assert dep.local_path == "./packages/my-skills"
        assert dep.repo_url == "_local/my-skills"

    def test_relative_parent_path(self):
        dep = DependencyReference.parse("../sibling-package")
        assert dep.is_local is True
        assert dep.local_path == "../sibling-package"
        assert dep.repo_url == "_local/sibling-package"

    def test_absolute_path(self):
        dep = DependencyReference.parse("/home/user/repos/my-package")
        assert dep.is_local is True
        assert dep.local_path == "/home/user/repos/my-package"
        assert dep.repo_url == "_local/my-package"

    def test_home_path(self):
        dep = DependencyReference.parse("~/repos/my-ai-pkg")
        assert dep.is_local is True
        assert dep.local_path == "~/repos/my-ai-pkg"
        assert dep.repo_url == "_local/my-ai-pkg"

    def test_deeply_nested_relative(self):
        dep = DependencyReference.parse("./a/b/c/d/my-deep-pkg")
        assert dep.is_local is True
        assert dep.local_path == "./a/b/c/d/my-deep-pkg"
        assert dep.repo_url == "_local/my-deep-pkg"

    def test_no_reference_for_local(self):
        """Local paths should not have reference, alias, or virtual_path."""
        dep = DependencyReference.parse("./my-pkg")
        assert dep.reference is None
        assert dep.alias is None
        assert dep.virtual_path is None
        assert dep.is_virtual is False

    def test_remote_dep_not_local(self):
        """Regular remote deps should remain unaffected."""
        dep = DependencyReference.parse("microsoft/apm-sample-package")
        assert dep.is_local is False
        assert dep.local_path is None

    def test_bare_dot_dot_slash_rejected(self):
        """Path '../' has name '..' which could escape _local/ — must be rejected."""
        with pytest.raises(ValueError, match="does not resolve to a named directory"):
            DependencyReference.parse("../")

    def test_bare_dot_slash_rejected(self):
        """Path './' has empty name — must be rejected."""
        with pytest.raises(ValueError, match="does not resolve to a named directory"):
            DependencyReference.parse("./")

    def test_bare_root_rejected(self):
        """Path '/' has empty name — must be rejected."""
        with pytest.raises(ValueError, match="does not resolve to a named directory"):
            DependencyReference.parse("/")

    def test_dot_dot_without_slash_rejected(self):
        """Path '..' is not detected as a local path (no trailing '/')."""
        # '..' doesn't start with '../' so is_local_path returns False.
        # It falls through to regular parsing which also rejects it.
        with pytest.raises(ValueError):
            DependencyReference.parse("..")


# ===========================================================================
# DependencyReference methods for local deps
# ===========================================================================


# ===========================================================================
# LockedDependency with local source
# ===========================================================================


# ===========================================================================
# APMPackage.from_apm_yml with local deps
# ===========================================================================


# ===========================================================================
# Pack guard: reject local deps
# ===========================================================================


# ===========================================================================
# Copy local package helper
# ===========================================================================


# ===========================================================================
# #857 Regression: transitive local_path anchor + dual-reject from remote
# ===========================================================================
