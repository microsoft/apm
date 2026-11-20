"""Unit tests for GitHubTokenManager.get_token_env_var_for_purpose."""

from __future__ import annotations

import pytest

from apm_cli.core.token_manager import GitHubTokenManager

pytestmark = pytest.mark.unit


class TestGetTokenEnvVarForPurpose:
    def test_returns_selected_variable_without_exposing_value(self) -> None:
        mgr = GitHubTokenManager()
        env = {
            "GITHUB_COPILOT_PAT": "copilot-secret",
            "GITHUB_TOKEN": "generic-secret",
            "GITHUB_APM_PAT": "apm-secret",
        }
        assert mgr.get_token_env_var_for_purpose("copilot", env) == "GITHUB_COPILOT_PAT"

    def test_mcp_compatibility_fallback_is_not_a_general_purpose_source(self) -> None:
        mgr = GitHubTokenManager()
        env = {"GITHUB_PERSONAL_ACCESS_TOKEN": "compat-secret"}
        assert mgr.get_token_env_var_for_purpose("copilot", env) is None

    def test_returns_none_when_no_variable_is_selected(self) -> None:
        mgr = GitHubTokenManager()
        assert mgr.get_token_env_var_for_purpose("copilot", {}) is None

    def test_unknown_purpose_raises_value_error(self) -> None:
        mgr = GitHubTokenManager()
        with pytest.raises(ValueError, match="Unknown purpose"):
            mgr.get_token_env_var_for_purpose("nonexistent_purpose", {})
