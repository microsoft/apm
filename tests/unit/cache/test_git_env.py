"""Tests for git subprocess environment sanitization."""

import os
import sys
from unittest.mock import patch

import pytest

from apm_cli.utils.git_env import (
    _STRIP_GIT_VARS,
    get_git_executable,
    git_subprocess_env,
    reset_git_cache,
    strip_git_auth_config_entries,
)

# Entire module: this is the canonical owner of resolved,
# PATH-independent git executable lookup (microsoft/apm#2233's bare
# ["git", ...] argv WinError 2 class). Selected by the PR-time Windows
# Compatibility Gate via `pytest -m windows_compat`; also runs on
# every other OS.
pytestmark = pytest.mark.windows_compat


class TestGetGitExecutable:
    """Test cached git binary lookup."""

    def setup_method(self) -> None:
        reset_git_cache()

    def teardown_method(self) -> None:
        reset_git_cache()

    @patch("shutil.which", return_value="/usr/bin/git")
    def test_returns_git_path(self, mock_which) -> None:
        result = get_git_executable()
        assert result == "/usr/bin/git"
        mock_which.assert_called_once_with("git")

    @patch("shutil.which", return_value="/usr/bin/git")
    def test_cached_after_first_call(self, mock_which) -> None:
        """shutil.which called only once across multiple invocations."""
        get_git_executable()
        get_git_executable()
        get_git_executable()
        mock_which.assert_called_once()

    @patch("shutil.which", return_value=None)
    def test_raises_if_git_not_found(self, mock_which) -> None:
        with pytest.raises(FileNotFoundError, match=r"git executable not found"):
            get_git_executable()

    @patch("shutil.which", side_effect=[None, "/usr/bin/git"])
    def test_transient_failure_does_not_poison_later_resolution(self, mock_which) -> None:
        """A transient PATH miss raises but remains retryable."""
        with pytest.raises(FileNotFoundError):
            get_git_executable()

        assert get_git_executable() == "/usr/bin/git"
        assert mock_which.call_count == 2


class TestGitSubprocessEnv:
    """Test environment sanitization."""

    def test_strips_git_dir(self) -> None:
        with patch.dict(os.environ, {"GIT_DIR": "/some/path/.git"}):
            env = git_subprocess_env()
            assert "GIT_DIR" not in env

    def test_strips_git_work_tree(self) -> None:
        with patch.dict(os.environ, {"GIT_WORK_TREE": "/some/path"}):
            env = git_subprocess_env()
            assert "GIT_WORK_TREE" not in env

    def test_strips_git_index_file(self) -> None:
        with patch.dict(os.environ, {"GIT_INDEX_FILE": "/tmp/index"}):
            env = git_subprocess_env()
            assert "GIT_INDEX_FILE" not in env

    def test_strips_all_ambient_vars(self) -> None:
        env_override = {var: "value" for var in _STRIP_GIT_VARS}
        with patch.dict(os.environ, env_override):
            env = git_subprocess_env()
            for var in _STRIP_GIT_VARS:
                assert var not in env

    def test_preserves_git_ssh_command(self) -> None:
        with patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -i ~/.ssh/id_rsa"}):
            env = git_subprocess_env()
            assert env["GIT_SSH_COMMAND"] == "ssh -i ~/.ssh/id_rsa"

    def test_preserves_git_config_global(self) -> None:
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": "/etc/gitconfig"}):
            env = git_subprocess_env()
            assert env["GIT_CONFIG_GLOBAL"] == "/etc/gitconfig"

    def test_preserves_https_proxy(self) -> None:
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.corp:8080"}):
            env = git_subprocess_env()
            assert env["HTTPS_PROXY"] == "http://proxy.corp:8080"

    def test_preserves_ssh_askpass(self) -> None:
        with patch.dict(os.environ, {"SSH_ASKPASS": "/usr/lib/ssh/ssh-askpass"}):
            env = git_subprocess_env()
            assert env["SSH_ASKPASS"] == "/usr/lib/ssh/ssh-askpass"

    def test_preserves_git_terminal_prompt(self) -> None:
        with patch.dict(os.environ, {"GIT_TERMINAL_PROMPT": "0"}):
            env = git_subprocess_env()
            assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_preserves_regular_env_vars(self) -> None:
        with patch.dict(os.environ, {"HOME": "/home/user", "PATH": "/usr/bin"}):
            env = git_subprocess_env()
            assert env["HOME"] == "/home/user"
            assert env["PATH"] == "/usr/bin"

    def test_strips_pyinstaller_ld_library_path_when_frozen(self) -> None:
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.dict(os.environ, {"LD_LIBRARY_PATH": "/bundle/internal"}, clear=True),
        ):
            env = git_subprocess_env()
            assert "LD_LIBRARY_PATH" not in env

    def test_restores_original_ld_library_path_when_frozen(self) -> None:
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.dict(
                os.environ,
                {
                    "LD_LIBRARY_PATH": "/bundle/internal",
                    "LD_LIBRARY_PATH_ORIG": "/custom/lib",
                },
                clear=True,
            ),
        ):
            env = git_subprocess_env()
            assert env["LD_LIBRARY_PATH"] == "/custom/lib"
            assert "LD_LIBRARY_PATH_ORIG" not in env

    def test_preserves_ld_library_path_when_not_frozen(self) -> None:
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/custom/lib"}, clear=True):
            env = git_subprocess_env()
            assert env["LD_LIBRARY_PATH"] == "/custom/lib"


