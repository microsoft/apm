"""Tests for the apm config command (pure logic cases)."""

import os
import sys  # noqa: F401
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


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


# ---------------------------------------------------------------------------
# Isolation fixture used by storage-layer tests that perform real disk writes.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Storage layer -- copilot_cowork_skills_dir
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Storage layer -- unset_temp_dir (new function added in the same commit)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI -- apm config set copilot-cowork-skills-dir
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI -- apm config get copilot-cowork-skills-dir
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI -- apm config unset copilot-cowork-skills-dir / temp-dir
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI -- flag-gated listing (apm config and apm config get with no key)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Flag-gating regression -- only copilot-cowork-skills-dir should be gated
# ---------------------------------------------------------------------------


class TestValidConfigKeys:
    """Tests for _valid_config_keys() feature-flag gating."""

    def test_valid_config_keys_excludes_cowork_when_flag_off(self):
        """copilot-cowork-skills-dir is hidden when the copilot_cowork flag is off."""
        from apm_cli.commands.config import _valid_config_keys

        with patch("apm_cli.core.experimental.is_enabled", return_value=False):
            result = _valid_config_keys()

        assert "auto-integrate" not in result
        assert "temp-dir" in result
        assert "copilot-cowork-skills-dir" not in result

    def test_valid_config_keys_includes_cowork_when_flag_on(self):
        """copilot-cowork-skills-dir is listed when the copilot_cowork flag is on."""
        from apm_cli.commands.config import _valid_config_keys

        with patch("apm_cli.core.experimental.is_enabled", return_value=True):
            result = _valid_config_keys()

        assert "auto-integrate" not in result
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


class TestGithubAuthFirstFunctions:
    """Tests for get_github_auth_first and set_github_auth_first in apm_cli.config."""

    def test_get_github_auth_first_default_is_false(self):
        """Default value is False when not set."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "get_config", return_value={}):
            assert cfg_module.get_github_auth_first() is False

    def test_get_github_auth_first_true(self):
        """Returns True when set to True."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "get_config", return_value={"github_auth_first": True}):
            assert cfg_module.get_github_auth_first() is True

    def test_set_github_auth_first_calls_update_config(self):
        """set_github_auth_first delegates to update_config."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "update_config") as mock_update:
            cfg_module.set_github_auth_first(True)
            mock_update.assert_called_once_with({"github_auth_first": True})

    def test_set_github_auth_first_false_calls_update_config(self):
        """set_github_auth_first(False) passes False to update_config."""
        import apm_cli.config as cfg_module

        with patch.object(cfg_module, "update_config") as mock_update:
            cfg_module.set_github_auth_first(False)
            mock_update.assert_called_once_with({"github_auth_first": False})


class TestGetApmGithubAuthFirst:
    """Tests for get_apm_github_auth_first resolution chain."""

    def test_env_var_wins_over_config(self):
        """APM_GITHUB_AUTH_FIRST=1 wins even when config is False."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_github_auth_first", return_value=False),
            patch.dict(os.environ, {"APM_GITHUB_AUTH_FIRST": "1"}),
        ):
            assert cfg_module.get_apm_github_auth_first() is True

    def test_env_var_true_wins(self):
        """APM_GITHUB_AUTH_FIRST=true is accepted."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_github_auth_first", return_value=False),
            patch.dict(os.environ, {"APM_GITHUB_AUTH_FIRST": "true"}),
        ):
            assert cfg_module.get_apm_github_auth_first() is True

    def test_config_used_when_env_unset(self):
        """Config value is used when APM_GITHUB_AUTH_FIRST is unset."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_github_auth_first", return_value=True),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_GITHUB_AUTH_FIRST", None)
            assert cfg_module.get_apm_github_auth_first() is True

    def test_returns_false_when_both_unset(self):
        """Returns False when neither env var nor config is set."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_github_auth_first", return_value=False),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_GITHUB_AUTH_FIRST", None)
            assert cfg_module.get_apm_github_auth_first() is False

    def test_env_var_explicit_zero_overrides_config_true(self):
        """APM_GITHUB_AUTH_FIRST=0 overrides a persisted config value of True."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_github_auth_first", return_value=True),
            patch.dict(os.environ, {"APM_GITHUB_AUTH_FIRST": "0"}),
        ):
            assert cfg_module.get_apm_github_auth_first() is False

    def test_empty_env_var_falls_through_to_config(self):
        """Empty APM_GITHUB_AUTH_FIRST (unset/empty) falls back to config."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_github_auth_first", return_value=True),
            patch.dict(os.environ, {"APM_GITHUB_AUTH_FIRST": ""}),
        ):
            assert cfg_module.get_apm_github_auth_first() is True


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


# ---------------------------------------------------------------------------
# CLI -- apm config get allow-protocol-fallback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI -- apm config set github-auth-first
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI -- apm config get github-auth-first
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI -- apm config set prefer-ssh
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI -- apm config get prefer-ssh
# ---------------------------------------------------------------------------
