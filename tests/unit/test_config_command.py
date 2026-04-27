"""Tests for the apm config command."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.config import config


class TestConfigShow:
    """Tests for `apm config` (show current configuration)."""

    def setup_method(self):
        self.runner = CliRunner()
        self.original_dir = os.getcwd()

    def teardown_method(self):
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            pass

    def test_config_show_outside_project(self):
        """Show config when not in an APM project directory."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                with patch("apm_cli.commands.config.get_version", return_value="1.2.3"):
                    result = self.runner.invoke(config, [])
            finally:
                os.chdir(self.original_dir)
        assert result.exit_code == 0

    def test_config_show_inside_project(self):
        """Show config when apm.yml is present."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                apm_yml = Path(tmp_dir) / "apm.yml"
                apm_yml.write_text("name: myproject\nversion: '0.1'\n")
                with (
                    patch("apm_cli.commands.config.get_version", return_value="1.2.3"),
                    patch(
                        "apm_cli.commands.config._load_apm_config",
                        return_value={
                            "name": "myproject",
                            "version": "0.1",
                            "entrypoint": "main.md",
                        },
                    ),
                ):
                    result = self.runner.invoke(config, [])
            finally:
                os.chdir(self.original_dir)
        assert result.exit_code == 0

    def test_config_show_inside_project_with_compilation(self):
        """Show config when apm.yml has compilation settings."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                apm_yml = Path(tmp_dir) / "apm.yml"
                apm_yml.write_text("name: myproject\ncompilation:\n  output: AGENTS.md\n")
                apm_config = {
                    "name": "myproject",
                    "version": "0.1",
                    "compilation": {
                        "output": "AGENTS.md",
                        "chatmode": "copilot",
                        "resolve_links": False,
                    },
                    "dependencies": {"mcp": ["server1", "server2"]},
                }
                with (
                    patch("apm_cli.commands.config.get_version", return_value="1.2.3"),
                    patch(
                        "apm_cli.commands.config._load_apm_config", return_value=apm_config
                    ),
                ):
                    result = self.runner.invoke(config, [])
            finally:
                os.chdir(self.original_dir)
        assert result.exit_code == 0

    def test_config_show_rich_import_error_fallback(self):
        """Fallback plain-text display when Rich (rich.table.Table) is unavailable."""
        import rich.table

        mock_table_cls = MagicMock(side_effect=ImportError("no rich"))
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                with (
                    patch("apm_cli.commands.config.get_version", return_value="0.9.0"),
                    patch.object(rich.table, "Table", side_effect=ImportError("no rich")),
                ):
                    result = self.runner.invoke(config, [])
            finally:
                os.chdir(self.original_dir)
        assert result.exit_code == 0

    def test_config_show_fallback_inside_project(self):
        """Fallback display inside a project directory when console/table unavailable."""
        import rich.table

        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                apm_yml = Path(tmp_dir) / "apm.yml"
                apm_yml.write_text("name: proj\n")
                apm_config = {
                    "name": "proj",
                    "version": "1.0",
                    "entrypoint": None,
                    "dependencies": {"mcp": []},
                }
                with (
                    patch("apm_cli.commands.config.get_version", return_value="0.9.0"),
                    patch(
                        "apm_cli.commands.config._load_apm_config", return_value=apm_config
                    ),
                    patch.object(rich.table, "Table", side_effect=ImportError("no rich")),
                ):
                    result = self.runner.invoke(config, [])
            finally:
                os.chdir(self.original_dir)
        assert result.exit_code == 0

    def test_config_show_displays_temp_dir_in_global_section(self):
        """Fallback display includes Temp Directory row when temp-dir is configured."""
        import rich.table

        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                with (
                    patch("apm_cli.commands.config.get_version", return_value="1.2.3"),
                    patch("apm_cli.config.get_temp_dir", return_value="/custom/tmp"),
                    patch.object(rich.table, "Table", side_effect=ImportError("no rich")),
                ):
                    result = self.runner.invoke(config, [])
            finally:
                os.chdir(self.original_dir)
        assert result.exit_code == 0
        assert "Temp Directory: /custom/tmp" in result.output

    def test_config_show_omits_temp_dir_when_not_configured(self):
        """Fallback display omits Temp Directory row when temp-dir is not configured."""
        import rich.table

        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                with (
                    patch("apm_cli.commands.config.get_version", return_value="1.2.3"),
                    patch("apm_cli.config.get_temp_dir", return_value=None),
                    patch.object(rich.table, "Table", side_effect=ImportError("no rich")),
                ):
                    result = self.runner.invoke(config, [])
            finally:
                os.chdir(self.original_dir)
        assert result.exit_code == 0
        assert "Temp Directory" not in result.output


