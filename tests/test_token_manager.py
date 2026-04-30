"""Comprehensive tests for GitHubTokenManager."""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest  # noqa: F401

from src.apm_cli.core.token_manager import GitHubTokenManager


class TestModulesTokenPrecedence:
    """Test GH_TOKEN addition to the modules token precedence chain."""

    def test_gh_token_used_when_no_other_tokens(self):
        """GH_TOKEN is used when GITHUB_APM_PAT and GITHUB_TOKEN are not set."""
        with patch.dict(os.environ, {"GH_TOKEN": "gh-cli-token"}, clear=True):
            manager = GitHubTokenManager()
            token = manager.get_token_for_purpose("modules")
            assert token == "gh-cli-token"

    def test_github_apm_pat_takes_precedence_over_gh_token(self):
        """GITHUB_APM_PAT takes precedence over GH_TOKEN."""
        with patch.dict(
            os.environ,
            {
                "GITHUB_APM_PAT": "apm-pat",
                "GH_TOKEN": "gh-cli-token",
            },
            clear=True,
        ):
            manager = GitHubTokenManager()
            token = manager.get_token_for_purpose("modules")
            assert token == "apm-pat"

    def test_github_token_takes_precedence_over_gh_token(self):
        """GITHUB_TOKEN takes precedence over GH_TOKEN."""
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "generic-token",
                "GH_TOKEN": "gh-cli-token",
            },
            clear=True,
        ):
            manager = GitHubTokenManager()
            token = manager.get_token_for_purpose("modules")
            assert token == "generic-token"

    def test_all_three_tokens_apm_pat_wins(self):
        """When all three tokens are present, GITHUB_APM_PAT wins."""
        with patch.dict(
            os.environ,
            {
                "GITHUB_APM_PAT": "apm-pat",
                "GITHUB_TOKEN": "generic-token",
                "GH_TOKEN": "gh-cli-token",
            },
            clear=True,
        ):
            manager = GitHubTokenManager()
            token = manager.get_token_for_purpose("modules")
            assert token == "apm-pat"

    def test_modules_precedence_order(self):
        """TOKEN_PRECEDENCE['modules'] has the expected order."""
        assert GitHubTokenManager.TOKEN_PRECEDENCE["modules"] == [
            "GITHUB_APM_PAT",
            "GITHUB_TOKEN",
            "GH_TOKEN",
        ]

    def test_no_tokens_returns_none(self):
        """Returns None when no module tokens are set."""
        with patch.dict(os.environ, {}, clear=True):
            manager = GitHubTokenManager()
            assert manager.get_token_for_purpose("modules") is None


