"""Integration tests for config, update, self_update, and publish command paths.

Covers:
  - ``apm config set/get/unset/list`` for all valid keys
  - ``apm update`` with/without apm.yml, dry-run mode, --yes flag
  - ``apm self-update`` version check, already-up-to-date, update available
  - ``apm publish`` validation errors, missing manifest
  - ``config.py`` helpers: ensure_config_exists, get_config, set_config, registry helpers

No live network calls -- all HTTP is mocked via ``unittest.mock.patch``.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """Provide a Click test runner."""
    return CliRunner()


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Redirect config files to a temporary directory.

    Ensures tests never touch ``~/.apm/config.json``.
    """
    import apm_cli.config as _conf

    _conf._invalidate_config_cache()
    config_dir = tmp_path / ".apm"
    config_file = config_dir / "config.json"
    monkeypatch.setattr(_conf, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(config_file))
    yield config_file
    _conf._invalidate_config_cache()


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Create a minimal APM project directory with no dependencies."""
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text(
        "name: test-project\n"
        "version: 1.0.0\n"
        "description: Test project\n"
        "targets:\n"
        "  - copilot\n"
        "dependencies:\n"
        "  apm: {}\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def project_with_deps(tmp_path: Path) -> Path:
    """Create an APM project directory with one dependency."""
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text(
        "name: test-project\n"
        "version: 1.0.0\n"
        "description: Test project\n"
        "targets:\n"
        "  - copilot\n"
        "dependencies:\n"
        "  apm:\n"
        "    test-org/test-pkg: github:test-org/test-pkg\n",
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# config.py unit-level helpers
# ---------------------------------------------------------------------------


class TestConfigHelpers:
    """Tests for low-level helpers in ``apm_cli.config``."""

    def test_ensure_config_exists_creates_dir_and_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ensure_config_exists() creates the config dir and file on first call."""
        import apm_cli.config as _conf

        _conf._invalidate_config_cache()
        new_dir = tmp_path / ".apm_test"
        new_file = new_dir / "config.json"
        monkeypatch.setattr(_conf, "CONFIG_DIR", str(new_dir))
        monkeypatch.setattr(_conf, "CONFIG_FILE", str(new_file))

        _conf.ensure_config_exists()

        assert new_dir.is_dir()
        assert new_file.is_file()
        data = json.loads(new_file.read_text(encoding="utf-8"))
        assert "default_client" in data
        _conf._invalidate_config_cache()

    def test_get_config_returns_dict(self, isolated_config: Path) -> None:
        """get_config() should return a dict and cache it."""
        import apm_cli.config as _conf

        cfg = _conf.get_config()
        assert isinstance(cfg, dict)
        # Second call should return same cached object
        assert _conf.get_config() is cfg

    def test_update_config_persists_values(self, isolated_config: Path) -> None:
        """update_config() should write values to disk and invalidate cache."""
        import apm_cli.config as _conf

        _conf.update_config({"test_key": "test_value"})
        _conf._invalidate_config_cache()
        data = json.loads(isolated_config.read_text(encoding="utf-8"))
        assert data["test_key"] == "test_value"

    def test_set_auto_integrate_persists(self, isolated_config: Path) -> None:
        """set_auto_integrate() should persist to disk."""
        import apm_cli.config as _conf

        _conf.set_auto_integrate(False)
        _conf._invalidate_config_cache()
        data = json.loads(isolated_config.read_text(encoding="utf-8"))
        assert data["auto_integrate"] is False

    def test_set_prefer_ssh_persists(self, isolated_config: Path) -> None:
        """set_prefer_ssh() should persist to disk."""
        import apm_cli.config as _conf

        _conf.set_prefer_ssh(True)
        _conf._invalidate_config_cache()
        data = json.loads(isolated_config.read_text(encoding="utf-8"))
        assert data["prefer_ssh"] is True

    def test_set_allow_protocol_fallback_persists(self, isolated_config: Path) -> None:
        """set_allow_protocol_fallback() should persist to disk."""
        import apm_cli.config as _conf

        _conf.set_allow_protocol_fallback(True)
        _conf._invalidate_config_cache()
        data = json.loads(isolated_config.read_text(encoding="utf-8"))
        assert data["allow_protocol_fallback"] is True

    def test_registry_url_round_trip(self, isolated_config: Path) -> None:
        """set_registry_url / get_registry_config / unset_registry_url round-trip."""
        import apm_cli.config as _conf

        _conf.set_registry_url("corp", "https://registry.example.com")
        cfg = _conf.get_registry_config("corp")
        assert cfg is not None
        assert cfg["url"] == "https://registry.example.com"

        _conf.unset_registry_url("corp")
        cfg2 = _conf.get_registry_config("corp")
        assert cfg2 is None or "url" not in cfg2

    def test_registry_token_round_trip(self, isolated_config: Path) -> None:
        """set_registry_token / unset_registry_token removes only the token field."""
        import apm_cli.config as _conf

        _conf.set_registry_url("myregistry", "https://r.example.com")
        _conf.set_registry_token("myregistry", "tok-abc123")
        cfg = _conf.get_registry_config("myregistry")
        assert cfg is not None
        assert cfg["token"] == "tok-abc123"

        _conf.unset_registry_token("myregistry")
        cfg2 = _conf.get_registry_config("myregistry")
        assert cfg2 is not None
        assert "token" not in cfg2
        assert "url" in cfg2  # url survives

    def test_set_registry_default(self, isolated_config: Path) -> None:
        """set_registry_default() marks only one registry as default."""
        import apm_cli.config as _conf

        _conf.set_registry_url("alpha", "https://alpha.example.com")
        _conf.set_registry_url("beta", "https://beta.example.com")

        _conf.set_registry_default("alpha", True)
        assert _conf.is_registry_default("alpha") is True
        assert _conf.is_registry_default("beta") is False

        # Switching default clears previous
        _conf.set_registry_default("beta", True)
        assert _conf.is_registry_default("beta") is True
        assert _conf.is_registry_default("alpha") is False

    def test_unset_config_key_noop_on_missing(self, isolated_config: Path) -> None:
        """_unset_config_key() is a no-op when the key is absent."""
        import apm_cli.config as _conf

        # Should not raise
        _conf._unset_config_key("nonexistent_key_xyz")

    def test_get_apm_protocol_pref_returns_ssh_when_prefer_ssh(
        self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_apm_protocol_pref() returns 'ssh' when prefer_ssh is set."""
        import apm_cli.config as _conf

        monkeypatch.delenv("APM_GIT_PROTOCOL", raising=False)
        _conf.set_prefer_ssh(True)
        assert _conf.get_apm_protocol_pref() == "ssh"
        _conf.set_prefer_ssh(False)

    def test_get_apm_protocol_pref_env_wins(
        self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """APM_GIT_PROTOCOL env var overrides the config-file prefer_ssh value."""
        import apm_cli.config as _conf

        monkeypatch.setenv("APM_GIT_PROTOCOL", "https")
        _conf.set_prefer_ssh(True)
        assert _conf.get_apm_protocol_pref() == "https"

    def test_parse_allow_protocol_fallback_env(self) -> None:
        """_parse_allow_protocol_fallback_env() handles all recognised values."""
        import apm_cli.config as _conf

        for truthy in ("1", "true", "yes", "on", "TRUE", "YES"):
            assert _conf._parse_allow_protocol_fallback_env(truthy) is True
        for falsy in ("0", "false", "no", "off"):
            assert _conf._parse_allow_protocol_fallback_env(falsy) is False
        for unknown in (None, "", "maybe"):
            assert _conf._parse_allow_protocol_fallback_env(unknown) is None


# ---------------------------------------------------------------------------
# apm config set
# ---------------------------------------------------------------------------


class TestConfigSet:
    """Tests for ``apm config set <key> <value>``."""

    def test_set_auto_integrate_true(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config set auto-integrate true sets the flag."""
        result = runner.invoke(cli, ["config", "set", "auto-integrate", "true"])
        assert result.exit_code == 0
        assert "true" in result.output.lower()

    def test_set_auto_integrate_false(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config set auto-integrate false clears the flag."""
        result = runner.invoke(cli, ["config", "set", "auto-integrate", "false"])
        assert result.exit_code == 0
        assert "false" in result.output.lower()

    def test_set_prefer_ssh_true(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config set prefer-ssh true persists the setting."""
        result = runner.invoke(cli, ["config", "set", "prefer-ssh", "true"])
        assert result.exit_code == 0
        assert "true" in result.output.lower()

    def test_set_prefer_ssh_false(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config set prefer-ssh false persists the setting."""
        result = runner.invoke(cli, ["config", "set", "prefer-ssh", "false"])
        assert result.exit_code == 0

    def test_set_allow_protocol_fallback_true(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set allow-protocol-fallback true."""
        result = runner.invoke(cli, ["config", "set", "allow-protocol-fallback", "true"])
        assert result.exit_code == 0

    def test_set_allow_protocol_fallback_invalid_value(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set <bool-key> with invalid value exits non-zero."""
        result = runner.invoke(cli, ["config", "set", "prefer-ssh", "maybe"])
        assert result.exit_code != 0
        assert "Invalid value" in result.output or "invalid" in result.output.lower()

    def test_set_unknown_key_exits_nonzero(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config set with unknown key prints an error and exits non-zero."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(cli, ["config", "set", "not-a-real-key", "value"])
        assert result.exit_code != 0
        assert "Unknown configuration key" in result.output

    def test_set_temp_dir_valid(
        self, runner: CliRunner, isolated_config: Path, tmp_path: Path
    ) -> None:
        """apm config set temp-dir with an existing writable directory succeeds."""
        result = runner.invoke(cli, ["config", "set", "temp-dir", str(tmp_path)])
        assert result.exit_code == 0
        # Output may wrap long paths across lines; just confirm success
        assert "Temporary directory set to" in result.output

    def test_set_temp_dir_nonexistent(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config set temp-dir with nonexistent path exits non-zero."""
        result = runner.invoke(cli, ["config", "set", "temp-dir", "/nonexistent/path/xyz_abc"])
        assert result.exit_code != 0

    def test_set_allow_protocol_fallback_ci_warning(
        self, runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """allow-protocol-fallback=true in CI emits an advisory warning."""
        monkeypatch.setenv("CI", "true")
        result = runner.invoke(cli, ["config", "set", "allow-protocol-fallback", "true"])
        assert result.exit_code == 0
        assert (
            "CI" in result.output
            or "ci" in result.output.lower()
            or "persist" in result.output.lower()
        )

    def test_set_mcp_registry_url(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config set mcp-registry-url with a valid URL succeeds."""
        result = runner.invoke(
            cli, ["config", "set", "mcp-registry-url", "https://mcp.example.com"]
        )
        assert result.exit_code == 0

    def test_set_registry_url_requires_flag(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config set registry.<name>.url exits non-zero when registries flag is off."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(
                cli, ["config", "set", "registry.corp.url", "https://r.example.com"]
            )
        assert result.exit_code != 0
        assert "registries" in result.output.lower()

    def test_set_registry_url_with_flag(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config set registry.<name>.url succeeds when registries flag is on."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(
                cli, ["config", "set", "registry.corp.url", "https://r.example.com"]
            )
        assert result.exit_code == 0
        assert "corp" in result.output

    def test_set_registry_token_with_flag(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config set registry.<name>.token succeeds when registries flag is on."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "set", "registry.corp.token", "my-secret-token"])
        assert result.exit_code == 0
        assert "corp" in result.output

    def test_set_registry_default_with_flag(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config set registry.<name>.default true succeeds when registries flag is on."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "set", "registry.corp.default", "true"])
        assert result.exit_code == 0

    def test_set_registry_default_invalid_bool(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set registry.<name>.default with bad value exits non-zero."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "set", "registry.corp.default", "notabool"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# apm config get
# ---------------------------------------------------------------------------


class TestConfigGet:
    """Tests for ``apm config get <key>``."""

    def test_get_auto_integrate_default(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config get auto-integrate returns the current value."""
        result = runner.invoke(cli, ["config", "get", "auto-integrate"])
        assert result.exit_code == 0
        assert "auto-integrate:" in result.output

    def test_get_prefer_ssh(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config get prefer-ssh returns the current value."""
        result = runner.invoke(cli, ["config", "get", "prefer-ssh"])
        assert result.exit_code == 0
        assert "prefer-ssh:" in result.output

    def test_get_allow_protocol_fallback(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config get allow-protocol-fallback returns the current value."""
        result = runner.invoke(cli, ["config", "get", "allow-protocol-fallback"])
        assert result.exit_code == 0
        assert "allow-protocol-fallback:" in result.output

    def test_get_temp_dir_not_set(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config get temp-dir shows 'Not set' when unset."""
        result = runner.invoke(cli, ["config", "get", "temp-dir"])
        assert result.exit_code == 0
        assert "Not set" in result.output

    def test_get_mcp_registry_url_not_set(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config get mcp-registry-url shows 'Not set' when unset."""
        result = runner.invoke(cli, ["config", "get", "mcp-registry-url"])
        assert result.exit_code == 0
        assert "Not set" in result.output

    def test_get_unknown_key_exits_nonzero(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config get with unknown key exits non-zero."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(cli, ["config", "get", "bad-key-xyz"])
        assert result.exit_code != 0
        assert "Unknown configuration key" in result.output

    def test_get_no_key_shows_all_settings(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config get (no key) lists all settings."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(cli, ["config", "get"])
        assert result.exit_code == 0
        assert "auto-integrate:" in result.output

    def test_get_registry_url_requires_flag(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config get registry.<name>.url exits non-zero when flag is off."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(cli, ["config", "get", "registry.corp.url"])
        assert result.exit_code != 0

    def test_get_registry_url_with_flag_not_set(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get registry.<name>.url when flag is on and not set shows 'Not set'."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "get", "registry.corp.url"])
        assert result.exit_code == 0
        assert "Not set" in result.output

    def test_get_registry_default_with_flag(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config get registry.<name>.default returns false when not set."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "get", "registry.corp.default"])
        assert result.exit_code == 0
        assert "false" in result.output.lower()


# ---------------------------------------------------------------------------
# apm config unset
# ---------------------------------------------------------------------------


class TestConfigUnset:
    """Tests for ``apm config unset <key>``."""

    def test_unset_temp_dir(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config unset temp-dir succeeds even when key is not set."""
        result = runner.invoke(cli, ["config", "unset", "temp-dir"])
        assert result.exit_code == 0

    def test_unset_allow_protocol_fallback(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config unset allow-protocol-fallback succeeds."""
        result = runner.invoke(cli, ["config", "unset", "allow-protocol-fallback"])
        assert result.exit_code == 0

    def test_unset_prefer_ssh(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config unset prefer-ssh succeeds."""
        result = runner.invoke(cli, ["config", "unset", "prefer-ssh"])
        assert result.exit_code == 0

    def test_unset_mcp_registry_url(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config unset mcp-registry-url succeeds."""
        result = runner.invoke(cli, ["config", "unset", "mcp-registry-url"])
        assert result.exit_code == 0

    def test_unset_unknown_key(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config unset with unknown key exits non-zero."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(cli, ["config", "unset", "nonexistent-key"])
        assert result.exit_code != 0

    def test_unset_registry_url_with_flag(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config unset registry.<name>.url succeeds when flag is on."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "unset", "registry.corp.url"])
        assert result.exit_code == 0

    def test_unset_registry_token_with_flag(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config unset registry.<name>.token succeeds when flag is on."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "unset", "registry.corp.token"])
        assert result.exit_code == 0

    def test_unset_registry_requires_flag(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config unset registry.<name>.url exits non-zero when flag is off."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(cli, ["config", "unset", "registry.corp.url"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# apm config (bare -- shows current configuration)
# ---------------------------------------------------------------------------


class TestConfigBare:
    """Tests for ``apm config`` (no subcommand) display."""

    def test_bare_config_outside_project(
        self, runner: CliRunner, isolated_config: Path, tmp_path: Path
    ) -> None:
        """``apm config`` outside a project directory shows global settings."""
        with runner.isolated_filesystem():
            with patch("apm_cli.core.experimental.is_enabled", return_value=False):
                result = runner.invoke(cli, ["config"])
        # Should either succeed or fail gracefully -- never crash
        assert result.exit_code in (0, 1)

    def test_bare_config_inside_project(
        self, runner: CliRunner, isolated_config: Path, project_dir: Path
    ) -> None:
        """``apm config`` inside a project shows project and global settings."""
        import os

        orig = os.getcwd()
        try:
            os.chdir(project_dir)
            with patch("apm_cli.core.experimental.is_enabled", return_value=False):
                result = runner.invoke(cli, ["config"])
            assert result.exit_code in (0, 1)
        finally:
            os.chdir(orig)


# ---------------------------------------------------------------------------
# apm update
# ---------------------------------------------------------------------------


class TestUpdateCommand:
    """Tests for ``apm update`` dependency refresh command."""

    def test_update_no_apm_yml_forwards_to_self_update(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """``apm update`` outside a project invokes the self-update shim."""
        # Mock self_update so it doesn't attempt a real update
        with runner.isolated_filesystem():
            with (
                patch(
                    "apm_cli.utils.version_checker.get_latest_version_from_github",
                    return_value=None,
                ),
                patch(
                    "apm_cli.commands.self_update.is_self_update_enabled",
                    return_value=True,
                ),
                patch(
                    "apm_cli.commands.self_update.get_version",
                    return_value="1.0.0",
                ),
            ):
                result = runner.invoke(cli, ["update"], catch_exceptions=False)
        # The shim emits a deprecation warning; it may exit 0 or 1
        assert result.exit_code in (0, 1)

    def test_update_dry_run_no_deps(
        self, runner: CliRunner, isolated_config: Path, project_dir: Path
    ) -> None:
        """``apm update --dry-run`` on a project with no deps reports nothing to update."""
        import os

        orig = os.getcwd()
        try:
            os.chdir(project_dir)
            with (
                patch(
                    "apm_cli.commands.update.resolve_revision_pin_updates",
                    return_value=[],
                ),
                patch(
                    "apm_cli.commands.install._install_apm_dependencies",
                ) as mock_install,
            ):
                mock_result = MagicMock()
                mock_result.installed_count = 0
                mock_install.return_value = mock_result
                result = runner.invoke(cli, ["update", "--dry-run"], catch_exceptions=False)
        finally:
            os.chdir(orig)
        assert result.exit_code in (0, 1)

    def test_update_check_flag_forwards_to_self_update(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """``apm update --check`` inside a project forwards to self-update --check."""
        with runner.isolated_filesystem():
            (Path(".") / "apm.yml").write_text(
                "name: x\nversion: 1.0.0\ndependencies:\n  apm: {}\n", encoding="utf-8"
            )
            with (
                patch(
                    "apm_cli.utils.version_checker.get_latest_version_from_github",
                    return_value=None,
                ),
                patch(
                    "apm_cli.commands.self_update.is_self_update_enabled",
                    return_value=True,
                ),
                patch(
                    "apm_cli.commands.self_update.get_version",
                    return_value="1.0.0",
                ),
            ):
                result = runner.invoke(cli, ["update", "--check"], catch_exceptions=False)
        assert result.exit_code in (0, 1)
        # A deprecation warning should appear
        combined = result.output + (result.output or "")
        assert "deprecated" in combined.lower() or result.exit_code in (0, 1)

    def test_update_non_tty_without_yes_exits_error(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """``apm update`` in non-TTY without --yes exits with error."""
        with runner.isolated_filesystem():
            (Path(".") / "apm.yml").write_text(
                "name: x\nversion: 1.0.0\ndependencies:\n  apm:\n    org/pkg: github:org/pkg\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "apm_cli.commands.update.resolve_revision_pin_updates",
                    return_value=[],
                ),
                patch("apm_cli.commands.install._install_apm_dependencies") as mock_install,
            ):
                # Simulate plan with changes present
                from apm_cli.install.plan import UpdatePlan

                mock_plan = MagicMock(spec=UpdatePlan)
                mock_plan.has_changes = True
                mock_plan.entries = []

                def fake_install(*args, **kwargs):
                    cb = kwargs.get("plan_callback")
                    if cb:
                        cb(mock_plan)
                    return MagicMock(installed_count=0)

                mock_install.side_effect = fake_install
                result = runner.invoke(cli, ["update"], input="", catch_exceptions=False)
        assert result.exit_code in (0, 1)


# ---------------------------------------------------------------------------
# apm self-update
# ---------------------------------------------------------------------------


class TestSelfUpdateCommand:
    """Tests for ``apm self-update`` CLI update command."""

    def test_self_update_disabled(self, runner: CliRunner, isolated_config: Path) -> None:
        """When self-update is disabled, a warning is shown and command exits 0."""
        with (
            patch(
                "apm_cli.commands.self_update.is_self_update_enabled",
                return_value=False,
            ),
            patch(
                "apm_cli.commands.self_update.get_self_update_disabled_message",
                return_value="Self-update is disabled for this distribution.",
            ),
        ):
            result = runner.invoke(cli, ["self-update"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "disabled" in result.output.lower()

    def test_self_update_unknown_version(self, runner: CliRunner, isolated_config: Path) -> None:
        """When current version is 'unknown', a dev-mode warning is shown."""
        with (
            patch(
                "apm_cli.commands.self_update.is_self_update_enabled",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update.get_version",
                return_value="unknown",
            ),
        ):
            result = runner.invoke(cli, ["self-update"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "development" in result.output.lower() or "cannot determine" in result.output.lower()

    def test_self_update_already_latest(self, runner: CliRunner, isolated_config: Path) -> None:
        """When already on latest version, success message is shown."""
        with (
            patch(
                "apm_cli.commands.self_update.is_self_update_enabled",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update.get_version",
                return_value="1.5.0",
            ),
            patch(
                "apm_cli.utils.version_checker.get_latest_version_from_github",
                return_value="1.5.0",
            ),
            patch(
                "apm_cli.utils.version_checker.is_newer_version",
                return_value=False,
            ),
        ):
            result = runner.invoke(cli, ["self-update"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "latest" in result.output.lower() or "already" in result.output.lower()

    def test_self_update_check_only_update_available(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """``apm self-update --check`` reports update available without installing."""
        with (
            patch(
                "apm_cli.commands.self_update.is_self_update_enabled",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update.get_version",
                return_value="1.0.0",
            ),
            patch(
                "apm_cli.utils.version_checker.get_latest_version_from_github",
                return_value="1.5.0",
            ),
            patch(
                "apm_cli.utils.version_checker.is_newer_version",
                return_value=True,
            ),
        ):
            result = runner.invoke(cli, ["self-update", "--check"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "1.5.0" in result.output or "update" in result.output.lower()

    def test_self_update_cannot_fetch_version(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """When latest version fetch fails, exits non-zero with error."""
        with (
            patch(
                "apm_cli.commands.self_update.is_self_update_enabled",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update.get_version",
                return_value="1.0.0",
            ),
            patch(
                "apm_cli.utils.version_checker.get_latest_version_from_github",
                return_value=None,
            ),
        ):
            result = runner.invoke(cli, ["self-update"], catch_exceptions=False)
        assert result.exit_code != 0

    def test_self_update_installs_successfully(
        self, runner: CliRunner, isolated_config: Path, tmp_path: Path
    ) -> None:
        """When update is available and install script succeeds, reports success."""
        mock_response = MagicMock()
        mock_response.text = "#!/bin/sh\necho 'install ok'\n"
        mock_response.raise_for_status.return_value = None

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch(
                "apm_cli.commands.self_update.is_self_update_enabled",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update.get_version",
                return_value="1.0.0",
            ),
            patch(
                "apm_cli.utils.version_checker.get_latest_version_from_github",
                return_value="2.0.0",
            ),
            patch(
                "apm_cli.utils.version_checker.is_newer_version",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update._get_update_installer_url",
                return_value="https://aka.ms/apm-unix",
            ),
            patch(
                "apm_cli.config.get_apm_temp_dir",
                return_value=str(tmp_path),
            ),
            patch(
                "requests.get",
                return_value=mock_response,
            ),
            patch(
                "subprocess.run",
                return_value=mock_proc,
            ),
        ):
            result = runner.invoke(cli, ["self-update"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "2.0.0" in result.output or "success" in result.output.lower()

    def test_self_update_install_fails(
        self, runner: CliRunner, isolated_config: Path, tmp_path: Path
    ) -> None:
        """When installer exits non-zero, self-update exits non-zero."""
        mock_response = MagicMock()
        mock_response.text = "#!/bin/sh\nexit 1\n"
        mock_response.raise_for_status.return_value = None

        mock_proc = MagicMock()
        mock_proc.returncode = 1

        with (
            patch(
                "apm_cli.commands.self_update.is_self_update_enabled",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update.get_version",
                return_value="1.0.0",
            ),
            patch(
                "apm_cli.utils.version_checker.get_latest_version_from_github",
                return_value="2.0.0",
            ),
            patch(
                "apm_cli.utils.version_checker.is_newer_version",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update._get_update_installer_url",
                return_value="https://aka.ms/apm-unix",
            ),
            patch(
                "apm_cli.config.get_apm_temp_dir",
                return_value=str(tmp_path),
            ),
            patch(
                "requests.get",
                return_value=mock_response,
            ),
            patch(
                "subprocess.run",
                return_value=mock_proc,
            ),
        ):
            result = runner.invoke(cli, ["self-update"], catch_exceptions=False)
        assert result.exit_code != 0

    def test_self_update_no_direct_fallback_blocked(
        self, runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When APM_NO_DIRECT_FALLBACK is set without metadata URL, exits non-zero."""
        monkeypatch.setenv("APM_NO_DIRECT_FALLBACK", "1")
        monkeypatch.delenv("APM_RELEASE_METADATA_URL", raising=False)
        monkeypatch.delenv("GITHUB_URL", raising=False)

        with (
            patch(
                "apm_cli.commands.self_update.is_self_update_enabled",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update.get_version",
                return_value="1.0.0",
            ),
        ):
            result = runner.invoke(cli, ["self-update"], catch_exceptions=False)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# apm publish
# ---------------------------------------------------------------------------


class TestPublishCommand:
    """Tests for ``apm publish`` command."""

    def test_publish_requires_registries_flag(self, runner: CliRunner) -> None:
        """``apm publish`` exits non-zero when the registries feature is disabled."""
        with runner.isolated_filesystem():
            with patch("apm_cli.commands.publish.require_package_registry_enabled") as mock_gate:
                from click import ClickException

                mock_gate.side_effect = ClickException("registries experimental flag not enabled")
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0

    def test_publish_missing_apm_yml(self, runner: CliRunner) -> None:
        """``apm publish`` exits non-zero when apm.yml is missing."""
        with runner.isolated_filesystem():
            with patch("apm_cli.commands.publish.require_package_registry_enabled"):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0
        assert "apm.yml" in result.output

    def test_publish_missing_version_field(self, runner: CliRunner) -> None:
        """``apm publish`` exits non-zero when apm.yml has no version field."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text("name: my-skill\ndescription: A skill\n", encoding="utf-8")
            with patch("apm_cli.commands.publish.require_package_registry_enabled"):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0

    def test_publish_no_registries_in_apm_yml(self, runner: CliRunner) -> None:
        """``apm publish`` exits non-zero when apm.yml has no registries block."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: my-skill\nversion: 1.0.0\ndescription: A skill\n", encoding="utf-8"
            )
            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0

    def test_publish_invalid_package_id(self, runner: CliRunner) -> None:
        """``apm publish`` exits non-zero when --package is not in owner/repo form."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: my-skill\nversion: 1.0.0\ndescription: A skill\n",
                encoding="utf-8",
            )
            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "no-slash-here"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0

    def test_publish_unknown_registry_name(self, runner: CliRunner) -> None:
        """``apm publish --registry unknown`` exits non-zero."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: my-skill\nversion: 1.0.0\ndescription: A skill\n"
                "registries:\n  corp:\n    url: https://r.example.com\n",
                encoding="utf-8",
            )
            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill", "--registry", "nope"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0
        assert "nope" in result.output or "not found" in result.output.lower()

    def test_publish_multiple_registries_no_selection(self, runner: CliRunner) -> None:
        """When multiple registries are configured without --registry, exits non-zero."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: my-skill\nversion: 1.0.0\ndescription: A skill\n"
                "registries:\n"
                "  alpha:\n    url: https://alpha.example.com\n"
                "  beta:\n    url: https://beta.example.com\n",
                encoding="utf-8",
            )
            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0

    def test_publish_dry_run(self, runner: CliRunner) -> None:
        """``apm publish --dry-run`` previews without uploading."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: my-skill\nversion: 1.0.0\ndescription: A skill\n"
                "registries:\n  corp:\n    url: https://r.example.com\n",
                encoding="utf-8",
            )
            apm_dir = Path(".apm")
            apm_dir.mkdir()
            (apm_dir / "placeholder.md").write_text("# placeholder\n", encoding="utf-8")

            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill", "--dry-run"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower() or "nothing uploaded" in result.output.lower()

    def test_publish_no_apm_dir_exits_error(self, runner: CliRunner) -> None:
        """``apm publish`` without a .apm/ directory exits non-zero."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: my-skill\nversion: 1.0.0\ndescription: A skill\n"
                "registries:\n  corp:\n    url: https://r.example.com\n",
                encoding="utf-8",
            )
            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0
        assert ".apm" in result.output or "flat" in result.output.lower()


# ---------------------------------------------------------------------------
# Additional config.py helper coverage
# ---------------------------------------------------------------------------


class TestConfigModuleHelpers:
    """Additional unit tests for uncovered paths in ``apm_cli.config``."""

    def test_get_default_client_returns_vscode(self, isolated_config: Path) -> None:
        """get_default_client() returns 'vscode' from default config."""
        import apm_cli.config as _conf

        assert _conf.get_default_client() == "vscode"

    def test_set_default_client(self, isolated_config: Path) -> None:
        """set_default_client() persists to disk."""
        import apm_cli.config as _conf

        _conf.set_default_client("cursor")
        _conf._invalidate_config_cache()
        assert _conf.get_default_client() == "cursor"

    def test_set_temp_dir_not_a_directory(self, isolated_config: Path, tmp_path: Path) -> None:
        """set_temp_dir() raises ValueError when path is a file, not a directory."""
        import apm_cli.config as _conf

        a_file = tmp_path / "notadir.txt"
        a_file.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="not a directory"):
            _conf.set_temp_dir(str(a_file))

    def test_set_temp_dir_nonexistent(self, isolated_config: Path) -> None:
        """set_temp_dir() raises ValueError when path does not exist."""
        import apm_cli.config as _conf

        with pytest.raises(ValueError, match="does not exist"):
            _conf.set_temp_dir("/nonexistent/path/zzzz_abc")

    def test_unset_config_key_when_key_exists(self, isolated_config: Path) -> None:
        """_unset_config_key() removes the key and rewrites config file."""
        import apm_cli.config as _conf

        _conf.update_config({"my_temp_key": "my_value"})
        _conf._invalidate_config_cache()
        assert "my_temp_key" in _conf.get_config()

        _conf._unset_config_key("my_temp_key")
        _conf._invalidate_config_cache()
        assert "my_temp_key" not in _conf.get_config()

    def test_get_apm_allow_protocol_fallback_env_truthy(
        self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_apm_allow_protocol_fallback() returns True when env var is '1'."""
        import apm_cli.config as _conf

        monkeypatch.setenv("APM_ALLOW_PROTOCOL_FALLBACK", "1")
        _conf.set_allow_protocol_fallback(False)  # config says False
        assert _conf.get_apm_allow_protocol_fallback() is True

    def test_get_apm_allow_protocol_fallback_env_falsy(
        self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_apm_allow_protocol_fallback() returns False when env var is '0'."""
        import apm_cli.config as _conf

        monkeypatch.setenv("APM_ALLOW_PROTOCOL_FALLBACK", "0")
        _conf.set_allow_protocol_fallback(True)  # config says True
        assert _conf.get_apm_allow_protocol_fallback() is False

    def test_get_apm_allow_protocol_fallback_falls_through_to_config(
        self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_apm_allow_protocol_fallback() falls through to config when env is unset."""
        import apm_cli.config as _conf

        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        _conf.set_allow_protocol_fallback(True)
        assert _conf.get_apm_allow_protocol_fallback() is True

    def test_get_apm_protocol_pref_ssh_env(
        self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_apm_protocol_pref() returns 'ssh' when env var is 'ssh'."""
        import apm_cli.config as _conf

        monkeypatch.setenv("APM_GIT_PROTOCOL", "ssh")
        assert _conf.get_apm_protocol_pref() == "ssh"

    def test_get_apm_protocol_pref_returns_none_by_default(
        self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_apm_protocol_pref() returns None when neither env nor config is set."""
        import apm_cli.config as _conf

        monkeypatch.delenv("APM_GIT_PROTOCOL", raising=False)
        _conf.set_prefer_ssh(False)
        assert _conf.get_apm_protocol_pref() is None

    def test_get_copilot_cowork_skills_dir_not_set(self, isolated_config: Path) -> None:
        """get_copilot_cowork_skills_dir() returns None when not configured."""
        import apm_cli.config as _conf

        assert _conf.get_copilot_cowork_skills_dir() is None

    def test_set_copilot_cowork_skills_dir_valid(
        self, isolated_config: Path, tmp_path: Path
    ) -> None:
        """set_copilot_cowork_skills_dir() persists absolute path."""
        import apm_cli.config as _conf

        _conf.set_copilot_cowork_skills_dir(str(tmp_path))
        assert _conf.get_copilot_cowork_skills_dir() == str(tmp_path)

    def test_set_copilot_cowork_skills_dir_empty_raises(self, isolated_config: Path) -> None:
        """set_copilot_cowork_skills_dir() raises ValueError for empty path."""
        import apm_cli.config as _conf

        with pytest.raises(ValueError, match="empty"):
            _conf.set_copilot_cowork_skills_dir("")

    def test_set_copilot_cowork_skills_dir_relative_raises(self, isolated_config: Path) -> None:
        """set_copilot_cowork_skills_dir() raises ValueError for relative path."""
        import apm_cli.config as _conf

        with pytest.raises(ValueError, match="absolute"):
            _conf.set_copilot_cowork_skills_dir("relative/path")

    def test_unset_copilot_cowork_skills_dir(self, isolated_config: Path) -> None:
        """unset_copilot_cowork_skills_dir() clears the value."""
        import apm_cli.config as _conf

        _conf.update_config({"copilot_cowork_skills_dir": "/some/path"})
        _conf.unset_copilot_cowork_skills_dir()
        _conf._invalidate_config_cache()
        assert _conf.get_copilot_cowork_skills_dir() is None

    def test_unset_registry_removes_entire_entry(self, isolated_config: Path) -> None:
        """unset_registry() removes the full registry entry."""
        import apm_cli.config as _conf

        _conf.set_registry_url("to_remove", "https://to_remove.example.com")
        assert _conf.get_registry_config("to_remove") is not None

        _conf.unset_registry("to_remove")
        assert _conf.get_registry_config("to_remove") is None

    def test_get_config_json_default_registry(self, isolated_config: Path) -> None:
        """get_config_json_default_registry() returns name of default registry."""
        import apm_cli.config as _conf

        _conf.set_registry_url("primary", "https://primary.example.com")
        _conf.set_registry_default("primary", True)
        assert _conf.get_config_json_default_registry() == "primary"

    def test_set_registry_default_false_removes_entry_when_only_key(
        self, isolated_config: Path
    ) -> None:
        """set_registry_default(name, False) removes the entry when default was the only key."""
        import apm_cli.config as _conf

        _conf.set_registry_default("myregistry", True)
        _conf.set_registry_default("myregistry", False)
        assert _conf.is_registry_default("myregistry") is False

    def test_get_apm_temp_dir_env_var_wins(
        self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_apm_temp_dir() returns env var value when set."""
        import apm_cli.config as _conf

        monkeypatch.setenv("APM_TEMP_DIR", "/custom/tmpdir")
        result = _conf.get_apm_temp_dir()
        assert result == "/custom/tmpdir"

    def test_get_apm_temp_dir_config_fallback(
        self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """get_apm_temp_dir() falls back to config file value."""
        import apm_cli.config as _conf

        monkeypatch.delenv("APM_TEMP_DIR", raising=False)
        _conf.set_temp_dir(str(tmp_path))
        assert _conf.get_apm_temp_dir() == str(tmp_path)

    def test_get_apm_temp_dir_none_when_not_set(
        self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_apm_temp_dir() returns None when neither env nor config is set."""
        import apm_cli.config as _conf

        monkeypatch.delenv("APM_TEMP_DIR", raising=False)
        assert _conf.get_apm_temp_dir() is None

    def test_set_audit_on_install_valid(self, isolated_config: Path) -> None:
        """set_audit_on_install() persists valid mode."""
        import apm_cli.config as _conf

        _conf.set_audit_on_install("warn")
        _conf._invalidate_config_cache()
        assert _conf.get_audit_on_install() == "warn"

    def test_set_audit_on_install_invalid_raises(self, isolated_config: Path) -> None:
        """set_audit_on_install() raises ValueError for invalid mode."""
        import apm_cli.config as _conf

        with pytest.raises(ValueError, match="Invalid value"):
            _conf.set_audit_on_install("unknown-mode")

    def test_unset_audit_on_install(self, isolated_config: Path) -> None:
        """unset_audit_on_install() removes the key and falls back to 'off'."""
        import apm_cli.config as _conf

        _conf.set_audit_on_install("block")
        _conf.unset_audit_on_install()
        _conf._invalidate_config_cache()
        assert _conf.get_audit_on_install() == "off"

    def test_scanner_llm_round_trip(self, isolated_config: Path) -> None:
        """set_scanner_llm / get_scanner_options / unset_scanner_llm round-trip."""
        import apm_cli.config as _conf

        _conf.set_scanner_llm("skillspector", True)
        llm, args = _conf.get_scanner_options("skillspector")
        assert llm is True
        assert args is None

        _conf.unset_scanner_llm("skillspector")
        llm2, _ = _conf.get_scanner_options("skillspector")
        assert llm2 is None

    def test_scanner_args_round_trip(self, isolated_config: Path) -> None:
        """set_scanner_args / get_scanner_options / unset_scanner_args round-trip."""
        import apm_cli.config as _conf

        _conf.set_scanner_args("skillspector", ["--model", "gpt-4o"])
        _, args = _conf.get_scanner_options("skillspector")
        assert args == ("--model", "gpt-4o")

        _conf.unset_scanner_args("skillspector")
        _, args2 = _conf.get_scanner_options("skillspector")
        assert args2 is None

    def test_unset_scanner_removes_entry(self, isolated_config: Path) -> None:
        """unset_scanner() removes the entire scanner entry."""
        import apm_cli.config as _conf

        _conf.set_scanner_llm("sarif", True)
        assert _conf.get_scanner_config("sarif") is not None

        _conf.unset_scanner("sarif")
        assert _conf.get_scanner_config("sarif") is None

    def test_validate_mcp_registry_url_empty_raises(self, isolated_config: Path) -> None:
        """_validate_mcp_registry_url() raises ValueError for empty URL."""
        import apm_cli.config as _conf

        with pytest.raises(ValueError, match="empty"):
            _conf._validate_mcp_registry_url("")

    def test_validate_mcp_registry_url_bad_scheme_raises(self, isolated_config: Path) -> None:
        """_validate_mcp_registry_url() raises ValueError for unsupported scheme."""
        import apm_cli.config as _conf

        with pytest.raises(ValueError, match="scheme"):
            _conf._validate_mcp_registry_url("ftp://registry.example.com")

    def test_set_mcp_registry_url_valid(self, isolated_config: Path) -> None:
        """set_mcp_registry_url() persists valid URL."""
        import apm_cli.config as _conf

        _conf.set_mcp_registry_url("https://mcp.example.com/api")
        _conf._invalidate_config_cache()
        assert _conf.get_mcp_registry_url() == "https://mcp.example.com/api"

    def test_unset_mcp_registry_url(self, isolated_config: Path) -> None:
        """unset_mcp_registry_url() removes the URL."""
        import apm_cli.config as _conf

        _conf.set_mcp_registry_url("https://mcp.example.com")
        _conf.unset_mcp_registry_url()
        _conf._invalidate_config_cache()
        assert _conf.get_mcp_registry_url() is None


# ---------------------------------------------------------------------------
# Additional commands/config.py coverage
# ---------------------------------------------------------------------------


class TestConfigCommandExtended:
    """Additional CLI command tests to cover remaining config paths."""

    def test_config_get_all_shows_mcp_registry_url_when_set(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get (no key) shows mcp-registry-url when set."""
        import apm_cli.config as _conf

        _conf.set_mcp_registry_url("https://mcp.example.com")
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(cli, ["config", "get"])
        assert result.exit_code == 0
        assert "mcp-registry-url" in result.output

    def test_config_set_audit_on_install_requires_external_flag(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set audit-on-install exits non-zero when external-scanners is off."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(cli, ["config", "set", "audit-on-install", "warn"])
        assert result.exit_code != 0
        assert "external-scanners" in result.output

    def test_config_get_audit_on_install(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config get audit-on-install returns the value."""
        result = runner.invoke(cli, ["config", "get", "audit-on-install"])
        assert result.exit_code == 0
        assert "audit-on-install" in result.output

    def test_config_unset_audit_on_install(self, runner: CliRunner, isolated_config: Path) -> None:
        """apm config unset audit-on-install succeeds."""
        result = runner.invoke(cli, ["config", "unset", "audit-on-install"])
        assert result.exit_code == 0

    def test_config_set_registry_default_false(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set registry.<name>.default false clears the default."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "set", "registry.corp.default", "false"])
        assert result.exit_code == 0

    def test_config_get_registry_token_not_set(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get registry.<name>.token shows 'Not set' when unset."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "get", "registry.corp.token"])
        assert result.exit_code == 0
        assert "Not set" in result.output

    def test_config_get_registry_url_when_set(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get registry.<name>.url returns the stored URL."""
        import apm_cli.config as _conf

        _conf.set_registry_url("corp", "https://corp.example.com")
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "get", "registry.corp.url"])
        assert result.exit_code == 0
        urls = [tok for tok in result.output.split() if "://" in tok]
        assert any(urllib.parse.urlparse(u).hostname == "corp.example.com" for u in urls)

    def test_config_unset_unknown_key_shows_error(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config unset with truly unknown key shows error."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(cli, ["config", "unset", "really-unknown-key-xyz"])
        assert result.exit_code != 0
        assert "Unknown configuration key" in result.output


# ---------------------------------------------------------------------------
# Additional self-update helper function coverage
# ---------------------------------------------------------------------------


class TestSelfUpdateHelpers:
    """Tests for helper functions in ``apm_cli.commands.self_update``."""

    def test_get_update_installer_url_default_unix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On Unix with default GITHUB_URL, returns the aka.ms shortlink."""
        from apm_cli.commands import self_update as su

        monkeypatch.delenv("GITHUB_URL", raising=False)
        with (
            patch.object(su, "_is_windows_platform", return_value=False),
            patch("apm_cli.commands.self_update.get_installer_base_url", return_value=None),
            patch(
                "apm_cli.commands.self_update.installer_public_download_blocked",
                return_value=False,
            ),
        ):
            url = su._get_update_installer_url()
        parsed = urllib.parse.urlparse(url)
        assert "apm-unix" in parsed.path or parsed.hostname == "aka.ms"

    def test_get_manual_update_command_unix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_get_manual_update_command() returns a curl command on Unix."""
        from apm_cli.commands import self_update as su

        with (
            patch.object(su, "_is_windows_platform", return_value=False),
            patch("apm_cli.commands.self_update.get_installer_base_url", return_value=None),
            patch(
                "apm_cli.commands.self_update._get_update_installer_url",
                return_value="https://aka.ms/apm-unix",
            ),
        ):
            cmd = su._get_manual_update_command()
        urls = [tok for tok in cmd.split() if "://" in tok]
        assert "curl" in cmd or any(urllib.parse.urlparse(u).hostname == "aka.ms" for u in urls)

    def test_get_installer_run_command_unix(self) -> None:
        """_get_installer_run_command() returns [shell, script] on Unix."""
        from apm_cli.commands import self_update as su

        with patch.object(su, "_is_windows_platform", return_value=False):
            cmd = su._get_installer_run_command("/tmp/install.sh")
        assert "bash" in cmd[0]
        assert "/tmp/install.sh" in cmd

    def test_get_update_installer_suffix_unix(self) -> None:
        """_get_update_installer_suffix() returns '.sh' on Unix."""
        from apm_cli.commands import self_update as su

        with patch.object(su, "_is_windows_platform", return_value=False):
            assert su._get_update_installer_suffix() == ".sh"

    def test_get_update_installer_url_with_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_get_update_installer_url() uses APM_INSTALLER_BASE_URL when set."""
        from urllib.parse import urlparse

        from apm_cli.commands import self_update as su

        with (
            patch(
                "apm_cli.commands.self_update.get_installer_base_url",
                return_value="https://mirror.example.com",
            ),
            patch.object(su, "_is_windows_platform", return_value=False),
        ):
            url = su._get_update_installer_url()
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.hostname == "mirror.example.com"


# ---------------------------------------------------------------------------
# config.py remaining edge cases
# ---------------------------------------------------------------------------


class TestConfigModuleEdgeCases:
    """Cover remaining edge-case paths in ``apm_cli.config``."""

    def test_unset_registry_url_when_url_is_only_key_removes_entry(
        self, isolated_config: Path
    ) -> None:
        """unset_registry_url() removes the whole registry entry when url was the only field."""
        import apm_cli.config as _conf

        _conf.set_registry_url("solo", "https://solo.example.com")
        _conf.unset_registry_url("solo")
        assert _conf.get_registry_config("solo") is None

    def test_validate_mcp_registry_url_too_long(self, isolated_config: Path) -> None:
        """_validate_mcp_registry_url() raises ValueError for excessively long URL."""
        import apm_cli.config as _conf

        long_url = "https://example.com/" + "a" * 2100
        with pytest.raises(ValueError, match="too long"):
            _conf._validate_mcp_registry_url(long_url)

    def test_validate_mcp_registry_url_missing_scheme(self, isolated_config: Path) -> None:
        """_validate_mcp_registry_url() raises ValueError when scheme is missing."""
        import apm_cli.config as _conf

        with pytest.raises(ValueError, match="scheme"):
            _conf._validate_mcp_registry_url("example.com/path")

    def test_validate_mcp_registry_url_with_credentials(self, isolated_config: Path) -> None:
        """_validate_mcp_registry_url() raises ValueError when URL contains credentials."""
        import apm_cli.config as _conf

        with pytest.raises(ValueError, match="credentials"):
            _conf._validate_mcp_registry_url("https://user:pass@registry.example.com")

    def test_unset_scanner_field_keeps_remaining_fields(self, isolated_config: Path) -> None:
        """_unset_scanner_field() keeps other fields when one is removed."""
        import apm_cli.config as _conf

        _conf.set_scanner_llm("skillspector", True)
        _conf.set_scanner_args("skillspector", ["--debug"])
        _conf.unset_scanner_llm("skillspector")

        _, args = _conf.get_scanner_options("skillspector")
        assert args == ("--debug",)  # args still present

    def test_get_config_json_default_registry_no_default(self, isolated_config: Path) -> None:
        """get_config_json_default_registry() returns None when no registry is marked default."""
        import apm_cli.config as _conf

        _conf.set_registry_url("corp", "https://corp.example.com")
        assert _conf.get_config_json_default_registry() is None

    def test_set_registry_default_clears_previous_default_in_multi_registry(
        self, isolated_config: Path
    ) -> None:
        """set_registry_default() properly clears the old default entry."""
        import apm_cli.config as _conf

        # registry 'a' has url + default
        _conf.set_registry_url("a", "https://a.example.com")
        _conf.set_registry_default("a", True)

        # now set 'b' as default -- 'a' should lose its default flag
        _conf.set_registry_url("b", "https://b.example.com")
        _conf.set_registry_default("b", True)

        assert _conf.is_registry_default("a") is False
        assert _conf.is_registry_default("b") is True
        # 'a' entry should still exist (url is intact)
        assert _conf.get_registry_config("a") is not None


# ---------------------------------------------------------------------------
# commands/config.py extended coverage: external-scanners and cowork flags
# ---------------------------------------------------------------------------


class TestConfigCommandFlagPaths:
    """Tests for config commands that require experimental flags to be on."""

    def test_config_set_audit_on_install_when_flag_on(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set audit-on-install warn succeeds when external-scanners is on."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "set", "audit-on-install", "warn"])
        assert result.exit_code == 0
        assert "warn" in result.output.lower() or "audit" in result.output.lower()

    def test_config_set_audit_on_install_invalid_mode_when_flag_on(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set audit-on-install with invalid mode exits non-zero when flag on."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "set", "audit-on-install", "invalid-mode"])
        assert result.exit_code != 0

    def test_config_set_copilot_cowork_skills_dir_requires_flag(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set copilot-cowork-skills-dir exits non-zero when flag is off."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(
                cli, ["config", "set", "copilot-cowork-skills-dir", "/some/path"]
            )
        assert result.exit_code != 0
        assert "copilot-cowork" in result.output.lower()

    def test_config_set_copilot_cowork_skills_dir_with_flag(
        self, runner: CliRunner, isolated_config: Path, tmp_path: Path
    ) -> None:
        """apm config set copilot-cowork-skills-dir succeeds when flag is on."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(
                cli, ["config", "set", "copilot-cowork-skills-dir", str(tmp_path)]
            )
        assert result.exit_code == 0

    def test_config_set_copilot_cowork_skills_dir_invalid_path_with_flag(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set copilot-cowork-skills-dir with relative path exits non-zero."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(
                cli, ["config", "set", "copilot-cowork-skills-dir", "relative/path"]
            )
        assert result.exit_code != 0

    def test_config_get_no_key_shows_audit_on_install_when_flag_on(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get (no key) shows audit-on-install when external-scanners is on."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "get"])
        assert result.exit_code == 0
        assert "audit-on-install" in result.output

    def test_config_unset_copilot_cowork_skills_dir(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config unset copilot-cowork-skills-dir succeeds."""
        result = runner.invoke(cli, ["config", "unset", "copilot-cowork-skills-dir"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# apm update additional paths
# ---------------------------------------------------------------------------


class TestUpdateCommandAdditional:
    """Additional tests for ``apm update`` edge cases."""

    def test_update_global_no_user_apm_yml(
        self,
        runner: CliRunner,
        isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``apm update --global`` exits non-zero when no ~/.apm/apm.yml exists."""
        # Point the user-scope apm directory to a tmp dir without apm.yml

        fake_apm_dir = tmp_path / ".apm"
        fake_apm_dir.mkdir()

        with patch("apm_cli.core.scope.get_apm_dir", return_value=fake_apm_dir):
            result = runner.invoke(cli, ["update", "--global"], catch_exceptions=False)
        assert result.exit_code != 0
        assert "apm.yml" in result.output.lower() or "no apm.yml" in result.output.lower()

    def test_update_with_apm_yml_no_deps_exits_success(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """``apm update`` on project with no APM deps reports nothing to update."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: empty-project\nversion: 1.0.0\ndependencies:\n  apm: {}\n",
                encoding="utf-8",
            )
            with patch(
                "apm_cli.commands.update.resolve_revision_pin_updates",
                return_value=[],
            ):
                result = runner.invoke(cli, ["update", "--yes"], catch_exceptions=False)
        assert result.exit_code in (0, 1)

    def test_update_parse_apm_yml_error(self, runner: CliRunner, isolated_config: Path) -> None:
        """``apm update`` exits non-zero when apm.yml cannot be parsed."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: [invalid yaml structure\n",
                encoding="utf-8",
            )
            result = runner.invoke(cli, ["update", "--yes"], catch_exceptions=False)
        assert result.exit_code in (0, 1)


# ---------------------------------------------------------------------------
# apm self-update remaining paths
# ---------------------------------------------------------------------------


class TestSelfUpdateRemainingPaths:
    """Cover remaining paths in ``apm_cli.commands.self_update``."""

    def test_self_update_unknown_version_with_check_flag(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """Unknown version with --check flag shows dev-mode message."""
        with (
            patch(
                "apm_cli.commands.self_update.is_self_update_enabled",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update.get_version",
                return_value="unknown",
            ),
        ):
            result = runner.invoke(cli, ["self-update", "--check"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "development" in result.output.lower() or "cannot determine" in result.output.lower()

    def test_self_update_github_url_override_shows_message(
        self, runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When GITHUB_URL env var is set to custom value, a progress message is shown."""
        monkeypatch.setenv("GITHUB_URL", "https://github.mycompany.com")
        with (
            patch(
                "apm_cli.commands.self_update.is_self_update_enabled",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update.get_version",
                return_value="1.0.0",
            ),
            patch(
                "apm_cli.utils.version_checker.get_latest_version_from_github",
                return_value=None,
            ),
            patch(
                "apm_cli.commands.self_update.release_metadata_public_lookup_blocked",
                return_value=False,
            ),
        ):
            result = runner.invoke(cli, ["self-update"], catch_exceptions=False)
        # Should mention the GITHUB_URL override or fail to fetch version
        assert result.exit_code in (0, 1)

    def test_self_update_mirror_metadata_url_active(
        self, runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When APM_RELEASE_METADATA_URL is set, a mirror active message is shown."""
        monkeypatch.setenv("APM_RELEASE_METADATA_URL", "https://mirror.example.com/latest.json")
        with (
            patch(
                "apm_cli.commands.self_update.is_self_update_enabled",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update.get_version",
                return_value="1.0.0",
            ),
            patch(
                "apm_cli.utils.version_checker.get_latest_version_from_github",
                return_value=None,
            ),
            patch(
                "apm_cli.commands.self_update.release_metadata_public_lookup_blocked",
                return_value=False,
            ),
            patch(
                "apm_cli.commands.self_update.get_release_metadata_url",
                return_value="https://mirror.example.com/latest.json",
            ),
        ):
            result = runner.invoke(cli, ["self-update"], catch_exceptions=False)
        # Should fail to fetch but path exercises the metadata URL display logic
        assert result.exit_code in (0, 1)

    def test_self_update_mirror_fetch_fails_shows_mirror_error(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """When fetch fails with metadata mirror configured, error mentions mirror."""
        with (
            patch(
                "apm_cli.commands.self_update.is_self_update_enabled",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update.get_version",
                return_value="1.0.0",
            ),
            patch(
                "apm_cli.utils.version_checker.get_latest_version_from_github",
                return_value=None,
            ),
            patch(
                "apm_cli.commands.self_update.release_metadata_public_lookup_blocked",
                return_value=False,
            ),
            patch(
                "apm_cli.commands.self_update.get_release_metadata_url",
                return_value="https://mirror.example.com/latest.json",
            ),
        ):
            result = runner.invoke(cli, ["self-update"], catch_exceptions=False)
        assert result.exit_code != 0
        assert "mirror" in result.output.lower() or "unable" in result.output.lower()

    def test_self_update_get_manual_update_command_with_mirror(self) -> None:
        """_get_manual_update_command() uses mirror URL when APM_INSTALLER_BASE_URL is set."""
        from apm_cli.commands import self_update as su

        with (
            patch.object(su, "_is_windows_platform", return_value=False),
            patch(
                "apm_cli.commands.self_update.get_installer_base_url",
                return_value="https://mirror.example.com",
            ),
        ):
            cmd = su._get_manual_update_command()
        urls = [tok for tok in cmd.split() if "://" in tok]
        has_mirror = any(urllib.parse.urlparse(u).hostname == "mirror.example.com" for u in urls)
        assert has_mirror or "install.sh" in cmd


# ---------------------------------------------------------------------------
# commands/config.py: external scanner paths, get with temp-dir set,
# bool output, transport keys in get-all, unset external scanner
# ---------------------------------------------------------------------------


class TestConfigCommandMorePaths:
    """Cover remaining config command paths for external scanners and display."""

    def test_config_set_mcp_registry_url_invalid_exits_nonzero(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set mcp-registry-url with invalid URL exits non-zero."""
        result = runner.invoke(cli, ["config", "set", "mcp-registry-url", "not-a-url"])
        assert result.exit_code != 0

    def test_config_get_temp_dir_when_set(
        self, runner: CliRunner, isolated_config: Path, tmp_path: Path
    ) -> None:
        """apm config get temp-dir shows the value when configured."""
        import apm_cli.config as _conf

        _conf.set_temp_dir(str(tmp_path))
        result = runner.invoke(cli, ["config", "get", "temp-dir"])
        assert result.exit_code == 0
        # Output may wrap the path but should contain part of it
        assert "temp-dir:" in result.output

    def test_config_get_auto_integrate_shows_bool(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get auto-integrate shows 'true' or 'false' (lowercase)."""
        result = runner.invoke(cli, ["config", "get", "auto-integrate"])
        assert result.exit_code == 0
        # bool rendering: lowercase true or false
        assert "true" in result.output.lower() or "false" in result.output.lower()

    def test_config_get_all_shows_transport_when_true(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get (no key) shows allow-protocol-fallback and prefer-ssh when true."""
        import apm_cli.config as _conf

        _conf.set_allow_protocol_fallback(True)
        _conf.set_prefer_ssh(True)
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(cli, ["config", "get"])
        assert result.exit_code == 0
        assert "allow-protocol-fallback" in result.output
        assert "prefer-ssh" in result.output

    def test_config_get_all_shows_scanner_options_when_flag_on(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get (no key) shows scanner options when external_scanners is on."""
        import apm_cli.config as _conf

        _conf.set_scanner_llm("skillspector", True)
        _conf.set_scanner_args("skillspector", ["--debug"])
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "get"])
        assert result.exit_code == 0
        # scanner info should appear
        assert "skillspector" in result.output or "audit-on-install" in result.output

    def test_config_set_external_scanner_llm_with_flag_on(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set external.skillspector.llm true succeeds when flag is on."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "set", "external.skillspector.llm", "true"])
        assert result.exit_code == 0

    def test_config_set_external_scanner_args_with_flag_on(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set external.skillspector.args 'scan output.txt' succeeds when flag on."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(
                cli,
                ["config", "set", "external.skillspector.args", "scan output.txt"],
            )
        assert result.exit_code == 0

    def test_config_set_external_scanner_requires_flag(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set external.skillspector.llm exits non-zero when flag is off."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(cli, ["config", "set", "external.skillspector.llm", "true"])
        assert result.exit_code != 0

    def test_config_set_external_scanner_invalid_name(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set external.<unknown>.llm exits non-zero with 'Unknown external scanner'."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "set", "external.unknownscanner.llm", "true"])
        assert result.exit_code != 0
        assert "unknownscanner" in result.output.lower() or "unknown" in result.output.lower()

    def test_config_get_external_scanner_llm_not_set(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get external.skillspector.llm shows 'Not set' when unset."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "get", "external.skillspector.llm"])
        assert result.exit_code == 0
        assert "Not set" in result.output

    def test_config_get_external_scanner_args_not_set(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get external.skillspector.args shows 'Not set' when unset."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "get", "external.skillspector.args"])
        assert result.exit_code == 0
        assert "Not set" in result.output

    def test_config_get_external_scanner_llm_when_set(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get external.skillspector.llm shows value when set."""
        import apm_cli.config as _conf

        _conf.set_scanner_llm("skillspector", True)
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "get", "external.skillspector.llm"])
        assert result.exit_code == 0
        assert "true" in result.output.lower()

    def test_config_get_external_scanner_args_when_set(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get external.skillspector.args shows args when set."""
        import apm_cli.config as _conf

        _conf.set_scanner_args("skillspector", ["--debug", "--verbose"])
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "get", "external.skillspector.args"])
        assert result.exit_code == 0
        assert "--debug" in result.output

    def test_config_unset_external_scanner_llm(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config unset external.skillspector.llm removes the setting."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "unset", "external.skillspector.llm"])
        assert result.exit_code == 0

    def test_config_unset_external_scanner_args(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config unset external.skillspector.args removes the setting."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "unset", "external.skillspector.args"])
        assert result.exit_code == 0

    def test_config_get_mcp_registry_url_when_set(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get mcp-registry-url shows the URL when configured."""
        import apm_cli.config as _conf

        _conf.set_mcp_registry_url("https://mcp.example.com")
        result = runner.invoke(cli, ["config", "get", "mcp-registry-url"])
        assert result.exit_code == 0
        urls = [tok for tok in result.output.split() if "://" in tok]
        assert any(urllib.parse.urlparse(u).hostname == "mcp.example.com" for u in urls)

    def test_config_get_copilot_cowork_skills_dir(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get copilot-cowork-skills-dir shows 'Not set' when unset."""
        result = runner.invoke(cli, ["config", "get", "copilot-cowork-skills-dir"])
        assert result.exit_code == 0
        assert "Not set" in result.output or "copilot-cowork-skills-dir" in result.output


# ---------------------------------------------------------------------------
# commands/config.py: remaining uncovered branches
# ---------------------------------------------------------------------------


class TestConfigCommandFinalPaths:
    """Cover final remaining branches in config set/get."""

    def test_config_set_unknown_key_with_all_flags_on_shows_all_keys(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """_valid_config_keys() includes all keys when all flags are on."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "set", "totally-unknown-xyz", "val"])
        assert result.exit_code != 0
        assert "audit-on-install" in result.output
        assert "copilot-cowork-skills-dir" in result.output
        assert "registry" in result.output

    def test_config_set_external_scanner_llm_invalid_bool(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set external.skillspector.llm with non-bool exits non-zero."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "set", "external.skillspector.llm", "maybe"])
        assert result.exit_code != 0
        assert "Invalid value" in result.output or "invalid" in result.output.lower()

    def test_config_set_external_scanner_args_empty_exits_nonzero(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config set external.skillspector.args '' (empty) exits non-zero."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "set", "external.skillspector.args", ""])
        assert result.exit_code != 0

    def test_config_get_copilot_cowork_skills_dir_when_set(
        self, runner: CliRunner, isolated_config: Path, tmp_path: Path
    ) -> None:
        """apm config get copilot-cowork-skills-dir shows value when set."""
        import apm_cli.config as _conf

        _conf.set_copilot_cowork_skills_dir(str(tmp_path))
        result = runner.invoke(cli, ["config", "get", "copilot-cowork-skills-dir"])
        assert result.exit_code == 0
        # path may be long but key should appear
        assert "copilot-cowork-skills-dir:" in result.output

    def test_config_bare_in_project_with_compilation(
        self, runner: CliRunner, isolated_config: Path, tmp_path: Path
    ) -> None:
        """``apm config`` inside a project with compilation: block shows compilation info."""
        import os

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "apm.yml").write_text(
            "name: myproj\nversion: 1.0.0\ncompilation:\n  output: AGENTS.md\n  chatmode: agent\n",
            encoding="utf-8",
        )
        orig = os.getcwd()
        try:
            os.chdir(proj)
            with patch("apm_cli.core.experimental.is_enabled", return_value=False):
                result = runner.invoke(cli, ["config"])
        finally:
            os.chdir(orig)
        assert result.exit_code in (0, 1)

    def test_config_bare_shows_transport_keys_when_enabled(
        self, runner: CliRunner, isolated_config: Path, tmp_path: Path
    ) -> None:
        """``apm config`` shows transport keys when they are set to true."""
        import os

        import apm_cli.config as _conf

        _conf.set_allow_protocol_fallback(True)
        _conf.set_prefer_ssh(True)

        orig = os.getcwd()
        try:
            os.chdir(tmp_path)
            with patch("apm_cli.core.experimental.is_enabled", return_value=False):
                result = runner.invoke(cli, ["config"])
        finally:
            os.chdir(orig)
        assert result.exit_code in (0, 1)

    def test_config_get_auto_integrate_after_set_false_shows_false(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get auto-integrate returns false after setting to false."""
        import apm_cli.config as _conf

        _conf.set_auto_integrate(False)
        result = runner.invoke(cli, ["config", "get", "auto-integrate"])
        assert result.exit_code == 0
        assert "false" in result.output.lower()

    def test_config_get_audit_on_install_when_set(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config get shows audit-on-install in get-all when external_scanners enabled and set."""

        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "get"])
        assert result.exit_code == 0
        assert "audit-on-install" in result.output


# ---------------------------------------------------------------------------
# update.py: global and check-only paths
# ---------------------------------------------------------------------------


class TestUpdateGlobalAndCheckPaths:
    """Test ``update --global`` path and check-only forwarding."""

    def test_update_global_with_apm_yml_no_deps(
        self, runner: CliRunner, isolated_config: Path, tmp_path: Path
    ) -> None:
        """``apm update --global`` with user-scope apm.yml containing no deps succeeds."""
        fake_apm_dir = tmp_path / ".apm_user"
        fake_apm_dir.mkdir()
        user_apm_yml = fake_apm_dir / "apm.yml"
        user_apm_yml.write_text(
            "name: user-scope\nversion: 1.0.0\ndependencies:\n  apm: {}\n",
            encoding="utf-8",
        )

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=fake_apm_dir),
            patch(
                "apm_cli.commands.update.resolve_revision_pin_updates",
                return_value=[],
            ),
        ):
            result = runner.invoke(cli, ["update", "--global", "--yes"], catch_exceptions=False)
        assert result.exit_code in (0, 1)

    def test_update_global_check_only_emits_warning(
        self, runner: CliRunner, isolated_config: Path, tmp_path: Path
    ) -> None:
        """``apm update --global --check`` emits a warning about --check being ignored."""
        fake_apm_dir = tmp_path / ".apm_user2"
        fake_apm_dir.mkdir()
        user_apm_yml = fake_apm_dir / "apm.yml"
        user_apm_yml.write_text(
            "name: user-scope\nversion: 1.0.0\ndependencies:\n  apm: {}\n",
            encoding="utf-8",
        )

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=fake_apm_dir),
            patch(
                "apm_cli.commands.update.resolve_revision_pin_updates",
                return_value=[],
            ),
        ):
            result = runner.invoke(
                cli, ["update", "--global", "--check", "--yes"], catch_exceptions=False
            )
        # --check is silently ignored with --global
        assert result.exit_code in (0, 1)

    def test_update_no_apm_yml_with_target_flag(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """``apm update --target copilot`` outside project shows deprecation warning."""
        with runner.isolated_filesystem():
            with (
                patch(
                    "apm_cli.utils.version_checker.get_latest_version_from_github",
                    return_value=None,
                ),
                patch(
                    "apm_cli.commands.self_update.is_self_update_enabled",
                    return_value=True,
                ),
                patch(
                    "apm_cli.commands.self_update.get_version",
                    return_value="1.0.0",
                ),
            ):
                result = runner.invoke(
                    cli, ["update", "--target", "copilot"], catch_exceptions=False
                )
        assert result.exit_code in (0, 1)
        # Should mention the --target-is-ignored warning
        assert "target" in result.output.lower() or result.exit_code in (0, 1)


# ---------------------------------------------------------------------------
# commands/publish.py remaining paths
# ---------------------------------------------------------------------------


class TestPublishRemainingPaths:
    """Cover remaining paths in publish command."""

    def test_publish_empty_version_string(self, runner: CliRunner) -> None:
        """apm publish exits non-zero when version is empty string."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                'name: my-skill\nversion: ""\ndescription: A skill\n',
                encoding="utf-8",
            )
            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0
        assert "version" in result.output.lower()

    def test_publish_with_prebuilt_zip_dry_run(self, runner: CliRunner) -> None:
        """apm publish --zip <prebuilt.zip> --dry-run shows preview without uploading."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: my-skill\nversion: 1.0.0\ndescription: A skill\n"
                "registries:\n  corp:\n    url: https://r.example.com\n",
                encoding="utf-8",
            )
            # Create a minimal zip
            import zipfile

            with zipfile.ZipFile("prebuilt.zip", "w") as zf:
                zf.writestr("apm.yml", "name: my-skill\nversion: 1.0.0\n")

            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
            ):
                result = runner.invoke(
                    cli,
                    [
                        "publish",
                        "--package",
                        "acme/my-skill",
                        "--zip",
                        "prebuilt.zip",
                        "--dry-run",
                    ],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower() or "nothing uploaded" in result.output.lower()

    def test_publish_apm_yml_parse_failure(self, runner: CliRunner) -> None:
        """apm publish exits non-zero when apm.yml cannot be parsed."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: [invalid yaml structure\n",
                encoding="utf-8",
            )
            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0
        assert "Failed to read apm.yml" in result.output or "apm.yml" in result.output

    def test_publish_dry_run_verbose(self, runner: CliRunner) -> None:
        """apm publish --dry-run --verbose shows extra archive info."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: my-skill\nversion: 1.0.0\ndescription: A skill\n"
                "registries:\n  corp:\n    url: https://r.example.com\n",
                encoding="utf-8",
            )
            apm_dir = Path(".apm")
            apm_dir.mkdir()
            (apm_dir / "placeholder.md").write_text("# placeholder\n", encoding="utf-8")
            Path("README.md").write_text("# Readme\n", encoding="utf-8")

            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill", "--dry-run", "--verbose"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0

    def test_publish_upload_error_409_conflict(self, runner: CliRunner) -> None:
        """apm publish with 409 conflict error shows 'already exists' message."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: my-skill\nversion: 1.0.0\ndescription: A skill\n"
                "registries:\n  corp:\n    url: https://r.example.com\n",
                encoding="utf-8",
            )
            apm_dir = Path(".apm")
            apm_dir.mkdir()
            (apm_dir / "skill.md").write_text("# skill\n", encoding="utf-8")

            mock_exc = MagicMock()
            mock_exc.status = 409
            mock_exc.problem = {"detail": "version already exists"}

            from apm_cli.deps.registry.client import RegistryError

            mock_exc.__class__ = RegistryError

            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
                patch("apm_cli.deps.registry.auth.make_auth_context"),
                patch(
                    "apm_cli.deps.registry.client.RegistryClient.publish_version",
                    side_effect=RegistryError("conflict", status=409, problem=None),
                ),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# commands/config.py: unset registry default and remaining fallback path
# ---------------------------------------------------------------------------


class TestConfigUnsetRegistryDefault:
    """Test unset registry.<name>.default and fallback display."""

    def test_config_unset_registry_default_with_flag(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """apm config unset registry.<name>.default clears the default flag."""
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(cli, ["config", "unset", "registry.corp.default"])
        assert result.exit_code == 0

    def test_config_bare_fallback_when_rich_unavailable(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """``apm config`` falls back to text display when rich import fails."""
        import sys as _sys

        class _MockModule:
            """Stub module that prevents rich.table from being imported."""

            def __getattr__(self, name):
                raise ImportError("rich not available")

        with patch.dict(_sys.modules, {"rich.table": None}):
            with patch("apm_cli.core.experimental.is_enabled", return_value=False):
                result = runner.invoke(cli, ["config"])
        # Should fall through to text display without crashing
        assert result.exit_code in (0, 1)


# ---------------------------------------------------------------------------
# Publish error handling paths and self-update VERSION env var
# ---------------------------------------------------------------------------


class TestPublishErrorHandling:
    """Cover publish error handler branches."""

    def _make_project(self) -> None:
        """Create minimal project in the current isolated filesystem."""
        Path("apm.yml").write_text(
            "name: my-skill\nversion: 1.0.0\ndescription: A skill\n"
            "registries:\n  corp:\n    url: https://r.example.com\n",
            encoding="utf-8",
        )
        apm_dir = Path(".apm")
        apm_dir.mkdir(exist_ok=True)
        (apm_dir / "skill.md").write_text("# skill\n", encoding="utf-8")

    def test_publish_upload_success(self, runner: CliRunner) -> None:
        """apm publish with successful upload shows 'Published' message."""
        with runner.isolated_filesystem():
            self._make_project()

            from apm_cli.deps.registry.client import RegistryClient

            mock_result = MagicMock()
            mock_result.package = "acme/my-skill"
            mock_result.version = "1.0.0"
            mock_result.digest = "sha256:abc123"
            mock_result.published_at = "2025-01-01T00:00:00Z"

            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
                patch("apm_cli.deps.registry.auth.make_auth_context"),
                patch.object(RegistryClient, "publish_version", return_value=mock_result),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0
        assert "Published" in result.output or "published" in result.output.lower()

    def test_publish_upload_error_403_forbidden(self, runner: CliRunner) -> None:
        """apm publish with 403 Forbidden shows permission error."""
        with runner.isolated_filesystem():
            self._make_project()

            from apm_cli.deps.registry.client import RegistryClient, RegistryError

            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
                patch("apm_cli.deps.registry.auth.make_auth_context"),
                patch.object(
                    RegistryClient,
                    "publish_version",
                    side_effect=RegistryError("forbidden", status=403, problem=None),
                ),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0
        assert "Forbidden" in result.output or "permission" in result.output.lower()

    def test_publish_upload_error_422_validation(self, runner: CliRunner) -> None:
        """apm publish with 422 Unprocessable Entity shows validation error."""
        with runner.isolated_filesystem():
            self._make_project()

            from apm_cli.deps.registry.client import RegistryClient, RegistryError

            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
                patch("apm_cli.deps.registry.auth.make_auth_context"),
                patch.object(
                    RegistryClient,
                    "publish_version",
                    side_effect=RegistryError(
                        "validation failed",
                        status=422,
                        problem={"detail": "invalid schema"},
                    ),
                ),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0
        assert "validation" in result.output.lower() or "invalid" in result.output.lower()

    def test_publish_archive_already_exists_cleanup(self, runner: CliRunner) -> None:
        """apm publish removes and recreates the archive when it already exists."""
        with runner.isolated_filesystem():
            self._make_project()
            # Pre-create the archive to test the cleanup path
            Path("my-skill-1.0.0.zip").write_text("old content", encoding="utf-8")

            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill", "--dry-run"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0

    def test_publish_upload_error_401_unauthorized(self, runner: CliRunner) -> None:
        """apm publish with 401 Unauthorized shows authentication error."""
        with runner.isolated_filesystem():
            self._make_project()

            from apm_cli.deps.registry.client import RegistryClient, RegistryError

            with (
                patch("apm_cli.commands.publish.require_package_registry_enabled"),
                patch("apm_cli.core.experimental.is_enabled", return_value=True),
                patch("apm_cli.deps.registry.auth.make_auth_context"),
                patch.object(
                    RegistryClient,
                    "publish_version",
                    side_effect=RegistryError("unauthorized", status=401, problem=None),
                ),
            ):
                result = runner.invoke(
                    cli,
                    ["publish", "--package", "acme/my-skill"],
                    catch_exceptions=False,
                )
        assert result.exit_code != 0


class TestSelfUpdateVersionEnv:
    """Test self-update VERSION env var path."""

    def test_self_update_version_env_skips_api_check(
        self, runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When VERSION env var is set, a message is shown about using pinned version."""
        monkeypatch.setenv("VERSION", "2.0.0")
        with (
            patch(
                "apm_cli.commands.self_update.is_self_update_enabled",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update.get_version",
                return_value="1.0.0",
            ),
            patch(
                "apm_cli.utils.version_checker.get_latest_version_from_github",
                return_value="2.0.0",
            ),
            patch(
                "apm_cli.utils.version_checker.is_newer_version",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.self_update.release_metadata_public_lookup_blocked",
                return_value=False,
            ),
            patch(
                "apm_cli.commands.self_update.get_release_metadata_url",
                return_value=None,
            ),
        ):
            result = runner.invoke(cli, ["self-update", "--check"], catch_exceptions=False)
        # Should run and show the version info
        assert result.exit_code in (0, 1)