class TestConfigSet:
    """Tests for `apm config set <key> <value>`."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_set_auto_integrate_true(self):
        """Enable auto-integration."""
        with patch("apm_cli.config.set_auto_integrate") as mock_set:
            result = self.runner.invoke(config, ["set", "auto-integrate", "true"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(True)

    def test_set_auto_integrate_yes(self):
        """Enable auto-integration with 'yes' alias."""
        with patch("apm_cli.config.set_auto_integrate") as mock_set:
            result = self.runner.invoke(config, ["set", "auto-integrate", "yes"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(True)

    def test_set_auto_integrate_one(self):
        """Enable auto-integration with '1' alias."""
        with patch("apm_cli.config.set_auto_integrate") as mock_set:
            result = self.runner.invoke(config, ["set", "auto-integrate", "1"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(True)

    def test_set_auto_integrate_false(self):
        """Disable auto-integration."""
        with patch("apm_cli.config.set_auto_integrate") as mock_set:
            result = self.runner.invoke(config, ["set", "auto-integrate", "false"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(False)

    def test_set_auto_integrate_no(self):
        """Disable auto-integration with 'no' alias."""
        with patch("apm_cli.config.set_auto_integrate") as mock_set:
            result = self.runner.invoke(config, ["set", "auto-integrate", "no"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(False)

    def test_set_auto_integrate_zero(self):
        """Disable auto-integration with '0' alias."""
        with patch("apm_cli.config.set_auto_integrate") as mock_set:
            result = self.runner.invoke(config, ["set", "auto-integrate", "0"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(False)

    def test_set_auto_integrate_invalid_value(self):
        """Reject an invalid value for auto-integrate."""
        result = self.runner.invoke(config, ["set", "auto-integrate", "maybe"])
        assert result.exit_code == 1

    def test_set_unknown_key(self):
        """Reject an unknown configuration key."""
        result = self.runner.invoke(config, ["set", "nonexistent", "value"])
        assert result.exit_code == 1

    def test_set_auto_integrate_case_insensitive(self):
        """Value comparison is case-insensitive."""
        with patch("apm_cli.config.set_auto_integrate") as mock_set:
            result = self.runner.invoke(config, ["set", "auto-integrate", "TRUE"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(True)

class TestConfigGet:
    """Tests for `apm config get [key]`."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_get_auto_integrate(self):
        """Get the auto-integrate setting."""
        with patch("apm_cli.config.get_auto_integrate", return_value=True):
            result = self.runner.invoke(config, ["get", "auto-integrate"])
        assert result.exit_code == 0
        assert "auto-integrate: True" in result.output

    def test_get_auto_integrate_disabled(self):
        """Get auto-integrate when disabled."""
        with patch("apm_cli.config.get_auto_integrate", return_value=False):
            result = self.runner.invoke(config, ["get", "auto-integrate"])
        assert result.exit_code == 0
        assert "auto-integrate: False" in result.output

    def test_get_unknown_key(self):
        """Reject an unknown key."""
        result = self.runner.invoke(config, ["get", "nonexistent"])
        assert result.exit_code == 1

    def test_get_all_config(self):
        """Show all config when no key is provided."""
        with patch("apm_cli.config.get_auto_integrate", return_value=True):
            result = self.runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "auto-integrate: True" in result.output
        # Internal keys must not appear - users cannot set them via apm config set
        assert "default_client" not in result.output

    def test_get_all_config_fresh_install(self):
        """auto-integrate is shown even on a fresh install with no key in the file."""
        with patch("apm_cli.config.get_auto_integrate", return_value=True):
            result = self.runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "auto-integrate: True" in result.output


