"""Tests for the apm install command auto-bootstrap feature."""

import os
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


class TestInstallCommandAutoBootstrap:
    """Test cases for apm install command auto-bootstrap feature."""

    def setup_method(self):
        """Set up test fixtures."""
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self):
        """Clean up after tests."""
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            repo_root = Path(__file__).parent.parent.parent
            os.chdir(str(repo_root))

    def test_install_no_apm_yml_no_packages_shows_helpful_error(self):
        """Test that install without apm.yml and without packages shows helpful error."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)

            result = self.runner.invoke(cli, ["install"])

            assert result.exit_code == 1
            assert "No apm.yml found" in result.output
            assert "apm init" in result.output
            clean_output = _ANSI_ESCAPE.sub("", result.output)
            assert "apm install <org/repo>" in clean_output

    @patch("apm_cli.cli._validate_package_exists")
    @patch("apm_cli.cli.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.cli.APMPackage")
    @patch("apm_cli.cli._install_apm_dependencies")
    def test_install_no_apm_yml_with_packages_creates_minimal_apm_yml(
        self, mock_install_apm, mock_apm_package, mock_validate, monkeypatch
    ):
        """Test that install with packages but no apm.yml creates minimal apm.yml."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)

            # Mock package validation to return True
            mock_validate.return_value = True

            # Mock APMPackage to return empty dependencies
            mock_pkg_instance = MagicMock()
            mock_pkg_instance.get_apm_dependencies.return_value = [
                MagicMock(repo_url="test/package", reference="main")
            ]
            mock_pkg_instance.get_mcp_dependencies.return_value = []
            mock_apm_package.from_apm_yml.return_value = mock_pkg_instance

            # Mock the install function to avoid actual installation
            mock_install_apm.return_value = (
                0,
                0,
                0,
            )  # Return tuple (installed_count, prompts_integrated, agents_integrated)

            result = self.runner.invoke(cli, ["install", "test/package"])

            # Should succeed and create apm.yml
            assert result.exit_code == 0
            assert "Created apm.yml" in result.output
            assert Path("apm.yml").exists()

            # Verify apm.yml structure
            with open("apm.yml") as f:
                config = yaml.safe_load(f)
                assert "dependencies" in config
                assert "apm" in config["dependencies"]
                assert "test/package" in config["dependencies"]["apm"]
                assert config["dependencies"]["mcp"] == []

    @patch("apm_cli.cli._validate_package_exists")
    @patch("apm_cli.cli.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.cli.APMPackage")
    @patch("apm_cli.cli._install_apm_dependencies")
    def test_install_no_apm_yml_with_multiple_packages(
        self, mock_install_apm, mock_apm_package, mock_validate, monkeypatch
    ):
        """Test that install with multiple packages creates apm.yml and adds all."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)

            # Mock package validation
            mock_validate.return_value = True

            # Mock APMPackage
            mock_pkg_instance = MagicMock()
            mock_pkg_instance.get_apm_dependencies.return_value = [
                MagicMock(repo_url="org1/pkg1", reference="main"),
                MagicMock(repo_url="org2/pkg2", reference="main"),
            ]
            mock_pkg_instance.get_mcp_dependencies.return_value = []
            mock_apm_package.from_apm_yml.return_value = mock_pkg_instance

            mock_install_apm.return_value = (
                0,
                0,
                0,
            )  # Return tuple (installed_count, prompts_integrated, agents_integrated)

            result = self.runner.invoke(cli, ["install", "org1/pkg1", "org2/pkg2"])

            assert result.exit_code == 0
            assert "Created apm.yml" in result.output
            assert Path("apm.yml").exists()

            # Verify both packages are in apm.yml
            with open("apm.yml") as f:
                config = yaml.safe_load(f)
                assert "org1/pkg1" in config["dependencies"]["apm"]
                assert "org2/pkg2" in config["dependencies"]["apm"]

    @patch("apm_cli.cli.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.cli.APMPackage")
    @patch("apm_cli.cli._install_apm_dependencies")
    def test_install_existing_apm_yml_preserves_behavior(
        self, mock_install_apm, mock_apm_package
    ):
        """Test that install with existing apm.yml works as before."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)

            # Create existing apm.yml
            existing_config = {
                "name": "test-project",
                "version": "1.0.0",
                "description": "Test project",
                "author": "Test Author",
                "dependencies": {"apm": [], "mcp": []},
                "scripts": {},
            }
            with open("apm.yml", "w") as f:
                yaml.dump(existing_config, f)

            # Mock APMPackage
            mock_pkg_instance = MagicMock()
            mock_pkg_instance.get_apm_dependencies.return_value = []
            mock_pkg_instance.get_mcp_dependencies.return_value = []
            mock_apm_package.from_apm_yml.return_value = mock_pkg_instance

            mock_install_apm.return_value = (
                0,
                0,
                0,
            )  # Return tuple (installed_count, prompts_integrated, agents_integrated)

            result = self.runner.invoke(cli, ["install"])

            # Should succeed and NOT show "Created apm.yml"
            assert result.exit_code == 0
            assert "Created apm.yml" not in result.output

            # Verify original config is preserved
            with open("apm.yml") as f:
                config = yaml.safe_load(f)
                assert config["name"] == "test-project"
                assert config["author"] == "Test Author"

    @patch("apm_cli.cli._validate_package_exists")
    @patch("apm_cli.cli.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.cli.APMPackage")
    @patch("apm_cli.cli._install_apm_dependencies")
    def test_install_auto_created_apm_yml_has_correct_metadata(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """Test that auto-created apm.yml has correct metadata."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create a directory with a specific name to test project name detection
            project_dir = Path(tmp_dir) / "my-awesome-project"
            project_dir.mkdir()
            os.chdir(project_dir)

            # Mock validation and installation
            mock_validate.return_value = True

            mock_pkg_instance = MagicMock()
            mock_pkg_instance.get_apm_dependencies.return_value = [
                MagicMock(repo_url="test/package", reference="main")
            ]
            mock_pkg_instance.get_mcp_dependencies.return_value = []
            mock_apm_package.from_apm_yml.return_value = mock_pkg_instance

            mock_install_apm.return_value = (
                0,
                0,
                0,
            )  # Return tuple (installed_count, prompts_integrated, agents_integrated)

            result = self.runner.invoke(cli, ["install", "test/package"])

            assert result.exit_code == 0
            assert Path("apm.yml").exists()

            # Verify auto-detected project name
            with open("apm.yml") as f:
                config = yaml.safe_load(f)
                assert config["name"] == "my-awesome-project"
                assert "version" in config
                assert "description" in config
                assert "APM project" in config["description"]

    @patch("apm_cli.cli._validate_package_exists")
    def test_install_invalid_package_format_with_no_apm_yml(self, mock_validate):
        """Test that invalid package format fails gracefully even with auto-bootstrap."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)

            # Don't mock validation - let it handle invalid format
            result = self.runner.invoke(cli, ["install", "invalid-package"])

            # Should create apm.yml but fail to add invalid package
            assert Path("apm.yml").exists()
            assert "Invalid package format" in result.output

    @patch("apm_cli.cli._validate_package_exists")
    @patch("apm_cli.cli.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.cli.APMPackage")
    @patch("apm_cli.cli._install_apm_dependencies")
    def test_install_dry_run_with_no_apm_yml_shows_what_would_be_created(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """Test that dry-run with no apm.yml shows what would be created."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)

            mock_validate.return_value = True

            mock_pkg_instance = MagicMock()
            mock_pkg_instance.get_apm_dependencies.return_value = []
            mock_pkg_instance.get_mcp_dependencies.return_value = []
            mock_apm_package.from_apm_yml.return_value = mock_pkg_instance

            result = self.runner.invoke(cli, ["install", "test/package", "--dry-run"])

            # Should show what would be added
            assert result.exit_code == 0
            assert "Would add" in result.output or "Dry run" in result.output
            # apm.yml should still be created (for dry-run to work)
            assert Path("apm.yml").exists()
