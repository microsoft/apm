"""Test Codex runtime adapter."""

from unittest.mock import MagicMock, Mock, patch  # noqa: F401

import pytest

from apm_cli.runtime.codex_runtime import CodexRuntime


class TestCodexRuntime:
    """Test Codex runtime adapter."""

    @patch("apm_cli.runtime.codex_runtime.shutil.which")
    def test_init_success(self, mock_which):
        """Test successful initialization."""
        mock_which.return_value = "/usr/local/bin/codex"

        runtime = CodexRuntime("test-model")

        assert runtime.model_name == "test-model"
        mock_which.assert_called_once_with("codex")

    @patch("apm_cli.runtime.codex_runtime.shutil.which")
    def test_init_not_available(self, mock_which):
        """Test initialization when Codex not available."""
        mock_which.return_value = None

        with pytest.raises(RuntimeError, match="Codex CLI not available"):
            CodexRuntime()

    @patch("apm_cli.runtime.codex_runtime.subprocess.Popen")
    @patch("apm_cli.runtime.codex_runtime.shutil.which")
    def test_execute_prompt_success(self, mock_which, mock_popen):
        """Test successful prompt execution."""
        mock_which.return_value = "/usr/local/bin/codex"

        # Mock the process
        mock_process = Mock()
        mock_process.stdout.readline.side_effect = ["Test response from Codex\n", ""]
        mock_process.wait.return_value = 0
        mock_popen.return_value = mock_process

        runtime = CodexRuntime()
        result = runtime.execute_prompt("Test prompt")

        assert result == "Test response from Codex"
        mock_popen.assert_called_once()

    @patch("apm_cli.runtime.codex_runtime.subprocess.Popen")
    @patch("apm_cli.runtime.codex_runtime.shutil.which")
    def test_execute_prompt_failure(self, mock_which, mock_popen):
        """Test prompt execution failure."""
        mock_which.return_value = "/usr/local/bin/codex"

        # Mock the process failure
        mock_process = Mock()
        mock_process.stdout.readline.side_effect = [""]  # Empty output
        mock_process.wait.return_value = 1  # Non-zero exit code
        mock_popen.return_value = mock_process

        runtime = CodexRuntime()

        with pytest.raises(RuntimeError, match="Codex execution failed"):
            runtime.execute_prompt("Test prompt")

    @patch("apm_cli.runtime.codex_runtime.shutil.which")
    def test_list_available_models(self, mock_which):
        """Test listing available models."""
        mock_which.return_value = "/usr/local/bin/codex"

        runtime = CodexRuntime()
        models = runtime.list_available_models()

        assert "codex-default" in models
        assert models["codex-default"]["provider"] == "codex"

    @patch("apm_cli.runtime.codex_runtime.subprocess.run")
    @patch("apm_cli.runtime.codex_runtime.shutil.which")
    def test_get_runtime_info(self, mock_which, mock_run):
        """Test getting runtime info."""
        mock_which.return_value = "/usr/local/bin/codex"
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "1.0.0"
        mock_run.return_value = mock_result

        runtime = CodexRuntime()
        info = runtime.get_runtime_info()

        assert info["name"] == "codex"
        assert info["type"] == "codex_cli"
        assert info["version"] == "1.0.0"
        assert info["capabilities"]["mcp_servers"] == "native_support"

    @patch("apm_cli.runtime.codex_runtime.shutil.which")
    def test_is_available_true(self, mock_which):
        """Test runtime availability check - available."""
        mock_which.return_value = "/usr/local/bin/codex"

        assert CodexRuntime.is_available() is True
        mock_which.assert_called_once_with("codex")

    @patch("apm_cli.runtime.codex_runtime.shutil.which")
    def test_is_available_false(self, mock_which):
        """Test runtime availability check - not available."""
        mock_which.return_value = None

        assert CodexRuntime.is_available() is False
        mock_which.assert_called_once_with("codex")

    def test_get_runtime_name(self):
        """Test runtime name getter."""
        assert CodexRuntime.get_runtime_name() == "codex"

    @patch("apm_cli.runtime.codex_runtime.shutil.which")
    def test_str_representation(self, mock_which):
        """Test string representation."""
        mock_which.return_value = "/usr/local/bin/codex"

        runtime = CodexRuntime("test-model")

        assert str(runtime) == "CodexRuntime(model=test-model)"
