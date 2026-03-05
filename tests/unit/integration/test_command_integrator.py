"""Unit tests for CommandIntegrator.

Tests cover:
- Command file discovery
- Command integration during install (no metadata injection)
- Command cleanup during uninstall (nuke-and-regenerate via sync_integration)
- Removal of all APM command files
"""

import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock
from dataclasses import dataclass

import pytest
import frontmatter

from apm_cli.integration.command_integrator import CommandIntegrator


class TestCommandIntegratorSyncIntegration:
    """Tests for sync_integration method (nuke-and-regenerate)."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project with .claude/commands directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        # Create commands directory
        commands_dir = temp_path / ".claude" / "commands"
        commands_dir.mkdir(parents=True)

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_sync_removes_all_apm_commands(self, temp_project):
        """Test that sync_integration removes all *-apm.md files."""
        commands_dir = temp_project / ".claude" / "commands"

        # Create command files for two packages
        pkg1_command = commands_dir / "audit-apm.md"
        pkg1_command.write_text("# Audit Command\n")

        pkg2_command = commands_dir / "review-apm.md"
        pkg2_command.write_text("# Review Command\n")

        integrator = CommandIntegrator()
        result = integrator.sync_integration(None, temp_project)

        assert result["files_removed"] == 2
        assert not pkg1_command.exists()
        assert not pkg2_command.exists()

    def test_sync_handles_empty_dependencies(self, temp_project):
        """Test sync removes all apm commands regardless of dependencies."""
        commands_dir = temp_project / ".claude" / "commands"

        command1 = commands_dir / "cmd1-apm.md"
        command1.write_text("# Command 1\n")

        command2 = commands_dir / "cmd2-apm.md"
        command2.write_text("# Command 2\n")

        mock_package = MagicMock()
        mock_package.dependencies = {"apm": []}

        integrator = CommandIntegrator()
        result = integrator.sync_integration(mock_package, temp_project)

        assert result["files_removed"] == 2
        assert not command1.exists()
        assert not command2.exists()

    def test_sync_ignores_non_apm_command_files(self, temp_project):
        """Test that sync_integration ignores command files without -apm suffix."""
        commands_dir = temp_project / ".claude" / "commands"

        # Create a non-APM command file (user-created)
        user_command = commands_dir / "my-custom-command.md"
        user_command.write_text("# My Custom Command\n")

        integrator = CommandIntegrator()
        result = integrator.sync_integration(None, temp_project)

        assert result["files_removed"] == 0
        assert user_command.exists()

    def test_sync_handles_nonexistent_commands_dir(self):
        """Test sync handles missing .claude/commands directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        try:
            integrator = CommandIntegrator()
            result = integrator.sync_integration(None, temp_path)
            assert result["files_removed"] == 0
            assert result["errors"] == 0
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_sync_removes_opencode_apm_commands(self):
        """Test that sync removes *-apm.md from .opencode/commands too."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        try:
            commands_dir = temp_path / ".opencode" / "commands"
            commands_dir.mkdir(parents=True)

            cmd = commands_dir / "review-apm.md"
            cmd.write_text("# Review command\n")

            integrator = CommandIntegrator()
            result = integrator.sync_integration(None, temp_path)

            assert result["files_removed"] == 1
            assert not cmd.exists()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_sync_apm_package_param_is_unused(self, temp_project):
        """Test that sync works regardless of what apm_package is passed."""
        commands_dir = temp_project / ".claude" / "commands"

        cmd = commands_dir / "test-apm.md"
        cmd.write_text("# Test\n")

        integrator = CommandIntegrator()

        # Works with None
        result = integrator.sync_integration(None, temp_project)
        assert result["files_removed"] == 1


class TestRemovePackageCommands:
    """Tests for remove_package_commands method."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project with .claude/commands directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        commands_dir = temp_path / ".claude" / "commands"
        commands_dir.mkdir(parents=True)

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_removes_all_apm_commands(self, temp_project):
        """Test that remove_package_commands removes all *-apm.md files."""
        commands_dir = temp_project / ".claude" / "commands"

        cmd1 = commands_dir / "audit-apm.md"
        cmd1.write_text("# Audit\n")

        cmd2 = commands_dir / "review-apm.md"
        cmd2.write_text("# Review\n")

        cmd3 = commands_dir / "design-apm.md"
        cmd3.write_text("# Design\n")

        integrator = CommandIntegrator()
        removed = integrator.remove_package_commands("any/package", temp_project)

        assert removed == 3
        assert not cmd1.exists()
        assert not cmd2.exists()
        assert not cmd3.exists()

    def test_returns_zero_when_no_commands_dir(self, temp_project):
        """Test that remove_package_commands returns 0 when no commands directory exists."""
        shutil.rmtree(temp_project / ".claude" / "commands")

        integrator = CommandIntegrator()
        removed = integrator.remove_package_commands("any/package", temp_project)

        assert removed == 0

    def test_preserves_non_apm_files(self, temp_project):
        """Test that non-APM files are preserved."""
        commands_dir = temp_project / ".claude" / "commands"

        user_cmd = commands_dir / "my-command.md"
        user_cmd.write_text("# User command\n")

        apm_cmd = commands_dir / "test-apm.md"
        apm_cmd.write_text("# APM command\n")

        integrator = CommandIntegrator()
        removed = integrator.remove_package_commands("any/package", temp_project)

        assert removed == 1
        assert not apm_cmd.exists()
        assert user_cmd.exists()


