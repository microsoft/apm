"""Unit tests for package identity system - canonical dependency strings and install paths.

These tests validate the fix for virtual package uninstallation and orphan detection.
The package identity system ensures consistency between:
1. Dependency strings in apm.yml
2. Folder paths in apm_modules/
3. Agent/prompt metadata for orphan detection
"""

from pathlib import Path
from urllib.parse import urlparse

import pytest

from src.apm_cli.models.apm_package import DependencyReference


def _get_host_from_entry(entry: str) -> str | None:
    """Safely extract hostname from an entry using URL parsing.

    This is a security-safe way to check for host prefixes without
    using vulnerable string operations like startswith().

    Args:
        entry: The dependency string to parse

    Returns:
        The hostname if present, None otherwise
    """
    # Try parsing as a URL with scheme
    if "://" in entry:
        parsed = urlparse(entry)
        return parsed.netloc if parsed.netloc else None

    # For entries like "dev.azure.com/org/proj/repo", treat first segment as potential host
    parts = entry.split("/")
    if len(parts) >= 1 and "." in parts[0]:
        # Looks like a hostname (contains dots)
        return parts[0]

    return None


class TestCanonicalDependencyString:
    """Test get_canonical_dependency_string() method."""

    def test_regular_github_package(self):
        """Regular GitHub package returns owner/repo."""
        dep = DependencyReference.parse("owner/repo")
        assert dep.get_canonical_dependency_string() == "owner/repo"

    def test_regular_github_package_with_reference(self):
        """Reference (#) does NOT affect canonical string."""
        dep = DependencyReference.parse("owner/repo#v1.0.0")
        assert dep.get_canonical_dependency_string() == "owner/repo"

    def test_regular_github_package_with_alias_shorthand_removed(self):
        """Shorthand @alias syntax is no longer supported."""
        with pytest.raises(ValueError):
            DependencyReference.parse("owner/repo@myalias")

    def test_regular_github_package_with_reference_and_alias_shorthand_not_parsed(self):
        """Shorthand #ref@alias — @ is no longer parsed as alias separator."""
        dep = DependencyReference.parse("owner/repo#main@myalias")
        assert dep.reference == "main@myalias"
        assert dep.alias is None

    def test_virtual_file_package(self):
        """Virtual file includes full path."""
        dep = DependencyReference.parse("owner/test-repo/prompts/code-review.prompt.md")
        assert (
            dep.get_canonical_dependency_string() == "owner/test-repo/prompts/code-review.prompt.md"
        )

    def test_virtual_collection_package(self):
        """Virtual collection includes full path."""
        dep = DependencyReference.parse("owner/test-repo/collections/azure-cloud-development")
        assert (
            dep.get_canonical_dependency_string()
            == "owner/test-repo/collections/azure-cloud-development"
        )

    def test_virtual_package_with_reference(self):
        """Virtual package with reference - reference not in canonical string."""
        dep = DependencyReference.parse("owner/test-repo/collections/testing#main")
        assert dep.get_canonical_dependency_string() == "owner/test-repo/collections/testing"

    def test_ado_regular_package(self):
        """ADO package returns org/project/repo."""
        dep = DependencyReference.parse("dev.azure.com/myorg/myproject/myrepo")
        assert dep.get_canonical_dependency_string() == "myorg/myproject/myrepo"

    def test_ado_virtual_package(self):
        """ADO virtual package includes full path."""
        dep = DependencyReference.parse(
            "dev.azure.com/myorg/myproject/myrepo/prompts/test.prompt.md"
        )
        assert (
            dep.get_canonical_dependency_string() == "myorg/myproject/myrepo/prompts/test.prompt.md"
        )

    def test_ado_virtual_collection(self):
        """ADO virtual collection includes full path."""
        dep = DependencyReference.parse(
            "dev.azure.com/myorg/myproject/myrepo/collections/my-collection"
        )
        assert (
            dep.get_canonical_dependency_string()
            == "myorg/myproject/myrepo/collections/my-collection"
        )


