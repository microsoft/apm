"""Tests for the apm config command and the apm_cli.config owner.

Every test reads and writes a real ``config.json`` in an isolated APM home
(the ``isolated_config`` fixture) and asserts the command output or the bytes
left in that file, instead of patching the owner's getters and setters.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import apm_cli.config as cfg_module
from apm_cli.commands.config import config

_TRANSPORT_ENV = (
    "APM_ALLOW_PROTOCOL_FALLBACK",
    "APM_GITHUB_AUTH_FIRST",
    "APM_GIT_PROTOCOL",
    "APM_TEMP_DIR",
)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Point the owner's config store at a temp APM home and return config.json.

    No test reads or writes the real ``~/.apm``; environment overrides of the
    transport keys are cleared so the file is the only source of truth, and
    the terminal width is pinned so Rich tables render rows on one line.
    """
    config_dir = tmp_path / ".apm"
    config_file = config_dir / "config.json"
    monkeypatch.setattr(cfg_module, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(cfg_module, "CONFIG_FILE", str(config_file))
    for name in _TRANSPORT_ENV:
        monkeypatch.delenv(name, raising=False)
    # Rich tables wrap at the terminal width; pin it so rendered rows are stable.
    monkeypatch.setenv("COLUMNS", "200")
    cfg_module._invalidate_config_cache()
    yield config_file
    cfg_module._invalidate_config_cache()


@pytest.fixture
def runner():
    return CliRunner()


def write_config(path: Path, data: dict) -> None:
    """Seed config.json with *data* and drop the owner's read cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    cfg_module._invalidate_config_cache()


def read_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def flat(output: str) -> str:
    return " ".join(output.split())


def _no_rich_table():
    import rich.table

    return patch.object(rich.table, "Table", side_effect=ImportError("no rich"))


class TestConfigShow:
    """Tests for `apm config` (show current configuration)."""

    @pytest.fixture(autouse=True)
    def _cwd(self, tmp_path, monkeypatch):
        self.project = tmp_path / "project"
        self.project.mkdir()
        monkeypatch.chdir(self.project)

    def test_config_show_outside_project(self, runner):
        """Show config when not in an APM project directory."""
        with patch("apm_cli.commands.config.get_version", return_value="1.2.3"):
            result = runner.invoke(config, [])
        assert result.exit_code == 0
        assert "Not in an APM project directory" in flat(result.output)
        assert "1.2.3" in result.output

    def test_config_show_inside_project(self, runner):
        """Show config when apm.yml is present."""
        (self.project / "apm.yml").write_text(
            "name: myproject\nversion: '0.1'\nentrypoint: main.md\n"
        )
        with patch("apm_cli.commands.config.get_version", return_value="1.2.3"):
            result = runner.invoke(config, [])
        assert result.exit_code == 0
        assert "myproject" in result.output
        assert "main.md" in result.output
        assert "Using defaults (no config)" in flat(result.output)

    def test_config_show_inside_project_with_compilation(self, runner):
        """Show config when apm.yml has compilation settings."""
        (self.project / "apm.yml").write_text(
            "name: myproject\nversion: '0.1'\n"
            "compilation:\n  output: CUSTOM.md\n  chatmode: copilot\n  resolve_links: false\n"
            "dependencies:\n  mcp:\n    - server1\n    - server2\n"
        )
        with patch("apm_cli.commands.config.get_version", return_value="1.2.3"):
            result = runner.invoke(config, [])
        assert result.exit_code == 0
        assert "CUSTOM.md" in result.output
        assert "copilot" in result.output
        assert "False" in result.output

    def test_config_show_rich_import_error_fallback(self, runner):
        """Fallback plain-text display when Rich (rich.table.Table) is unavailable."""
        with (
            patch("apm_cli.commands.config.get_version", return_value="0.9.0"),
            _no_rich_table(),
        ):
            result = runner.invoke(config, [])
        assert result.exit_code == 0
        assert "Current APM Configuration:" in result.output
        assert "APM CLI Version: 0.9.0" in flat(result.output)

    def test_config_show_fallback_inside_project(self, runner):
        """Fallback display inside a project directory when console/table unavailable."""
        (self.project / "apm.yml").write_text("name: proj\nversion: '1.0'\n")
        with (
            patch("apm_cli.commands.config.get_version", return_value="0.9.0"),
            _no_rich_table(),
        ):
            result = runner.invoke(config, [])
        assert result.exit_code == 0
        assert "Name: proj" in flat(result.output)
        assert "MCP Dependencies: 0" in flat(result.output)

    def test_config_show_displays_temp_dir_in_global_section(self, runner, isolated_config):
        """Fallback display includes Temp Directory row when temp-dir is configured."""
        write_config(isolated_config, {"temp_dir": "/custom/tmp"})
        with (
            patch("apm_cli.commands.config.get_version", return_value="1.2.3"),
            _no_rich_table(),
        ):
            result = runner.invoke(config, [])
        assert result.exit_code == 0
        assert "Temp Directory: /custom/tmp" in result.output

    def test_config_show_omits_temp_dir_when_not_configured(self, runner):
        """Fallback display omits Temp Directory row when temp-dir is not configured."""
        with (
            patch("apm_cli.commands.config.get_version", return_value="1.2.3"),
            _no_rich_table(),
        ):
            result = runner.invoke(config, [])
        assert result.exit_code == 0
        assert "Temp Directory" not in result.output


class TestConfigSet:
    """Tests for `apm config set <key> <value>`."""

    @pytest.mark.parametrize(
        ("value", "stored"),
        [
            ("true", True),
            ("yes", True),
            ("1", True),
            ("TRUE", True),
            ("false", False),
            ("no", False),
            ("0", False),
        ],
    )
    def test_set_auto_integrate_stores_value(self, runner, isolated_config, value, stored):
        """auto-integrate accepts every boolean alias, case-insensitively."""
        result = runner.invoke(config, ["set", "auto-integrate", value])
        assert result.exit_code == 0
        assert read_config(isolated_config)["auto_integrate"] is stored

    def test_set_auto_integrate_invalid_value(self, runner, isolated_config):
        """Reject an invalid value for auto-integrate."""
        result = runner.invoke(config, ["set", "auto-integrate", "maybe"])
        assert result.exit_code == 1
        assert "auto_integrate" not in read_config(isolated_config)

    def test_set_unknown_key(self, runner, isolated_config):
        """Reject an unknown configuration key."""
        result = runner.invoke(config, ["set", "nonexistent", "value"])
        assert result.exit_code == 1
        assert "nonexistent" not in read_config(isolated_config)

    def test_set_auto_integrate_warns_deprecated(self, runner):
        """auto-integrate is still accepted but warns that it has no effect (#3839)."""
        result = runner.invoke(config, ["set", "auto-integrate", "false"])
        assert result.exit_code == 0
        assert "[!] 'auto-integrate' is deprecated and has no effect" in flat(result.output)

    def test_set_auto_integrate_warns_first_and_claims_no_success(self, runner):
        """A deprecated key never prints a success line implying it took effect."""
        result = runner.invoke(config, ["set", "auto-integrate", "false"])
        output = flat(result.output)
        assert result.exit_code == 0
        assert "Auto-integration set to" not in output
        assert "[i] Stored auto-integrate=false (no effect)" in output
        assert output.index("deprecated") < output.index("Stored auto-integrate")

    def test_set_target(self, runner, isolated_config):
        """Set install default target."""
        result = runner.invoke(config, ["set", "target", "claude"])
        assert result.exit_code == 0
        assert "Default install target set to: claude" in flat(result.output)
        assert cfg_module.get_install_target() == "claude"

    def test_set_target_invalid_value(self, runner, isolated_config):
        """Reject invalid target value."""
        result = runner.invoke(config, ["set", "target", "oops"])
        assert result.exit_code == 1
        assert "install_target" not in read_config(isolated_config)

    def test_set_self_update_channel(self, runner, isolated_config):
        """Set self-update channel preference."""
        result = runner.invoke(config, ["set", "self-update.channel", "prerelease"])
        assert result.exit_code == 0
        assert read_config(isolated_config)["self_update_channel"] == "prerelease"
        assert "Self-update channel saved: prerelease" in flat(result.output)

    def test_set_self_update_install_dir(self, runner, isolated_config, tmp_path):
        """Windows retains the existing saved-directory confirmation."""
        target = str(tmp_path / "apm-bin")
        with patch("apm_cli.commands.self_update._is_windows_platform", return_value=True):
            result = runner.invoke(config, ["set", "self-update.install-dir", target])
        assert result.exit_code == 0
        assert read_config(isolated_config)["self_update_install_dir"] == target
        assert f"Self-update install directory saved: {target}" in flat(result.output)

    def test_set_self_update_install_dir_describes_unix_preservation(self, runner, tmp_path):
        """Unix describes the value as a matching preservation preference."""
        target = str(tmp_path / "apm-bin")
        with patch("apm_cli.commands.self_update._is_windows_platform", return_value=False):
            result = runner.invoke(config, ["set", "self-update.install-dir", target])

        assert result.exit_code == 0
        assert (
            f"Self-update launcher directory preference saved: {target} "
            "(must match the existing Unix installation)"
        ) in flat(result.output)

    @pytest.mark.parametrize(
        "key",
        [
            "self-update.token",
            "self-update.registry-token",
            "self-update.installer-base-url",
            "self-update.command",
        ],
    )
    def test_set_rejects_secret_or_executable_self_update_keys(self, runner, isolated_config, key):
        """Do not persist secret or executable self-update settings."""
        result = runner.invoke(config, ["set", key, "value"])

        assert result.exit_code == 1
        assert "self-update config only supports non-secret installer preferences" in result.output
        assert read_config(isolated_config) == {}


class TestConfigGet:
    """Tests for `apm config get [key]`."""

    @pytest.mark.parametrize(("stored", "shown"), [(True, "true"), (False, "false")])
    def test_get_auto_integrate(self, runner, isolated_config, stored, shown):
        """Get the auto-integrate setting."""
        write_config(isolated_config, {"auto_integrate": stored})
        result = runner.invoke(config, ["get", "auto-integrate"])
        assert result.exit_code == 0
        assert f"auto-integrate: {shown}" in result.output

    def test_get_auto_integrate_warns_deprecated(self, runner):
        """Reading the deprecated key still works but warns (#3839)."""
        result = runner.invoke(config, ["get", "auto-integrate"])
        assert result.exit_code == 0
        assert "[!] 'auto-integrate' is deprecated and has no effect" in flat(result.output)

    def test_get_unknown_key(self, runner):
        """Reject an unknown key."""
        result = runner.invoke(config, ["get", "nonexistent"])
        assert result.exit_code == 1
        assert "Unknown configuration key: 'nonexistent'" in flat(result.output)

    def test_get_all_config(self, runner, isolated_config):
        """Show all config when no key is provided."""
        write_config(isolated_config, {"auto_integrate": True, "default_client": "copilot"})
        result = runner.invoke(config, ["get"])
        assert result.exit_code == 0
        # Deprecated keys are not listed in the summary (#3839).
        assert "auto-integrate" not in result.output
        # Internal keys must not appear - users cannot set them via apm config set
        assert "default_client" not in result.output

    def test_list_config_matches_get_without_key(self, runner):
        """`apm config list` is an alias for listing all effective config values."""
        get_result = runner.invoke(config, ["get"])
        list_result = runner.invoke(config, ["list"])

        assert get_result.exit_code == 0
        assert list_result.exit_code == 0
        assert list_result.output == get_result.output
        assert (
            "mcp-registry-url: Not set (using default https://api.mcp.github.com)"
            in list_result.output
        )

    def test_get_all_config_fresh_install(self, runner):
        """temp-dir is shown even on a fresh install with no key in the file."""
        result = runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "temp-dir: Not set" in result.output

    def test_get_all_config_suppresses_default_self_update_channel(self, runner, isolated_config):
        """apm config get (no key) omits the default stable self-update channel."""
        write_config(isolated_config, {"self_update_channel": "stable"})
        result = runner.invoke(config, ["get"])

        assert result.exit_code == 0
        assert "self-update.channel" not in result.output

    def test_get_all_config_shows_non_default_self_update_channel(self, runner, isolated_config):
        """apm config get (no key) shows a configured non-default channel."""
        write_config(isolated_config, {"self_update_channel": "prerelease"})
        result = runner.invoke(config, ["get"])

        assert result.exit_code == 0
        assert "self-update.channel: prerelease" in result.output

    def test_get_all_config_suppresses_unset_self_update_install_dir(self, runner):
        """apm config get (no key) omits unset self-update install-dir."""
        result = runner.invoke(config, ["get"])

        assert result.exit_code == 0
        assert "self-update.install-dir" not in result.output

    def test_get_all_config_shows_configured_self_update_install_dir(self, runner, isolated_config):
        """apm config get (no key) shows configured self-update install-dir."""
        write_config(isolated_config, {"self_update_install_dir": "/opt/apm/bin"})
        result = runner.invoke(config, ["get"])

        assert result.exit_code == 0
        assert "self-update.install-dir: /opt/apm/bin" in result.output

    def test_get_target_when_set(self, runner, isolated_config):
        """Get configured default install target."""
        write_config(isolated_config, {"install_target": "claude"})
        result = runner.invoke(config, ["get", "target"])
        assert result.exit_code == 0
        assert "target: claude" in result.output

    def test_get_target_when_unset(self, runner):
        """Show not-set message for default install target."""
        result = runner.invoke(config, ["get", "target"])
        assert result.exit_code == 0
        assert "target: Not set (using auto-detection)" in result.output

    def test_get_self_update_channel(self, runner, isolated_config):
        """Get configured self-update channel preference."""
        write_config(isolated_config, {"self_update_channel": "stable"})
        result = runner.invoke(config, ["get", "self-update.channel"])
        assert result.exit_code == 0
        assert "self-update.channel: stable" in result.output

    def test_get_self_update_install_dir(self, runner, isolated_config):
        """Get configured self-update install directory preference."""
        write_config(isolated_config, {"self_update_install_dir": "/opt/apm/bin"})
        result = runner.invoke(config, ["get", "self-update.install-dir"])
        assert result.exit_code == 0
        assert "self-update.install-dir: /opt/apm/bin" in result.output

    def test_get_unset_self_update_install_dir_preserves_unix_installation(self, runner):
        """Unix unset state describes preservation, not a default destination."""
        with patch("apm_cli.commands.self_update._is_windows_platform", return_value=False):
            result = runner.invoke(config, ["get", "self-update.install-dir"])

        assert result.exit_code == 0
        assert (
            "self-update.install-dir: Not set (preserving the detected Unix installation)"
        ) in result.output

    def test_get_unset_self_update_install_dir_keeps_windows_default_wording(self, runner):
        """Windows retains its installer-default description."""
        with patch("apm_cli.commands.self_update._is_windows_platform", return_value=True):
            result = runner.invoke(config, ["get", "self-update.install-dir"])

        assert result.exit_code == 0
        assert "self-update.install-dir: Not set (using installer default)" in result.output


class TestAutoIntegrateFunctions:
    """Tests for get_auto_integrate and set_auto_integrate in apm_cli.config."""

    def test_get_auto_integrate_default(self):
        """Default value is True when not set."""
        assert cfg_module.get_auto_integrate() is True

    def test_get_auto_integrate_false(self, isolated_config):
        """Returns False when set to False."""
        write_config(isolated_config, {"auto_integrate": False})
        assert cfg_module.get_auto_integrate() is False

    @pytest.mark.parametrize("enabled", [True, False])
    def test_set_auto_integrate_persists(self, isolated_config, enabled):
        """set_auto_integrate writes the flag to config.json."""
        cfg_module.set_auto_integrate(enabled)
        assert read_config(isolated_config)["auto_integrate"] is enabled
        assert cfg_module.get_auto_integrate() is enabled


class TestTempDirFunctions:
    """Tests for get_temp_dir, set_temp_dir, and get_apm_temp_dir in apm_cli.config."""

    def test_get_temp_dir_default_is_none(self):
        """Returns None when temp_dir is not set."""
        assert cfg_module.get_temp_dir() is None

    def test_get_temp_dir_returns_stored_value(self, isolated_config):
        """Returns stored temp_dir value."""
        write_config(isolated_config, {"temp_dir": "/custom/tmp"})
        assert cfg_module.get_temp_dir() == "/custom/tmp"

    def test_set_temp_dir_validates_and_stores(self, isolated_config, tmp_path):
        """set_temp_dir normalises the path and stores it."""
        target = tmp_path / "scratch"
        target.mkdir()
        cfg_module.set_temp_dir(str(target))
        assert read_config(isolated_config)["temp_dir"] == os.path.abspath(str(target))

    def test_set_temp_dir_rejects_nonexistent_directory(self, isolated_config):
        """Raises ValueError when path does not exist."""
        with pytest.raises(ValueError, match="does not exist"):
            cfg_module.set_temp_dir("/nonexistent/path/xyz")
        assert "temp_dir" not in read_config(isolated_config)

    def test_set_temp_dir_rejects_file_path(self, isolated_config, tmp_path):
        """Raises ValueError when path is a file, not a directory."""
        regular = tmp_path / "file.txt"
        regular.write_text("x")
        with pytest.raises(ValueError, match="not a directory"):
            cfg_module.set_temp_dir(str(regular))
        assert "temp_dir" not in read_config(isolated_config)

    def test_set_temp_dir_normalises_home_path(self, isolated_config, tmp_path, monkeypatch):
        """Tilde paths are expanded before storage."""
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg_module.set_temp_dir("~")
        assert read_config(isolated_config)["temp_dir"] == os.path.expanduser("~")

    @pytest.mark.parametrize(
        ("stored", "env", "expected"),
        [
            pytest.param("/from/config", "/from/env", "/from/env", id="env-wins"),
            pytest.param("/from/config", None, "/from/config", id="config-fallback"),
            pytest.param(None, None, None, id="both-unset"),
            pytest.param("/from/config", "", "/from/config", id="empty-env-ignored"),
            pytest.param(None, "   ", None, id="whitespace-env-ignored"),
            pytest.param("", None, None, id="empty-config-ignored"),
        ],
    )
    def test_get_apm_temp_dir_resolution(self, isolated_config, monkeypatch, stored, env, expected):
        """APM_TEMP_DIR beats config; blank values on either side count as unset."""
        if stored is not None:
            write_config(isolated_config, {"temp_dir": stored})
        if env is not None:
            monkeypatch.setenv("APM_TEMP_DIR", env)
        assert cfg_module.get_apm_temp_dir() == expected


class TestConfigSetTempDir:
    """Tests for `apm config set temp-dir <path>`."""

    def test_set_temp_dir_success(self, runner, isolated_config, tmp_path):
        """Set a valid temp-dir."""
        target = tmp_path / "apm-temp"
        target.mkdir()
        result = runner.invoke(config, ["set", "temp-dir", str(target)])
        assert result.exit_code == 0
        assert read_config(isolated_config)["temp_dir"] == os.path.abspath(str(target))

    def test_set_temp_dir_validation_error(self, runner, isolated_config, tmp_path):
        """Exit 1 when the directory does not exist."""
        missing = tmp_path / "missing"
        result = runner.invoke(config, ["set", "temp-dir", str(missing)])
        assert result.exit_code == 1
        assert "Directory does not exist" in flat(result.output)
        assert "temp_dir" not in read_config(isolated_config)

    def test_set_unknown_key_includes_temp_dir_in_valid_keys(self, runner):
        """Error message lists temp-dir as a valid key."""
        result = runner.invoke(config, ["set", "nonexistent", "value"])
        assert result.exit_code == 1
        assert "temp-dir" in result.output


class TestConfigGetTempDir:
    """Tests for `apm config get temp-dir`."""

    def test_get_temp_dir_when_set(self, runner, isolated_config):
        """Display the configured temp-dir."""
        write_config(isolated_config, {"temp_dir": "/custom/tmp"})
        result = runner.invoke(config, ["get", "temp-dir"])
        assert result.exit_code == 0
        assert "temp-dir: /custom/tmp" in result.output

    def test_get_temp_dir_when_unset(self, runner):
        """Display fallback message when temp-dir is not configured."""
        result = runner.invoke(config, ["get", "temp-dir"])
        assert result.exit_code == 0
        assert "Not set (using system default)" in result.output

    def test_get_unknown_key_includes_temp_dir_in_valid_keys(self, runner):
        """Error message lists temp-dir as a valid key."""
        result = runner.invoke(config, ["get", "nonexistent"])
        assert result.exit_code == 1
        assert "temp-dir" in result.output

    def test_get_all_config_maps_temp_dir_key(self, runner, isolated_config):
        """All-config listing maps internal temp_dir to display temp-dir."""
        write_config(isolated_config, {"auto_integrate": True, "temp_dir": "/my/temp"})
        result = runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "temp-dir: /my/temp" in result.output


# ---------------------------------------------------------------------------
# Storage layer -- copilot_cowork_skills_dir
# ---------------------------------------------------------------------------


class TestCoworkSkillsDirFunctions:
    """Tests for get/set/unset_copilot_cowork_skills_dir."""

    def test_get_copilot_cowork_skills_dir_default_is_none(self):
        """Returns None when copilot_cowork_skills_dir key is absent from the config."""
        assert cfg_module.get_copilot_cowork_skills_dir() is None

    def test_get_copilot_cowork_skills_dir_returns_stored_value(self, isolated_config):
        """Returns the stored copilot_cowork_skills_dir value from config."""
        write_config(isolated_config, {"copilot_cowork_skills_dir": "/stored/path"})
        assert cfg_module.get_copilot_cowork_skills_dir() == "/stored/path"

    def test_set_copilot_cowork_skills_dir_stores_absolute_path(self, isolated_config):
        """set_copilot_cowork_skills_dir persists the absolute path."""
        cfg_module.set_copilot_cowork_skills_dir("/absolute/skills")
        stored = read_config(isolated_config)["copilot_cowork_skills_dir"]
        assert stored == os.path.normpath("/absolute/skills")

    def test_set_copilot_cowork_skills_dir_expands_tilde_before_storing(
        self, isolated_config, tmp_path, monkeypatch
    ):
        """Tilde in path is expanded to an absolute path before storage."""
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg_module.set_copilot_cowork_skills_dir("~/myskills")
        stored = read_config(isolated_config)["copilot_cowork_skills_dir"]
        assert stored == os.path.join(os.path.expanduser("~"), "myskills")

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            pytest.param("", None, id="empty"),
            pytest.param("   ", None, id="whitespace"),
            pytest.param("relative/path", "absolute", id="relative"),
        ],
    )
    def test_set_copilot_cowork_skills_dir_rejects(self, isolated_config, raw, match):
        """Empty, blank and relative paths raise and leave config.json untouched."""
        with pytest.raises(ValueError, match=match):
            cfg_module.set_copilot_cowork_skills_dir(raw)
        assert "copilot_cowork_skills_dir" not in read_config(isolated_config)

    def test_set_copilot_cowork_skills_dir_accepts_nonexistent_absolute_path(self, isolated_config):
        """Non-existent absolute path is accepted; OneDrive may not yet be synced."""
        target = "/nonexistent/absolute/path/xyz"
        cfg_module.set_copilot_cowork_skills_dir(target)
        stored = read_config(isolated_config)["copilot_cowork_skills_dir"]
        assert stored == os.path.normpath(target)

    def test_unset_copilot_cowork_skills_dir_removes_key(self, isolated_config):
        """unset_copilot_cowork_skills_dir removes the key; subsequent get returns None."""
        raw = "/absolute/skills/path"
        cfg_module.set_copilot_cowork_skills_dir(raw)
        assert cfg_module.get_copilot_cowork_skills_dir() == os.path.normpath(raw)

        cfg_module.unset_copilot_cowork_skills_dir()
        assert cfg_module.get_copilot_cowork_skills_dir() is None
        assert "copilot_cowork_skills_dir" not in read_config(isolated_config)

    def test_unset_copilot_cowork_skills_dir_noop_when_absent(self):
        """unset_copilot_cowork_skills_dir is a no-op when the key was never set."""
        cfg_module.unset_copilot_cowork_skills_dir()
        assert cfg_module.get_copilot_cowork_skills_dir() is None