class TestResolveCredentialFromGit:
    """Test resolve_credential_from_git static method."""

    def test_success_returns_password(self):
        """Parses password from successful git credential fill output."""
        mock_result = MagicMock(
            returncode=0,
            stdout="protocol=https\nhost=github.com\nusername=user\npassword=ghp_token123\n",
        )
        with patch("subprocess.run", return_value=mock_result):
            token = GitHubTokenManager.resolve_credential_from_git("github.com")
            assert token == "ghp_token123"

    def test_no_password_line_returns_none(self):
        """Returns None when output has no password= line."""
        mock_result = MagicMock(
            returncode=0,
            stdout="protocol=https\nhost=github.com\nusername=user\n",
        )
        with patch("subprocess.run", return_value=mock_result):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_empty_password_returns_none(self):
        """Returns None when password= value is empty."""
        mock_result = MagicMock(
            returncode=0,
            stdout="protocol=https\nhost=github.com\npassword=\n",
        )
        with patch("subprocess.run", return_value=mock_result):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_nonzero_exit_code_returns_none(self):
        """Returns None on non-zero exit code."""
        mock_result = MagicMock(returncode=1, stdout="")
        with patch("subprocess.run", return_value=mock_result):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_timeout_returns_none(self):
        """Returns None when subprocess times out."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5)):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_file_not_found_returns_none(self):
        """Returns None when git is not installed."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_os_error_returns_none(self):
        """Returns None on generic OSError."""
        with patch("subprocess.run", side_effect=OSError("unexpected")):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_correct_input_sent(self):
        """Verifies protocol=https and host are sent as input."""
        mock_result = MagicMock(returncode=0, stdout="password=tok\n")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            GitHubTokenManager.resolve_credential_from_git("github.com")
            call_kwargs = mock_run.call_args
            assert call_kwargs.kwargs["input"] == "protocol=https\nhost=github.com\n\n"

    def test_git_terminal_prompt_disabled(self):
        """GIT_TERMINAL_PROMPT=0 is set in the subprocess env."""
        mock_result = MagicMock(returncode=0, stdout="password=tok\n")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            GitHubTokenManager.resolve_credential_from_git("github.com")
            call_env = mock_run.call_args.kwargs["env"]
            assert call_env["GIT_TERMINAL_PROMPT"] == "0"

    def test_git_askpass_set_to_empty(self):
        """GIT_ASKPASS is set to empty string (not 'echo') to prevent prompt echo."""
        mock_result = MagicMock(returncode=0, stdout="password=tok\n")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            GitHubTokenManager.resolve_credential_from_git("github.com")
            call_env = mock_run.call_args.kwargs["env"]
            assert call_env["GIT_ASKPASS"] == ""

    def test_rejects_password_prompt_as_token(self):
        """Rejects 'Password for ...' prompt text echoed back by GIT_ASKPASS."""
        mock_result = MagicMock(
            returncode=0,
            stdout="password=Password for 'https://github.com': \n",
        )
        with patch("subprocess.run", return_value=mock_result):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_rejects_username_prompt_as_token(self):
        """Rejects 'Username for ...' prompt text."""
        mock_result = MagicMock(
            returncode=0,
            stdout="password=Username for 'https://github.com': \n",
        )
        with patch("subprocess.run", return_value=mock_result):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_rejects_token_with_spaces(self):
        """Rejects tokens containing spaces (likely prompt garbage)."""
        mock_result = MagicMock(
            returncode=0,
            stdout="password=some garbage token value\n",
        )
        with patch("subprocess.run", return_value=mock_result):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_rejects_token_with_tabs(self):
        """Rejects tokens containing tab characters."""
        mock_result = MagicMock(
            returncode=0,
            stdout="password=some\ttoken\n",
        )
        with patch("subprocess.run", return_value=mock_result):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_rejects_excessively_long_token(self):
        """Rejects tokens longer than 1024 characters."""
        mock_result = MagicMock(
            returncode=0,
            stdout=f"password={'x' * 1025}\n",
        )
        with patch("subprocess.run", return_value=mock_result):
            assert GitHubTokenManager.resolve_credential_from_git("github.com") is None

    def test_accepts_valid_ghp_token(self):
        """Accepts a normal GitHub PAT (ghp_ prefix)."""
        mock_result = MagicMock(
            returncode=0,
            stdout="password=ghp_abcdefghijk1234567890abcdefghijk1234\n",
        )
        with patch("subprocess.run", return_value=mock_result):
            token = GitHubTokenManager.resolve_credential_from_git("github.com")
            assert token == "ghp_abcdefghijk1234567890abcdefghijk1234"

    def test_accepts_valid_gho_token(self):
        """Accepts a GitHub OAuth token (gho_ prefix)."""
        mock_result = MagicMock(
            returncode=0,
            stdout="password=gho_abc123def456\n",
        )
        with patch("subprocess.run", return_value=mock_result):
            token = GitHubTokenManager.resolve_credential_from_git("github.com")
            assert token == "gho_abc123def456"