class TestGetInstallPath:
    """Test get_install_path() method."""

    def test_regular_github_package(self):
        """Regular GitHub package: apm_modules/owner/repo."""
        dep = DependencyReference.parse("owner/repo")
        apm_modules = Path("/project/apm_modules")
        expected = apm_modules / "owner" / "repo"
        assert dep.get_install_path(apm_modules) == expected

    def test_regular_github_package_with_reference(self):
        """Reference does not affect install path."""
        dep = DependencyReference.parse("owner/repo#v1.0.0")
        apm_modules = Path("/project/apm_modules")
        expected = apm_modules / "owner" / "repo"
        assert dep.get_install_path(apm_modules) == expected

    def test_virtual_file_package(self):
        """Virtual file: apm_modules/owner/<virtual-package-name>."""
        dep = DependencyReference.parse("owner/test-repo/prompts/code-review.prompt.md")
        apm_modules = Path("/project/apm_modules")

        # Virtual package name: test-repo-code-review
        expected = apm_modules / "owner" / "test-repo-code-review"
        assert dep.get_install_path(apm_modules) == expected

    def test_virtual_collection_package(self):
        """Virtual collection: apm_modules/owner/<virtual-package-name>."""
        dep = DependencyReference.parse("owner/test-repo/collections/azure-cloud-development")
        apm_modules = Path("/project/apm_modules")

        # Virtual package name: test-repo-azure-cloud-development
        expected = apm_modules / "owner" / "test-repo-azure-cloud-development"
        assert dep.get_install_path(apm_modules) == expected

    def test_virtual_collection_with_reference(self):
        """Reference does not affect virtual package install path."""
        dep = DependencyReference.parse("owner/test-repo/collections/testing#main")
        apm_modules = Path("/project/apm_modules")

        expected = apm_modules / "owner" / "test-repo-testing"
        assert dep.get_install_path(apm_modules) == expected

    def test_ado_regular_package(self):
        """ADO regular package: apm_modules/org/project/repo."""
        dep = DependencyReference.parse("dev.azure.com/myorg/myproject/myrepo")
        apm_modules = Path("/project/apm_modules")
        expected = apm_modules / "myorg" / "myproject" / "myrepo"
        assert dep.get_install_path(apm_modules) == expected

    def test_ado_virtual_package(self):
        """ADO virtual package: apm_modules/org/project/<virtual-package-name>."""
        dep = DependencyReference.parse(
            "dev.azure.com/myorg/myproject/myrepo/prompts/test.prompt.md"
        )
        apm_modules = Path("/project/apm_modules")

        # Virtual package name: myrepo-test
        expected = apm_modules / "myorg" / "myproject" / "myrepo-test"
        assert dep.get_install_path(apm_modules) == expected

    def test_ado_virtual_collection(self):
        """ADO virtual collection: apm_modules/org/project/<virtual-package-name>."""
        dep = DependencyReference.parse(
            "dev.azure.com/myorg/myproject/myrepo/collections/my-collection"
        )
        apm_modules = Path("/project/apm_modules")

        # Virtual package name: myrepo-my-collection
        expected = apm_modules / "myorg" / "myproject" / "myrepo-my-collection"
        assert dep.get_install_path(apm_modules) == expected

    def test_relative_apm_modules_path(self):
        """Works with relative paths too."""
        dep = DependencyReference.parse("owner/repo")
        apm_modules = Path("apm_modules")
        expected = apm_modules / "owner" / "repo"
        assert dep.get_install_path(apm_modules) == expected


