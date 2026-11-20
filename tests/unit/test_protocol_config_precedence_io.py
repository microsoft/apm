"""Tests for the CLI > env var > apm config > default transport-preference precedence chain (filesystem, process and CLI boundary cases)."""

import os
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_apm_allow_protocol_fallback precedence
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_apm_protocol_pref precedence (ssh preference)
# ---------------------------------------------------------------------------


class TestProtocolPrefPrecedence:
    """CLI flag > APM_GIT_PROTOCOL env > apm config prefer-ssh > None (git insteadOf)."""

    def test_cli_flag_ssh_wins_over_env_and_config(self, isolated_config, tmp_path, monkeypatch):
        """``apm install --ssh`` pins SSH even when env says https and config is unset."""
        from click.testing import CliRunner

        import apm_cli.config as cfg_module
        from apm_cli.commands.install import install
        from apm_cli.deps.transport_selection import ProtocolPreference
        from apm_cli.utils.diagnostics import DiagnosticCollector

        cfg_module.set_prefer_ssh(False)
        (tmp_path / "apm.yml").write_text(
            "name: proto-test\nversion: 1.0.0\ndependencies:\n  apm: []\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APM_GIT_PROTOCOL", "https")

        captured = []

        def _capture(ctx, _outcome):
            captured.append(ctx.protocol_pref)
            return 0, 0, 0, DiagnosticCollector()

        with (
            patch("apm_cli.commands.install._install_apm_packages", side_effect=_capture),
            patch.object(
                cfg_module, "get_apm_protocol_pref", wraps=cfg_module.get_apm_protocol_pref
            ) as pref_helper,
        ):
            runner = CliRunner()
            with_flag = runner.invoke(install, ["--ssh"], catch_exceptions=False)
            assert pref_helper.call_count == 0
            without_flag = runner.invoke(install, [], catch_exceptions=False)
            assert pref_helper.call_count == 1

        assert with_flag.exit_code == 0, with_flag.output
        assert without_flag.exit_code == 0, without_flag.output
        assert captured == [ProtocolPreference.SSH, ProtocolPreference.HTTPS]


# ---------------------------------------------------------------------------
# ProtocolPreference.from_str round-trip
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Round-trip: config set → get_apm_* helpers
# ---------------------------------------------------------------------------


class TestConfigRoundTrip:
    """Verify that set_* followed by get_apm_* helpers honour the stored value."""

    def test_set_prefer_ssh_true_reflected_in_get_apm_protocol_pref(self, isolated_config):
        """After set_prefer_ssh(True), get_apm_protocol_pref returns 'ssh' when env is absent."""
        import apm_cli.config as cfg_module

        cfg_module.set_prefer_ssh(True)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_GIT_PROTOCOL", None)
            assert cfg_module.get_apm_protocol_pref() == "ssh"

    def test_set_prefer_ssh_false_reflected_in_get_apm_protocol_pref(self, isolated_config):
        """After set_prefer_ssh(False), get_apm_protocol_pref returns None when env is absent."""
        import apm_cli.config as cfg_module

        cfg_module.set_prefer_ssh(False)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_GIT_PROTOCOL", None)
            assert cfg_module.get_apm_protocol_pref() is None

    def test_set_allow_protocol_fallback_reflected_in_get_apm_helper(self, isolated_config):
        """After set_allow_protocol_fallback(True), get_apm_allow_protocol_fallback is True."""
        import apm_cli.config as cfg_module

        cfg_module.set_allow_protocol_fallback(True)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_ALLOW_PROTOCOL_FALLBACK", None)
            assert cfg_module.get_apm_allow_protocol_fallback() is True

    def test_set_allow_protocol_fallback_false_reflected(self, isolated_config):
        """After set_allow_protocol_fallback(False), get_apm_allow_protocol_fallback is False."""
        import apm_cli.config as cfg_module

        cfg_module.set_allow_protocol_fallback(False)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_ALLOW_PROTOCOL_FALLBACK", None)
            assert cfg_module.get_apm_allow_protocol_fallback() is False


# ---------------------------------------------------------------------------
# Fixture: isolated config (mirrors test_config_command.py fixture)
# ---------------------------------------------------------------------------


@pytest.fixture()
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
