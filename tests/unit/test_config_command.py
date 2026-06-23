"""Tests for the apm config command."""

import os
import sys  # noqa: F401
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
        try:  # noqa: SIM105
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
                    patch("apm_cli.commands.config._load_apm_config", return_value=apm_config),
                ):
                    result = self.runner.invoke(config, [])
            finally:
                os.chdir(self.original_dir)
        assert result.exit_code == 0

    def test_config_show_rich_import_error_fallback(self):
        """Fallback plain-text display when Rich (rich.table.Table) is unavailable."""
        import rich.table

        mock_table_cls = MagicMock(side_effect=ImportError("no rich"))  # noqa: F841
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
                    patch("apm_cli.commands.config._load_apm_config", return_value=apm_config),
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

    def test_set_target(self):
        """Set install default target."""
        with patch("apm_cli.config.set_install_target", return_value="claude") as mock_set:
            result = self.runner.invoke(config, ["set", "target", "claude"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with("claude")

    def test_set_target_invalid_value(self):
        """Reject invalid target value."""
        with patch(
            "apm_cli.config.set_install_target",
            side_effect=ValueError("Invalid target: 'oops' is not a valid target"),
        ):
            result = self.runner.invoke(config, ["set", "target", "oops"])
        assert result.exit_code == 1


class TestConfigGet:
    """Tests for `apm config get [key]`."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_get_auto_integrate(self):
        """Get the auto-integrate setting."""
        with patch("apm_cli.config.get_auto_integrate", return_value=True):
            result = self.runner.invoke(config, ["get", "auto-integrate"])
        assert result.exit_code == 0
        assert "auto-integrate: true" in result.output

    def test_get_auto_integrate_disabled(self):
        """Get auto-integrate when disabled."""
        with patch("apm_cli.config.get_auto_integrate", return_value=False):
            result = self.runner.invoke(config, ["get", "auto-integrate"])
        assert result.exit_code == 0
        assert "auto-integrate: false" in result.output

    def test_get_unknown_key(self):
        """Reject an unknown key."""
        result = self.runner.invoke(config, ["get", "nonexistent"])
        assert result.exit_code == 1

    def test_get_all_config(self):
        """Show all config when no key is provided."""
        with patch("apm_cli.config.get_auto_integrate", return_value=True):
            result = self.runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "auto-integrate: true" in result.output
        # Internal keys must not appear - users cannot set them via apm config set
        assert "default_client" not in result.output

    def test_get_all_config_fresh_install(self):
        """auto-integrate is shown even on a fresh install with no key in the file."""
        with patch("apm_cli.config.get_auto_integrate", return_value=True):
            result = self.runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "auto-integrate: true" in result.output

    def test_get_target_when_set(self):
        """Get configured default install target."""
        with patch("apm_cli.config.get_install_target", return_value="claude"):
            result = self.runner.invoke(config, ["get", "target"])
        assert result.exit_code == 0
        assert "target: claude" in result.output

    def test_get_target_when_unset(self):
        """Show not-set message for default install target."""
        with patch("apm_cli.config.get_install_target", return_value=None):
            result = self.runner.invoke(config, ["get", "target"])
        assert result.exit_code == 0
        assert "target: Not set" in result.output


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

        with patch.object(cfg_module, "get_config", return_value={"auto_integrate": False}):
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

        with patch.object(cfg_module, "get_config", return_value={"temp_dir": "/custom/tmp"}):
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
            mock_update.assert_called_once_with({"copilot_cowork_skills_dir": expected})

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
            result = self.runner.invoke(config, ["set", "copilot-cowork-skills-dir", "/tmp/foo"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with("/tmp/foo")

    def test_set_copilot_cowork_skills_dir_flag_disabled_returns_exit_1(self):
        """Attempting to set copilot-cowork-skills-dir without the cowork flag exits 1."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = self.runner.invoke(config, ["set", "copilot-cowork-skills-dir", "/tmp/foo"])
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

    def test_unset_target_exits_0(self):
        """apm config unset target exits 0."""
        with patch("apm_cli.config.unset_install_target") as mock_unset:
            result = self.runner.invoke(config, ["unset", "target"])
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
        try:  # noqa: SIM105
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
                    patch.object(rich.table, "Table", side_effect=ImportError("no rich")),
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
                    patch.object(rich.table, "Table", side_effect=ImportError("no rich")),
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
            result = self.runner.invoke(config, ["set", "copilot-cowork-skills-dir", "/some/path"])
        assert result.exit_code == 1


class TestValidConfigKeys:
    """Tests for _valid_config_keys() feature-flag gating."""

    def test_valid_config_keys_excludes_cowork_when_flag_off(self):
        """copilot-cowork-skills-dir is hidden when the copilot_cowork flag is off."""
        from apm_cli.commands.config import _valid_config_keys

        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = _valid_config_keys()

        assert "auto-integrate" in result
        assert "temp-dir" in result
        assert "copilot-cowork-skills-dir" not in result

    def test_valid_config_keys_includes_cowork_when_flag_on(self):
        """copilot-cowork-skills-dir is listed when the copilot_cowork flag is on."""
        from apm_cli.commands.config import _valid_config_keys

        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = _valid_config_keys()

        assert "auto-integrate" in result
        assert "temp-dir" in result
        assert "copilot-cowork-skills-dir" in result

    def test_valid_config_keys_always_includes_transport_keys(self):
        """allow-protocol-fallback and ssh are always listed regardless of feature flags."""
        from apm_cli.commands.config import _valid_config_keys

        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = _valid_config_keys()

        assert "allow-protocol-fallback" in result
        assert "ssh" in result


# ---------------------------------------------------------------------------
# Storage layer -- allow_protocol_fallback
# ---------------------------------------------------------------------------


class TestAllowProtocolFallbackFunctions:
    """Tests for get_allow_protocol_fallback and set_allow_protocol_fallback in apm_cli.config."""

    def test_get_allow_protocol_fallback_default_is_false(self):
        """Default value is False when not set."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "get_config", return_value={}):
            assert cfg_module.get_allow_protocol_fallback() is False

    def test_get_allow_protocol_fallback_true(self):
        """Returns True when set to True."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "get_config", return_value={"allow_protocol_fallback": True}):
            assert cfg_module.get_allow_protocol_fallback() is True

    def test_set_allow_protocol_fallback_calls_update_config(self):
        """set_allow_protocol_fallback delegates to update_config."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "update_config") as mock_update:
            cfg_module.set_allow_protocol_fallback(True)
            mock_update.assert_called_once_with({"allow_protocol_fallback": True})

    def test_set_allow_protocol_fallback_false_calls_update_config(self):
        """set_allow_protocol_fallback(False) passes False to update_config."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "update_config") as mock_update:
            cfg_module.set_allow_protocol_fallback(False)
            mock_update.assert_called_once_with({"allow_protocol_fallback": False})


# ---------------------------------------------------------------------------
# Storage layer -- prefer-ssh
# ---------------------------------------------------------------------------


class TestPreferSshFunctions:
    """Tests for get_prefer_ssh and set_prefer_ssh in apm_cli.config."""

    def test_get_prefer_ssh_default_is_false(self):
        """Default value is False when not set."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "get_config", return_value={}):
            assert cfg_module.get_prefer_ssh() is False

    def test_get_prefer_ssh_true(self):
        """Returns True when set to True."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "get_config", return_value={"prefer_ssh": True}):
            assert cfg_module.get_prefer_ssh() is True

    def test_set_prefer_ssh_calls_update_config(self):
        """set_prefer_ssh delegates to update_config."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "update_config") as mock_update:
            cfg_module.set_prefer_ssh(True)
            mock_update.assert_called_once_with({"prefer_ssh": True})

    def test_set_prefer_ssh_false_calls_update_config(self):
        """set_prefer_ssh(False) passes False to update_config."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "update_config") as mock_update:
            cfg_module.set_prefer_ssh(False)
            mock_update.assert_called_once_with({"prefer_ssh": False})


# ---------------------------------------------------------------------------
# Effective-value helpers (env > config > default)
# ---------------------------------------------------------------------------


class TestGetApmAllowProtocolFallback:
    """Tests for get_apm_allow_protocol_fallback resolution chain."""

    def test_env_var_wins_over_config(self):
        """APM_ALLOW_PROTOCOL_FALLBACK=1 wins even when config is False."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=False),
            patch.dict(os.environ, {"APM_ALLOW_PROTOCOL_FALLBACK": "1"}),
        ):
            assert cfg_module.get_apm_allow_protocol_fallback() is True

    def test_env_var_true_wins(self):
        """APM_ALLOW_PROTOCOL_FALLBACK=true is accepted."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=False),
            patch.dict(os.environ, {"APM_ALLOW_PROTOCOL_FALLBACK": "true"}),
        ):
            assert cfg_module.get_apm_allow_protocol_fallback() is True

    def test_config_used_when_env_unset(self):
        """Config value is used when APM_ALLOW_PROTOCOL_FALLBACK is unset."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=True),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_ALLOW_PROTOCOL_FALLBACK", None)
            assert cfg_module.get_apm_allow_protocol_fallback() is True

    def test_returns_false_when_both_unset(self):
        """Returns False when neither env var nor config is set."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=False),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_ALLOW_PROTOCOL_FALLBACK", None)
            assert cfg_module.get_apm_allow_protocol_fallback() is False

    def test_env_var_explicit_zero_overrides_config_true(self):
        """APM_ALLOW_PROTOCOL_FALLBACK=0 overrides a persisted config value of True."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=True),
            patch.dict(os.environ, {"APM_ALLOW_PROTOCOL_FALLBACK": "0"}),
        ):
            assert cfg_module.get_apm_allow_protocol_fallback() is False

    def test_empty_env_var_falls_through_to_config(self):
        """Empty APM_ALLOW_PROTOCOL_FALLBACK (unset/empty) falls back to config."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=True),
            patch.dict(os.environ, {"APM_ALLOW_PROTOCOL_FALLBACK": ""}),
        ):
            assert cfg_module.get_apm_allow_protocol_fallback() is True


