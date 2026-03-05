"""Unit tests for the GitHubTokenManager and token management utilities."""

import os
from unittest.mock import patch

import pytest

from apm_cli.core.token_manager import (
    GitHubTokenManager,
    get_github_token_for_runtime,
    setup_runtime_environment,
    validate_github_tokens,
)


class TestGitHubTokenManagerInit:
    """Tests for GitHubTokenManager initialization."""

    def test_default_preserve_existing_is_true(self):
        mgr = GitHubTokenManager()
        assert mgr.preserve_existing is True

    def test_preserve_existing_can_be_set_false(self):
        mgr = GitHubTokenManager(preserve_existing=False)
        assert mgr.preserve_existing is False


class TestGetTokenForPurpose:
    """Tests for get_token_for_purpose()."""

    def test_returns_none_when_no_tokens(self):
        mgr = GitHubTokenManager()
        result = mgr.get_token_for_purpose("copilot", {})
        assert result is None

    def test_raises_for_unknown_purpose(self):
        mgr = GitHubTokenManager()
        with pytest.raises(ValueError, match="Unknown purpose"):
            mgr.get_token_for_purpose("unknown", {})

    def test_copilot_purpose_prefers_copilot_pat(self):
        mgr = GitHubTokenManager()
        env = {
            "GITHUB_COPILOT_PAT": "copilot-token",
            "GITHUB_TOKEN": "generic-token",
            "GITHUB_APM_PAT": "apm-token",
        }
        assert mgr.get_token_for_purpose("copilot", env) == "copilot-token"

    def test_copilot_purpose_falls_back_to_github_token(self):
        mgr = GitHubTokenManager()
        env = {"GITHUB_TOKEN": "generic-token", "GITHUB_APM_PAT": "apm-token"}
        assert mgr.get_token_for_purpose("copilot", env) == "generic-token"

    def test_copilot_purpose_falls_back_to_apm_pat(self):
        mgr = GitHubTokenManager()
        env = {"GITHUB_APM_PAT": "apm-token"}
        assert mgr.get_token_for_purpose("copilot", env) == "apm-token"

    def test_models_purpose_prefers_github_token(self):
        mgr = GitHubTokenManager()
        env = {"GITHUB_TOKEN": "generic-token", "GITHUB_APM_PAT": "apm-token"}
        assert mgr.get_token_for_purpose("models", env) == "generic-token"

    def test_models_purpose_falls_back_to_apm_pat(self):
        mgr = GitHubTokenManager()
        env = {"GITHUB_APM_PAT": "apm-token"}
        assert mgr.get_token_for_purpose("models", env) == "apm-token"

    def test_modules_purpose_prefers_apm_pat(self):
        mgr = GitHubTokenManager()
        env = {"GITHUB_APM_PAT": "apm-token", "GITHUB_TOKEN": "generic-token"}
        assert mgr.get_token_for_purpose("modules", env) == "apm-token"

    def test_modules_purpose_falls_back_to_github_token(self):
        mgr = GitHubTokenManager()
        env = {"GITHUB_TOKEN": "generic-token"}
        assert mgr.get_token_for_purpose("modules", env) == "generic-token"

    def test_ado_modules_purpose_returns_ado_token(self):
        mgr = GitHubTokenManager()
        env = {"ADO_APM_PAT": "ado-token", "GITHUB_TOKEN": "generic-token"}
        assert mgr.get_token_for_purpose("ado_modules", env) == "ado-token"

    def test_ado_modules_returns_none_without_ado_token(self):
        mgr = GitHubTokenManager()
        env = {"GITHUB_TOKEN": "generic-token"}
        assert mgr.get_token_for_purpose("ado_modules", env) is None

    def test_uses_os_environ_when_env_is_none(self):
        mgr = GitHubTokenManager()
        with patch.dict(os.environ, {"GITHUB_TOKEN": "env-token"}, clear=False):
            result = mgr.get_token_for_purpose("models", None)
        assert result == "env-token"

    def test_ignores_empty_string_tokens(self):
        mgr = GitHubTokenManager()
        env = {"GITHUB_COPILOT_PAT": "", "GITHUB_TOKEN": "generic-token"}
        assert mgr.get_token_for_purpose("copilot", env) == "generic-token"


