"""Tests for the CLI > env var > apm config > default transport-preference precedence chain.

These tests verify the wiring between the install command and the config module
introduced in issue #1243. They do NOT exercise the full install pipeline;
they focus exclusively on the precedence resolution so each layer of the chain
can be validated in isolation.
"""

import os
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_apm_allow_protocol_fallback precedence
# ---------------------------------------------------------------------------


class TestAllowProtocolFallbackPrecedence:
    """CLI flag > env var > apm config > False."""

    def test_cli_flag_true_wins_over_everything(self):
        """CLI flag=True overrides config=False and env=unset."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=False),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_ALLOW_PROTOCOL_FALLBACK", None)
            # Simulate: allow_protocol_fallback (CLI) = True
            cli_flag = True
            result = cli_flag or cfg_module.get_apm_allow_protocol_fallback()
        assert result is True

    def test_env_var_wins_over_config_false(self):
        """APM_ALLOW_PROTOCOL_FALLBACK=1 overrides config=False."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=False),
            patch.dict(os.environ, {"APM_ALLOW_PROTOCOL_FALLBACK": "1"}),
        ):
            result = cfg_module.get_apm_allow_protocol_fallback()
        assert result is True

    def test_config_true_used_when_cli_false_env_unset(self):
        """Config=True is used when CLI flag is False and env var is absent."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=True),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_ALLOW_PROTOCOL_FALLBACK", None)
            # Simulate: CLI flag = False, so we call get_apm_allow_protocol_fallback
            cli_flag = False
            result = cli_flag or cfg_module.get_apm_allow_protocol_fallback()
        assert result is True

    def test_default_false_when_all_layers_absent(self):
        """Returns False when CLI flag=False, env unset, and config=False."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=False),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_ALLOW_PROTOCOL_FALLBACK", None)
            cli_flag = False
            result = cli_flag or cfg_module.get_apm_allow_protocol_fallback()
        assert result is False

    @pytest.mark.parametrize("env_val", ["1", "true", "yes", "on", "TRUE", "Yes"])
    def test_env_var_truthy_values(self, env_val):
        """All accepted truthy values for APM_ALLOW_PROTOCOL_FALLBACK."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=False),
            patch.dict(os.environ, {"APM_ALLOW_PROTOCOL_FALLBACK": env_val}),
        ):
            assert cfg_module.get_apm_allow_protocol_fallback() is True

    @pytest.mark.parametrize("env_val", ["0", "false", "no", "off"])
    def test_env_var_explicit_falsy_overrides_config_true(self, env_val):
        """Explicit falsy env values return False even when config=True (env wins)."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=True),
            patch.dict(os.environ, {"APM_ALLOW_PROTOCOL_FALLBACK": env_val}),
        ):
            # Explicit falsy env var overrides persisted config=True
            assert cfg_module.get_apm_allow_protocol_fallback() is False


# ---------------------------------------------------------------------------
# get_apm_protocol_pref precedence (ssh preference)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ProtocolPreference.from_str round-trip
# ---------------------------------------------------------------------------


class TestProtocolPreferenceFromStr:
    """Verify ProtocolPreference.from_str handles get_apm_protocol_pref outputs."""

    def test_ssh_string_maps_to_ssh(self):
        """'ssh' maps to ProtocolPreference.SSH."""
        from apm_cli.deps.transport_selection import ProtocolPreference

        assert ProtocolPreference.from_str("ssh") == ProtocolPreference.SSH

    def test_https_string_maps_to_https(self):
        """'https' maps to ProtocolPreference.HTTPS."""
        from apm_cli.deps.transport_selection import ProtocolPreference

        assert ProtocolPreference.from_str("https") == ProtocolPreference.HTTPS

    def test_none_maps_to_none(self):
        """None maps to ProtocolPreference.NONE (uses git insteadOf)."""
        from apm_cli.deps.transport_selection import ProtocolPreference

        assert ProtocolPreference.from_str(None) == ProtocolPreference.NONE

    def test_empty_string_maps_to_none(self):
        """Empty string maps to ProtocolPreference.NONE."""
        from apm_cli.deps.transport_selection import ProtocolPreference

        assert ProtocolPreference.from_str("") == ProtocolPreference.NONE


# ---------------------------------------------------------------------------
# Round-trip: config set → get_apm_* helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fixture: isolated config (mirrors test_config_command.py fixture)
# ---------------------------------------------------------------------------