class TestUnsetTempDir:
    """Tests for unset_temp_dir in apm_cli.config."""

    def test_unset_temp_dir_removes_key(self, isolated_config, tmp_path):
        """unset_temp_dir removes temp_dir; subsequent get_temp_dir returns None."""
        cfg_module.set_temp_dir(str(tmp_path))
        assert cfg_module.get_temp_dir() == str(tmp_path)

        cfg_module.unset_temp_dir()
        assert cfg_module.get_temp_dir() is None
        assert "temp_dir" not in read_config(isolated_config)

    def test_unset_temp_dir_noop_when_absent(self):
        """unset_temp_dir is a no-op when temp_dir was never set."""
        cfg_module.unset_temp_dir()
        assert cfg_module.get_temp_dir() is None


def _enable(path: Path, *flags: str, **values) -> None:
    """Seed config.json with experimental *flags* turned on plus *values*."""
    write_config(path, {"experimental": dict.fromkeys(flags, True), **values})


# ---------------------------------------------------------------------------
# CLI -- apm config set/get copilot-cowork-skills-dir
# ---------------------------------------------------------------------------


class TestConfigSetCoworkSkillsDir:
    """Tests for `apm config set copilot-cowork-skills-dir <value>`."""

    def test_set_copilot_cowork_skills_dir_flag_enabled_returns_exit_0(
        self, runner, isolated_config
    ):
        """Valid absolute path with the cowork flag enabled succeeds."""
        _enable(isolated_config, "copilot_cowork")
        result = runner.invoke(config, ["set", "copilot-cowork-skills-dir", "/tmp/foo"])
        assert result.exit_code == 0
        stored = read_config(isolated_config)["copilot_cowork_skills_dir"]
        assert stored == os.path.normpath("/tmp/foo")

    def test_set_copilot_cowork_skills_dir_flag_disabled_returns_exit_1(
        self, runner, isolated_config
    ):
        """Attempting to set copilot-cowork-skills-dir without the cowork flag exits 1."""
        result = runner.invoke(config, ["set", "copilot-cowork-skills-dir", "/tmp/foo"])
        assert result.exit_code == 1
        assert "experimental" in result.output
        assert "enable copilot-cowork" in result.output
        assert "copilot_cowork_skills_dir" not in read_config(isolated_config)

    @pytest.mark.parametrize(
        ("raw", "message"),
        [
            pytest.param("relative/path", "absolute", id="relative"),
            pytest.param("", None, id="empty"),
        ],
    )
    def test_set_copilot_cowork_skills_dir_invalid_path_exits_1(
        self, runner, isolated_config, raw, message
    ):
        """Relative or empty paths are rejected with exit code 1."""
        _enable(isolated_config, "copilot_cowork")
        result = runner.invoke(config, ["set", "copilot-cowork-skills-dir", raw])
        assert result.exit_code == 1
        if message:
            assert message in result.output
        assert "copilot_cowork_skills_dir" not in read_config(isolated_config)