class TestCredentialTimeout:
    """Tests for configurable git credential fill timeout."""

    def test_default_timeout_is_60(self):
        with patch.dict(os.environ, {}, clear=True):
            assert GitHubTokenManager._get_credential_timeout() == 60

    def test_env_override(self):
        with patch.dict(os.environ, {"APM_GIT_CREDENTIAL_TIMEOUT": "42"}):
            assert GitHubTokenManager._get_credential_timeout() == 42

    def test_clamps_to_max(self):
        with patch.dict(os.environ, {"APM_GIT_CREDENTIAL_TIMEOUT": "999"}):
            assert GitHubTokenManager._get_credential_timeout() == 180

    def test_clamps_to_min(self):
        with patch.dict(os.environ, {"APM_GIT_CREDENTIAL_TIMEOUT": "0"}):
            assert GitHubTokenManager._get_credential_timeout() == 1

    def test_invalid_value_falls_back(self):
        with patch.dict(os.environ, {"APM_GIT_CREDENTIAL_TIMEOUT": "abc"}):
            assert GitHubTokenManager._get_credential_timeout() == 60

    def test_timeout_used_in_subprocess(self):
        mock_result = MagicMock(returncode=0, stdout="password=tok\n")
        with (
            patch.dict(os.environ, {"APM_GIT_CREDENTIAL_TIMEOUT": "90"}, clear=True),
            patch("subprocess.run", return_value=mock_result) as mock_run,
        ):
            GitHubTokenManager.resolve_credential_from_git("github.com")
            assert mock_run.call_args.kwargs["timeout"] == 90


class TestIsValidCredentialToken:
    """Test _is_valid_credential_token validation."""

    def test_empty_string_invalid(self):
        assert not GitHubTokenManager._is_valid_credential_token("")

    def test_none_coerced_invalid(self):
        """None would fail the truthiness check (caller already guards this)."""
        assert not GitHubTokenManager._is_valid_credential_token("")

    def test_whitespace_only_invalid(self):
        assert not GitHubTokenManager._is_valid_credential_token("  ")

    def test_normal_pat_valid(self):
        assert GitHubTokenManager._is_valid_credential_token("ghp_abc123")

    def test_over_1024_chars_invalid(self):
        assert not GitHubTokenManager._is_valid_credential_token("a" * 1025)

    def test_exactly_1024_chars_valid(self):
        assert GitHubTokenManager._is_valid_credential_token("a" * 1024)

    def test_password_for_prompt_invalid(self):
        assert not GitHubTokenManager._is_valid_credential_token(
            "Password for 'https://github.com': "
        )

    def test_username_for_prompt_invalid(self):
        assert not GitHubTokenManager._is_valid_credential_token(
            "Username for 'https://github.com': "
        )

    def test_newline_in_token_invalid(self):
        assert not GitHubTokenManager._is_valid_credential_token("tok\nen")

    def test_tab_in_token_invalid(self):
        assert not GitHubTokenManager._is_valid_credential_token("tok\ten")


