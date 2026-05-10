"""End-to-end tests for config valid-keys gating (Issue #923).

Verifies that ``apm config set <unknown-key> <value>`` only mentions
``copilot-cowork-skills-dir`` in the valid-keys hint when the
``copilot_cowork`` experimental flag is enabled.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirect CONFIG_FILE to a temp dir so tests never touch ~/.apm."""
    import apm_cli.config as _conf

    _conf._invalidate_config_cache()
    config_dir = tmp_path / ".apm"
    config_file = config_dir / "config.json"
    monkeypatch.setattr(_conf, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(config_file))
    yield config_file
    _conf._invalidate_config_cache()


class TestConfigValidKeysE2E:
    """Full CLI pipeline tests for valid-keys gating in config set."""

    def test_config_set_unknown_key_omits_cowork_when_flag_off(self, isolated_config):
        """When copilot_cowork is disabled, cowork key must not appear."""
        runner = CliRunner()
        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = runner.invoke(
                cli,
                ["config", "set", "unknown-key", "value"],
                catch_exceptions=False,
            )

        assert result.exit_code != 0, f"Expected non-zero exit code, got {result.exit_code}"
        assert "copilot-cowork-skills-dir" not in result.output
        assert "auto-integrate" in result.output

    def test_config_set_unknown_key_includes_cowork_when_flag_on(self, isolated_config):
        """When copilot_cowork is enabled, cowork key must appear."""
        runner = CliRunner()
        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = runner.invoke(
                cli,
                ["config", "set", "unknown-key", "value"],
                catch_exceptions=False,
            )

        assert result.exit_code != 0, f"Expected non-zero exit code, got {result.exit_code}"
        assert "copilot-cowork-skills-dir" in result.output
        assert "auto-integrate" in result.output