class TestConfigGetCoworkSkillsDir:
    """Tests for `apm config get copilot-cowork-skills-dir`."""

    def test_get_copilot_cowork_skills_dir_displays_stored_value(self, runner, isolated_config):
        """Displays the configured copilot-cowork-skills-dir path."""
        write_config(isolated_config, {"copilot_cowork_skills_dir": "/my/skills"})
        result = runner.invoke(config, ["get", "copilot-cowork-skills-dir"])
        assert result.exit_code == 0
        assert "/my/skills" in result.output

    def test_get_copilot_cowork_skills_dir_when_unset_shows_not_set(self, runner):
        """get needs no copilot-cowork flag and reports 'Not set' when unconfigured."""
        result = runner.invoke(config, ["get", "copilot-cowork-skills-dir"])
        assert result.exit_code == 0
        assert "Not set" in result.output


# ---------------------------------------------------------------------------
# CLI -- apm config unset
# ---------------------------------------------------------------------------


class TestConfigUnsetSubcommand:
    """Tests for `apm config unset <key>`."""

    @pytest.mark.parametrize(
        ("key", "stored"),
        [
            ("copilot-cowork-skills-dir", {"copilot_cowork_skills_dir": "/s"}),
            ("temp-dir", {"temp_dir": "/t"}),
            ("target", {"install_target": "claude"}),
            ("self-update.channel", {"self_update_channel": "prerelease"}),
        ],
    )
    def test_unset_removes_the_key(self, runner, isolated_config, key, stored):
        """apm config unset <key> exits 0 and removes only that key."""
        write_config(isolated_config, {**stored, "prefer_ssh": True})
        result = runner.invoke(config, ["unset", key])
        assert result.exit_code == 0
        assert read_config(isolated_config) == {"prefer_ssh": True}

    def test_unset_copilot_cowork_skills_dir_idempotent(self, runner, isolated_config):
        """Unsetting an absent copilot-cowork-skills-dir key is safe and exits 0."""
        result = runner.invoke(config, ["unset", "copilot-cowork-skills-dir"])
        assert result.exit_code == 0
        assert "copilot_cowork_skills_dir" not in read_config(isolated_config)

    def test_unset_self_update_install_dir_exits_0(self, runner, isolated_config):
        """Windows retains the existing removed-directory confirmation."""
        write_config(isolated_config, {"self_update_install_dir": "/opt/apm/bin"})
        with patch("apm_cli.commands.self_update._is_windows_platform", return_value=True):
            result = runner.invoke(config, ["unset", "self-update.install-dir"])
        assert result.exit_code == 0
        assert "self_update_install_dir" not in read_config(isolated_config)
        assert "Self-update install directory removed (will use installer default)" in result.output

    def test_unset_self_update_install_dir_describes_unix_preservation(
        self, runner, isolated_config
    ):
        """Unix removal describes preservation of the detected installation."""
        write_config(isolated_config, {"self_update_install_dir": "/opt/apm/bin"})
        with patch("apm_cli.commands.self_update._is_windows_platform", return_value=False):
            result = runner.invoke(config, ["unset", "self-update.install-dir"])

        assert result.exit_code == 0
        assert "self_update_install_dir" not in read_config(isolated_config)
        assert (
            "Self-update launcher directory preference removed "
            "(will preserve the detected Unix installation)"
        ) in flat(result.output)

    def test_unset_unknown_key_exits_1(self, runner):
        """Unsetting an unknown key exits 1 with an informative error."""
        result = runner.invoke(config, ["unset", "unknown-key"])
        assert result.exit_code == 1
        assert "Unknown configuration key: 'unknown-key'" in flat(result.output)