class TestValidateTokens:
    """Tests for validate_tokens()."""

    def test_returns_false_when_no_tokens(self):
        mgr = GitHubTokenManager()
        valid, msg = mgr.validate_tokens({})
        assert valid is False
        assert "No tokens found" in msg

    def test_returns_true_with_github_token(self):
        mgr = GitHubTokenManager()
        valid, msg = mgr.validate_tokens({"GITHUB_TOKEN": "tok"})
        assert valid is True

    def test_returns_true_with_copilot_pat(self):
        mgr = GitHubTokenManager()
        valid, msg = mgr.validate_tokens({"GITHUB_COPILOT_PAT": "copilot-tok"})
        assert valid is True

    def test_returns_true_with_apm_pat(self):
        # GITHUB_APM_PAT is a valid 'models' token (fallback), so no warning is produced.
        mgr = GitHubTokenManager()
        valid, msg = mgr.validate_tokens({"GITHUB_APM_PAT": "apm-tok"})
        assert valid is True

    def test_returns_true_with_all_tokens(self):
        mgr = GitHubTokenManager()
        env = {
            "GITHUB_COPILOT_PAT": "copilot-tok",
            "GITHUB_TOKEN": "generic-tok",
            "GITHUB_APM_PAT": "apm-tok",
        }
        valid, msg = mgr.validate_tokens(env)
        assert valid is True
        assert "passed" in msg

    def test_uses_os_environ_when_env_is_none(self):
        mgr = GitHubTokenManager()
        with patch.dict(os.environ, {"GITHUB_TOKEN": "env-tok"}, clear=False):
            valid, msg = mgr.validate_tokens(None)
        assert valid is True

    def test_apm_pat_alone_passes_without_warning(self):
        # GITHUB_APM_PAT satisfies 'models' purpose, so the warning branch is unreachable.
        mgr = GitHubTokenManager()
        env = {"GITHUB_APM_PAT": "fine-grained"}
        valid, msg = mgr.validate_tokens(env)
        assert valid is True
        assert "passed" in msg


class TestSetupEnvironment:
    """Tests for setup_environment()."""

    def test_returns_env_dict(self):
        mgr = GitHubTokenManager()
        result = mgr.setup_environment({"GITHUB_TOKEN": "tok"})
        assert isinstance(result, dict)

    def test_sets_gh_token_for_copilot(self):
        mgr = GitHubTokenManager()
        env = {"GITHUB_COPILOT_PAT": "copilot-tok"}
        result = mgr.setup_environment(env)
        assert result["GH_TOKEN"] == "copilot-tok"
        assert result["GITHUB_PERSONAL_ACCESS_TOKEN"] == "copilot-tok"

    def test_sets_github_models_key_for_llm(self):
        mgr = GitHubTokenManager()
        env = {"GITHUB_TOKEN": "generic-tok"}
        result = mgr.setup_environment(env)
        assert result["GITHUB_MODELS_KEY"] == "generic-tok"

    def test_preserve_existing_does_not_overwrite(self):
        mgr = GitHubTokenManager(preserve_existing=True)
        env = {
            "GITHUB_COPILOT_PAT": "copilot-tok",
            "GH_TOKEN": "existing-gh-token",
        }
        result = mgr.setup_environment(env)
        assert result["GH_TOKEN"] == "existing-gh-token"

    def test_preserve_existing_false_overwrites(self):
        mgr = GitHubTokenManager(preserve_existing=False)
        env = {
            "GITHUB_COPILOT_PAT": "copilot-tok",
            "GH_TOKEN": "existing-gh-token",
        }
        result = mgr.setup_environment(env)
        assert result["GH_TOKEN"] == "copilot-tok"

    def test_uses_os_environ_copy_when_env_is_none(self):
        mgr = GitHubTokenManager()
        with patch.dict(os.environ, {"GITHUB_TOKEN": "env-tok"}, clear=False):
            result = mgr.setup_environment(None)
        assert "GITHUB_TOKEN" in result

    def test_no_tokens_available_does_not_fail(self):
        mgr = GitHubTokenManager()
        result = mgr.setup_environment({})
        assert isinstance(result, dict)


class TestSetupCopilotTokens:
    """Tests for _setup_copilot_tokens()."""

    def test_sets_both_copilot_env_vars(self):
        mgr = GitHubTokenManager()
        env = {}
        available = {"GITHUB_COPILOT_PAT": "copilot-tok"}
        mgr._setup_copilot_tokens(env, available)
        assert env["GH_TOKEN"] == "copilot-tok"
        assert env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "copilot-tok"

    def test_no_op_when_no_copilot_token(self):
        mgr = GitHubTokenManager()
        env = {}
        mgr._setup_copilot_tokens(env, {})
        assert "GH_TOKEN" not in env

    def test_skips_existing_when_preserve_true(self):
        mgr = GitHubTokenManager(preserve_existing=True)
        env = {"GH_TOKEN": "existing"}
        available = {"GITHUB_COPILOT_PAT": "copilot-tok"}
        mgr._setup_copilot_tokens(env, available)
        assert env["GH_TOKEN"] == "existing"

    def test_overwrites_when_preserve_false(self):
        mgr = GitHubTokenManager(preserve_existing=False)
        env = {"GH_TOKEN": "existing"}
        available = {"GITHUB_COPILOT_PAT": "copilot-tok"}
        mgr._setup_copilot_tokens(env, available)
        assert env["GH_TOKEN"] == "copilot-tok"


