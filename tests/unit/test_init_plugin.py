"""Tests for apm init --plugin flag.

Focused test suite for the plugin author initialization workflow.
Complements the broader ``TestInitCommand`` tests in ``test_init_command.py``.
"""

import json
import os
import tempfile
from pathlib import Path

import pytest  # noqa: F401
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.commands._helpers import _validate_plugin_name

# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestInitPlugin:
    """Tests for apm init --plugin."""

    def setup_method(self):
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self):
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            repo_root = Path(__file__).parent.parent.parent
            os.chdir(str(repo_root))

    def test_plugin_creates_two_files(self):
        """--plugin creates exactly plugin.json and apm.yml."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "my-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                result = self.runner.invoke(cli, ["init", "--plugin", "--yes"])

                assert result.exit_code == 0, result.output
                created = {e.name for e in project_dir.iterdir()}
                assert "apm.yml" in created
                assert "plugin.json" in created
                # Only these two files, nothing else
                assert created == {"apm.yml", "plugin.json"}
            finally:
                os.chdir(self.original_dir)

    def test_plugin_json_structure(self):
        """plugin.json has required fields: name, version, description, author, license."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "my-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                result = self.runner.invoke(cli, ["init", "--plugin", "--yes"])
                assert result.exit_code == 0, result.output

                with open("plugin.json", encoding="utf-8") as f:
                    data = json.load(f)

                assert data["name"] == "my-plugin"
                assert "version" in data
                assert isinstance(data["description"], str)
                assert isinstance(data["author"], dict)
                assert "name" in data["author"]
                assert data["license"] == "MIT"
            finally:
                os.chdir(self.original_dir)

    def test_apm_yml_has_dev_dependencies(self):
        """apm.yml includes devDependencies section when --plugin."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "my-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                result = self.runner.invoke(cli, ["init", "--plugin", "--yes"])
                assert result.exit_code == 0, result.output

                with open("apm.yml", encoding="utf-8") as f:
                    config = yaml.safe_load(f)

                assert "devDependencies" in config
                assert config["devDependencies"] == {"apm": []}
            finally:
                os.chdir(self.original_dir)

    def test_no_skill_md_created(self):
        """SKILL.md is NOT created (not mandatory per spec)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "my-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                self.runner.invoke(cli, ["init", "--plugin", "--yes"])
                assert not Path("SKILL.md").exists()
            finally:
                os.chdir(self.original_dir)

    def test_no_empty_directories_created(self):
        """No empty agents/, skills/ dirs (only files)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "my-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                self.runner.invoke(cli, ["init", "--plugin", "--yes"])

                entries = list(project_dir.iterdir())
                dirs = [e for e in entries if e.is_dir()]
                assert dirs == [], f"Unexpected directories: {dirs}"
            finally:
                os.chdir(self.original_dir)

    def test_name_validation_rejects_uppercase(self):
        """Plugin names must be lowercase kebab-case."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "MyPlugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                result = self.runner.invoke(cli, ["init", "--plugin", "--yes"])
                assert result.exit_code != 0
                assert "Invalid plugin name" in result.output
            finally:
                os.chdir(self.original_dir)

    def test_name_validation_rejects_too_long(self):
        """Plugin names max 64 chars."""
        assert _validate_plugin_name("a" * 65) is False
        assert _validate_plugin_name("a" * 64) is True

    def test_name_validation_accepts_valid(self):
        """Valid kebab-case names pass."""
        assert _validate_plugin_name("my-plugin") is True
        assert _validate_plugin_name("plugin2") is True
        assert _validate_plugin_name("a") is True
        assert _validate_plugin_name("cool-plugin-v3") is True

    def test_name_validation_rejects_underscores(self):
        """Underscores are not valid in plugin names."""
        assert _validate_plugin_name("my_plugin") is False

    def test_name_validation_rejects_start_with_number(self):
        """Names starting with numbers are invalid."""
        assert _validate_plugin_name("1plugin") is False

    def test_name_validation_rejects_start_with_hyphen(self):
        """Names starting with hyphens are invalid."""
        assert _validate_plugin_name("-plugin") is False

    def test_name_validation_rejects_empty(self):
        """Empty name is invalid."""
        assert _validate_plugin_name("") is False

    def test_yes_mode_works_with_plugin(self):
        """--yes and --plugin together work without interaction."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "auto-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                result = self.runner.invoke(cli, ["init", "--plugin", "--yes"])

                assert result.exit_code == 0, result.output
                assert Path("apm.yml").exists()
                assert Path("plugin.json").exists()

                with open("apm.yml", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                # --yes + --plugin uses 0.1.0 version
                assert config["version"] == "0.1.0"
            finally:
                os.chdir(self.original_dir)

    def test_plugin_flag_without_plugin(self):
        """Regular init (no --plugin) still works unchanged."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                result = self.runner.invoke(cli, ["init", "--yes"])

                assert result.exit_code == 0, result.output
                assert Path("apm.yml").exists()
                assert not Path("plugin.json").exists()

                with open("apm.yml", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                assert "devDependencies" not in config
            finally:
                os.chdir(self.original_dir)

    def test_plugin_version_defaults_to_0_1_0(self):
        """--plugin --yes defaults version to 0.1.0 (not 1.0.0)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "my-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                self.runner.invoke(cli, ["init", "--plugin", "--yes"])

                with open("plugin.json", encoding="utf-8") as f:
                    data = json.load(f)
                assert data["version"] == "0.1.0"

                with open("apm.yml", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                assert config["version"] == "0.1.0"
            finally:
                os.chdir(self.original_dir)

    def test_plugin_author_is_object(self):
        """Author in plugin.json is an object with 'name' key."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "my-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                self.runner.invoke(cli, ["init", "--plugin", "--yes"])

                with open("plugin.json", encoding="utf-8") as f:
                    data = json.load(f)
                assert isinstance(data["author"], dict)
                assert "name" in data["author"]
            finally:
                os.chdir(self.original_dir)

    def test_plugin_shows_next_steps(self):
        """Plugin init shows plugin-specific next steps."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "my-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                result = self.runner.invoke(cli, ["init", "--plugin", "--yes"])
                assert result.exit_code == 0, result.output
                assert "apm pack" in result.output
            finally:
                os.chdir(self.original_dir)

    def test_plugin_with_project_name_argument(self):
        """--plugin with explicit project_name creates directory."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                result = self.runner.invoke(cli, ["init", "cool-plugin", "--plugin", "--yes"])
                assert result.exit_code == 0, result.output

                project_path = Path(tmp_dir) / "cool-plugin"
                assert (project_path / "apm.yml").exists()
                assert (project_path / "plugin.json").exists()

                with open(project_path / "plugin.json", encoding="utf-8") as f:
                    data = json.load(f)
                assert data["name"] == "cool-plugin"
            finally:
                os.chdir(self.original_dir)

    def test_plugin_json_ends_with_newline(self):
        """plugin.json ends with a trailing newline."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "my-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                self.runner.invoke(cli, ["init", "--plugin", "--yes"])
                raw = Path("plugin.json").read_text()
                assert raw.endswith("\n")
            finally:
                os.chdir(self.original_dir)

    def test_plugin_does_not_create_start_prompt(self):
        """start.prompt.md is NOT created in plugin mode."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "my-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                result = self.runner.invoke(cli, ["init", "--plugin", "--yes"])
                assert result.exit_code == 0, result.output
                assert not Path("start.prompt.md").exists()
            finally:
                os.chdir(self.original_dir)

    def test_plugin_apm_yml_has_dependencies(self):
        """apm.yml created with --plugin still has regular dependencies section."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "my-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                self.runner.invoke(cli, ["init", "--plugin", "--yes"])

                with open("apm.yml", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                assert "dependencies" in config
                assert config["dependencies"] == {"apm": [], "mcp": []}
            finally:
                os.chdir(self.original_dir)