# ---------------------------------------------------------------------------
# CLI -- flag-gated listing (apm config and apm config get with no key)
# ---------------------------------------------------------------------------


class TestConfigListingFlagGating:
    """copilot-cowork-skills-dir appears in listings only when the flag is enabled."""

    @pytest.fixture(autouse=True)
    def _cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    def test_config_get_shows_copilot_cowork_skills_dir_when_flag_enabled(
        self, runner, isolated_config
    ):
        """apm config get (no key) includes copilot-cowork-skills-dir when the flag is on."""
        _enable(isolated_config, "copilot_cowork", copilot_cowork_skills_dir="/enabled/path")
        result = runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "copilot-cowork-skills-dir: /enabled/path" in flat(result.output)

    def test_config_get_lists_scanner_and_registry_settings_when_flags_enabled(
        self, runner, isolated_config
    ):
        """With the external-scanners and registries flags on, the listing shows their keys."""
        _enable(
            isolated_config,
            "external_scanners",
            "registries",
            audit_on_install="warn",
            external_scanners={"skillspector": {"llm": False, "args": ["--model", "m1"]}},
            registries={"corp": {"url": "https://registry.corp.example", "token": "s3cret-tok"}},
        )
        result = runner.invoke(config, ["get"])
        output = flat(result.output)
        assert result.exit_code == 0
        assert "audit-on-install: warn" in output
        assert "external.skillspector.llm: false" in output
        assert "external.skillspector.args: --model m1" in output
        assert "external.sarif" not in output
        from urllib.parse import urlparse

        url_lines = [
            line.split(":", 1)[1].strip()
            for line in result.output.splitlines()
            if line.strip().startswith("registry.corp.url:")
        ]
        assert len(url_lines) == 1
        listed = urlparse(url_lines[0])
        assert (listed.scheme, listed.hostname) == ("https", "registry.corp.example")
        assert "registry.corp.token:" in output
        assert "s3cret-tok" not in output

    def test_config_get_hides_copilot_cowork_skills_dir_when_flag_disabled(
        self, runner, isolated_config
    ):
        """apm config get (no key) omits copilot-cowork-skills-dir when the flag is off."""
        write_config(isolated_config, {"copilot_cowork_skills_dir": "/hidden/path"})
        result = runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert "copilot-cowork-skills-dir" not in result.output

    def test_config_show_includes_copilot_cowork_skills_dir_when_flag_enabled(
        self, runner, isolated_config
    ):
        """apm config (no subcommand) includes copilot-cowork-skills-dir when the flag is on."""
        _enable(isolated_config, "copilot_cowork", copilot_cowork_skills_dir="/cowork/skills")
        with (
            patch("apm_cli.commands.config.get_version", return_value="1.0.0"),
            _no_rich_table(),
        ):
            result = runner.invoke(config, [])
        assert result.exit_code == 0
        assert "Cowork Skills Dir: /cowork/skills" in flat(result.output)

    def test_config_show_omits_copilot_cowork_skills_dir_when_flag_disabled(self, runner):
        """apm config (no subcommand) omits copilot-cowork-skills-dir when the flag is off."""
        with (
            patch("apm_cli.commands.config.get_version", return_value="1.0.0"),
            _no_rich_table(),
        ):
            result = runner.invoke(config, [])
        assert result.exit_code == 0
        assert "Cowork Skills Dir" not in result.output