class TestAutoIntegrateFunctions:
    """Tests for get_auto_integrate and set_auto_integrate in apm_cli.config."""

    def test_get_auto_integrate_default(self):
        """Default value is True when not set."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "get_config", return_value={}):
            assert cfg_module.get_auto_integrate() is True

    def test_get_auto_integrate_false(self):
        """Returns False when set to False."""
        import apm_cli.config as cfg_module

        with patch.object(
            cfg_module, "get_config", return_value={"auto_integrate": False}
        ):
            assert cfg_module.get_auto_integrate() is False

    def test_set_auto_integrate_calls_update_config(self):
        """set_auto_integrate delegates to update_config."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "update_config") as mock_update:
            cfg_module.set_auto_integrate(True)
            mock_update.assert_called_once_with({"auto_integrate": True})

    def test_set_auto_integrate_false_calls_update_config(self):
        """set_auto_integrate(False) passes False to update_config."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "update_config") as mock_update:
            cfg_module.set_auto_integrate(False)
            mock_update.assert_called_once_with({"auto_integrate": False})


class TestTempDirFunctions:
    """Tests for get_temp_dir, set_temp_dir, and get_apm_temp_dir in apm_cli.config."""

    def test_get_temp_dir_default_is_none(self):
        """Returns None when temp_dir is not set."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "get_config", return_value={}):
            assert cfg_module.get_temp_dir() is None

    def test_get_temp_dir_returns_stored_value(self):
        """Returns stored temp_dir value."""
        import apm_cli.config as cfg_module

        with patch.object(
            cfg_module, "get_config", return_value={"temp_dir": "/custom/tmp"}
        ):
            assert cfg_module.get_temp_dir() == "/custom/tmp"

    def test_set_temp_dir_validates_and_stores(self):
        """set_temp_dir normalises path and stores via update_config."""
        import apm_cli.config as cfg_module

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cfg_module, "update_config") as mock_update:
                cfg_module.set_temp_dir(tmp)
                resolved = os.path.abspath(os.path.expanduser(tmp))
                mock_update.assert_called_once_with({"temp_dir": resolved})

    def test_set_temp_dir_rejects_nonexistent_directory(self):
        """Raises ValueError when path does not exist."""
        import apm_cli.config as cfg_module

        with pytest.raises(ValueError, match="does not exist"):
            cfg_module.set_temp_dir("/nonexistent/path/xyz")

    def test_set_temp_dir_rejects_file_path(self):
        """Raises ValueError when path is a file, not a directory."""
        import apm_cli.config as cfg_module

        with tempfile.NamedTemporaryFile() as f:
            with pytest.raises(ValueError, match="not a directory"):
                cfg_module.set_temp_dir(f.name)

    def test_set_temp_dir_normalises_home_path(self):
        """Tilde paths are expanded before storage."""
        import apm_cli.config as cfg_module

        home = os.path.expanduser("~")
        with patch.object(cfg_module, "update_config") as mock_update:
            cfg_module.set_temp_dir("~")
            mock_update.assert_called_once_with({"temp_dir": home})

    def test_get_apm_temp_dir_prefers_env(self):
        """Env var takes precedence over config value."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_temp_dir", return_value="/from/config"),
            patch.dict(os.environ, {"APM_TEMP_DIR": "/from/env"}),
        ):
            assert cfg_module.get_apm_temp_dir() == "/from/env"

    def test_get_apm_temp_dir_falls_back_to_config(self):
        """Falls back to config when env var is not set."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_temp_dir", return_value="/from/config"),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_TEMP_DIR", None)
            assert cfg_module.get_apm_temp_dir() == "/from/config"

    def test_get_apm_temp_dir_returns_none_when_unset(self):
        """Returns None when neither config nor env var is set."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_temp_dir", return_value=None),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_TEMP_DIR", None)
            assert cfg_module.get_apm_temp_dir() is None

    def test_get_apm_temp_dir_ignores_empty_env(self):
        """Empty APM_TEMP_DIR is treated as unset."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_temp_dir", return_value="/from/config"),
            patch.dict(os.environ, {"APM_TEMP_DIR": ""}),
        ):
            assert cfg_module.get_apm_temp_dir() == "/from/config"

    def test_get_apm_temp_dir_ignores_whitespace_env(self):
        """Whitespace-only APM_TEMP_DIR is treated as unset."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_temp_dir", return_value=None),
            patch.dict(os.environ, {"APM_TEMP_DIR": "   "}),
        ):
            assert cfg_module.get_apm_temp_dir() is None

    def test_get_apm_temp_dir_ignores_empty_config(self):
        """Empty config temp_dir is treated as unset."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_temp_dir", return_value=""),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_TEMP_DIR", None)
            assert cfg_module.get_apm_temp_dir() is None