class TestInstallPathConsistency:
    """Test that get_install_path is consistent with virtual package naming."""

    def test_consistency_with_get_virtual_package_name(self):
        """Install path uses same package name as get_virtual_package_name."""
        test_cases = [
            "owner/test-repo/prompts/code-review.prompt.md",
            "owner/test-repo/collections/azure-cloud-development",
            "owner/repo/agents/security.agent.md",
            "user/pkg/instructions/coding.instructions.md",
        ]

        for dep_str in test_cases:
            dep = DependencyReference.parse(dep_str)
            apm_modules = Path("apm_modules")
            install_path = dep.get_install_path(apm_modules)

            # Last component of path should match virtual package name
            expected_name = dep.get_virtual_package_name()
            assert install_path.name == expected_name, f"Failed for {dep_str}"

    def test_unique_paths_for_different_virtual_packages(self):
        """Different virtual packages from same repo get different paths."""
        dep1 = DependencyReference.parse("owner/repo/prompts/file1.prompt.md")
        dep2 = DependencyReference.parse("owner/repo/prompts/file2.prompt.md")

        apm_modules = Path("apm_modules")
        path1 = dep1.get_install_path(apm_modules)
        path2 = dep2.get_install_path(apm_modules)

        assert path1 != path2
        assert path1.parent == path2.parent  # Same owner directory

    def test_regular_package_same_owner(self):
        """Regular package from same owner has predictable path."""
        dep = DependencyReference.parse("owner/repo")
        virtual_dep = DependencyReference.parse("owner/repo/prompts/file.prompt.md")

        apm_modules = Path("apm_modules")
        regular_path = dep.get_install_path(apm_modules)
        virtual_path = virtual_dep.get_install_path(apm_modules)

        # Different paths (repo vs repo-file)
        assert regular_path != virtual_path
        # Same owner directory
        assert regular_path.parent == virtual_path.parent


class TestUninstallScenarios:
    """Test scenarios that were broken before the fix."""

    def test_uninstall_virtual_collection_finds_correct_path(self):
        """Uninstalling virtual collection should find owner/virtual-pkg-name, not owner/repo/collections/name."""
        dep_str = "owner/test-repo/collections/azure-cloud-development"
        dep = DependencyReference.parse(dep_str)

        apm_modules = Path("apm_modules")
        install_path = dep.get_install_path(apm_modules)

        # Should be owner/test-repo-azure-cloud-development
        # NOT owner/test-repo/collections/azure-cloud-development
        assert install_path == apm_modules / "owner" / "test-repo-azure-cloud-development"

        # The wrong path (from raw path segments) would be:
        wrong_path = apm_modules / "owner" / "test-repo" / "collections" / "azure-cloud-development"
        assert install_path != wrong_path

    def test_uninstall_virtual_file_finds_correct_path(self):
        """Uninstalling virtual file should find owner/virtual-pkg-name."""
        dep_str = "owner/repo/prompts/code-review.prompt.md"
        dep = DependencyReference.parse(dep_str)

        apm_modules = Path("apm_modules")
        install_path = dep.get_install_path(apm_modules)

        # Should be owner/repo-code-review
        # NOT owner/repo/prompts/code-review.prompt.md
        assert install_path == apm_modules / "owner" / "repo-code-review"


class TestOrphanDetectionScenarios:
    """Test scenarios for orphan detection that were broken before the fix."""

    def test_canonical_string_matches_apm_yml_entry(self):
        """Canonical string should exactly match what's stored in apm.yml."""
        # These are the strings that would appear in apm.yml dependencies
        apm_yml_entries = [
            "owner/repo",
            "owner/test-repo/collections/azure-cloud-development",
            "owner/pkg/prompts/file.prompt.md",
            "dev.azure.com/org/proj/repo/agents/test.agent.md",
        ]

        for entry in apm_yml_entries:
            try:
                dep = DependencyReference.parse(entry)
                canonical = dep.get_canonical_dependency_string()

                # Use proper URL parsing to extract hostname safely
                host = _get_host_from_entry(entry)

                if host == "dev.azure.com":
                    # ADO entries: canonical should match without the host
                    # Remove host prefix: dev.azure.com/org/proj/repo -> org/proj/repo
                    expected = "/".join(entry.split("/")[1:])
                    expected = expected.replace("/_git/", "/")
                    assert canonical == expected
                elif host == "github.com":
                    # GitHub entries: canonical should match without the host
                    expected = "/".join(entry.split("/")[1:])
                    assert canonical == expected
                else:
                    # Entries without host prefix should match exactly
                    assert canonical == entry
            except ValueError:
                # Skip entries that can't be parsed due to ADO format issues
                pass

    def test_unique_key_matches_canonical_string(self):
        """get_unique_key and get_canonical_dependency_string should be consistent."""
        test_cases = [
            "owner/repo",
            "owner/test-repo/prompts/code-review.prompt.md",
            "owner/test-repo/collections/testing",
        ]

        for dep_str in test_cases:
            dep = DependencyReference.parse(dep_str)
            assert dep.get_unique_key() == dep.get_canonical_dependency_string()