# ---------------------------------------------------------------------------
# Flag-gating regression -- only copilot-cowork-skills-dir should be gated
# ---------------------------------------------------------------------------


class TestFlagGatingRegression:
    """Regression checks: only copilot-cowork-skills-dir is gated on the copilot-cowork flag."""

    def test_auto_integrate_set_is_not_gated(self, runner, isolated_config):
        """apm config set auto-integrate does not require the cowork flag."""
        result = runner.invoke(config, ["set", "auto-integrate", "true"])
        assert result.exit_code == 0
        assert read_config(isolated_config)["auto_integrate"] is True

    def test_temp_dir_set_is_not_gated(self, runner, isolated_config, tmp_path):
        """apm config set temp-dir does not require the cowork flag."""
        result = runner.invoke(config, ["set", "temp-dir", str(tmp_path)])
        assert result.exit_code == 0
        assert read_config(isolated_config)["temp_dir"] == os.path.abspath(str(tmp_path))

    def test_copilot_cowork_skills_dir_set_is_gated(self, runner):
        """apm config set copilot-cowork-skills-dir exits 1 when the copilot-cowork flag is off."""
        result = runner.invoke(config, ["set", "copilot-cowork-skills-dir", "/some/path"])
        assert result.exit_code == 1


class TestValidConfigKeys:
    """Tests for _valid_config_keys() feature-flag gating."""

    def test_valid_config_keys_excludes_cowork_when_flag_off(self):
        """copilot-cowork-skills-dir is hidden when the copilot_cowork flag is off."""
        from apm_cli.commands.config import _valid_config_keys

        result = _valid_config_keys()

        assert "auto-integrate" not in result
        assert "temp-dir" in result
        assert "copilot-cowork-skills-dir" not in result
        # Transport keys are listed regardless of feature flags.
        assert "allow-protocol-fallback" in result
        assert "ssh" in result

    def test_valid_config_keys_includes_cowork_when_flag_on(self, isolated_config):
        """copilot-cowork-skills-dir is listed when the copilot_cowork flag is on."""
        from apm_cli.commands.config import _valid_config_keys

        _enable(isolated_config, "copilot_cowork")
        result = _valid_config_keys()

        assert "auto-integrate" not in result
        assert "temp-dir" in result
        assert "copilot-cowork-skills-dir" in result


