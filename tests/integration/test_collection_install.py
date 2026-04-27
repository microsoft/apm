"""Integration tests for collection virtual package installation."""

import shutil  # noqa: F401
import tempfile
from pathlib import Path

import pytest

from apm_cli.deps.github_downloader import GitHubPackageDownloader, normalize_collection_path
from apm_cli.models.apm_package import DependencyReference


class TestCollectionInstallation:
    """Test collection virtual package installation from GitHub."""

    def test_parse_collection_dependency(self):
        """Test parsing a collection dependency reference."""
        dep_ref = DependencyReference.parse("owner/test-repo/collections/awesome-copilot")

        assert dep_ref.is_virtual is True
        assert dep_ref.is_virtual_collection() is True
        assert dep_ref.is_virtual_file() is False
        assert dep_ref.repo_url == "owner/test-repo"
        assert dep_ref.virtual_path == "collections/awesome-copilot"
        assert dep_ref.get_virtual_package_name() == "test-repo-awesome-copilot"

    def test_parse_collection_with_reference(self):
        """Test parsing a collection dependency with git reference."""
        dep_ref = DependencyReference.parse("owner/test-repo/collections/project-planning#main")

        assert dep_ref.is_virtual is True
        assert dep_ref.is_virtual_collection() is True
        assert dep_ref.reference == "main"
        assert dep_ref.virtual_path == "collections/project-planning"

    @pytest.mark.integration
    @pytest.mark.slow
    @pytest.mark.skip(
        reason="github/awesome-copilot no longer has collections/ directory (deprecated in favor of plugins)"
    )
    def test_download_small_collection(self):
        """Test downloading a small collection from awesome-copilot.

        This is a real integration test that requires:
        - Network access
        - GitHub API access
        - The github/awesome-copilot repository to be accessible
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "test-collection"

            downloader = GitHubPackageDownloader()

            # Download the smallest collection (awesome-copilot has 6 items)
            package_info = downloader.download_package(
                "github/awesome-copilot/collections/awesome-copilot", target_path
            )

            # Verify package was created
            assert package_info is not None
            assert package_info.package.name == "awesome-copilot-awesome-copilot"
            assert "Meta prompts" in package_info.package.description

            # Verify apm.yml was generated
            apm_yml = target_path / "apm.yml"
            assert apm_yml.exists()

            # Verify .apm directory structure
            apm_dir = target_path / ".apm"
            assert apm_dir.exists()

            # Verify files were downloaded to correct subdirectories
            # The collection should have prompts and chatmodes
            prompts_dir = apm_dir / "prompts"
            chatmodes_dir = apm_dir / "chatmodes"

            # At least one of these should exist and have files
            has_prompts = prompts_dir.exists() and any(prompts_dir.iterdir())
            has_chatmodes = chatmodes_dir.exists() and any(chatmodes_dir.iterdir())

            assert has_prompts or has_chatmodes, "Collection should have downloaded some files"

    def test_collection_manifest_parsing(self):
        """Test parsing a collection manifest."""
        from apm_cli.deps.collection_parser import parse_collection_yml

        manifest_yaml = b"""
id: test-collection
name: Test Collection
description: A test collection for unit testing
tags: [testing, example]
items:
  - path: prompts/test-prompt.prompt.md
    kind: prompt
  - path: instructions/test-instruction.instructions.md
    kind: instruction
  - path: chatmodes/test-mode.chatmode.md
    kind: chat-mode
display:
  ordering: alpha
  show_badge: true
"""

        manifest = parse_collection_yml(manifest_yaml)

        assert manifest.id == "test-collection"
        assert manifest.name == "Test Collection"
        assert manifest.description == "A test collection for unit testing"
        assert len(manifest.items) == 3
        assert manifest.tags == ["testing", "example"]

        # Check item parsing
        assert manifest.items[0].path == "prompts/test-prompt.prompt.md"
        assert manifest.items[0].kind == "prompt"
        assert manifest.items[0].subdirectory == "prompts"

        assert manifest.items[1].kind == "instruction"
        assert manifest.items[1].subdirectory == "instructions"

        assert manifest.items[2].kind == "chat-mode"
        assert manifest.items[2].subdirectory == "chatmodes"

    def test_collection_manifest_validation_missing_fields(self):
        """Test that collection manifest validation catches missing fields."""
        from apm_cli.deps.collection_parser import parse_collection_yml

        # Missing required field 'description'
        invalid_yaml = b"""
