"""Test LLM runtime integration."""

from unittest.mock import Mock, patch

import pytest

from apm_cli.runtime.llm_runtime import LLMRuntime


class TestLLMRuntime:
    """Test LLM runtime adapter."""

    @patch("apm_cli.runtime.llm_runtime.subprocess.run")
    def test_init_success(self, mock_run):
        """Test successful initialization."""
        # Mock the --version check
        mock_run.return_value = Mock(returncode=0, stdout="llm 0.17.0")

        runtime = LLMRuntime("gpt-4o-mini")

        assert runtime.model_name == "gpt-4o-mini"
        mock_run.assert_called_once_with(
            ["llm", "--version"], capture_output=True, text=True, encoding="utf-8", check=True
        )

    @patch("apm_cli.runtime.llm_runtime.subprocess.run")
    def test_init_fallback(self, mock_run):
        """Test fallback when llm CLI not available."""
        mock_run.side_effect = FileNotFoundError("llm command not found")

        with pytest.raises(RuntimeError, match="llm CLI not found"):
            LLMRuntime("invalid-model")

    @patch("apm_cli.runtime.llm_runtime.subprocess.Popen")
    @patch("apm_cli.runtime.llm_runtime.subprocess.run")
    def test_execute_prompt_success(self, mock_run, mock_popen):
        """Test successful prompt execution."""
        # Mock version check
        mock_run.return_value = Mock(returncode=0, stdout="llm 0.17.0")

        # Mock prompt execution
        mock_process = Mock()
        # Mock stdout.readline to return lines then empty string to stop iteration
        mock_process.stdout.readline.side_effect = ["Test response\n", ""]
        mock_process.wait.return_value = 0
        mock_popen.return_value = mock_process

        runtime = LLMRuntime()
        result = runtime.execute_prompt("Test prompt")

        assert result == "Test response"
        mock_popen.assert_called_once()

    @patch("apm_cli.runtime.llm_runtime.subprocess.Popen")
    @patch("apm_cli.runtime.llm_runtime.subprocess.run")
    def test_execute_prompt_failure(self, mock_run, mock_popen):
        """Test prompt execution failure."""
        # Mock version check
        mock_run.return_value = Mock(returncode=0, stdout="llm 0.17.0")

        # Mock prompt execution failure
        mock_process = Mock()
        mock_process.stdout.readline.side_effect = [""]  # Empty output
        mock_process.wait.return_value = 1  # Non-zero exit code
        mock_popen.return_value = mock_process

        runtime = LLMRuntime()

        with pytest.raises(RuntimeError, match="Failed to execute prompt"):
            runtime.execute_prompt("Test prompt")

    def test_get_default_model(self):
        """Test default model getter."""
        assert LLMRuntime.get_default_model() is None

    @patch("apm_cli.runtime.llm_runtime.subprocess.run")
    def test_str_representation(self, mock_run):
        """Test string representation."""
        mock_run.return_value = Mock(returncode=0, stdout="llm 0.17.0")

        runtime = LLMRuntime("claude-3-sonnet")

        assert str(runtime) == "LLMRuntime(model=claude-3-sonnet)"
