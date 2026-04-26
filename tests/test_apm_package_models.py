"""Unit tests for APM package data models and validation."""

import json
import tempfile
from pathlib import Path
from unittest.mock import mock_open, patch

import pytest
import yaml

from apm_cli.utils import github_host
from src.apm_cli.models.apm_package import (
    APMPackage,
    DependencyReference,
    GitReferenceType,
    PackageContentType,
    PackageInfo,
    PackageType,
    ResolvedReference,
    ValidationError,
    ValidationResult,
    parse_git_reference,
    validate_apm_package,
)


class TestDependencyReference:
    """Test DependencyReference parsing and functionality."""

    def test_parse_simple_repo(self):
        """Test parsing simple user/repo format."""
        dep = DependencyReference.parse("user/repo")
        assert dep.repo_url == "user/repo"
        assert dep.reference is None
        assert dep.alias is None

    def test_parse_with_branch(self):
        """Test parsing with branch reference."""
        dep = DependencyReference.parse("user/repo#main")
        assert dep.repo_url == "user/repo"
        assert dep.reference == "main"
        assert dep.alias is None

    def test_parse_with_tag(self):
        """Test parsing with tag reference."""
        dep = DependencyReference.parse("user/repo#v1.0.0")
        assert dep.repo_url == "user/repo"
        assert dep.reference == "v1.0.0"
        assert dep.alias is None

    def test_parse_with_commit(self):
        """Test parsing with commit SHA."""
        dep = DependencyReference.parse("user/repo#abc123def")
        assert dep.repo_url == "user/repo"
        assert dep.reference == "abc123def"
        assert dep.alias is None

    def test_parse_with_alias_shorthand_removed(self):
        """Shorthand @alias syntax is no longer supported -- @ in shorthand is rejected."""
        with pytest.raises(ValueError):
            DependencyReference.parse("user/repo@myalias")

    def test_parse_with_reference_and_alias_shorthand_not_parsed(self):
        """Shorthand #ref@alias -- @ is no longer parsed as alias separator."""
        dep = DependencyReference.parse("user/repo#main@myalias")
        assert dep.repo_url == "user/repo"
        assert dep.reference == "main@myalias"  # @ treated as part of ref
        assert dep.alias is None

    def test_parse_github_urls(self):
        """Test parsing various GitHub URL formats."""
        host = github_host.default_host()
        formats = [
            f"{host}/user/repo",
            f"https://{host}/user/repo",
            f"https://{host}/user/repo.git",
            f"git@{host}:user/repo",
            f"git@{host}:user/repo.git",
        ]

        for url_format in formats:
            dep = DependencyReference.parse(url_format)
            assert dep.repo_url == "user/repo"

    def test_parse_ghe_urls(self):
        """Test parsing GitHub Enterprise (GHE) hostname formats like orgname.ghe.com."""
        formats = [
            "orgname.ghe.com/user/repo",
            "https://orgname.ghe.com/user/repo",
            "https://orgname.ghe.com/user/repo.git",
        ]

        for url_format in formats:
            dep = DependencyReference.parse(url_format)
            assert dep.repo_url == "user/repo"

    def test_parse_invalid_formats(self):
        """Test parsing invalid dependency formats."""
        invalid_formats = [
            "",
            "   ",
            "just-repo-name",
            "user/",
        ]

        for invalid_format in invalid_formats:
            with pytest.raises(ValueError):
                DependencyReference.parse(invalid_format)

    def test_parse_malicious_url_bypass_attempts(self):
        """Test that URL parsing prevents injection attacks.

        This tests the security fix for CWE-20: Improper Input Validation.
        With generic git host support, any valid FQDN is accepted as a host.
        The security focus is on preventing:
        - Protocol-relative URL attacks

        With nested group support on generic hosts, path segments that happen
        to look like hostnames (e.g., 'github.com/user/repo') are treated as
        repo path segments -- not injection. The host is correctly identified.
        """
        # Attack vectors that should still be REJECTED
        rejected_formats = [
            # Protocol-relative URL attacks
            (
                "//evil.com/github.com/user/repo",
                "Protocol-relative URLs are not supported",
            ),
        ]

        for malicious_url, expected_match in rejected_formats:
            with pytest.raises(ValueError, match=expected_match):
                DependencyReference.parse(malicious_url)

        # With generic git host support + nested groups, these are valid
        # (host is correctly identified, remaining segments are repo path)
        nested_group_on_generic_host = [
            ("evil.com/github.com/user/repo", "evil.com", "github.com/user/repo"),
            (
                "attacker.net/github.com/malicious/repo",
                "attacker.net",
                "github.com/malicious/repo",
            ),
        ]
        for url, expected_host, expected_repo in nested_group_on_generic_host:
            dep = DependencyReference.parse(url)
            assert dep.host == expected_host
            assert dep.repo_url == expected_repo
            assert dep.is_virtual is False

        # With generic git host support, valid FQDNs are accepted as hosts.
        # These are not injection attacks -- they are legitimate host references.
        accepted_as_generic_hosts = [
            "evil-github.com/user/repo",
            "malicious-github.com/user/repo",
            "github.com.evil.com/user/repo",
            "fakegithub.com/user/repo",
            "notgithub.com/user/repo",
            "GitHub.COM.evil.com/user/repo",
            "GITHUB.com.attacker.net/user/repo",
        ]

        for url in accepted_as_generic_hosts:
            dep = DependencyReference.parse(url)
            assert dep.repo_url == "user/repo"
            assert dep.host is not None

    def test_parse_legitimate_github_enterprise_formats(self):
        """Test that legitimate GitHub Enterprise hostnames are accepted.

        Ensures the security fix doesn't break valid GHE instances.
        According to is_github_hostname(), only github.com and *.ghe.com are valid.
        """
        # These should be ACCEPTED (valid GitHub Enterprise hostnames)
        valid_ghe_formats = [
            "company.ghe.com/user/repo",
            "myorg.ghe.com/user/repo",
            "github.com/user/repo",  # Standard GitHub
        ]

        for valid_url in valid_ghe_formats:
            dep = DependencyReference.parse(valid_url)
            assert dep.repo_url == "user/repo"
            assert dep.host is not None

    def test_parse_azure_devops_formats(self):
        """Test that Azure DevOps hostnames are accepted with org/project/repo format.

        Azure DevOps uses 3 segments (org/project/repo) instead of GitHub's 2 segments (owner/repo).
        """
        # Full ADO URL with _git segment
        dep = DependencyReference.parse(
            "dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules"
        )
        assert dep.host == "dev.azure.com"
        assert dep.ado_organization == "dmeppiel-org"
        assert dep.ado_project == "market-js-app"
        assert dep.ado_repo == "compliance-rules"
        assert dep.is_azure_devops() == True
        assert dep.repo_url == "dmeppiel-org/market-js-app/compliance-rules"

        # Simplified ADO format (without _git)
        dep = DependencyReference.parse("dev.azure.com/myorg/myproject/myrepo")
        assert dep.host == "dev.azure.com"
        assert dep.ado_organization == "myorg"
        assert dep.ado_project == "myproject"
        assert dep.ado_repo == "myrepo"
        assert dep.is_azure_devops() == True

        # Legacy visualstudio.com format
        dep = DependencyReference.parse(
            "mycompany.visualstudio.com/myorg/myproject/myrepo"
        )
        assert dep.host == "mycompany.visualstudio.com"
        assert dep.is_azure_devops() == True
        assert dep.ado_organization == "myorg"
        assert dep.ado_project == "myproject"
        assert dep.ado_repo == "myrepo"

    def test_parse_azure_devops_virtual_package(self):
        """Test ADO virtual package parsing with 4-segment format (org/project/repo/path)."""
        # ADO virtual package with host prefix
        dep = DependencyReference.parse(
            "dev.azure.com/myorg/myproject/myrepo/prompts/code-review.prompt.md"
        )
        assert dep.is_azure_devops() == True
        assert dep.is_virtual == True
        assert dep.repo_url == "myorg/myproject/myrepo"
        assert dep.virtual_path == "prompts/code-review.prompt.md"
        assert dep.ado_organization == "myorg"
        assert dep.ado_project == "myproject"
        assert dep.ado_repo == "myrepo"

        # ADO virtual package with _git segment
        dep = DependencyReference.parse(
            "dev.azure.com/myorg/myproject/_git/myrepo/prompts/test.prompt.md"
        )
        assert dep.is_azure_devops() == True
        assert dep.is_virtual == True
        assert dep.virtual_path == "prompts/test.prompt.md"

    def test_parse_azure_devops_invalid_virtual_package(self):
        """Test that incomplete ADO virtual packages are rejected."""
        # Test case: path looks like virtual package but not enough segments for ADO
        # This would be caught when trying to extract only 3 segments but path has extension
        # (4 segments after host needed: org/project/repo/file.ext)
        # Note: "myrepo.prompt.md" is treated as repo name, not as virtual path
        # The bounds check kicks in when we have a recognized virtual package format
        # but not enough segments. This test verifies ADO virtual package paths require
        # the full org/project/repo/path structure.

        # Valid 4-segment ADO virtual package should work
        dep = DependencyReference.parse("dev.azure.com/org/proj/repo/file.prompt.md")
        assert dep.is_virtual == True
        assert dep.repo_url == "org/proj/repo"

        # 3 segments after host (org/proj/repo) without a path - this is a regular package, not virtual
        dep = DependencyReference.parse("dev.azure.com/myorg/myproject/myrepo")
        assert dep.is_virtual == False
        assert dep.repo_url == "myorg/myproject/myrepo"

    def test_parse_azure_devops_project_with_spaces(self):
        """Test that ADO project names with spaces are correctly parsed.

        Azure DevOps project names can contain spaces (e.g., 'My Project').
        Users may specify them with %20 encoding or literal spaces (shell-quoted).
        """
        # Percent-encoded space in project name with _git segment
        dep = DependencyReference.parse("dev.azure.com/myorg/My%20Project/_git/myrepo")
        assert dep.host == "dev.azure.com"
        assert dep.ado_organization == "myorg"
        assert dep.ado_project == "My Project"
        assert dep.ado_repo == "myrepo"
        assert dep.is_azure_devops() == True
        assert dep.repo_url == "myorg/My Project/myrepo"

        # Literal space in project name (simplified format without _git)
        dep = DependencyReference.parse("dev.azure.com/myorg/My Project/myrepo")
        assert dep.host == "dev.azure.com"
        assert dep.ado_organization == "myorg"
        assert dep.ado_project == "My Project"
        assert dep.ado_repo == "myrepo"
        assert dep.is_azure_devops() == True

        # Percent-encoded space in simplified format
        dep = DependencyReference.parse("dev.azure.com/org/America%20Oh%20Yeah/repo")
        assert dep.ado_project == "America Oh Yeah"
        assert dep.ado_repo == "repo"

        # to_github_url() should produce a properly percent-encoded URL
        dep = DependencyReference.parse("dev.azure.com/myorg/My%20Project/_git/myrepo")
        url = dep.to_github_url()
        assert url == "https://dev.azure.com/myorg/My%20Project/_git/myrepo"

        # Spaces in repo name (percent-encoded) with _git segment
        dep = DependencyReference.parse(
            "dev.azure.com/Zifo/AIdeate%20and%20AIterate/_git/AiDeate%20SDLC%20Guidelines"
        )
        assert dep.host == "dev.azure.com"
        assert dep.ado_organization == "Zifo"
        assert dep.ado_project == "AIdeate and AIterate"
        assert dep.ado_repo == "AiDeate SDLC Guidelines"
        assert dep.is_azure_devops() == True
        assert dep.repo_url == "Zifo/AIdeate and AIterate/AiDeate SDLC Guidelines"

        # Spaces in both project and repo names (literal)
        dep = DependencyReference.parse("dev.azure.com/myorg/My Project/My Repo Name")
        assert dep.ado_organization == "myorg"
        assert dep.ado_project == "My Project"
        assert dep.ado_repo == "My Repo Name"

        # Spaces in repo name only (percent-encoded, no _git)
        dep = DependencyReference.parse(
            "dev.azure.com/myorg/myproject/My%20Repo%20Name"
        )
        assert dep.ado_organization == "myorg"
        assert dep.ado_project == "myproject"
        assert dep.ado_repo == "My Repo Name"

        # to_github_url() with spaces in repo name encodes correctly
        dep = DependencyReference.parse(
            "dev.azure.com/Zifo/AIdeate%20and%20AIterate/_git/AiDeate%20SDLC%20Guidelines"
        )
        url = dep.to_github_url()
        assert "AiDeate%20SDLC%20Guidelines" in url

        # Spaces should NOT be allowed in GitHub owner/repo names
        with pytest.raises(ValueError):
            DependencyReference.parse("github.com/my%20owner/repo")

    def test_parse_virtual_package_with_malicious_host(self):
        """Test virtual packages with various host types.

        With generic git host support, valid FQDNs are accepted as hosts.
        Path injection (embedding a host in a sub-path) is still rejected.
        """
        # Path injection: still rejected (creates invalid repo format)
        with pytest.raises(ValueError):
            DependencyReference.parse(
                "evil.com/github.com/user/repo/prompts/file.prompt.md"
            )

        # Valid generic hosts: now accepted with generic git URL support
        dep1 = DependencyReference.parse(
            "github.com.evil.com/user/repo/prompts/file.prompt.md"
        )
        assert dep1.host == "github.com.evil.com"
        assert dep1.repo_url == "user/repo"
        assert dep1.is_virtual is True

        dep2 = DependencyReference.parse(
            "attacker.net/user/repo/prompts/file.prompt.md"
        )
        assert dep2.host == "attacker.net"
        assert dep2.repo_url == "user/repo"
        assert dep2.is_virtual is True

    def test_parse_virtual_file_package(self):
        """Test parsing virtual file package (individual file)."""
        dep = DependencyReference.parse("owner/test-repo/prompts/code-review.prompt.md")
        assert dep.repo_url == "owner/test-repo"
        assert dep.is_virtual is True
        assert dep.virtual_path == "prompts/code-review.prompt.md"
        assert dep.is_virtual_file() is True
        assert dep.is_virtual_collection() is False
        assert dep.get_virtual_package_name() == "test-repo-code-review"

    def test_parse_virtual_file_with_reference(self):
        """Test parsing virtual file package with git reference."""
        dep = DependencyReference.parse(
            "owner/test-repo/prompts/code-review.prompt.md#v1.0.0"
        )
        assert dep.repo_url == "owner/test-repo"
        assert dep.is_virtual is True
        assert dep.virtual_path == "prompts/code-review.prompt.md"
        assert dep.reference == "v1.0.0"
        assert dep.is_virtual_file() is True

    def test_parse_virtual_file_all_extensions(self):
        """Test parsing virtual files with all supported extensions."""
        extensions = [".prompt.md", ".instructions.md", ".chatmode.md", ".agent.md"]

        for ext in extensions:
            dep = DependencyReference.parse(f"user/repo/path/to/file{ext}")
            assert dep.is_virtual is True
            assert dep.is_virtual_file() is True
            assert dep.virtual_path == f"path/to/file{ext}"

    def test_parse_virtual_collection(self):
        """Test parsing virtual collection package."""
        dep = DependencyReference.parse("owner/test-repo/collections/project-planning")
        assert dep.repo_url == "owner/test-repo"
        assert dep.is_virtual is True
        assert dep.virtual_path == "collections/project-planning"
        assert dep.is_virtual_file() is False
        assert dep.is_virtual_collection() is True
        assert dep.get_virtual_package_name() == "test-repo-project-planning"

    def test_parse_virtual_collection_with_reference(self):
        """Test parsing virtual collection with git reference."""
        dep = DependencyReference.parse("owner/test-repo/collections/testing#main")
        assert dep.repo_url == "owner/test-repo"
        assert dep.is_virtual is True
        assert dep.virtual_path == "collections/testing"
        assert dep.reference == "main"
        assert dep.is_virtual_collection() is True

    def test_parse_invalid_virtual_file_extension(self):
        """Test that invalid file extensions are rejected for virtual files."""
        invalid_paths = [
            "user/repo/path/to/file.txt",
            "user/repo/path/to/file.md",
            "user/repo/path/to/README.md",
            "user/repo/path/to/script.py",
        ]

        for path in invalid_paths:
            with pytest.raises(
                ValueError, match="Individual files must end with one of"
            ):
                DependencyReference.parse(path)

    def test_virtual_package_str_representation(self):
        """Test string representation of virtual packages.

        Note: After PR #33, host is explicit in string representation.
        """
        dep = DependencyReference.parse(
            "owner/test-repo/prompts/code-review.prompt.md#v1.0.0"
        )
        # Check that key components are present (host may be explicit now)
        assert "owner/test-repo" in str(dep)
        assert "prompts/code-review.prompt.md" in str(dep)
        assert "#v1.0.0" in str(dep)

        dep_with_ref = DependencyReference.parse(
            "owner/test-repo/prompts/test.prompt.md#v2.0"
        )
        assert "owner/test-repo" in str(dep_with_ref)
        assert "prompts/test.prompt.md" in str(dep_with_ref)
        assert "#v2.0" in str(dep_with_ref)

    def test_regular_package_not_virtual(self):
        """Test that regular packages (2 segments) are not marked as virtual."""
        dep = DependencyReference.parse("user/repo")
        assert dep.is_virtual is False
        assert dep.virtual_path is None
        assert dep.is_virtual_file() is False
        assert dep.is_virtual_collection() is False

    def test_parse_control_characters_rejected(self):
        """Test that control characters are rejected."""
        invalid_formats = [
            "user//repo",
            "user repo",
        ]

        for invalid_format in invalid_formats:
            with pytest.raises(
                ValueError,
                match="Invalid Git host|Empty dependency string|Invalid repository|Use 'user/repo'|path component",
            ):
                DependencyReference.parse(invalid_format)

    def test_parse_absolute_path_as_local(self):
        """Test that an absolute path like /repo is parsed as a local dependency."""
        dep = DependencyReference.parse("/repo")
        assert dep.is_local is True
        assert dep.local_path == "/repo"

    def test_to_github_url(self):
        """Test converting to GitHub URL."""
        dep = DependencyReference.parse("user/repo")
        expected = f"https://{github_host.default_host()}/user/repo"
        assert dep.to_github_url() == expected

    def test_get_display_name(self):
        """Test getting display name."""
        dep1 = DependencyReference.parse("user/repo")
        assert dep1.get_display_name() == "user/repo"

        # Dict format alias still works for display name
        dep2 = DependencyReference.parse_from_dict(
            {"git": "https://github.com/user/repo.git", "alias": "myalias"}
        )
        assert dep2.get_display_name() == "myalias"

    def test_string_representation(self):
        """Test string representation.

        Note: After PR #33, bare "user/repo" references will have host defaulted
        to github.com, so string representation includes it explicitly.
        """
        dep1 = DependencyReference.parse("user/repo")
        # After PR #33 changes, host is explicit in string representation
        assert dep1.repo_url == "user/repo"
        assert "user/repo" in str(dep1)

        dep2 = DependencyReference.parse("user/repo#main")
        assert dep2.repo_url == "user/repo"
        assert dep2.reference == "main"
        assert "user/repo" in str(dep2) and "#main" in str(dep2)

    def test_string_representation_with_enterprise_host(self):
        """Test that string representation includes host for enterprise dependencies.

        This tests the fix from PR #33 where __str__ now includes the host prefix
        for dependencies from non-default GitHub hosts.
        """
        # Enterprise host with just repo
        dep1 = DependencyReference.parse("company.ghe.com/user/repo")
        assert str(dep1) == "company.ghe.com/user/repo"

        # Enterprise host with reference
        dep2 = DependencyReference.parse("company.ghe.com/user/repo#v1.0.0")
        assert str(dep2) == "company.ghe.com/user/repo#v1.0.0"

        # Explicit github.com should also include host
        dep5 = DependencyReference.parse("github.com/user/repo")
        assert str(dep5) == "github.com/user/repo"