id: test
name: Test
items:
  - path: test.prompt.md
    kind: prompt
"""

        with pytest.raises(ValueError, match="missing required fields"):
            parse_collection_yml(invalid_yaml)

    def test_collection_manifest_validation_empty_items(self):
        """Test that collection manifest validation catches empty items."""
        from apm_cli.deps.collection_parser import parse_collection_yml

        # Empty items array
        invalid_yaml = b"""
id: test
name: Test
description: Test collection
items: []
"""

        with pytest.raises(ValueError, match="must contain at least one item"):
            parse_collection_yml(invalid_yaml)

    def test_collection_manifest_validation_invalid_item(self):
        """Test that collection manifest validation catches invalid items."""
        from apm_cli.deps.collection_parser import parse_collection_yml

        # Item missing 'kind' field
        invalid_yaml = b"""
id: test
name: Test
description: Test collection
items:
  - path: test.prompt.md
"""

        with pytest.raises(ValueError, match="missing required field"):
            parse_collection_yml(invalid_yaml)

    def test_parse_collection_with_yml_extension(self):
        """Test parsing a collection dependency with .collection.yml extension.

        Regression test for bug where specifying the full extension caused
        double-extension paths like 'collections/name.collection.yml.collection.yml'.
        """
        dep_ref = DependencyReference.parse(
            "copilot/copilot-primitives/collections/markdown-documentation.collection.yml"
        )

        assert dep_ref.is_virtual is True
        assert dep_ref.is_virtual_collection() is True
        assert dep_ref.repo_url == "copilot/copilot-primitives"
        # virtual_path preserves the extension as specified by user
        assert dep_ref.virtual_path == "collections/markdown-documentation.collection.yml"
        # get_virtual_package_name() should return sanitized name without extension
        assert dep_ref.get_virtual_package_name() == "copilot-primitives-markdown-documentation"

    def test_parse_collection_with_yaml_extension(self):
        """Test parsing a collection dependency with .collection.yaml extension."""
        dep_ref = DependencyReference.parse("owner/repo/collections/my-collection.collection.yaml")

        assert dep_ref.is_virtual is True
        assert dep_ref.is_virtual_collection() is True
        assert dep_ref.repo_url == "owner/repo"
        assert dep_ref.virtual_path == "collections/my-collection.collection.yaml"
        # get_virtual_package_name() should return sanitized name without extension
        assert dep_ref.get_virtual_package_name() == "repo-my-collection"

    def test_collection_manifest_path_normalization(self):
        """Test that normalize_collection_path correctly strips extensions.

        Regression test: when user specifies .collection.yml in their dependency,
        the downloader should NOT append .collection.yml again.
        """
        test_cases = [
            # (virtual_path, expected_normalized_path)
            ("collections/markdown-documentation", "collections/markdown-documentation"),
            (
                "collections/markdown-documentation.collection.yml",
                "collections/markdown-documentation",
            ),
            (
                "collections/markdown-documentation.collection.yaml",
                "collections/markdown-documentation",
            ),
            ("path/to/collections/nested", "path/to/collections/nested"),
            ("path/to/collections/nested.collection.yml", "path/to/collections/nested"),
        ]

        for virtual_path, expected_base in test_cases:
            # Test the actual normalize_collection_path function
            normalized = normalize_collection_path(virtual_path)
            assert normalized == expected_base, (
                f"normalize_collection_path('{virtual_path}'): expected '{expected_base}', got '{normalized}'"
            )

            # Verify that appending extension gives correct manifest path
            manifest_path = f"{normalized}.collection.yml"
            expected_manifest = f"{expected_base}.collection.yml"
            assert manifest_path == expected_manifest
