"""Unit tests for apm_cli.core.token_manager.

Covers error/edge branches not exercised by existing test suites:
- _format_credential_host (port vs no-port)
- _sanitize_credential_path (all filtering paths)
- _is_valid_credential_token (all rejection paths)
- _supports_gh_cli_host (all branches)
- _get_credential_timeout (valid / invalid / clamping)
- resolve_credential_from_git (success, fail, timeout, bad token)
- resolve_credential_from_gh_cli (eligibility gate, success, fail, bad token)
- setup_environment / _setup_*_tokens methods
- get_token_for_purpose (unknown purpose)
- get_token_with_credential_fallback (cache hits, gh CLI, git fallback)
- validate_tokens (no token, only fine-grained)
- Module-level helpers: setup_runtime_environment, validate_github_tokens,
  get_github_token_for_runtime
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.token_manager import (
    GitHubTokenManager,
    _format_credential_host,
    _sanitize_credential_path,
    get_github_token_for_runtime,
    setup_runtime_environment,
    validate_github_tokens,
)

# ---------------------------------------------------------------------------
# _format_credential_host
# ---------------------------------------------------------------------------


class TestFormatCredentialHost:
    def test_no_port_returns_host_only(self) -> None:
        assert _format_credential_host("github.com", None) == "github.com"

    def test_port_zero_is_embedded(self) -> None:
        # port=0 is not None so it must be embedded (even if unusual)
        assert _format_credential_host("myhost.com", 0) == "myhost.com:0"

    def test_custom_port_is_embedded(self) -> None:
        result = _format_credential_host("bitbucket.example.com", 7999)
        assert result == "bitbucket.example.com:7999"

    def test_standard_port_is_embedded(self) -> None:
        assert _format_credential_host("host.example.com", 443) == "host.example.com:443"


# ---------------------------------------------------------------------------
# _sanitize_credential_path
# ---------------------------------------------------------------------------


class TestSanitizeCredentialPath:
    def test_normal_owner_repo_is_preserved(self) -> None:
        assert _sanitize_credential_path("owner/repo") == "owner/repo"

    def test_leading_slash_is_stripped(self) -> None:
        assert _sanitize_credential_path("/owner/repo") == "owner/repo"

    def test_empty_string_returns_empty(self) -> None:
        assert _sanitize_credential_path("") == ""

    def test_only_slash_returns_empty(self) -> None:
        assert _sanitize_credential_path("/") == ""

    def test_control_char_newline_returns_empty(self) -> None:
        assert _sanitize_credential_path("owner/repo\ninjected=x") == ""

    def test_control_char_tab_returns_empty(self) -> None:
        assert _sanitize_credential_path("owner/repo\tmore") == ""

    def test_del_char_returns_empty(self) -> None:
        assert _sanitize_credential_path("owner/repo\x7f") == ""

    def test_low_control_char_returns_empty(self) -> None:
        assert _sanitize_credential_path("owner/\x01repo") == ""

    def test_space_inside_returns_empty(self) -> None:
        assert _sanitize_credential_path("owner/re po") == ""

    def test_https_url_extracts_path(self) -> None:
        result = _sanitize_credential_path("https://github.com/owner/repo")
        # Should extract the path component without leading slash
        assert result == "owner/repo"

    def test_http_url_extracts_path(self) -> None:
        result = _sanitize_credential_path("http://github.com/owner/repo")
        assert result == "owner/repo"

    def test_ssh_url_extracts_path(self) -> None:
        result = _sanitize_credential_path("ssh://github.com/owner/repo")
        assert result == "owner/repo"

    def test_disallowed_scheme_returns_empty(self) -> None:
        assert _sanitize_credential_path("file:///etc/passwd") == ""

    def test_data_scheme_returns_empty(self) -> None:
        assert _sanitize_credential_path("data:text/plain,hello") == ""

    def test_javascript_scheme_returns_empty(self) -> None:
        assert _sanitize_credential_path("javascript:alert(1)") == ""


# ---------------------------------------------------------------------------
# _is_valid_credential_token
# ---------------------------------------------------------------------------


class TestIsValidCredentialToken:
    def test_empty_string_is_invalid(self) -> None:
        assert not GitHubTokenManager._is_valid_credential_token("")

    def test_token_over_1024_bytes_is_invalid(self) -> None:
        assert not GitHubTokenManager._is_valid_credential_token("a" * 1025)

    def test_token_exactly_1024_bytes_is_valid(self) -> None:
        assert GitHubTokenManager._is_valid_credential_token("a" * 1024)

    def test_token_with_space_is_invalid(self) -> None:
        assert not GitHubTokenManager._is_valid_credential_token("ghp_abc def")

    def test_token_with_tab_is_invalid(self) -> None:
        assert not GitHubTokenManager._is_valid_credential_token("ghp_abc\tdef")

    def test_token_with_newline_is_invalid(self) -> None:
        assert not GitHubTokenManager._is_valid_credential_token("ghp_abc\ndef")

    def test_token_with_carriage_return_is_invalid(self) -> None:
        assert not GitHubTokenManager._is_valid_credential_token("ghp_abc\rdef")

    def test_prompt_password_for_is_invalid(self) -> None:
        assert not GitHubTokenManager._is_valid_credential_token("Password for https://github.com:")

    def test_prompt_username_for_is_invalid(self) -> None:
        assert not GitHubTokenManager._is_valid_credential_token("Username for https://github.com:")

    def test_lower_prompt_password_for_is_invalid(self) -> None:
        assert not GitHubTokenManager._is_valid_credential_token("password for you:")

    def test_lower_prompt_username_for_is_invalid(self) -> None:
        assert not GitHubTokenManager._is_valid_credential_token("username for me:")

    def test_normal_pat_is_valid(self) -> None:
        assert GitHubTokenManager._is_valid_credential_token("ghp_AbCdEfGhIjKlMnOpQrStUv1234567890")


# ---------------------------------------------------------------------------
# _supports_gh_cli_host
# ---------------------------------------------------------------------------


class TestSupportsGhCliHost:
    def test_none_returns_false(self) -> None:
        assert not GitHubTokenManager._supports_gh_cli_host(None)

    def test_empty_string_returns_false(self) -> None:
        assert not GitHubTokenManager._supports_gh_cli_host("")

    def test_github_com_returns_true(self) -> None:
        with patch("apm_cli.core.token_manager.is_github_hostname", return_value=True):
            assert GitHubTokenManager._supports_gh_cli_host("github.com")

    def test_ado_hostname_returns_false(self) -> None:
        with (
            patch("apm_cli.core.token_manager.is_github_hostname", return_value=False),
            patch("apm_cli.core.token_manager.default_host", return_value="dev.azure.com"),
            patch("apm_cli.core.token_manager.is_azure_devops_hostname", return_value=True),
        ):
            assert not GitHubTokenManager._supports_gh_cli_host("dev.azure.com")

    def test_unrelated_fqdn_returns_false(self) -> None:
        with (
            patch("apm_cli.core.token_manager.is_github_hostname", return_value=False),
            patch("apm_cli.core.token_manager.default_host", return_value="github.com"),
        ):
            # host_lower != configured_host -> False
            assert not GitHubTokenManager._supports_gh_cli_host("other.example.com")

    def test_configured_ghes_host_that_matches_returns_true(self) -> None:
        with (
            patch("apm_cli.core.token_manager.is_github_hostname", return_value=False),
            patch("apm_cli.core.token_manager.default_host", return_value="ghes.mycompany.com"),
            patch("apm_cli.core.token_manager.is_azure_devops_hostname", return_value=False),
            patch("apm_cli.core.token_manager.is_valid_fqdn", return_value=True),
        ):
            assert GitHubTokenManager._supports_gh_cli_host("ghes.mycompany.com")

    def test_configured_host_equal_to_github_com_returns_false(self) -> None:
        with (
            patch("apm_cli.core.token_manager.is_github_hostname", return_value=False),
            patch("apm_cli.core.token_manager.default_host", return_value="github.com"),
        ):
            # host equals configured but configured == github.com -> short-circuit False
            # (host_lower != configured_host since is_github_hostname was False)
            assert not GitHubTokenManager._supports_gh_cli_host("other.host.com")


# ---------------------------------------------------------------------------
# _get_credential_timeout
# ---------------------------------------------------------------------------


class TestGetCredentialTimeout:
    def test_default_when_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APM_GIT_CREDENTIAL_TIMEOUT", raising=False)
        assert GitHubTokenManager._get_credential_timeout() == 60

    def test_valid_value_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_GIT_CREDENTIAL_TIMEOUT", "90")
        assert GitHubTokenManager._get_credential_timeout() == 90

    def test_invalid_value_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_GIT_CREDENTIAL_TIMEOUT", "not-a-number")
        assert GitHubTokenManager._get_credential_timeout() == 60

    def test_value_clamped_to_max(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_GIT_CREDENTIAL_TIMEOUT", "9999")
        assert GitHubTokenManager._get_credential_timeout() == 180

    def test_value_clamped_to_min(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_GIT_CREDENTIAL_TIMEOUT", "0")
        assert GitHubTokenManager._get_credential_timeout() == 1

    def test_negative_value_clamped_to_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_GIT_CREDENTIAL_TIMEOUT", "-5")
        assert GitHubTokenManager._get_credential_timeout() == 1


# ---------------------------------------------------------------------------
# resolve_credential_from_git
# ---------------------------------------------------------------------------


class TestResolveCredentialFromGit:
    def test_returns_token_on_success(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "protocol=https\nhost=github.com\nusername=user\npassword=ghp_tok123\n"
        with patch("subprocess.run", return_value=mock_result):
            token = GitHubTokenManager.resolve_credential_from_git("github.com")
        assert token == "ghp_tok123"

    def test_returns_none_on_nonzero_exit(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_returns_none_when_no_password_line(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "protocol=https\nhost=github.com\nusername=user\n"
        with patch("subprocess.run", return_value=mock_result):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_returns_none_when_password_is_invalid(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "password=Password for https://github.com:\n"
        with patch("subprocess.run", return_value=mock_result):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_returns_none_on_timeout(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 60)):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_returns_none_on_file_not_found(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_returns_none_on_os_error(self) -> None:
        with patch("subprocess.run", side_effect=OSError("no git")):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_path_parameter_included_in_request(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "password=ghp_abc\n"
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            GitHubTokenManager.resolve_credential_from_git("github.com", path="owner/repo")
        call_kwargs = mock_run.call_args
        stdin_arg = call_kwargs.kwargs.get("input") or call_kwargs[1].get("input", "")
        assert "path=owner/repo" in stdin_arg

    def test_port_embedded_in_host_field(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "password=ghp_abc\n"
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            GitHubTokenManager.resolve_credential_from_git("bitbucket.example.com", port=7999)
        stdin_arg = mock_run.call_args.kwargs.get("input") or mock_run.call_args[1].get("input", "")
        assert "host=bitbucket.example.com:7999" in stdin_arg

    def test_invalid_path_sanitized_away(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "password=ghp_abc\n"
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            GitHubTokenManager.resolve_credential_from_git("github.com", path="bad\npath")
        stdin_arg = mock_run.call_args.kwargs.get("input") or mock_run.call_args[1].get("input", "")
        # Sanitized path is empty so the path= line is omitted
        assert "path=" not in stdin_arg


# ---------------------------------------------------------------------------
# resolve_credential_from_gh_cli
# ---------------------------------------------------------------------------


class TestResolveCredentialFromGhCli:
    def test_unsupported_host_returns_none_without_subprocess(self) -> None:
        with (
            patch.object(GitHubTokenManager, "_supports_gh_cli_host", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            assert GitHubTokenManager.resolve_credential_from_gh_cli("other.example.com") is None
            mock_run.assert_not_called()

    def test_returns_token_on_success(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ghp_tok123\n"
        with (
            patch.object(GitHubTokenManager, "_supports_gh_cli_host", return_value=True),
            patch("subprocess.run", return_value=mock_result),
        ):
            assert GitHubTokenManager.resolve_credential_from_gh_cli("github.com") == "ghp_tok123"

    def test_returns_none_on_nonzero_exit(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "not logged in"
        mock_result.stdout = ""
        with (
            patch.object(GitHubTokenManager, "_supports_gh_cli_host", return_value=True),
            patch("subprocess.run", return_value=mock_result),
        ):
            assert GitHubTokenManager.resolve_credential_from_gh_cli("github.com") is None

    def test_returns_none_on_invalid_token(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Username for https://github.com:\n"
        with (
            patch.object(GitHubTokenManager, "_supports_gh_cli_host", return_value=True),
            patch("subprocess.run", return_value=mock_result),
        ):
            assert GitHubTokenManager.resolve_credential_from_gh_cli("github.com") is None

    def test_returns_none_on_timeout(self) -> None:
        with (
            patch.object(GitHubTokenManager, "_supports_gh_cli_host", return_value=True),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 60)),
        ):
            assert GitHubTokenManager.resolve_credential_from_gh_cli("github.com") is None

    def test_returns_none_on_file_not_found(self) -> None:
        with (
            patch.object(GitHubTokenManager, "_supports_gh_cli_host", return_value=True),
            patch("subprocess.run", side_effect=FileNotFoundError),
        ):
            assert GitHubTokenManager.resolve_credential_from_gh_cli("github.com") is None


# ---------------------------------------------------------------------------
# get_token_for_purpose
# ---------------------------------------------------------------------------


class TestGetTokenForPurpose:
    def test_unknown_purpose_raises_value_error(self) -> None:
        mgr = GitHubTokenManager()
        with pytest.raises(ValueError, match="Unknown purpose"):
            mgr.get_token_for_purpose("nonexistent_purpose")

    def test_returns_first_matching_env_var(self) -> None:
        mgr = GitHubTokenManager()
        env = {"GITHUB_APM_PAT": "apm-tok", "GITHUB_TOKEN": "models-tok"}
        assert mgr.get_token_for_purpose("modules", env) == "apm-tok"

    def test_returns_fallback_env_var(self) -> None:
        mgr = GitHubTokenManager()
        env = {"GITHUB_TOKEN": "models-tok"}
        assert mgr.get_token_for_purpose("modules", env) == "models-tok"

    def test_returns_none_when_no_env_var(self) -> None:
        mgr = GitHubTokenManager()
        assert mgr.get_token_for_purpose("modules", {}) is None

    def test_ado_modules_purpose(self) -> None:
        mgr = GitHubTokenManager()
        env = {"ADO_APM_PAT": "ado-tok"}
        assert mgr.get_token_for_purpose("ado_modules", env) == "ado-tok"


# ---------------------------------------------------------------------------
# get_token_with_credential_fallback
# ---------------------------------------------------------------------------


class TestGetTokenWithCredentialFallback:
    def test_env_token_wins_without_subprocess(self) -> None:
        mgr = GitHubTokenManager()
        env = {"GITHUB_APM_PAT": "env-tok"}
        with patch.object(mgr, "resolve_credential_from_git") as mock_git:
            result = mgr.get_token_with_credential_fallback("modules", "github.com", env)
        assert result == "env-tok"
        mock_git.assert_not_called()

    def test_cache_hit_returns_cached_value(self) -> None:
        mgr = GitHubTokenManager()
        mgr._credential_cache[("github.com", None)] = "cached-tok"
        result = mgr.get_token_with_credential_fallback("modules", "github.com", {})
        assert result == "cached-tok"

    def test_gh_cli_fallback_on_github_host(self) -> None:
        mgr = GitHubTokenManager()
        with (
            patch.object(mgr, "_supports_gh_cli_host", return_value=True),
            patch.object(mgr, "resolve_credential_from_gh_cli", return_value="gh-tok"),
        ):
            result = mgr.get_token_with_credential_fallback("modules", "github.com", {})
        assert result == "gh-tok"
        assert mgr._credential_cache[("github.com", None)] == "gh-tok"

    def test_git_credential_fallback(self) -> None:
        mgr = GitHubTokenManager()
        with (
            patch.object(mgr, "_supports_gh_cli_host", return_value=False),
            patch.object(mgr, "resolve_credential_from_git", return_value="git-tok"),
        ):
            result = mgr.get_token_with_credential_fallback("modules", "bitbucket.example.com", {})
        assert result == "git-tok"

    def test_port_is_part_of_cache_key(self) -> None:
        mgr = GitHubTokenManager()
        mgr._credential_cache[("host.com", 7999)] = "port-tok"
        result = mgr.get_token_with_credential_fallback("modules", "host.com", {}, port=7999)
        assert result == "port-tok"

    def test_none_is_cached_when_no_credential_found(self) -> None:
        mgr = GitHubTokenManager()
        with (
            patch.object(mgr, "_supports_gh_cli_host", return_value=False),
            patch.object(mgr, "resolve_credential_from_git", return_value=None),
        ):
            result = mgr.get_token_with_credential_fallback("modules", "missing.example.com", {})
        assert result is None
        assert ("missing.example.com", None) in mgr._credential_cache


# ---------------------------------------------------------------------------
# validate_tokens
# ---------------------------------------------------------------------------


class TestValidateTokens:
    def test_no_tokens_returns_false(self) -> None:
        mgr = GitHubTokenManager()
        valid, msg = mgr.validate_tokens({})
        assert not valid
        assert "No tokens found" in msg

    def test_github_token_present_returns_true(self) -> None:
        mgr = GitHubTokenManager()
        valid, _ = mgr.validate_tokens({"GITHUB_TOKEN": "tok"})
        assert valid

    def test_only_apm_pat_is_valid(self) -> None:
        mgr = GitHubTokenManager()
        # GITHUB_APM_PAT satisfies 'modules' (and also 'models' as fallback) -> valid
        valid, _ = mgr.validate_tokens({"GITHUB_APM_PAT": "tok"})
        assert valid

    def test_ado_pat_only_passes_validation(self) -> None:
        mgr = GitHubTokenManager()
        # ADO_APM_PAT satisfies 'ado_modules' but not copilot/models/modules
        # has_any_token checks copilot/models/modules only -> returns False
        valid, _ = mgr.validate_tokens({"ADO_APM_PAT": "ado-tok"})
        assert not valid

    def test_copilot_pat_is_valid(self) -> None:
        mgr = GitHubTokenManager()
        valid, _ = mgr.validate_tokens({"GITHUB_COPILOT_PAT": "tok"})
        assert valid


# ---------------------------------------------------------------------------
# setup_environment / _setup_* helpers
# ---------------------------------------------------------------------------


class TestSetupEnvironment:
    def test_copilot_token_set_when_missing(self) -> None:
        mgr = GitHubTokenManager()
        env = mgr.setup_environment({"GITHUB_COPILOT_PAT": "cop-tok"})
        assert env.get("GH_TOKEN") == "cop-tok"
        assert env.get("GITHUB_PERSONAL_ACCESS_TOKEN") == "cop-tok"

    def test_preserve_existing_does_not_overwrite(self) -> None:
        mgr = GitHubTokenManager(preserve_existing=True)
        env = mgr.setup_environment({"GITHUB_COPILOT_PAT": "cop-tok", "GH_TOKEN": "existing"})
        assert env["GH_TOKEN"] == "existing"

    def test_llm_github_models_key_set(self) -> None:
        mgr = GitHubTokenManager()
        env = mgr.setup_environment({"GITHUB_TOKEN": "models-tok"})
        assert env.get("GITHUB_MODELS_KEY") == "models-tok"

    def test_llm_preserve_existing_github_models_key(self) -> None:
        mgr = GitHubTokenManager(preserve_existing=True)
        env = mgr.setup_environment(
            {"GITHUB_TOKEN": "new-tok", "GITHUB_MODELS_KEY": "existing-key"}
        )
        assert env["GITHUB_MODELS_KEY"] == "existing-key"

    def test_no_copilot_token_skips_copilot_vars(self) -> None:
        mgr = GitHubTokenManager()
        env = mgr.setup_environment({})
        assert "GH_TOKEN" not in env
        assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in env

    def test_returns_copy_of_os_environ_when_none_passed(self) -> None:
        mgr = GitHubTokenManager()
        with patch("os.environ", {"GITHUB_TOKEN": "env-tok"}):
            env = mgr.setup_environment()
        # Should not raise; the returned dict should be a dict
        assert isinstance(env, dict)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestModuleLevelHelpers:
    def test_setup_runtime_environment_returns_dict(self) -> None:
        result = setup_runtime_environment({"GITHUB_TOKEN": "tok"})
        assert isinstance(result, dict)

    def test_validate_github_tokens_delegates_to_manager(self) -> None:
        valid, _ = validate_github_tokens({"GITHUB_TOKEN": "tok"})
        assert valid

    def test_validate_github_tokens_no_tokens(self) -> None:
        valid, _ = validate_github_tokens({})
        assert not valid

    def test_get_github_token_for_runtime_copilot(self) -> None:
        result = get_github_token_for_runtime("copilot", {"GITHUB_COPILOT_PAT": "cop-tok"})
        assert result == "cop-tok"

    def test_get_github_token_for_runtime_codex(self) -> None:
        result = get_github_token_for_runtime("codex", {"GITHUB_TOKEN": "models-tok"})
        assert result == "models-tok"

    def test_get_github_token_for_runtime_llm(self) -> None:
        result = get_github_token_for_runtime("llm", {"GITHUB_TOKEN": "llm-tok"})
        assert result == "llm-tok"

    def test_get_github_token_for_runtime_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown runtime"):
            get_github_token_for_runtime("unknown_runtime", {})