class TestConfigKeyRegistry:
    """The valid-key registry and the get/set/unset dispatch must agree."""

    @staticmethod
    def _concrete(name: str) -> str:
        from apm_cli.security.external.registry import SUPPORTED_SCANNERS

        placeholder = sorted(SUPPORTED_SCANNERS)[0] if name.startswith("external.") else "corp"
        return name.replace("<name>", placeholder)

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))

    @pytest.mark.parametrize("operation", ["get", "set", "unset"])
    def test_unknown_key_is_a_plain_user_error(self, runner, operation):
        args = [operation, "no-such-key"] + (["x"] if operation == "set" else [])
        result = runner.invoke(config, args)

        assert result.exit_code == 1
        assert "Unknown configuration key: 'no-such-key'" in result.output
        assert "command routing" not in result.output
        assert "github-auth-first" in result.output

    def test_every_registered_key_is_dispatched(self, runner, isolated_config):
        from apm_cli.commands.config import _CONFIG_KEYS
        from apm_cli.core.experimental import FLAGS

        for name, _flag, unsettable in _CONFIG_KEYS:
            _enable(isolated_config, *FLAGS)
            key = self._concrete(name)
            operations = [["get", key], ["set", key, "true"]]
            if unsettable:
                operations.append(["unset", key])
            for args in operations:
                result = runner.invoke(config, args)
                assert "Unknown configuration key" not in result.output, (args, result.output)
        rejected = runner.invoke(config, ["unset", "auto-integrate"])

        assert "Unknown configuration key: 'auto-integrate'" in rejected.output
        valid = rejected.output.split("Valid keys:", 1)[1]
        assert "auto-integrate" not in valid
        assert "registry.<name>.default" in valid


# ---------------------------------------------------------------------------
# Storage layer -- boolean transport preferences
# ---------------------------------------------------------------------------

_BOOLEAN_KEYS = [
    pytest.param(
        "allow_protocol_fallback", "allow-protocol-fallback", id="allow-protocol-fallback"
    ),
    pytest.param("prefer_ssh", "prefer-ssh", id="prefer-ssh"),
    pytest.param("github_auth_first", "github-auth-first", id="github-auth-first"),
]


@pytest.mark.parametrize(("stored_key", "_cli_key"), _BOOLEAN_KEYS)
class TestBooleanPreferenceFunctions:
    """get_<key>/set_<key> for allow_protocol_fallback, prefer_ssh and github_auth_first."""

    def test_default_is_false(self, stored_key, _cli_key):
        """Default value is False when not set."""
        assert getattr(cfg_module, f"get_{stored_key}")() is False

    def test_reads_true_from_config(self, isolated_config, stored_key, _cli_key):
        """Returns True when config.json stores True."""
        write_config(isolated_config, {stored_key: True})
        assert getattr(cfg_module, f"get_{stored_key}")() is True

    @pytest.mark.parametrize("enabled", [True, False])
    def test_set_persists(self, isolated_config, stored_key, _cli_key, enabled):
        """set_<key> writes the value to config.json."""
        getattr(cfg_module, f"set_{stored_key}")(enabled)
        assert read_config(isolated_config)[stored_key] is enabled
        assert getattr(cfg_module, f"get_{stored_key}")() is enabled