class TestGetApmProtocolPref:
    """Tests for get_apm_protocol_pref resolution chain."""

    def test_env_var_ssh_wins_over_config(self):
        """APM_GIT_PROTOCOL=ssh wins even when config prefer_ssh is False."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_prefer_ssh", return_value=False),
            patch.dict(os.environ, {"APM_GIT_PROTOCOL": "ssh"}),
        ):
            assert cfg_module.get_apm_protocol_pref() == "ssh"

    def test_env_var_https_wins(self):
        """APM_GIT_PROTOCOL=https is returned as-is."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_prefer_ssh", return_value=True),
            patch.dict(os.environ, {"APM_GIT_PROTOCOL": "https"}),
        ):
            assert cfg_module.get_apm_protocol_pref() == "https"

    def test_config_prefer_ssh_used_when_env_unset(self):
        """Config prefer_ssh=True maps to 'ssh' when APM_GIT_PROTOCOL is unset."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_prefer_ssh", return_value=True),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_GIT_PROTOCOL", None)
            assert cfg_module.get_apm_protocol_pref() == "ssh"

    def test_returns_none_when_both_unset(self):
        """Returns None when neither env var nor config is set."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_prefer_ssh", return_value=False),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_GIT_PROTOCOL", None)
            assert cfg_module.get_apm_protocol_pref() is None

    def test_invalid_env_var_ignored(self):
        """An unrecognised APM_GIT_PROTOCOL value falls back to config."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_prefer_ssh", return_value=True),
            patch.dict(os.environ, {"APM_GIT_PROTOCOL": "git"}),
        ):
            assert cfg_module.get_apm_protocol_pref() == "ssh"


# ---------------------------------------------------------------------------
# CLI -- apm config set allow-protocol-fallback
# ---------------------------------------------------------------------------


class TestConfigSetAllowProtocolFallback:
    """Tests for `apm config set allow-protocol-fallback <value>`."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_set_allow_protocol_fallback_true(self):
        """Set allow-protocol-fallback to true."""
        with patch("apm_cli.config.set_allow_protocol_fallback") as mock_set:
            result = self.runner.invoke(config, ["set", "allow-protocol-fallback", "true"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(True)

    def test_set_allow_protocol_fallback_false(self):
        """Set allow-protocol-fallback to false."""
        with patch("apm_cli.config.set_allow_protocol_fallback") as mock_set:
            result = self.runner.invoke(config, ["set", "allow-protocol-fallback", "false"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(False)

    def test_set_allow_protocol_fallback_yes(self):
        """'yes' is accepted as a truthy value."""
        with patch("apm_cli.config.set_allow_protocol_fallback") as mock_set:
            result = self.runner.invoke(config, ["set", "allow-protocol-fallback", "yes"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(True)

    def test_set_allow_protocol_fallback_zero(self):
        """'0' is accepted as a falsy value."""
        with patch("apm_cli.config.set_allow_protocol_fallback") as mock_set:
            result = self.runner.invoke(config, ["set", "allow-protocol-fallback", "0"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(False)

    def test_set_allow_protocol_fallback_invalid_value(self):
        """Reject an invalid value."""
        result = self.runner.invoke(config, ["set", "allow-protocol-fallback", "maybe"])
        assert result.exit_code == 1

    def test_set_allow_protocol_fallback_case_insensitive(self):
        """Value comparison is case-insensitive."""
        with patch("apm_cli.config.set_allow_protocol_fallback") as mock_set:
            result = self.runner.invoke(config, ["set", "allow-protocol-fallback", "TRUE"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(True)

    def test_set_allow_protocol_fallback_not_gated(self):
        """allow-protocol-fallback does not require any experimental flag."""
        with patch("apm_cli.config.set_allow_protocol_fallback"):
            result = self.runner.invoke(config, ["set", "allow-protocol-fallback", "true"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# CLI -- apm config get allow-protocol-fallback
# ---------------------------------------------------------------------------


class TestConfigGetAllowProtocolFallback:
    """Tests for `apm config get allow-protocol-fallback`."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_get_allow_protocol_fallback_when_true(self):
        """Display the configured allow-protocol-fallback when True."""
        with patch("apm_cli.config.get_allow_protocol_fallback", return_value=True):
            result = self.runner.invoke(config, ["get", "allow-protocol-fallback"])
        assert result.exit_code == 0
        assert "allow-protocol-fallback: true" in result.output

    def test_get_allow_protocol_fallback_when_false(self):
        """Display the configured allow-protocol-fallback when False."""
        with patch("apm_cli.config.get_allow_protocol_fallback", return_value=False):
            result = self.runner.invoke(config, ["get", "allow-protocol-fallback"])
        assert result.exit_code == 0
        assert "allow-protocol-fallback: false" in result.output

    def test_get_all_config_includes_allow_protocol_fallback(self):
        """apm config get (no key) shows allow-protocol-fallback only when true."""
        with (
            patch("apm_cli.config.get_auto_integrate", return_value=True),
            patch("apm_cli.config.get_allow_protocol_fallback", return_value=True),
            patch("apm_cli.config.get_prefer_ssh", return_value=False),
            patch("apm_cli.config.get_temp_dir", return_value=None),
            patch("apm_cli.core.experimental.is_enabled", return_value=False),
        ):
            result = self.runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "allow-protocol-fallback" in result.output

    def test_get_all_config_suppresses_allow_protocol_fallback_when_false(self):
        """apm config get (no key) omits allow-protocol-fallback when false (noise reduction)."""
        with (
            patch("apm_cli.config.get_auto_integrate", return_value=True),
            patch("apm_cli.config.get_allow_protocol_fallback", return_value=False),
            patch("apm_cli.config.get_prefer_ssh", return_value=False),
            patch("apm_cli.config.get_temp_dir", return_value=None),
            patch("apm_cli.core.experimental.is_enabled", return_value=False),
        ):
            result = self.runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "allow-protocol-fallback" not in result.output

    def test_unknown_key_error_lists_allow_protocol_fallback(self):
        """Error message for unknown keys lists allow-protocol-fallback as valid."""
        result = self.runner.invoke(config, ["get", "nonexistent"])
        assert result.exit_code == 1
        assert "allow-protocol-fallback" in result.output


# ---------------------------------------------------------------------------
# CLI -- apm config set prefer-ssh
# ---------------------------------------------------------------------------


class TestConfigSetPreferSsh:
    """Tests for `apm config set prefer-ssh <value>`."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_set_prefer_ssh_true(self):
        """Set prefer-ssh to true."""
        with patch("apm_cli.config.set_prefer_ssh") as mock_set:
            result = self.runner.invoke(config, ["set", "prefer-ssh", "true"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(True)

    def test_set_prefer_ssh_false(self):
        """Set prefer-ssh to false."""
        with patch("apm_cli.config.set_prefer_ssh") as mock_set:
            result = self.runner.invoke(config, ["set", "prefer-ssh", "false"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(False)

    def test_set_prefer_ssh_one(self):
        """'1' is accepted as a truthy value."""
        with patch("apm_cli.config.set_prefer_ssh") as mock_set:
            result = self.runner.invoke(config, ["set", "prefer-ssh", "1"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(True)

    def test_set_prefer_ssh_no(self):
        """'no' is accepted as a falsy value."""
        with patch("apm_cli.config.set_prefer_ssh") as mock_set:
            result = self.runner.invoke(config, ["set", "prefer-ssh", "no"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with(False)

    def test_set_prefer_ssh_invalid_value(self):
        """Reject an invalid value."""
        result = self.runner.invoke(config, ["set", "prefer-ssh", "enabled"])
        assert result.exit_code == 1

    def test_set_prefer_ssh_not_gated(self):
        """prefer-ssh does not require any experimental flag."""
        with patch("apm_cli.config.set_prefer_ssh"):
            result = self.runner.invoke(config, ["set", "prefer-ssh", "true"])
        assert result.exit_code == 0

    def test_set_prefer_ssh_unknown_key_lists_prefer_ssh_as_valid(self):
        """Error listing includes 'prefer-ssh' as a valid key."""
        result = self.runner.invoke(config, ["set", "nonexistent", "true"])
        assert result.exit_code == 1
        assert "prefer-ssh" in result.output


# ---------------------------------------------------------------------------
# CLI -- apm config get prefer-ssh
# ---------------------------------------------------------------------------


class TestConfigGetPreferSsh:
    """Tests for `apm config get prefer-ssh`."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_get_prefer_ssh_when_true(self):
        """Display the configured prefer-ssh preference when True."""
        with patch("apm_cli.config.get_prefer_ssh", return_value=True):
            result = self.runner.invoke(config, ["get", "prefer-ssh"])
        assert result.exit_code == 0
        assert "prefer-ssh: true" in result.output

    def test_get_prefer_ssh_when_false(self):
        """Display the configured prefer-ssh preference when False."""
        with patch("apm_cli.config.get_prefer_ssh", return_value=False):
            result = self.runner.invoke(config, ["get", "prefer-ssh"])
        assert result.exit_code == 0
        assert "prefer-ssh: false" in result.output

    def test_get_all_config_includes_prefer_ssh(self):
        """apm config get (no key) shows prefer-ssh only when true."""
        with (
            patch("apm_cli.config.get_auto_integrate", return_value=True),
            patch("apm_cli.config.get_allow_protocol_fallback", return_value=False),
            patch("apm_cli.config.get_prefer_ssh", return_value=True),
            patch("apm_cli.config.get_temp_dir", return_value=None),
            patch("apm_cli.core.experimental.is_enabled", return_value=False),
        ):
            result = self.runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "prefer-ssh" in result.output

    def test_get_all_config_suppresses_prefer_ssh_when_false(self):
        """apm config get (no key) omits prefer-ssh when false (noise reduction)."""
        with (
            patch("apm_cli.config.get_auto_integrate", return_value=True),
            patch("apm_cli.config.get_allow_protocol_fallback", return_value=False),
            patch("apm_cli.config.get_prefer_ssh", return_value=False),
            patch("apm_cli.config.get_temp_dir", return_value=None),
            patch("apm_cli.core.experimental.is_enabled", return_value=False),
        ):
            result = self.runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "prefer-ssh" not in result.output

    def test_unknown_key_error_lists_prefer_ssh_as_valid(self):
        """Error message for unknown keys lists prefer-ssh as valid."""
        result = self.runner.invoke(config, ["get", "nonexistent"])
        assert result.exit_code == 1
        assert "prefer-ssh" in result.output


class TestConfigShowTempDir:
    """Lines 127, 132-135: config show with temp-dir and copilot cowork dir."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_show_with_temp_dir_set(self):
        """Line 127: temp_dir is set -- shown in config table."""
        with (
            patch("apm_cli.commands.config.get_version", return_value="1.0.0"),
            patch("apm_cli.config.get_temp_dir", return_value="/tmp/apm-temp"),
            patch("apm_cli.core.experimental.is_enabled", return_value=False),
        ):
            result = self.runner.invoke(config, [])
        assert result.exit_code == 0
        assert "/tmp/apm-temp" in result.output or "Temp Directory" in result.output

    def test_show_with_copilot_cowork_dir_set(self):
        """Lines 132-135: copilot_cowork enabled + dir set -- shown."""
        with (
            patch("apm_cli.commands.config.get_version", return_value="1.0.0"),
            patch("apm_cli.config.get_temp_dir", return_value=None),
            patch("apm_cli.core.experimental.is_enabled", return_value=True),
            patch("apm_cli.config.get_copilot_cowork_skills_dir", return_value="/some/skills"),
        ):
            result = self.runner.invoke(config, [])
        assert result.exit_code == 0
        assert "/some/skills" in result.output or "Cowork Skills Dir" in result.output

    def test_show_with_copilot_cowork_dir_not_set(self):
        """Lines 132-138: copilot_cowork enabled + dir NOT set -- auto-detection msg."""
        with (
            patch("apm_cli.commands.config.get_version", return_value="1.0.0"),
            patch("apm_cli.config.get_temp_dir", return_value=None),
            patch("apm_cli.core.experimental.is_enabled", return_value=True),
            patch("apm_cli.config.get_copilot_cowork_skills_dir", return_value=None),
        ):
            result = self.runner.invoke(config, [])
        assert result.exit_code == 0
        assert "auto-detection" in result.output or "Cowork" in result.output


class TestAuditOnInstallCommand:
    """`apm config set/get/unset audit-on-install` (flag-gated)."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_set_blocked_without_flag(self):
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = self.runner.invoke(config, ["set", "audit-on-install", "warn"])
        assert result.exit_code == 1
        assert "external-scanners experimental flag" in result.output

    def test_set_allowed_with_flag(self):
        with (
            patch("apm_cli.core.experimental.is_enabled", return_value=True),
            patch("apm_cli.config.set_audit_on_install") as mock_set,
            patch("apm_cli.config.get_audit_on_install", return_value="warn"),
        ):
            result = self.runner.invoke(config, ["set", "audit-on-install", "warn"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with("warn")

    def test_get_audit_on_install(self):
        with patch("apm_cli.config.get_audit_on_install", return_value="block"):
            result = self.runner.invoke(config, ["get", "audit-on-install"])
        assert result.exit_code == 0
        assert "block" in result.output

    def test_unset_audit_on_install(self):
        with patch("apm_cli.config.unset_audit_on_install") as mock_unset:
            result = self.runner.invoke(config, ["unset", "audit-on-install"])
        assert result.exit_code == 0
        mock_unset.assert_called_once()


class TestExternalScannerConfigCommand:
    """`apm config set/get/unset external.<name>.{llm,args}` (flag-gated)."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_set_llm_blocked_without_flag(self):
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = self.runner.invoke(config, ["set", "external.skillspector.llm", "true"])
        assert result.exit_code == 1
        assert "external-scanners experimental" in result.output

    def test_set_llm_with_flag(self):
        with (
            patch("apm_cli.core.experimental.is_enabled", return_value=True),
            patch("apm_cli.config.set_scanner_llm") as mock_set,
        ):
            result = self.runner.invoke(config, ["set", "external.skillspector.llm", "true"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with("skillspector", True)

    def test_set_args_shlex_split(self):
        with (
            patch("apm_cli.core.experimental.is_enabled", return_value=True),
            patch("apm_cli.config.set_scanner_args") as mock_set,
        ):
            result = self.runner.invoke(
                config, ["set", "external.skillspector.args", "--", "--model gpt-4o"]
            )
        assert result.exit_code == 0
        mock_set.assert_called_once_with("skillspector", ["--model", "gpt-4o"])

    def test_set_args_empty_string_rejected(self):
        with (
            patch("apm_cli.core.experimental.is_enabled", return_value=True),
            patch("apm_cli.config.set_scanner_args") as mock_set,
        ):
            result = self.runner.invoke(config, ["set", "external.skillspector.args", "--", "   "])
        assert result.exit_code == 1
        mock_set.assert_not_called()

    def test_set_unknown_scanner_rejected(self):
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = self.runner.invoke(config, ["set", "external.bogus.llm", "true"])
        assert result.exit_code == 1
        assert "Unknown external scanner" in result.output

    def test_get_llm(self):
        with (
            patch("apm_cli.core.experimental.is_enabled", return_value=True),
            patch(
                "apm_cli.config.get_scanner_options",
                return_value=(True, None),
            ),
        ):
            result = self.runner.invoke(config, ["get", "external.skillspector.llm"])
        assert result.exit_code == 0
        assert "true" in result.output

    def test_get_args_not_set(self):
        with (
            patch("apm_cli.core.experimental.is_enabled", return_value=True),
            patch(
                "apm_cli.config.get_scanner_options",
                return_value=(None, None),
            ),
        ):
            result = self.runner.invoke(config, ["get", "external.skillspector.args"])
        assert result.exit_code == 0
        assert "Not set" in result.output

    def test_unset_llm(self):
        with (
            patch("apm_cli.core.experimental.is_enabled", return_value=True),
            patch("apm_cli.config.unset_scanner_llm") as mock_unset,
        ):
            result = self.runner.invoke(config, ["unset", "external.skillspector.llm"])
        assert result.exit_code == 0
        mock_unset.assert_called_once_with("skillspector")


class TestMcpRegistryUrlCommand:
    """`apm config set/get/unset mcp-registry-url` -- issue #818."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_set_valid_https_url(self):
        with (
            patch("apm_cli.config.set_mcp_registry_url") as mock_set,
            patch(
                "apm_cli.config.get_mcp_registry_url",
                return_value="https://corp.mcp.example.com",
            ),
        ):
            result = self.runner.invoke(
                config, ["set", "mcp-registry-url", "https://corp.mcp.example.com"]
            )
        assert result.exit_code == 0
        mock_set.assert_called_once_with("https://corp.mcp.example.com")
        from urllib.parse import urlparse

        urls = [tok for tok in result.output.split() if "://" in tok]
        assert len(urls) >= 1
        assert urlparse(urls[0]).hostname == "corp.mcp.example.com"

    def test_set_valid_http_url(self):
        with (
            patch("apm_cli.config.set_mcp_registry_url") as mock_set,
            patch(
                "apm_cli.config.get_mcp_registry_url",
                return_value="http://internal.corp/mcp",
            ),
        ):
            result = self.runner.invoke(
                config, ["set", "mcp-registry-url", "http://internal.corp/mcp"]
            )
        assert result.exit_code == 0
        mock_set.assert_called_once_with("http://internal.corp/mcp")

    def test_set_invalid_scheme_rejected(self):
        with patch(
            "apm_cli.config.set_mcp_registry_url",
            side_effect=ValueError("scheme 'file' is not supported"),
        ):
            result = self.runner.invoke(config, ["set", "mcp-registry-url", "file:///etc/hosts"])
        assert result.exit_code == 1
        assert "file" in result.output or "not supported" in result.output

    def test_get_when_set(self):
        with patch(
            "apm_cli.config.get_mcp_registry_url",
            return_value="https://corp.mcp.example.com",
        ):
            result = self.runner.invoke(config, ["get", "mcp-registry-url"])
        assert result.exit_code == 0
        from urllib.parse import urlparse

        urls = [tok for tok in result.output.split() if "://" in tok]
        assert len(urls) >= 1
        assert urlparse(urls[0]).hostname == "corp.mcp.example.com"

    def test_get_when_not_set(self):
        with patch("apm_cli.config.get_mcp_registry_url", return_value=None):
            result = self.runner.invoke(config, ["get", "mcp-registry-url"])
        assert result.exit_code == 0
        assert "Not set" in result.output

    def test_unset_removes_key(self):
        with patch("apm_cli.config.unset_mcp_registry_url") as mock_unset:
            result = self.runner.invoke(config, ["unset", "mcp-registry-url"])
        assert result.exit_code == 0
        mock_unset.assert_called_once()
        assert "removed" in result.output
