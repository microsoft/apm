"""Tests for git subprocess environment sanitization."""

import os
from unittest.mock import patch

import pytest

from apm_cli.utils.git_env import (
    _STRIP_GIT_VARS,
    get_git_executable,
    git_subprocess_env,
    reset_git_cache,
)


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

    @patch("shutil.which", return_value=None)
    def test_cached_failure(self, mock_which) -> None:
        """Once git is determined missing, subsequent calls raise immediately."""
        with pytest.raises(FileNotFoundError):
            get_git_executable()
        # Second call should also raise without calling which again
        with pytest.raises(FileNotFoundError):
            get_git_executable()
        mock_which.assert_called_once()


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