# ---------------------------------------------------------------------------
# Effective-value helpers (env > config > default)
# ---------------------------------------------------------------------------

_ENV_RESOLUTION = [
    pytest.param(False, "1", True, id="env-1-wins-over-config"),
    pytest.param(False, "true", True, id="env-true-wins"),
    pytest.param(True, None, True, id="config-used-when-env-unset"),
    pytest.param(False, None, False, id="false-when-both-unset"),
    pytest.param(True, "0", False, id="env-0-overrides-config-true"),
    pytest.param(True, "", True, id="empty-env-falls-through"),
]


@pytest.mark.parametrize(("stored", "env", "expected"), _ENV_RESOLUTION)
@pytest.mark.parametrize(
    ("stored_key", "env_name", "resolver"),
    [
        pytest.param(
            "allow_protocol_fallback",
            "APM_ALLOW_PROTOCOL_FALLBACK",
            "get_apm_allow_protocol_fallback",
            id="allow-protocol-fallback",
        ),
        pytest.param(
            "github_auth_first",
            "APM_GITHUB_AUTH_FIRST",
            "get_apm_github_auth_first",
            id="github-auth-first",
        ),
    ],
)
def test_effective_boolean_resolution(
    isolated_config, monkeypatch, stored_key, env_name, resolver, stored, env, expected
):
    """The environment variable beats config.json; an empty value falls through."""
    write_config(isolated_config, {stored_key: stored})
    if env is not None:
        monkeypatch.setenv(env_name, env)
    assert getattr(cfg_module, resolver)() is expected


class TestGetApmProtocolPref:
    """Tests for get_apm_protocol_pref resolution chain."""

    @pytest.mark.parametrize(
        ("prefer_ssh", "env", "expected"),
        [
            pytest.param(False, "ssh", "ssh", id="env-ssh-wins"),
            pytest.param(True, "https", "https", id="env-https-wins"),
            pytest.param(True, None, "ssh", id="config-used-when-env-unset"),
            pytest.param(False, None, None, id="none-when-both-unset"),
            pytest.param(True, "git", "ssh", id="invalid-env-ignored"),
        ],
    )
    def test_resolution(self, isolated_config, monkeypatch, prefer_ssh, env, expected):
        write_config(isolated_config, {"prefer_ssh": prefer_ssh})
        if env is not None:
            monkeypatch.setenv("APM_GIT_PROTOCOL", env)
        assert cfg_module.get_apm_protocol_pref() == expected


# ---------------------------------------------------------------------------
# CLI -- apm config set/get for the boolean transport keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("stored_key", "cli_key"), _BOOLEAN_KEYS)
class TestConfigSetBooleanKey:
    """`apm config set <key> <value>` for allow-protocol-fallback, prefer-ssh, github-auth-first."""

    @pytest.mark.parametrize(
        ("value", "stored"),
        [
            ("true", True),
            ("false", False),
            ("yes", True),
            ("0", False),
            ("TRUE", True),
            ("1", True),
            ("no", False),
        ],
    )
    def test_set_stores_value(self, runner, isolated_config, stored_key, cli_key, value, stored):
        """Every boolean alias is accepted, case-insensitively, without a feature flag."""
        result = runner.invoke(config, ["set", cli_key, value])
        assert result.exit_code == 0
        assert read_config(isolated_config)[stored_key] is stored

    def test_set_invalid_value(self, runner, isolated_config, stored_key, cli_key):
        """Reject an invalid value."""
        result = runner.invoke(config, ["set", cli_key, "maybe"])
        assert result.exit_code == 1
        assert stored_key not in read_config(isolated_config)

    def test_unknown_key_error_lists_key_as_valid(self, runner, stored_key, cli_key):
        """Error listings for unknown keys name the key as valid (get and set)."""
        for args in (["get", "nonexistent"], ["set", "nonexistent", "true"]):
            result = runner.invoke(config, args)
            assert result.exit_code == 1
            assert cli_key in result.output


@pytest.mark.parametrize(("stored_key", "cli_key"), _BOOLEAN_KEYS)
class TestConfigGetBooleanKey:
    """`apm config get [<key>]` for allow-protocol-fallback, prefer-ssh, github-auth-first."""

    @pytest.mark.parametrize(("stored", "shown"), [(True, "true"), (False, "false")])
    def test_get_key(self, runner, isolated_config, stored_key, cli_key, stored, shown):
        """Display the configured value."""
        write_config(isolated_config, {stored_key: stored})
        result = runner.invoke(config, ["get", cli_key])
        assert result.exit_code == 0
        assert f"{cli_key}: {shown}" in result.output

    def test_get_all_config_includes_key_when_true(
        self, runner, isolated_config, stored_key, cli_key
    ):
        """apm config get (no key) shows the key only when true."""
        write_config(isolated_config, {stored_key: True})
        result = runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert cli_key in result.output

    def test_get_all_config_suppresses_key_when_false(
        self, runner, isolated_config, stored_key, cli_key
    ):
        """apm config get (no key) omits the key when false (noise reduction)."""
        write_config(isolated_config, {stored_key: False})
        result = runner.invoke(config, ["get"])
        assert result.exit_code == 0
        assert cli_key not in result.output


class TestConfigShowTempDir:
    """`apm config` (rich table) with temp-dir and the copilot cowork dir."""

    @pytest.fixture(autouse=True)
    def _cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    def test_show_with_temp_dir_set(self, runner, isolated_config):
        """temp_dir is set -- shown in the config table."""
        write_config(isolated_config, {"temp_dir": "/tmp/apm-temp"})
        with patch("apm_cli.commands.config.get_version", return_value="1.0.0"):
            result = runner.invoke(config, [])
        assert result.exit_code == 0
        assert "/tmp/apm-temp" in result.output
        assert "Temp Directory" in result.output

    def test_show_with_copilot_cowork_dir_set(self, runner, isolated_config):
        """copilot_cowork enabled + dir set -- shown."""
        _enable(isolated_config, "copilot_cowork", copilot_cowork_skills_dir="/some/skills")
        with patch("apm_cli.commands.config.get_version", return_value="1.0.0"):
            result = runner.invoke(config, [])
        assert result.exit_code == 0
        assert "/some/skills" in result.output

    def test_show_with_copilot_cowork_dir_not_set(self, runner, isolated_config):
        """copilot_cowork enabled + dir NOT set -- auto-detection message."""
        _enable(isolated_config, "copilot_cowork")
        with patch("apm_cli.commands.config.get_version", return_value="1.0.0"):
            result = runner.invoke(config, [])
        assert result.exit_code == 0
        assert "auto-detection" in flat(result.output)