class TestStripGitAuthConfigEntries:
    """Canonical indexed GIT_CONFIG_* retain/reindex policy."""

    def test_strips_auth_entries_and_reindexes_safe_entries_stably(self) -> None:
        env = {
            "OTHER": "preserved",
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "safe.bareRepository",
            "GIT_CONFIG_VALUE_0": "explicit",
            "GIT_CONFIG_KEY_1": "http.ExtraHeader",
            "GIT_CONFIG_VALUE_1": "X-Trace: inherited",
            "GIT_CONFIG_KEY_2": "credential.helper",
            "GIT_CONFIG_VALUE_2": "note\r\n\tAUTHORIZATION : Bearer stale-secret",
            "GIT_CONFIG_KEY_3": "http.sslCAInfo",
            "GIT_CONFIG_VALUE_3": "/corporate/ca.pem",
            "GIT_CONFIG_KEY_9": "http.extraheader",
            "GIT_CONFIG_VALUE_9": "Authorization: Bearer orphan-secret",
        }

        retained_count = strip_git_auth_config_entries(env)

        assert retained_count == 2
        assert env == {
            "OTHER": "preserved",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "safe.bareRepository",
            "GIT_CONFIG_VALUE_0": "explicit",
            "GIT_CONFIG_KEY_1": "http.sslCAInfo",
            "GIT_CONFIG_VALUE_1": "/corporate/ca.pem",
        }
        assert not any("secret" in value for value in env.values())

    @pytest.mark.parametrize(
        "value",
        [
            "Authorization: Bearer stale",
            "  authorization:\tBasic stale",
            "note\nAuthorization: Bearer stale",
            "note\rAuthorization: Bearer stale",
            "note\r\n\tAuthorization : Bearer stale",
        ],
    )
    def test_strips_exact_authorization_header_lines(self, value: str) -> None:
        env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": value,
        }

        assert strip_git_auth_config_entries(env) == 0
        assert env == {}

    @pytest.mark.parametrize(
        "value",
        [
            "http://authorization-proxy.corp.example:3128",
            "authorization is required",
            "x=Authorization: example",
            "X-Authorization: example",
        ],
    )
    def test_preserves_non_header_authorization_text(self, value: str) -> None:
        env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.proxy",
            "GIT_CONFIG_VALUE_0": value,
        }

        assert strip_git_auth_config_entries(env) == 1
        assert env["GIT_CONFIG_VALUE_0"] == value

    @pytest.mark.parametrize("count", [None, "", " ", "invalid", "-3"])
    def test_invalid_or_non_positive_count_discards_all_slots(self, count: str | None) -> None:
        env: dict[str, str] = {
            "GIT_CONFIG_COUNT": count,  # type: ignore[dict-item]
            "GIT_CONFIG_KEY_0": "safe.bareRepository",
            "GIT_CONFIG_VALUE_0": "explicit",
        }

        assert strip_git_auth_config_entries(env) == 0
        assert env == {}

    def test_runtime_is_bounded_by_present_slots_not_declared_count(self) -> None:
        env = {
            "GIT_CONFIG_COUNT": "999999999999999999999999999999999999",
            "GIT_CONFIG_KEY_7": "safe.bareRepository",
            "GIT_CONFIG_VALUE_7": "explicit",
        }

        assert strip_git_auth_config_entries(env) == 1
        assert env == {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.bareRepository",
            "GIT_CONFIG_VALUE_0": "explicit",
        }

    def test_missing_values_and_noncanonical_slots_are_cleaned(self) -> None:
        env = {
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_VALUE_0": "value-without-key",
            "GIT_CONFIG_KEY_1": "credential.interactive",
            "GIT_CONFIG_KEY_00": "http.extraheader",
            "GIT_CONFIG_VALUE_00": "Authorization: Bearer stale",
            "GIT_CONFIG_KEY_bad": "http.extraheader",
            "GIT_CONFIG_VALUE_bad": "Authorization: Bearer stale",
        }

        assert strip_git_auth_config_entries(env) == 1
        assert env == {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.interactive",
            "GIT_CONFIG_VALUE_0": "",
        }