class TestConfigSetTempDir:
    """Tests for `apm config set temp-dir <path>`."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_set_temp_dir_success(self):
        """Set a valid temp-dir."""
        with patch("apm_cli.config.set_temp_dir") as mock_set:
            result = self.runner.invoke(config, ["set", "temp-dir", "/tmp/apm"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with("/tmp/apm")

    def test_set_temp_dir_validation_error(self):
        """Exit 1 when set_temp_dir raises ValueError."""
        with patch(
            "apm_cli.config.set_temp_dir",
            side_effect=ValueError("Directory does not exist: /bad"),
        ):
            result = self.runner.invoke(config, ["set", "temp-dir", "/bad"])
        assert result.exit_code == 1

    def test_set_unknown_key_includes_temp_dir_in_valid_keys(self):
        """Error message lists temp-dir as a valid key."""
        result = self.runner.invoke(config, ["set", "nonexistent", "value"])
        assert result.exit_code == 1
        assert "temp-dir" in result.output


class TestConfigGetTempDir:
    """Tests for `apm config get temp-dir`."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_get_temp_dir_when_set(self):
        """Display the configured temp-dir."""
        with patch("apm_cli.config.get_temp_dir", return_value="/custom/tmp"):
            result = self.runner.invoke(config, ["get", "temp-dir"])
        assert result.exit_code == 0
        assert "temp-dir: /custom/tmp" in result.output

    def test_get_temp_dir_when_unset(self):
        """Display fallback message when temp-dir is not configured."""
        with patch("apm_cli.config.get_temp_dir", return_value=None):
            result = self.runner.invoke(config, ["get", "temp-dir"])
        assert result.exit_code == 0
        assert "Not set (using system default)" in result.output

    def test_get_unknown_key_includes_temp_dir_in_valid_keys(self):
        """Error message lists temp-dir as a valid key."""
        result = self.runner.invoke(config, ["get", "nonexistent"])
        assert result.exit_code == 1
        assert "temp-dir" in result.output

    def test_get_all_config_maps_temp_dir_key(self):
        """All-config listing maps internal temp_dir to display temp-dir."""
        fake_config = {
            "auto_integrate": True,
            "temp_dir": "/my/temp",
        }
        with patch("apm_cli.config.get_config", return_value=fake_config):
            result = self.runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "temp-dir: /my/temp" in result.output