class TestSetupCodexTokens:
    """Tests for _setup_codex_tokens()."""

    def test_sets_github_token_from_models_token(self):
        mgr = GitHubTokenManager()
        env = {}
        available = {"GITHUB_TOKEN": "generic-tok"}
        mgr._setup_codex_tokens(env, available)
        assert env["GITHUB_TOKEN"] == "generic-tok"

    def test_sets_github_apm_pat_when_available(self):
        mgr = GitHubTokenManager()
        env = {}
        available = {"GITHUB_APM_PAT": "apm-tok"}
        mgr._setup_codex_tokens(env, available)
        assert env["GITHUB_APM_PAT"] == "apm-tok"

    def test_does_not_set_github_token_when_already_present(self):
        mgr = GitHubTokenManager(preserve_existing=True)
        env = {"GITHUB_TOKEN": "existing-tok"}
        available = {"GITHUB_TOKEN": "new-tok"}
        mgr._setup_codex_tokens(env, available)
        assert env["GITHUB_TOKEN"] == "existing-tok"


class TestSetupLlmTokens:
    """Tests for _setup_llm_tokens()."""

    def test_sets_github_models_key(self):
        mgr = GitHubTokenManager()
        env = {}
        available = {"GITHUB_TOKEN": "tok"}
        mgr._setup_llm_tokens(env, available)
        assert env["GITHUB_MODELS_KEY"] == "tok"

    def test_skips_existing_when_preserve_true(self):
        mgr = GitHubTokenManager(preserve_existing=True)
        env = {"GITHUB_MODELS_KEY": "existing"}
        available = {"GITHUB_TOKEN": "tok"}
        mgr._setup_llm_tokens(env, available)
        assert env["GITHUB_MODELS_KEY"] == "existing"

    def test_overwrites_when_preserve_false(self):
        mgr = GitHubTokenManager(preserve_existing=False)
        env = {"GITHUB_MODELS_KEY": "existing"}
        available = {"GITHUB_TOKEN": "tok"}
        mgr._setup_llm_tokens(env, available)
        assert env["GITHUB_MODELS_KEY"] == "tok"

    def test_no_op_when_no_models_token(self):
        mgr = GitHubTokenManager()
        env = {}
        mgr._setup_llm_tokens(env, {})
        assert "GITHUB_MODELS_KEY" not in env

    def test_prefers_github_token_over_apm_pat_for_llm(self):
        mgr = GitHubTokenManager()
        env = {}
        available = {"GITHUB_TOKEN": "generic-tok", "GITHUB_APM_PAT": "apm-tok"}
        mgr._setup_llm_tokens(env, available)
        assert env["GITHUB_MODELS_KEY"] == "generic-tok"


class TestGetAvailableTokens:
    """Tests for _get_available_tokens()."""

    def test_returns_only_present_tokens(self):
        mgr = GitHubTokenManager()
        env = {"GITHUB_TOKEN": "tok1", "GITHUB_APM_PAT": "tok2"}
        tokens = mgr._get_available_tokens(env)
        assert tokens["GITHUB_TOKEN"] == "tok1"
        assert tokens["GITHUB_APM_PAT"] == "tok2"
        assert "GITHUB_COPILOT_PAT" not in tokens

    def test_ignores_empty_values(self):
        mgr = GitHubTokenManager()
        env = {"GITHUB_TOKEN": "", "GITHUB_APM_PAT": "tok"}
        tokens = mgr._get_available_tokens(env)
        assert "GITHUB_TOKEN" not in tokens
        assert tokens["GITHUB_APM_PAT"] == "tok"

    def test_returns_empty_dict_when_no_tokens(self):
        mgr = GitHubTokenManager()
        tokens = mgr._get_available_tokens({})
        assert tokens == {}


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_setup_runtime_environment_returns_dict(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False):
            result = setup_runtime_environment()
        assert isinstance(result, dict)

    def test_setup_runtime_environment_with_explicit_env(self):
        env = {"GITHUB_TOKEN": "explicit-tok"}
        result = setup_runtime_environment(env)
        assert "GITHUB_TOKEN" in result

    def test_validate_github_tokens_valid(self):
        env = {"GITHUB_TOKEN": "tok"}
        valid, msg = validate_github_tokens(env)
        assert valid is True

    def test_validate_github_tokens_invalid(self):
        valid, msg = validate_github_tokens({})
        assert valid is False

    def test_get_github_token_for_runtime_copilot(self):
        env = {"GITHUB_COPILOT_PAT": "copilot-tok"}
        result = get_github_token_for_runtime("copilot", env)
        assert result == "copilot-tok"

    def test_get_github_token_for_runtime_codex(self):
        env = {"GITHUB_TOKEN": "generic-tok"}
        result = get_github_token_for_runtime("codex", env)
        assert result == "generic-tok"

    def test_get_github_token_for_runtime_llm(self):
        env = {"GITHUB_TOKEN": "generic-tok"}
        result = get_github_token_for_runtime("llm", env)
        assert result == "generic-tok"

    def test_get_github_token_for_runtime_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown runtime"):
            get_github_token_for_runtime("unknown", {})

    def test_get_github_token_for_runtime_uses_os_environ(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "env-tok"}, clear=False):
            result = get_github_token_for_runtime("codex", None)
        assert result == "env-tok"