class TestGetTokenWithCredentialFallback:
    """Test get_token_with_credential_fallback method."""

    def test_returns_env_token_without_credential_fill(self):
        """Returns env var token and never calls credential fill."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "env-token"}, clear=True):
            manager = GitHubTokenManager()
            with patch.object(GitHubTokenManager, "resolve_credential_from_git") as mock_cred:
                token = manager.get_token_with_credential_fallback("modules", "github.com")
                assert token == "env-token"
                mock_cred.assert_not_called()

    def test_falls_back_to_credential_fill(self):
        """Falls back to resolve_credential_from_git when no env token."""
        with patch.dict(os.environ, {}, clear=True):
            manager = GitHubTokenManager()
            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="cred-token"
            ) as mock_cred:
                token = manager.get_token_with_credential_fallback("modules", "github.com")
                assert token == "cred-token"
                mock_cred.assert_called_once_with("github.com", port=None)

    def test_caches_credential_result(self):
        """Second call uses cache, subprocess not invoked again."""
        with patch.dict(os.environ, {}, clear=True):
            manager = GitHubTokenManager()
            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="cached-tok"
            ) as mock_cred:
                first = manager.get_token_with_credential_fallback("modules", "github.com")
                second = manager.get_token_with_credential_fallback("modules", "github.com")
                assert first == second == "cached-tok"
                mock_cred.assert_called_once()

    def test_caches_none_results(self):
        """None results are cached to avoid retrying failed lookups."""
        with patch.dict(os.environ, {}, clear=True):
            manager = GitHubTokenManager()
            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value=None
            ) as mock_cred:
                first = manager.get_token_with_credential_fallback("modules", "github.com")
                second = manager.get_token_with_credential_fallback("modules", "github.com")
                assert first is None
                assert second is None
                mock_cred.assert_called_once()

    def test_different_hosts_separate_cache(self):
        """Different hosts get independent cache entries."""
        with patch.dict(os.environ, {}, clear=True):
            manager = GitHubTokenManager()
            with patch.object(
                GitHubTokenManager,
                "resolve_credential_from_git",
                side_effect=lambda h, port=None: f"tok-{h}",
            ) as mock_cred:
                tok1 = manager.get_token_with_credential_fallback("modules", "github.com")
                tok2 = manager.get_token_with_credential_fallback("modules", "gitlab.com")
                assert tok1 == "tok-github.com"
                assert tok2 == "tok-gitlab.com"
                assert mock_cred.call_count == 2

    def test_same_host_different_ports_separate_cache(self):
        """Same host on different ports must not cross-contaminate credentials."""
        with patch.dict(os.environ, {}, clear=True):
            manager = GitHubTokenManager()
            with patch.object(
                GitHubTokenManager,
                "resolve_credential_from_git",
                side_effect=lambda h, port=None: f"tok-{h}-{port}",
            ) as mock_cred:
                tok_a = manager.get_token_with_credential_fallback(
                    "modules", "bitbucket.corp.com", port=7990
                )
                tok_b = manager.get_token_with_credential_fallback(
                    "modules", "bitbucket.corp.com", port=7991
                )
                assert tok_a == "tok-bitbucket.corp.com-7990"
                assert tok_b == "tok-bitbucket.corp.com-7991"
                assert mock_cred.call_count == 2

    def test_same_host_same_port_hits_cache(self):
        """Identical (host, port) pair is cached -- only one subprocess call."""
        with patch.dict(os.environ, {}, clear=True):
            manager = GitHubTokenManager()
            with patch.object(
                GitHubTokenManager,
                "resolve_credential_from_git",
                return_value="tok",
            ) as mock_cred:
                manager.get_token_with_credential_fallback(
                    "modules", "bitbucket.corp.com", port=7990
                )
                manager.get_token_with_credential_fallback(
                    "modules", "bitbucket.corp.com", port=7990
                )
                mock_cred.assert_called_once()


class TestCredentialFillPortEmbedding:
    """Port is embedded into the host= field per gitcredentials(7).

    There is no standalone ``port=`` attribute in the credential protocol --
    if we ever sent one, helpers would ignore it and return the wrong token.
    """

    def test_port_embedded_in_host_field(self):
        """host=host:port, not a separate port= line."""
        mock_result = MagicMock(returncode=0, stdout="password=tok\n")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            GitHubTokenManager.resolve_credential_from_git("bitbucket.corp.com", port=7999)
            sent = mock_run.call_args.kwargs["input"]
        assert sent == "protocol=https\nhost=bitbucket.corp.com:7999\n\n"
        # Guard against the gitcredentials(7) anti-pattern:
        assert "\nport=" not in sent

    def test_no_port_leaves_host_bare(self):
        """Backward compatible: port=None produces the original input."""
        mock_result = MagicMock(returncode=0, stdout="password=tok\n")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            GitHubTokenManager.resolve_credential_from_git("github.com")
            sent = mock_run.call_args.kwargs["input"]
        assert sent == "protocol=https\nhost=github.com\n\n"