# ---------------------------------------------------------------------------
# Isolation fixture used by storage-layer tests that perform real disk writes.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirect CONFIG_FILE to a temp dir so mutator tests never touch ~/.apm.

    Returns the Path to the config.json file for post-write inspection.
    The cache is invalidated before and after the test body.
    """
    import apm_cli.config as _conf

    _conf._invalidate_config_cache()
    config_dir = tmp_path / ".apm"
    config_file = config_dir / "config.json"
    monkeypatch.setattr(_conf, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(config_file))
    yield config_file
    _conf._invalidate_config_cache()


# ---------------------------------------------------------------------------
# Storage layer -- copilot_cowork_skills_dir
# ---------------------------------------------------------------------------


class TestCoworkSkillsDirFunctions:
    """Tests for get_copilot_cowork_skills_dir, set_copilot_cowork_skills_dir, unset_copilot_cowork_skills_dir."""

    def test_get_copilot_cowork_skills_dir_default_is_none(self):
        """Returns None when copilot_cowork_skills_dir key is absent from the config."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "get_config", return_value={}):
            assert cfg_module.get_copilot_cowork_skills_dir() is None

    def test_get_copilot_cowork_skills_dir_returns_stored_value(self):
        """Returns the stored copilot_cowork_skills_dir value from config."""
        import apm_cli.config as cfg_module

        with patch.object(
            cfg_module,
            "get_config",
            return_value={"copilot_cowork_skills_dir": "/stored/path"},
        ):
            assert cfg_module.get_copilot_cowork_skills_dir() == "/stored/path"

    def test_set_copilot_cowork_skills_dir_stores_absolute_path(self):
        """set_copilot_cowork_skills_dir persists the absolute path via update_config."""
        import apm_cli.config as cfg_module

        raw = "/absolute/skills"
        expected = os.path.normpath(raw)
        with patch.object(cfg_module, "update_config") as mock_update:
            cfg_module.set_copilot_cowork_skills_dir(raw)
            mock_update.assert_called_once_with(
                {"copilot_cowork_skills_dir": expected}
            )

    def test_set_copilot_cowork_skills_dir_expands_tilde_before_storing(self):
        """Tilde in path is expanded to an absolute path before storage."""
        import apm_cli.config as cfg_module

        home = os.path.expanduser("~")
        with patch.object(cfg_module, "update_config") as mock_update:
            cfg_module.set_copilot_cowork_skills_dir("~/myskills")
            expected = os.path.join(home, "myskills")
            mock_update.assert_called_once_with({"copilot_cowork_skills_dir": expected})

    def test_set_copilot_cowork_skills_dir_raises_for_empty_string(self):
        """Raises ValueError when path is an empty string."""
        import apm_cli.config as cfg_module

        with pytest.raises(ValueError):
            cfg_module.set_copilot_cowork_skills_dir("")

    def test_set_copilot_cowork_skills_dir_raises_for_whitespace_only(self):
        """Raises ValueError when path is whitespace only."""
        import apm_cli.config as cfg_module

        with pytest.raises(ValueError):
            cfg_module.set_copilot_cowork_skills_dir("   ")

    def test_set_copilot_cowork_skills_dir_raises_for_relative_path(self):
        """Raises ValueError when path is relative after tilde expansion."""
        import apm_cli.config as cfg_module

        with pytest.raises(ValueError, match="absolute"):
            cfg_module.set_copilot_cowork_skills_dir("relative/path")

    def test_set_copilot_cowork_skills_dir_accepts_nonexistent_absolute_path(self):
        """Non-existent absolute path is accepted; OneDrive may not yet be synced."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "update_config"):
            # Should not raise even when the path does not exist on disk.
            cfg_module.set_copilot_cowork_skills_dir("/nonexistent/absolute/path/xyz")

    def test_unset_copilot_cowork_skills_dir_removes_key(self, isolated_config):
        """unset_copilot_cowork_skills_dir removes the key; subsequent get returns None."""
        import apm_cli.config as cfg_module

        raw = "/absolute/skills/path"
        expected = os.path.normpath(raw)
        cfg_module.set_copilot_cowork_skills_dir(raw)
        assert cfg_module.get_copilot_cowork_skills_dir() == expected

        cfg_module.unset_copilot_cowork_skills_dir()
        assert cfg_module.get_copilot_cowork_skills_dir() is None

    def test_unset_copilot_cowork_skills_dir_noop_when_absent(self, isolated_config):
        """unset_copilot_cowork_skills_dir is a no-op when the key was never set."""
        import apm_cli.config as cfg_module

        # Should not raise even though the key does not exist.
        cfg_module.unset_copilot_cowork_skills_dir()
        assert cfg_module.get_copilot_cowork_skills_dir() is None


# ---------------------------------------------------------------------------
# Storage layer -- unset_temp_dir (new function added in the same commit)
# ---------------------------------------------------------------------------


class TestUnsetTempDir:
    """Tests for the new unset_temp_dir function in apm_cli.config."""

    def test_unset_temp_dir_removes_key(self, isolated_config, tmp_path):
        """unset_temp_dir removes temp_dir; subsequent get_temp_dir returns None."""
        import apm_cli.config as cfg_module

        # Use tmp_path itself as the real temp directory to satisfy set_temp_dir
        # validation (must exist and be writable).
        cfg_module.set_temp_dir(str(tmp_path))
        assert cfg_module.get_temp_dir() == str(tmp_path)

        cfg_module.unset_temp_dir()
        assert cfg_module.get_temp_dir() is None

    def test_unset_temp_dir_noop_when_absent(self, isolated_config):
        """unset_temp_dir is a no-op when temp_dir was never set."""
        import apm_cli.config as cfg_module

        # Should not raise.
        cfg_module.unset_temp_dir()
        assert cfg_module.get_temp_dir() is None


# ---------------------------------------------------------------------------
# CLI -- apm config set copilot-cowork-skills-dir
# ---------------------------------------------------------------------------


class TestConfigSetCoworkSkillsDir:
    """Tests for `apm config set copilot-cowork-skills-dir <value>`."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_set_copilot_cowork_skills_dir_flag_enabled_returns_exit_0(self):
        """Valid absolute path with the cowork flag enabled succeeds."""
        with (
            patch("apm_cli.core.experimental.is_enabled", return_value=True),
            patch("apm_cli.config.set_copilot_cowork_skills_dir") as mock_set,
            patch("apm_cli.config.get_copilot_cowork_skills_dir", return_value="/tmp/foo"),
        ):
            result = self.runner.invoke(
                config, ["set", "copilot-cowork-skills-dir", "/tmp/foo"]
            )
        assert result.exit_code == 0
        mock_set.assert_called_once_with("/tmp/foo")

    def test_set_copilot_cowork_skills_dir_flag_disabled_returns_exit_1(self):
        """Attempting to set copilot-cowork-skills-dir without the cowork flag exits 1."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = self.runner.invoke(
                config, ["set", "copilot-cowork-skills-dir", "/tmp/foo"]
            )
        assert result.exit_code == 1
        # The phrase may be line-wrapped in terminal output; check for the
        # key parts that appear on the same output line.
        assert "experimental" in result.output
        assert "enable copilot-cowork" in result.output

    def test_set_copilot_cowork_skills_dir_relative_path_exits_1(self):
        """Relative path is rejected with exit code 1 and an absolute-path hint."""
        with (
            patch("apm_cli.core.experimental.is_enabled", return_value=True),
            patch(
                "apm_cli.config.set_copilot_cowork_skills_dir",
                side_effect=ValueError("Path must be absolute: relative/path"),
            ),
        ):
            result = self.runner.invoke(
                config, ["set", "copilot-cowork-skills-dir", "relative/path"]
            )
        assert result.exit_code == 1
        assert "absolute" in result.output

    def test_set_copilot_cowork_skills_dir_empty_string_exits_1(self):
        """Empty string is rejected with exit code 1."""
        with (
            patch("apm_cli.core.experimental.is_enabled", return_value=True),
            patch(
                "apm_cli.config.set_copilot_cowork_skills_dir",
                side_effect=ValueError("Path cannot be empty"),
            ),
        ):
            result = self.runner.invoke(config, ["set", "copilot-cowork-skills-dir", ""])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# CLI -- apm config get copilot-cowork-skills-dir
# ---------------------------------------------------------------------------


class TestConfigGetCoworkSkillsDir:
    """Tests for `apm config get copilot-cowork-skills-dir`."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_get_copilot_cowork_skills_dir_displays_stored_value(self):
        """Displays the configured copilot-cowork-skills-dir path."""
        with patch("apm_cli.config.get_copilot_cowork_skills_dir", return_value="/my/skills"):
            result = self.runner.invoke(config, ["get", "copilot-cowork-skills-dir"])
        assert result.exit_code == 0
        assert "/my/skills" in result.output

    def test_get_copilot_cowork_skills_dir_when_unset_shows_not_set(self):
        """Displays a 'Not set' message when copilot-cowork-skills-dir has not been configured."""
        with patch("apm_cli.config.get_copilot_cowork_skills_dir", return_value=None):
            result = self.runner.invoke(config, ["get", "copilot-cowork-skills-dir"])
        assert result.exit_code == 0
        assert "Not set" in result.output

    def test_get_copilot_cowork_skills_dir_requires_no_flag(self):
        """get copilot-cowork-skills-dir does not require the copilot-cowork experimental flag."""
        with patch("apm_cli.config.get_copilot_cowork_skills_dir", return_value=None):
            # No patch on is_enabled -- the real function must not gate the get path.
            result = self.runner.invoke(config, ["get", "copilot-cowork-skills-dir"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# CLI -- apm config unset copilot-cowork-skills-dir / temp-dir
# ---------------------------------------------------------------------------


class TestConfigUnsetSubcommand:
    """Tests for `apm config unset <key>`."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_unset_copilot_cowork_skills_dir_exits_0(self):
        """apm config unset copilot-cowork-skills-dir exits 0 and prints success message."""
        with patch("apm_cli.config.unset_copilot_cowork_skills_dir") as mock_unset:
            result = self.runner.invoke(config, ["unset", "copilot-cowork-skills-dir"])
        assert result.exit_code == 0
        mock_unset.assert_called_once()

    def test_unset_copilot_cowork_skills_dir_idempotent(self):
        """Unsetting an absent copilot-cowork-skills-dir key is safe and exits 0."""
        with patch(
            "apm_cli.config.unset_copilot_cowork_skills_dir"
        ):  # real no-op behaviour tested in storage tests
            result = self.runner.invoke(config, ["unset", "copilot-cowork-skills-dir"])
        assert result.exit_code == 0

    def test_unset_temp_dir_exits_0(self):
        """apm config unset temp-dir exits 0."""
        with patch("apm_cli.config.unset_temp_dir") as mock_unset:
            result = self.runner.invoke(config, ["unset", "temp-dir"])
        assert result.exit_code == 0
        mock_unset.assert_called_once()

    def test_unset_unknown_key_exits_1(self):
        """Unsetting an unknown key exits 1 with an informative error."""
        result = self.runner.invoke(config, ["unset", "unknown-key"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# CLI -- flag-gated listing (apm config and apm config get with no key)
# ---------------------------------------------------------------------------


class TestConfigListingFlagGating:
    """Tests that copilot-cowork-skills-dir appears in listings only when the flag is enabled."""

    def setup_method(self):
        self.runner = CliRunner()
        self.original_dir = os.getcwd()

    def teardown_method(self):
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            pass

    def test_config_get_shows_copilot_cowork_skills_dir_when_flag_enabled(self):
        """apm config get (no key) includes copilot-cowork-skills-dir when the flag is on."""
        fake_config = {"auto_integrate": True}
        with (
            patch("apm_cli.config.get_config", return_value=fake_config),
            patch("apm_cli.core.experimental.is_enabled", return_value=True),
            patch(
                "apm_cli.config.get_copilot_cowork_skills_dir",
                return_value="/enabled/path",
            ),
        ):
            result = self.runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "copilot-cowork-skills-dir" in result.output

    def test_config_get_hides_copilot_cowork_skills_dir_when_flag_disabled(self):
        """apm config get (no key) omits copilot-cowork-skills-dir when the flag is off."""
        fake_config = {"auto_integrate": True}
        with (
            patch("apm_cli.config.get_config", return_value=fake_config),
            patch("apm_cli.core.experimental.is_enabled", return_value=False),
        ):
            result = self.runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "copilot-cowork-skills-dir" not in result.output

    def test_config_show_includes_copilot_cowork_skills_dir_when_flag_enabled(self):
        """apm config (no subcommand) includes copilot-cowork-skills-dir when the flag is on."""
        import rich.table

        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                with (
                    patch("apm_cli.commands.config.get_version", return_value="1.0.0"),
                    patch("apm_cli.config.get_temp_dir", return_value=None),
                    patch("apm_cli.core.experimental.is_enabled", return_value=True),
                    patch(
                        "apm_cli.config.get_copilot_cowork_skills_dir",
                        return_value="/cowork/skills",
                    ),
                    patch.object(
                        rich.table, "Table", side_effect=ImportError("no rich")
                    ),
                ):
                    result = self.runner.invoke(config, [])
            finally:
                os.chdir(self.original_dir)
        assert result.exit_code == 0
        assert "Cowork Skills Dir" in result.output

    def test_config_show_omits_copilot_cowork_skills_dir_when_flag_disabled(self):
        """apm config (no subcommand) omits copilot-cowork-skills-dir when the flag is off."""
        import rich.table

        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                with (
                    patch("apm_cli.commands.config.get_version", return_value="1.0.0"),
                    patch("apm_cli.config.get_temp_dir", return_value=None),
                    patch("apm_cli.core.experimental.is_enabled", return_value=False),
                    patch.object(
                        rich.table, "Table", side_effect=ImportError("no rich")
                    ),
                ):
                    result = self.runner.invoke(config, [])
            finally:
                os.chdir(self.original_dir)
        assert result.exit_code == 0
        assert "Cowork Skills Dir" not in result.output


# ---------------------------------------------------------------------------
# Flag-gating regression -- only copilot-cowork-skills-dir should be gated
# ---------------------------------------------------------------------------


class TestFlagGatingRegression:
    """Regression checks: only copilot-cowork-skills-dir is gated on the copilot-cowork flag."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_auto_integrate_set_is_not_gated(self):
        """apm config set auto-integrate does not require the cowork flag."""
        with patch("apm_cli.config.set_auto_integrate"):
            result = self.runner.invoke(config, ["set", "auto-integrate", "true"])
        assert result.exit_code == 0

    def test_temp_dir_set_is_not_gated(self):
        """apm config set temp-dir does not require the cowork flag."""
        with (
            patch("apm_cli.config.set_temp_dir"),
            patch("apm_cli.config.get_temp_dir", return_value="/tmp/foo"),
        ):
            result = self.runner.invoke(config, ["set", "temp-dir", "/tmp/foo"])
        assert result.exit_code == 0

    def test_copilot_cowork_skills_dir_set_is_gated(self):
        """apm config set copilot-cowork-skills-dir exits 1 when the copilot-cowork flag is off."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = self.runner.invoke(
                config, ["set", "copilot-cowork-skills-dir", "/some/path"]
            )
        assert result.exit_code == 1