class TestAPMPackage:
    """Test APMPackage functionality."""

    def test_from_apm_yml_minimal(self):
        """Test loading minimal valid apm.yml."""
        apm_content = {"name": "test-package", "version": "1.0.0"}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(apm_content, f)
            f.flush()

            package = APMPackage.from_apm_yml(Path(f.name))
            assert package.name == "test-package"
            assert package.version == "1.0.0"
            assert package.description is None
            assert package.author is None
            assert package.dependencies is None

        Path(f.name).unlink()  # Clean up

    def test_from_apm_yml_complete(self):
        """Test loading complete apm.yml."""
        apm_content = {
            "name": "test-package",
            "version": "1.0.0",
            "description": "A test package",
            "author": "Test Author",
            "license": "MIT",
            "dependencies": {
                "apm": ["user/repo#main", "another/repo#v2.0"],
                "mcp": ["some-mcp-server"],
            },
            "scripts": {"start": "echo hello"},
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(apm_content, f)
            f.flush()

            package = APMPackage.from_apm_yml(Path(f.name))
            assert package.name == "test-package"
            assert package.version == "1.0.0"
            assert package.description == "A test package"
            assert package.author == "Test Author"
            assert package.license == "MIT"
            assert len(package.get_apm_dependencies()) == 2
            assert len(package.get_mcp_dependencies()) == 1
            assert package.scripts["start"] == "echo hello"

        Path(f.name).unlink()  # Clean up

    def test_from_apm_yml_missing_file(self):
        """Test loading from non-existent file."""
        with pytest.raises(FileNotFoundError):
            APMPackage.from_apm_yml(Path("/non/existent/file.yml"))

    def test_from_apm_yml_missing_required_fields(self):
        """Test loading apm.yml with missing required fields."""
        # Missing name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump({"version": "1.0.0"}, f)
            f.flush()

            with pytest.raises(ValueError, match="Missing required field 'name'"):
                APMPackage.from_apm_yml(Path(f.name))

        Path(f.name).unlink()

        # Missing version
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump({"name": "test"}, f)
            f.flush()

            with pytest.raises(ValueError, match="Missing required field 'version'"):
                APMPackage.from_apm_yml(Path(f.name))

        Path(f.name).unlink()

    def test_from_apm_yml_invalid_yaml(self):
        """Test loading invalid YAML."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("name: test\nversion: 1.0.0\ninvalid: [unclosed")
            f.flush()

            with pytest.raises(ValueError, match="Invalid YAML format"):
                APMPackage.from_apm_yml(Path(f.name))

        Path(f.name).unlink()

    def test_from_apm_yml_invalid_dependencies(self):
        """Test loading apm.yml with invalid dependency format."""
        apm_content = {
            "name": "test-package",
            "version": "1.0.0",
            "dependencies": {"apm": ["invalid-repo-format"]},
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(apm_content, f)
            f.flush()

            with pytest.raises(ValueError, match="Invalid APM dependency"):
                APMPackage.from_apm_yml(Path(f.name))

        Path(f.name).unlink()

    def test_has_apm_dependencies(self):
        """Test checking for APM dependencies."""
        # Package without dependencies
        pkg1 = APMPackage(name="test", version="1.0.0")
        assert not pkg1.has_apm_dependencies()

        # Package with MCP dependencies only
        pkg2 = APMPackage(
            name="test", version="1.0.0", dependencies={"mcp": ["server"]}
        )
        assert not pkg2.has_apm_dependencies()

        # Package with APM dependencies
        apm_deps = [DependencyReference.parse("user/repo")]
        pkg3 = APMPackage(name="test", version="1.0.0", dependencies={"apm": apm_deps})
        assert pkg3.has_apm_dependencies()


class TestValidationResult:
    """Test ValidationResult functionality."""

    def test_initial_state(self):
        """Test initial validation result state."""
        result = ValidationResult()
        assert result.is_valid is True
        assert result.errors == []
        assert result.warnings == []
        assert result.package is None
        assert not result.has_issues()

    def test_add_error(self):
        """Test adding validation errors."""
        result = ValidationResult()
        result.add_error("Test error")

        assert result.is_valid is False
        assert "Test error" in result.errors
        assert result.has_issues()

    def test_add_warning(self):
        """Test adding validation warnings."""
        result = ValidationResult()
        result.add_warning("Test warning")

        assert result.is_valid is True  # Warnings don't make package invalid
        assert "Test warning" in result.warnings
        assert result.has_issues()

    def test_summary(self):
        """Test validation summary messages."""
        # Valid with no issues
        result1 = ValidationResult()
        assert "[+] Package is valid" in result1.summary()

        # Valid with warnings
        result2 = ValidationResult()
        result2.add_warning("Test warning")
        assert "[!] Package is valid with 1 warning(s)" in result2.summary()

        # Invalid with errors
        result3 = ValidationResult()
        result3.add_error("Test error")
        assert "[x] Package is invalid with 1 error(s)" in result3.summary()


class TestPackageValidation:
    """Test APM package validation functionality."""

    def test_validate_non_existent_directory(self):
        """Test validating non-existent directory."""
        result = validate_apm_package(Path("/non/existent/dir"))
        assert not result.is_valid
        assert any("does not exist" in error for error in result.errors)

    def test_validate_file_instead_of_directory(self):
        """Test validating a file instead of directory."""
        with tempfile.NamedTemporaryFile() as f:
            result = validate_apm_package(Path(f.name))
            assert not result.is_valid
            assert any("not a directory" in error for error in result.errors)

    def test_validate_missing_apm_yml(self):
        """Test that a directory without apm.yml/SKILL.md/plugin evidence is invalid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = validate_apm_package(Path(tmpdir))
            # Empty directories without plugin.json or component dirs are not valid
            assert not result.is_valid
            assert result.package_type == PackageType.INVALID

    def test_validate_invalid_apm_yml(self):
        """Test validating directory with invalid apm.yml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            apm_yml = Path(tmpdir) / "apm.yml"
            apm_yml.write_text("invalid: [yaml")

            result = validate_apm_package(Path(tmpdir))
            assert not result.is_valid
            assert any("Invalid apm.yml" in error for error in result.errors)

    def test_validate_missing_apm_directory(self):
        """Test validating package without .apm directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            apm_yml = Path(tmpdir) / "apm.yml"
            apm_yml.write_text("name: test\nversion: 1.0.0")

            result = validate_apm_package(Path(tmpdir))
            assert not result.is_valid
            assert any(
                "Missing required directory: .apm/" in error for error in result.errors
            )

    def test_validate_apm_file_instead_of_directory(self):
        """Test validating package with .apm as file instead of directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            apm_yml = Path(tmpdir) / "apm.yml"
            apm_yml.write_text("name: test\nversion: 1.0.0")

            apm_file = Path(tmpdir) / ".apm"
            apm_file.write_text("this should be a directory")

            result = validate_apm_package(Path(tmpdir))
            assert not result.is_valid
            assert any(".apm must be a directory" in error for error in result.errors)

    def test_validate_empty_apm_directory(self):
        """Test validating package with empty .apm directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            apm_yml = Path(tmpdir) / "apm.yml"
            apm_yml.write_text("name: test\nversion: 1.0.0")

            apm_dir = Path(tmpdir) / ".apm"
            apm_dir.mkdir()

            result = validate_apm_package(Path(tmpdir))
            assert result.is_valid  # Should be valid but with warning
            assert any(
                "No primitive files found" in warning for warning in result.warnings
            )

    def test_validate_valid_package(self):
        """Test validating completely valid package."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create apm.yml
            apm_yml = Path(tmpdir) / "apm.yml"
            apm_yml.write_text("name: test\nversion: 1.0.0\ndescription: Test package")

            # Create .apm directory with primitives
            apm_dir = Path(tmpdir) / ".apm"
            apm_dir.mkdir()

            instructions_dir = apm_dir / "instructions"
            instructions_dir.mkdir()
            (instructions_dir / "test.instructions.md").write_text("# Test instruction")

            chatmodes_dir = apm_dir / "chatmodes"
            chatmodes_dir.mkdir()
            (chatmodes_dir / "test.chatmode.md").write_text("# Test chatmode")

            result = validate_apm_package(Path(tmpdir))
            assert result.is_valid
            assert result.package is not None
            assert result.package.name == "test"
            assert result.package.version == "1.0.0"

    def test_validate_version_format_warning(self):
        """Test validation warning for non-semver version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            apm_yml = Path(tmpdir) / "apm.yml"
            apm_yml.write_text("name: test\nversion: v1.0")  # Not proper semver

            apm_dir = Path(tmpdir) / ".apm"
            apm_dir.mkdir()
            instructions_dir = apm_dir / "instructions"
            instructions_dir.mkdir()
            (instructions_dir / "test.instructions.md").write_text("# Test")

            result = validate_apm_package(Path(tmpdir))
            assert result.is_valid
            assert any(
                "doesn't follow semantic versioning" in warning
                for warning in result.warnings
            )

    def test_validate_numeric_version_types(self):
        """Test that version validation handles YAML numeric types.

        This tests the fix from PR #33 for non-string version values.
        YAML may parse unquoted version numbers as numeric types (int/float).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            apm_yml = Path(tmpdir) / "apm.yml"
            # Write YAML with numeric version (no quotes)
            apm_yml.write_text("name: test\nversion: 1.0\ndescription: Test")

            apm_dir = Path(tmpdir) / ".apm"
            apm_dir.mkdir()
            instructions_dir = apm_dir / "instructions"
            instructions_dir.mkdir()
            (instructions_dir / "test.instructions.md").write_text("# Test")

            # Should not crash when validating
            result = validate_apm_package(Path(tmpdir))
            assert result is not None
            # May have warning about semver format, but should not crash
            if not result.is_valid:
                # Check that any errors are about semver format, not type errors
                for error in result.errors:
                    assert "AttributeError" not in error
                    assert "has no attribute" not in error


class TestClaudeSkillValidation:
    """Test Claude Skill (SKILL.md-only) validation and APMPackage creation from SKILL metadata without generating an apm.yml."""

    def test_validate_skill_with_simple_description(self):
        """Test validating a Claude Skill with simple description."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_md = Path(tmpdir) / "SKILL.md"
            skill_md.write_text("""---
name: test-skill
description: A simple test skill
---

# Test Skill

This is a test skill content.
""")

            result = validate_apm_package(Path(tmpdir))
            assert result.is_valid, f"Errors: {result.errors}"
            assert result.package is not None
            assert result.package.name == "test-skill"

    def test_validate_skill_with_colons_in_description(self):
        """Test validating a Claude Skill with colons in description (GitHub issue #66)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_md = Path(tmpdir) / "SKILL.md"
            # This is the actual pptx skill description that was causing issues
            skill_md.write_text("""---
name: pptx
description: "Presentation creation, editing, and analysis. When Claude needs to work with presentations (.pptx files) for: (1) Creating new presentations, (2) Modifying or editing content, (3) Working with layouts, (4) Adding comments or speaker notes, or any other presentation tasks"
---

# PPTX Skill

Content here.
""")

            result = validate_apm_package(Path(tmpdir))
            assert result.is_valid, f"Errors: {result.errors}"
            assert result.package is not None
            assert result.package.name == "pptx"
            # Verify the description was preserved (colons and all)
            assert "for:" in result.package.description

    def test_validate_skill_with_quotes_in_description(self):
        """Test validating a Claude Skill with quotes in description."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_md = Path(tmpdir) / "SKILL.md"
            skill_md.write_text("""---
name: test-skill
description: 'A skill that handles "quoted" strings'
---

# Test Skill
""")

            result = validate_apm_package(Path(tmpdir))
            assert result.is_valid, f"Errors: {result.errors}"
            assert result.package is not None
            assert '"quoted"' in result.package.description

    def test_validate_skill_with_special_yaml_characters(self):
        """Test validating a Claude Skill with various YAML special characters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_md = Path(tmpdir) / "SKILL.md"
            skill_md.write_text("""---
name: special-skill
description: "Handles: colons, #hashtags, [brackets], {braces}, and 'quotes'"
---

# Special Skill
""")

            result = validate_apm_package(Path(tmpdir))
            assert result.is_valid, f"Errors: {result.errors}"
            assert result.package is not None

    def test_validate_skill_without_description(self):
        """Test validating a Claude Skill without description field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_md = Path(tmpdir) / "SKILL.md"
            skill_md.write_text("""---
name: minimal-skill
---

# Minimal Skill
""")

            result = validate_apm_package(Path(tmpdir))
            assert result.is_valid, f"Errors: {result.errors}"
            assert result.package is not None
            # Description should be auto-generated
            assert "Claude Skill: minimal-skill" in result.package.description


class TestHookPackageValidation:
    """Test hook-only package validation."""

    def test_validate_hook_package_with_hooks_dir(self):
        """Test validating a package with only hooks/hooks.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_dir = Path(tmpdir) / "hooks"
            hooks_dir.mkdir()
            hooks_json = hooks_dir / "hooks.json"
            hooks_json.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "hooks": [
                                        {"type": "command", "command": "echo hello"}
                                    ]
                                }
                            ]
                        }
                    }
                )
            )

            result = validate_apm_package(Path(tmpdir))
            assert result.is_valid, f"Errors: {result.errors}"
            assert result.package_type == PackageType.HOOK_PACKAGE
            assert result.package is not None
            assert result.package.name == Path(tmpdir).name

    def test_validate_hook_package_with_apm_hooks_dir(self):
        """Test validating a package with .apm/hooks/*.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_dir = Path(tmpdir) / ".apm" / "hooks"
            hooks_dir.mkdir(parents=True)
            hooks_json = hooks_dir / "my-hooks.json"
            hooks_json.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {"hooks": [{"type": "command", "command": "echo bye"}]}
                            ]
                        }
                    }
                )
            )

            result = validate_apm_package(Path(tmpdir))
            assert result.is_valid, f"Errors: {result.errors}"
            assert result.package_type == PackageType.HOOK_PACKAGE

    def test_validate_hook_package_prefers_apm_yml(self):
        """Test that apm.yml takes precedence over hooks/ for type detection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create both apm.yml + .apm/ and hooks/
            apm_yml = Path(tmpdir) / "apm.yml"
            apm_yml.write_text("name: test\nversion: 1.0.0")
            apm_dir = Path(tmpdir) / ".apm" / "instructions"
            apm_dir.mkdir(parents=True)
            (apm_dir / "main.md").write_text("# Instructions")
            hooks_dir = Path(tmpdir) / "hooks"
            hooks_dir.mkdir()
            (hooks_dir / "hooks.json").write_text('{"hooks": {}}')

            result = validate_apm_package(Path(tmpdir))
            assert result.is_valid, f"Errors: {result.errors}"
            assert result.package_type == PackageType.APM_PACKAGE

    def test_validate_empty_dir_is_invalid(self):
        """Test that a dir with no apm.yml, SKILL.md, hooks, or plugin evidence is invalid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = validate_apm_package(Path(tmpdir))
            # Empty directories without plugin.json or component dirs are not valid
            assert not result.is_valid
            assert result.package_type == PackageType.INVALID


from src.apm_cli.models.validation import detect_package_type


class TestDetectPackageType:
    """Tests for the centralized detect_package_type() function."""

    def test_hybrid_when_both_apm_yml_and_skill_md(self, tmp_path):
        (tmp_path / "apm.yml").write_text("name: test")
        (tmp_path / "SKILL.md").write_text("# Skill")
        pkg_type, pj_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.HYBRID
        assert pj_path is None

    def test_apm_package_when_only_apm_yml(self, tmp_path):
        (tmp_path / "apm.yml").write_text("name: test")
        pkg_type, pj_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.APM_PACKAGE
        assert pj_path is None

    def test_claude_skill_when_only_skill_md(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("# Skill")
        pkg_type, pj_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.CLAUDE_SKILL
        assert pj_path is None

    def test_hook_package_when_hooks_json(self, tmp_path):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "pre-commit.json").write_text("{}")
        pkg_type, pj_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.HOOK_PACKAGE
        assert pj_path is None

    def test_marketplace_plugin_with_plugin_json(self, tmp_path):
        (tmp_path / "plugin.json").write_text('{"name": "test"}')
        pkg_type, pj_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN
        assert pj_path is not None
        assert pj_path.name == "plugin.json"

    def test_marketplace_plugin_with_agents_dir(self, tmp_path):
        (tmp_path / "agents").mkdir()
        pkg_type, pj_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN
        assert pj_path is None

    def test_marketplace_plugin_with_skills_dir(self, tmp_path):
        (tmp_path / "skills").mkdir()
        pkg_type, pj_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN
        assert pj_path is None

    def test_marketplace_plugin_with_commands_dir(self, tmp_path):
        (tmp_path / "commands").mkdir()
        pkg_type, pj_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN
        assert pj_path is None

    def test_invalid_when_empty_dir(self, tmp_path):
        pkg_type, pj_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID
        assert pj_path is None

    def test_apm_yml_takes_precedence_over_plugin_json(self, tmp_path):
        (tmp_path / "apm.yml").write_text("name: test")
        (tmp_path / "plugin.json").write_text('{"name": "test"}')
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.APM_PACKAGE

    def test_hook_package_apm_yml_precedence(self, tmp_path):
        """apm.yml takes precedence even when hooks exist."""
        (tmp_path / "apm.yml").write_text("name: test")
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "pre-commit.json").write_text("{}")
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.APM_PACKAGE

    def test_marketplace_plugin_wins_over_hooks_via_agents_dir(self, tmp_path):
        """Regression: a Claude plugin that ships hooks AND agents/ must
        classify as MARKETPLACE_PLUGIN so the plugin synthesizer maps
        agents alongside hooks. See microsoft/apm#780.
        """
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "hooks.json").write_text("{}")
        (tmp_path / "agents").mkdir()
        pkg_type, pj_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN
        assert pj_path is None

    def test_marketplace_plugin_wins_over_hooks_via_plugin_json(self, tmp_path):
        """Regression: hooks must not pre-empt classification when a
        plugin.json is present. See microsoft/apm#780.
        """
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "hooks.json").write_text("{}")
        (tmp_path / "plugin.json").write_text('{"name": "test"}')
        pkg_type, pj_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN
        assert pj_path is not None
        assert pj_path.name == "plugin.json"

    def test_obra_superpowers_layout(self, tmp_path):
        """Full-fidelity reproducer for microsoft/apm#780: the
        obra/superpowers repo ships hooks/hooks.json alongside
        .claude-plugin/plugin.json + agents/ + skills/ + commands/.
        Pre-fix, this classified as HOOK_PACKAGE and the skills,
        agent, and commands were silently dropped.
        """
        # Hooks (the trap that was firing first).
        (tmp_path / "hooks").mkdir()
        (tmp_path / "hooks" / "hooks.json").write_text("{}")
        # Plugin shape.
        (tmp_path / ".claude-plugin").mkdir()
        (tmp_path / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "superpowers"}'
        )
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "code-reviewer.md").write_text("# agent")
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "tdd").mkdir()
        (tmp_path / "skills" / "tdd" / "SKILL.md").write_text("# tdd")
        (tmp_path / "commands").mkdir()
        (tmp_path / "commands" / "foo.md").write_text("# cmd")
        pkg_type, pj_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN
        assert pj_path is not None
        assert pj_path.name == "plugin.json"


class TestHybridPackageValidation:
    """Tests for HYBRID package validation (apm.yml + SKILL.md, no .apm/).

    Genesis-layout reproducer: apm.yml + SKILL.md + optional agents/ at
    repo root, no .apm/ directory.  validate_apm_package must return
    package_type == HYBRID with no errors.
    """

    def test_hybrid_no_apm_dir_validates_as_skill_bundle(self, tmp_path):
        """Core reproducer: HYBRID layout without .apm/ is valid."""
        (tmp_path / "apm.yml").write_text(
            "name: genesis\n"
            "version: 1.0.0\n"
            "description: Genesis architect\n"
        )
        (tmp_path / "SKILL.md").write_text(
            "---\nname: genesis\ndescription: skill desc\n---\n# Genesis Skill\n"
        )
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "genesis-architect.agent.md").write_text("# Agent")

        result = validate_apm_package(tmp_path)
        assert result.is_valid, f"Expected valid but got errors: {result.errors}"
        assert result.package_type == PackageType.HYBRID
        assert result.package is not None
        assert result.package.name == "genesis"
        assert result.package.version == "1.0.0"

    def test_hybrid_with_apm_dir_falls_through_to_standard(self, tmp_path):
        """HYBRID with .apm/ present uses standard APM package validation."""
        (tmp_path / "apm.yml").write_text(
            "name: hybrid-classic\nversion: 2.0.0\n"
        )
        (tmp_path / "SKILL.md").write_text("# Skill")
        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir()
        inst_dir = apm_dir / "instructions"
        inst_dir.mkdir()
        (inst_dir / "foo.instructions.md").write_text("# Foo")

        result = validate_apm_package(tmp_path)
        assert result.is_valid, f"Expected valid but got errors: {result.errors}"
        assert result.package_type == PackageType.HYBRID

    def test_hybrid_bad_apm_yml_reports_error(self, tmp_path):
        """HYBRID with malformed apm.yml is invalid."""
        (tmp_path / "apm.yml").write_text("invalid: [yaml")
        (tmp_path / "SKILL.md").write_text("# Skill")

        result = validate_apm_package(tmp_path)
        assert not result.is_valid
        assert any("Invalid apm.yml" in e for e in result.errors)

    def test_hybrid_skill_md_description_does_not_backfill_into_apm_yml(self, tmp_path):
        """apm.yml.description and SKILL.md description are independent.

        SKILL.md is consumed by the agent runtime (invocation matcher per
        agentskills.io); apm.yml.description is consumed by APM tooling
        (`apm view`, search, listings). They serve different consumers
        and APM never merges them. When apm.yml omits its description,
        ``APMPackage.description`` stays ``None`` -- the SKILL.md value
        does NOT silently leak into the human-facing tagline slot.
        """
        (tmp_path / "apm.yml").write_text(
            "name: genesis\nversion: 1.0.0\n"
        )
        (tmp_path / "SKILL.md").write_text(
            "---\ndescription: from-skill-md\n---\n# Skill\n"
        )

        result = validate_apm_package(tmp_path)
        assert result.is_valid
        assert result.package.description is None

    def test_hybrid_apm_yml_description_wins_over_skill_md(self, tmp_path):
        """apm.yml.description is the only source for APMPackage.description.

        When apm.yml provides a description, that value is used verbatim
        regardless of SKILL.md frontmatter -- there is no merge.
        """
        (tmp_path / "apm.yml").write_text(
            "name: genesis\nversion: 1.0.0\ndescription: from-apm-yml\n"
        )
        (tmp_path / "SKILL.md").write_text(
            "---\ndescription: from-skill-md\n---\n# Skill\n"
        )

        result = validate_apm_package(tmp_path)
        assert result.is_valid
        assert result.package.description == "from-apm-yml"

    def test_hybrid_both_descriptions_independent(self, tmp_path):
        """SKILL.md content is preserved on disk untouched after validation.

        APM must never mutate the SKILL.md file; the agent runtime reads
        it byte-for-byte from `<target>/skills/<name>/SKILL.md` after
        integration. This test asserts (a) APMPackage.description comes
        only from apm.yml and (b) SKILL.md is untouched on disk.
        """
        skill_md_content = (
            "---\n"
            "name: genesis\n"
            "description: This skill should be invoked when the user asks "
            "about Genesis architecture decisions.\n"
            "allowed-tools: [bash, view]\n"
            "---\n"
            "# Genesis Skill\n"
        )
        (tmp_path / "apm.yml").write_text(
            "name: genesis\nversion: 1.0.0\ndescription: short tagline\n"
        )
        (tmp_path / "SKILL.md").write_text(skill_md_content)

        result = validate_apm_package(tmp_path)
        assert result.is_valid
        assert result.package.description == "short tagline"
        # SKILL.md must be untouched -- the agent runtime reads it verbatim.
        assert (tmp_path / "SKILL.md").read_text() == skill_md_content


class TestClaudeSkillPackageValidation:
    """Tests for CLAUDE_SKILL packages (SKILL.md only, no apm.yml).

    Verifies the ``SKILL.md + agents/ + assets/`` layout (no apm.yml)
    classifies as CLAUDE_SKILL and is NOT misclassified as
    MARKETPLACE_PLUGIN even though ``agents/`` is in ``_PLUGIN_DIRS``.
    """

    def test_claude_skill_with_agents_and_assets_validates(self, tmp_path):
        """CLAUDE_SKILL with agents/ and assets/ sub-dirs passes validation."""
        (tmp_path / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: A skill with agents\n---\n"
            "# My Skill\n"
        )
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "foo.agent.md").write_text("# Foo Agent")
        assets_dir = tmp_path / "assets"
        assets_dir.mkdir()
        (assets_dir / "logo.png").write_bytes(b"\x89PNG")

        result = validate_apm_package(tmp_path)
        assert result.is_valid, f"Expected valid but got errors: {result.errors}"
        assert result.package_type == PackageType.CLAUDE_SKILL
        assert result.package is not None
        assert result.package.name == "my-skill"

    def test_claude_skill_with_agents_dir_not_misclassified_as_plugin(self, tmp_path):
        """SKILL.md presence beats agents/ directory in the detection cascade.

        ``agents/`` is in ``_PLUGIN_DIRS``, so without SKILL.md it would
        classify as MARKETPLACE_PLUGIN.  With SKILL.md present the cascade
        must short-circuit to CLAUDE_SKILL (step 3 precedes step 4).
        """
        (tmp_path / "SKILL.md").write_text(
            "---\nname: agents-skill\n---\n# Has Agents\n"
        )
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "bar.agent.md").write_text("# Bar Agent")

        # Detection level
        pkg_type, pj = detect_package_type(tmp_path)
        assert pkg_type == PackageType.CLAUDE_SKILL
        assert pj is None

        # Full validation level
        result = validate_apm_package(tmp_path)
        assert result.is_valid, f"Expected valid but got errors: {result.errors}"
        assert result.package_type == PackageType.CLAUDE_SKILL
        assert result.package_type != PackageType.MARKETPLACE_PLUGIN


class TestGatherDetectionEvidence:
    """Tests for the evidence-gathering helper that powers observability."""

    def test_empty_directory(self, tmp_path):
        from apm_cli.models.validation import gather_detection_evidence

        evidence = gather_detection_evidence(tmp_path)
        assert evidence.has_apm_yml is False
        assert evidence.has_skill_md is False
        assert evidence.has_hook_json is False
        assert evidence.plugin_json_path is None
        assert evidence.plugin_dirs_present == ()
        assert evidence.has_plugin_evidence is False

    def test_records_plugin_dirs_in_canonical_order(self, tmp_path):
        from apm_cli.models.validation import gather_detection_evidence

        # Create in non-canonical order; expect canonical order in result.
        (tmp_path / "commands").mkdir()
        (tmp_path / "agents").mkdir()
        (tmp_path / "skills").mkdir()
        evidence = gather_detection_evidence(tmp_path)
        assert evidence.plugin_dirs_present == ("agents", "skills", "commands")
        assert evidence.has_plugin_evidence is True

    def test_obra_superpowers_evidence(self, tmp_path):
        """Evidence on the #780 repro should expose every signal the
        UX layer needs to explain the classification.
        """
        from apm_cli.models.validation import gather_detection_evidence

        (tmp_path / "hooks").mkdir()
        (tmp_path / "hooks" / "hooks.json").write_text("{}")
        (tmp_path / ".claude-plugin").mkdir()
        (tmp_path / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "superpowers"}'
        )
        (tmp_path / "agents").mkdir()
        (tmp_path / "skills").mkdir()
        (tmp_path / "commands").mkdir()
        evidence = gather_detection_evidence(tmp_path)
        assert evidence.has_hook_json is True
        assert evidence.plugin_json_path is not None
        assert evidence.plugin_dirs_present == ("agents", "skills", "commands")
        assert evidence.has_claude_plugin_dir is True
        assert evidence.has_plugin_evidence is True

    def test_claude_plugin_dir_alone_is_plugin_evidence(self, tmp_path):
        """A bare ``.claude-plugin/`` directory (no plugin.json, no
        agents/skills/commands) must still classify as plugin evidence
        so a Claude Code plugin without a manifest is not silently
        treated as hooks-only.  See microsoft/apm#780.
        """
        from src.apm_cli.models.validation import (
            detect_package_type,
            gather_detection_evidence,
        )

        (tmp_path / ".claude-plugin").mkdir()
        (tmp_path / "hooks").mkdir()
        (tmp_path / "hooks" / "hooks.json").write_text("{}")

        evidence = gather_detection_evidence(tmp_path)
        assert evidence.has_claude_plugin_dir is True
        assert evidence.plugin_dirs_present == ()
        assert evidence.plugin_json_path is None
        assert evidence.has_plugin_evidence is True

        pkg_type, pj_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN
        # No plugin.json file present -> path is None even though we matched.
        assert pj_path is None


class TestGitReferenceUtils:
    """Test Git reference parsing utilities."""

    def test_parse_git_reference_branch(self):
        """Test parsing branch references."""
        ref_type, ref = parse_git_reference("main")
        assert ref_type == GitReferenceType.BRANCH
        assert ref == "main"

        ref_type, ref = parse_git_reference("feature/new-stuff")
        assert ref_type == GitReferenceType.BRANCH
        assert ref == "feature/new-stuff"

    def test_parse_git_reference_tag(self):
        """Test parsing tag references."""
        ref_type, ref = parse_git_reference("v1.0.0")
        assert ref_type == GitReferenceType.TAG
        assert ref == "v1.0.0"

        ref_type, ref = parse_git_reference("1.2.3")
        assert ref_type == GitReferenceType.TAG
        assert ref == "1.2.3"

    def test_parse_git_reference_commit(self):
        """Test parsing commit SHA references."""
        # Full SHA
        ref_type, ref = parse_git_reference("abcdef1234567890abcdef1234567890abcdef12")
        assert ref_type == GitReferenceType.COMMIT
        assert ref == "abcdef1234567890abcdef1234567890abcdef12"

        # Short SHA
        ref_type, ref = parse_git_reference("abcdef1")
        assert ref_type == GitReferenceType.COMMIT
        assert ref == "abcdef1"

    def test_parse_git_reference_empty(self):
        """Test parsing empty reference defaults to main branch."""
        ref_type, ref = parse_git_reference("")
        assert ref_type == GitReferenceType.BRANCH
        assert ref == "main"

        ref_type, ref = parse_git_reference(None)
        assert ref_type == GitReferenceType.BRANCH
        assert ref == "main"


class TestResolvedReference:
    """Test ResolvedReference functionality."""

    def test_string_representation(self):
        """Test string representation of resolved references."""
        # Commit reference
        commit_ref = ResolvedReference(
            original_ref="abc123",
            ref_type=GitReferenceType.COMMIT,
            resolved_commit="abc123def456",
            ref_name="abc123",
        )
        assert str(commit_ref) == "abc123de"  # First 8 chars

        # Branch reference
        branch_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123def456",
            ref_name="main",
        )
        assert str(branch_ref) == "main (abc123de)"

        # Tag reference
        tag_ref = ResolvedReference(
            original_ref="v1.0.0",
            ref_type=GitReferenceType.TAG,
            resolved_commit="abc123def456",
            ref_name="v1.0.0",
        )
        assert str(tag_ref) == "v1.0.0 (abc123de)"


class TestPackageInfo:
    """Test PackageInfo functionality."""

    def test_get_primitives_path(self):
        """Test getting primitives path."""
        package = APMPackage(name="test", version="1.0.0")
        install_path = Path("/tmp/package")

        info = PackageInfo(package=package, install_path=install_path)
        assert info.get_primitives_path() == install_path / ".apm"

    def test_has_primitives(self):
        """Test checking if package has primitives."""
        with tempfile.TemporaryDirectory() as tmpdir:
            package = APMPackage(name="test", version="1.0.0")
            install_path = Path(tmpdir)

            info = PackageInfo(package=package, install_path=install_path)

            # No .apm directory
            assert not info.has_primitives()

            # Empty .apm directory
            apm_dir = install_path / ".apm"
            apm_dir.mkdir()
            assert not info.has_primitives()

            # .apm with empty subdirectories
            (apm_dir / "instructions").mkdir()
            assert not info.has_primitives()

            # .apm with primitive files
            (apm_dir / "instructions" / "test.md").write_text("# Test")
            assert info.has_primitives()


class TestPackageContentType:
    """Test PackageContentType enum and parsing."""

    def test_enum_values(self):
        """Test that all expected enum values exist."""
        assert PackageContentType.INSTRUCTIONS.value == "instructions"
        assert PackageContentType.SKILL.value == "skill"
        assert PackageContentType.HYBRID.value == "hybrid"
        assert PackageContentType.PROMPTS.value == "prompts"

    def test_from_string_valid_values(self):
        """Test parsing all valid type values."""
        assert (
            PackageContentType.from_string("instructions")
            == PackageContentType.INSTRUCTIONS
        )
        assert PackageContentType.from_string("skill") == PackageContentType.SKILL
        assert PackageContentType.from_string("hybrid") == PackageContentType.HYBRID
        assert PackageContentType.from_string("prompts") == PackageContentType.PROMPTS

    def test_from_string_case_insensitive(self):
        """Test that parsing is case-insensitive."""
        assert (
            PackageContentType.from_string("INSTRUCTIONS")
            == PackageContentType.INSTRUCTIONS
        )
        assert PackageContentType.from_string("Skill") == PackageContentType.SKILL
        assert PackageContentType.from_string("HYBRID") == PackageContentType.HYBRID
        assert PackageContentType.from_string("Prompts") == PackageContentType.PROMPTS

    def test_from_string_with_whitespace(self):
        """Test that parsing handles leading/trailing whitespace."""
        assert (
            PackageContentType.from_string("  instructions  ")
            == PackageContentType.INSTRUCTIONS
        )
        assert PackageContentType.from_string("\tskill\n") == PackageContentType.SKILL

    def test_from_string_invalid_value(self):
        """Test that invalid values raise ValueError with helpful message."""
        with pytest.raises(ValueError) as exc_info:
            PackageContentType.from_string("invalid")

        error_msg = str(exc_info.value)
        assert "Invalid package type 'invalid'" in error_msg
        assert "'instructions'" in error_msg
        assert "'skill'" in error_msg
        assert "'hybrid'" in error_msg
        assert "'prompts'" in error_msg

    def test_from_string_empty_value(self):
        """Test that empty string raises ValueError."""
        with pytest.raises(ValueError, match="Package type cannot be empty"):
            PackageContentType.from_string("")

    def test_from_string_typo_suggestions(self):
        """Test helpful error message for common typos."""
        # Test that error message lists all valid types
        with pytest.raises(ValueError) as exc_info:
            PackageContentType.from_string("instruction")  # Missing 's'

        error_msg = str(exc_info.value)
        assert "'instructions'" in error_msg  # Shows correct spelling


class TestAPMPackageTypeField:
    """Test APMPackage type field parsing from apm.yml."""

    def test_type_field_instructions(self):
        """Test parsing type: instructions from apm.yml."""
        apm_content = {
            "name": "test-package",
            "version": "1.0.0",
            "type": "instructions",
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(apm_content, f)
            f.flush()

            package = APMPackage.from_apm_yml(Path(f.name))
            assert package.type == PackageContentType.INSTRUCTIONS

        Path(f.name).unlink()

    def test_type_field_skill(self):
        """Test parsing type: skill from apm.yml."""
        apm_content = {"name": "test-package", "version": "1.0.0", "type": "skill"}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(apm_content, f)
            f.flush()

            package = APMPackage.from_apm_yml(Path(f.name))
            assert package.type == PackageContentType.SKILL

        Path(f.name).unlink()

    def test_type_field_hybrid(self):
        """Test parsing type: hybrid from apm.yml."""
        apm_content = {"name": "test-package", "version": "1.0.0", "type": "hybrid"}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(apm_content, f)
            f.flush()

            package = APMPackage.from_apm_yml(Path(f.name))
            assert package.type == PackageContentType.HYBRID

        Path(f.name).unlink()

    def test_type_field_prompts(self):
        """Test parsing type: prompts from apm.yml."""
        apm_content = {"name": "test-package", "version": "1.0.0", "type": "prompts"}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(apm_content, f)
            f.flush()

            package = APMPackage.from_apm_yml(Path(f.name))
            assert package.type == PackageContentType.PROMPTS

        Path(f.name).unlink()

    def test_type_field_missing_defaults_to_none(self):
        """Test that missing type field defaults to None (hybrid behavior)."""
        apm_content = {"name": "test-package", "version": "1.0.0"}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(apm_content, f)
            f.flush()

            package = APMPackage.from_apm_yml(Path(f.name))
            assert package.type is None  # Default to None for backward compatibility

        Path(f.name).unlink()

    def test_type_field_invalid_raises_error(self):
        """Test that invalid type value raises ValueError."""
        apm_content = {
            "name": "test-package",
            "version": "1.0.0",
            "type": "invalid-type",
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(apm_content, f)
            f.flush()

            with pytest.raises(ValueError) as exc_info:
                APMPackage.from_apm_yml(Path(f.name))

            error_msg = str(exc_info.value)
            assert "Invalid 'type' field" in error_msg
            assert "invalid-type" in error_msg

        Path(f.name).unlink()

    def test_type_field_non_string_raises_error(self):
        """Test that non-string type value raises ValueError."""
        apm_content = {
            "name": "test-package",
            "version": "1.0.0",
            "type": 123,  # Numeric type
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(apm_content, f)
            f.flush()

            with pytest.raises(ValueError) as exc_info:
                APMPackage.from_apm_yml(Path(f.name))

            error_msg = str(exc_info.value)
            assert "expected string" in error_msg
            assert "int" in error_msg

        Path(f.name).unlink()

    def test_type_field_case_insensitive_in_yaml(self):
        """Test that type field parsing is case-insensitive in YAML."""
        apm_content = {
            "name": "test-package",
            "version": "1.0.0",
            "type": "SKILL",  # Uppercase
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(apm_content, f)
            f.flush()

            package = APMPackage.from_apm_yml(Path(f.name))
            assert package.type == PackageContentType.SKILL

        Path(f.name).unlink()

    def test_type_field_null_treated_as_missing(self):
        """Test that explicit null type field is treated as missing."""
        # Write YAML directly to handle null explicitly
        yaml_content = """name: test-package
version: "1.0.0"
type: null
"""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()

            package = APMPackage.from_apm_yml(Path(f.name))
            assert package.type is None

        Path(f.name).unlink()

    def test_package_dataclass_with_type(self):
        """Test that APMPackage dataclass accepts type parameter."""
        package = APMPackage(
            name="test", version="1.0.0", type=PackageContentType.SKILL
        )
        assert package.type == PackageContentType.SKILL

    def test_package_dataclass_type_defaults_to_none(self):
        """Test that APMPackage type defaults to None when not provided."""
        package = APMPackage(name="test", version="1.0.0")
        assert package.type is None


class TestGenericHostSubdirectoryRoundTrip:
    """Regression tests for issue #382: subdirectory packages on generic git hosts.

    The str() -> parse() round-trip must preserve virtual_path for all hosts,
    not just GitHub and ADO.
    """

    @pytest.mark.parametrize(
        "git_url,path,ref,desc",
        [
            (
                "https://git.example.com/ai/grandpa-s-skills",
                "dist/brain-council",
                "master",
                "reporter case",
            ),
            (
                "https://gitlab.com/my-org/my-group/my-skills",
                "dist/skill-a",
                "main",
                "GitLab nested groups",
            ),
            (
                "https://gitea.example.com/org/repo",
                "prompts/helper",
                "v1.0",
                "Gitea simple",
            ),
            (
                "https://bitbucket.example.com/team/prompts",
                "agents/summarizer",
                "develop",
                "Bitbucket self-hosted",
            ),
        ],
    )
    def test_parse_from_dict_preserves_virtual_path(self, git_url, path, ref, desc):
        """parse_from_dict correctly separates repo URL from subdirectory path."""
        entry = {"git": git_url, "path": path, "ref": ref}
        dep = DependencyReference.parse_from_dict(entry)
        assert dep.virtual_path == path, f"Failed for {desc}"
        assert dep.is_virtual is True, f"Failed for {desc}"

    @pytest.mark.parametrize(
        "git_url,path,ref",
        [
            (
                "https://git.example.com/ai/grandpa-s-skills",
                "dist/brain-council",
                "master",
            ),
            ("https://gitlab.com/org/repo", "prompts/helper", "v1.0"),
            (
                "https://bitbucket.example.com/team/prompts",
                "agents/summarizer",
                "develop",
            ),
        ],
    )
    def test_download_package_skips_parse_with_structured_dep(self, git_url, path, ref):
        """download_package must skip DependencyReference.parse() when given
        a structured object, avoiding the lossy round-trip."""
        entry = {"git": git_url, "path": path, "ref": ref}
        dep = DependencyReference.parse_from_dict(entry)

        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        downloader = GitHubPackageDownloader()

        # Monkey-patch DependencyReference.parse to detect if it's called
        original_parse = DependencyReference.parse
        parse_called = False

        @classmethod
        def tracking_parse(cls, s):
            nonlocal parse_called
            parse_called = True
            return original_parse(s)

        DependencyReference.parse = tracking_parse
        try:
            # download_package will fail on the actual clone, but the important
            # thing is that it does NOT call parse() when given an object
            downloader.download_package(dep, Path("/tmp/apm-test-nonexistent"))
        except Exception:
            pass  # Expected: clone will fail, but parse should not be called
        finally:
            DependencyReference.parse = original_parse

        assert not parse_called, (
            "DependencyReference.parse() was called when passing a structured "
            "DependencyReference -- the lossy round-trip was NOT avoided"
        )

    def test_github_round_trip_works(self):
        """GitHub round-trip works because min_base_segments=2 is hardcoded."""
        entry = {
            "git": "https://github.com/anthropics/skills",
            "path": "skills/skill-creator",
            "ref": "main",
        }
        dep = DependencyReference.parse_from_dict(entry)
        dep2 = DependencyReference.parse(str(dep))
        assert dep2.virtual_path == dep.virtual_path
        assert dep2.is_virtual == dep.is_virtual

    def test_build_download_ref_preserves_virtual_path(self):
        """build_download_ref returns a DependencyReference that preserves
        virtual_path for generic hosts (not a lossy flat string)."""
        from unittest.mock import Mock

        from apm_cli.drift import build_download_ref

        dep = DependencyReference(
            repo_url="org/my-skills",
            host="git.example.com",
            reference="main",
            virtual_path="dist/brain-council",
            is_virtual=True,
        )
        lockfile = Mock()
        locked_dep = Mock()
        locked_dep.resolved_commit = "abc123"
        lockfile.get_dependency = Mock(return_value=locked_dep)

        result = build_download_ref(dep, lockfile, update_refs=False, ref_changed=False)
        assert result.virtual_path == "dist/brain-council"
        assert result.repo_url == "org/my-skills"
        assert result.host == "git.example.com"
        assert result.reference == "abc123"
        assert result.is_virtual is True