class TestIntegrateCommandNoMetadata:
    """Tests that integrate_command does NOT inject APM metadata."""

    @pytest.fixture
    def temp_project(self):
        """Create temporary project with source and target dirs."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        (temp_path / "source").mkdir()
        (temp_path / ".claude" / "commands").mkdir(parents=True)

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_no_apm_metadata_in_output(self, temp_project):
        """Test that integrated command files contain no APM metadata block."""
        source = temp_project / "source" / "audit.prompt.md"
        source.write_text("""---
description: Run audit checks
---
# Audit Command
Run compliance audit.
""")

        target = temp_project / ".claude" / "commands" / "audit-apm.md"

        mock_info = MagicMock()
        mock_info.package.name = "test/pkg"
        mock_info.package.version = "1.0.0"
        mock_info.package.source = "https://github.com/test/pkg"
        mock_info.resolved_reference = None
        mock_info.install_path = temp_project / "source"
        mock_info.installed_at = "2024-01-01"
        mock_info.get_canonical_dependency_string.return_value = "test/pkg"

        integrator = CommandIntegrator()
        integrator.integrate_command(source, target, mock_info, source)

        # Verify no APM metadata
        post = frontmatter.load(target)
        assert "apm" not in post.metadata

        # Verify legitimate metadata IS preserved
        assert post.metadata.get("description") == "Run audit checks"

    def test_content_preserved_verbatim(self, temp_project):
        """Test that command content is preserved without modification."""
        content = "# My Command\nDo something useful.\n\n## Steps\n1. First\n2. Second"
        source = temp_project / "source" / "test.prompt.md"
        source.write_text(f"---\ndescription: Test\n---\n{content}\n")

        target = temp_project / ".claude" / "commands" / "test-apm.md"

        mock_info = MagicMock()
        mock_info.resolved_reference = None

        integrator = CommandIntegrator()
        integrator.integrate_command(source, target, mock_info, source)

        post = frontmatter.load(target)
        assert content in post.content

    def test_claude_metadata_mapping(self, temp_project):
        """Test that Claude-specific frontmatter fields are mapped correctly."""
        source = temp_project / "source" / "cmd.prompt.md"
        source.write_text("""---
description: A command
allowed-tools: ["bash", "edit"]
model: claude-sonnet
argument-hint: "file path"
---
# Command
""")

        target = temp_project / ".claude" / "commands" / "cmd-apm.md"

        mock_info = MagicMock()
        mock_info.resolved_reference = None

        integrator = CommandIntegrator()
        integrator.integrate_command(source, target, mock_info, source)

        post = frontmatter.load(target)
        assert post.metadata["description"] == "A command"
        assert post.metadata["allowed-tools"] == ["bash", "edit"]
        assert post.metadata["model"] == "claude-sonnet"
        assert post.metadata["argument-hint"] == "file path"
        assert "apm" not in post.metadata


class TestOpenCodeCommandIntegration:
    """Tests for OpenCode command path integration."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        (temp_path / "source").mkdir()
        (temp_path / ".opencode").mkdir()
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def _mock_package_info(self, temp_project):
        mock_info = MagicMock()
        mock_info.package.name = "test/pkg"
        mock_info.package.version = "1.0.0"
        mock_info.package.source = "https://github.com/test/pkg"
        mock_info.resolved_reference = None
        mock_info.install_path = temp_project / "source"
        mock_info.installed_at = "2024-01-01"
        mock_info.get_canonical_dependency_string.return_value = "test/pkg"
        return mock_info

    def test_integrates_to_opencode_commands_when_opencode_exists(self, temp_project):
        source = temp_project / "source" / "audit.prompt.md"
        source.write_text("---\ndescription: Run audit checks\n---\n# Audit\n")

        integrator = CommandIntegrator()
        result = integrator.integrate_package_commands(
            self._mock_package_info(temp_project), temp_project
        )

        assert result.files_integrated == 1
        target = temp_project / ".opencode" / "commands" / "audit-apm.md"
        assert target.exists()

    def test_does_not_create_opencode_root_if_missing(self, temp_project):
        shutil.rmtree(temp_project / ".opencode")
        source = temp_project / "source" / "audit.prompt.md"
        source.write_text("---\ndescription: Run audit checks\n---\n# Audit\n")

        integrator = CommandIntegrator()
        result = integrator.integrate_package_commands(
            self._mock_package_info(temp_project), temp_project
        )

        assert result.files_integrated == 1
        assert not (temp_project / ".opencode").exists()
        # Falls back to legacy Claude path when no explicit integration root exists
        assert (temp_project / ".claude" / "commands" / "audit-apm.md").exists()