class TestAuditOnInstallCommand:
    """`apm config set/get/unset audit-on-install` (flag-gated)."""

    def test_set_blocked_without_flag(self, runner, isolated_config):
        result = runner.invoke(config, ["set", "audit-on-install", "warn"])
        assert result.exit_code == 1
        assert "external-scanners experimental flag" in result.output
        assert "audit_on_install" not in read_config(isolated_config)

    def test_set_allowed_with_flag(self, runner, isolated_config):
        _enable(isolated_config, "external_scanners")
        result = runner.invoke(config, ["set", "audit-on-install", "warn"])
        assert result.exit_code == 0
        assert read_config(isolated_config)["audit_on_install"] == "warn"

    def test_get_audit_on_install(self, runner, isolated_config):
        write_config(isolated_config, {"audit_on_install": "block"})
        result = runner.invoke(config, ["get", "audit-on-install"])
        assert result.exit_code == 0
        assert "block" in result.output

    def test_unset_audit_on_install(self, runner, isolated_config):
        write_config(isolated_config, {"audit_on_install": "block"})
        result = runner.invoke(config, ["unset", "audit-on-install"])
        assert result.exit_code == 0
        assert "audit_on_install" not in read_config(isolated_config)


class TestExternalScannerConfigCommand:
    """`apm config set/get/unset external.<name>.{llm,args}` (flag-gated)."""

    def _scanners(self, path: Path) -> dict:
        return read_config(path).get("external_scanners", {})

    def test_set_llm_blocked_without_flag(self, runner, isolated_config):
        result = runner.invoke(config, ["set", "external.skillspector.llm", "true"])
        assert result.exit_code == 1
        assert "external-scanners experimental" in result.output
        assert self._scanners(isolated_config) == {}

    def test_set_llm_with_flag(self, runner, isolated_config):
        _enable(isolated_config, "external_scanners")
        result = runner.invoke(config, ["set", "external.skillspector.llm", "true"])
        assert result.exit_code == 0
        assert self._scanners(isolated_config) == {"skillspector": {"llm": True}}

    def test_set_args_shlex_split(self, runner, isolated_config):
        _enable(isolated_config, "external_scanners")
        result = runner.invoke(
            config, ["set", "external.skillspector.args", "--", "--model gpt-4o"]
        )
        assert result.exit_code == 0
        assert self._scanners(isolated_config) == {"skillspector": {"args": ["--model", "gpt-4o"]}}

    def test_set_args_empty_string_rejected(self, runner, isolated_config):
        _enable(isolated_config, "external_scanners")
        result = runner.invoke(config, ["set", "external.skillspector.args", "--", "   "])
        assert result.exit_code == 1
        assert self._scanners(isolated_config) == {}

    def test_set_unknown_scanner_rejected(self, runner, isolated_config):
        _enable(isolated_config, "external_scanners")
        result = runner.invoke(config, ["set", "external.bogus.llm", "true"])
        assert result.exit_code == 1
        assert "Unknown external scanner" in result.output
        assert self._scanners(isolated_config) == {}

    def test_get_llm(self, runner, isolated_config):
        _enable(
            isolated_config,
            "external_scanners",
            external_scanners={"skillspector": {"llm": True}},
        )
        result = runner.invoke(config, ["get", "external.skillspector.llm"])
        assert result.exit_code == 0
        assert "true" in result.output

    def test_get_args_not_set(self, runner, isolated_config):
        _enable(isolated_config, "external_scanners")
        result = runner.invoke(config, ["get", "external.skillspector.args"])
        assert result.exit_code == 0
        assert "Not set" in result.output

    def test_unset_llm(self, runner, isolated_config):
        _enable(
            isolated_config,
            "external_scanners",
            external_scanners={"skillspector": {"llm": True, "args": ["-v"]}},
        )
        result = runner.invoke(config, ["unset", "external.skillspector.llm"])
        assert result.exit_code == 0
        assert self._scanners(isolated_config) == {"skillspector": {"args": ["-v"]}}


class TestMcpRegistryUrlCommand:
    """`apm config set/get/unset mcp-registry-url` -- issue #818."""

    def test_set_valid_https_url(self, runner, isolated_config):
        result = runner.invoke(config, ["set", "mcp-registry-url", "https://corp.mcp.example.com"])
        assert result.exit_code == 0
        assert read_config(isolated_config)["mcp_registry_url"] == "https://corp.mcp.example.com"
        from urllib.parse import urlparse

        urls = [tok for tok in result.output.split() if "://" in tok]
        assert len(urls) >= 1
        assert urlparse(urls[0]).hostname == "corp.mcp.example.com"

    def test_set_valid_http_url(self, runner, isolated_config):
        result = runner.invoke(config, ["set", "mcp-registry-url", "http://internal.corp/mcp"])
        assert result.exit_code == 0
        assert read_config(isolated_config)["mcp_registry_url"] == "http://internal.corp/mcp"
        # Config-sourced URLs are exempt from the env-var gate; do not point at it.
        assert "MCP_REGISTRY_ALLOW_HTTP" not in result.output
        assert "[!]" in result.output
        assert "plaintext HTTP" in flat(result.output)

    def test_set_invalid_scheme_rejected(self, runner, isolated_config):
        result = runner.invoke(config, ["set", "mcp-registry-url", "file:///etc/hosts"])
        assert result.exit_code == 1
        assert "not supported" in flat(result.output) or "file" in result.output
        assert "mcp_registry_url" not in read_config(isolated_config)

    def test_get_when_set(self, runner, isolated_config):
        write_config(isolated_config, {"mcp_registry_url": "https://corp.mcp.example.com"})
        result = runner.invoke(config, ["get", "mcp-registry-url"])
        assert result.exit_code == 0
        from urllib.parse import urlparse

        urls = [tok for tok in result.output.split() if "://" in tok]
        assert len(urls) >= 1
        assert urlparse(urls[0]).hostname == "corp.mcp.example.com"

    def test_get_when_not_set(self, runner):
        result = runner.invoke(config, ["get", "mcp-registry-url"])
        assert result.exit_code == 0
        assert "Not set" in result.output

    def test_unset_removes_key(self, runner, isolated_config):
        write_config(isolated_config, {"mcp_registry_url": "https://corp.mcp.example.com"})
        result = runner.invoke(config, ["unset", "mcp-registry-url"])
        assert result.exit_code == 0
        assert "mcp_registry_url" not in read_config(isolated_config)
        assert "removed" in result.output
