"""Unit tests for script runner functionality (pure logic cases)."""

import os
import shutil  # noqa: F401
from unittest.mock import MagicMock, mock_open, patch

import pytest

from apm_cli.core.script_runner import PromptCompiler, ScriptRunner

pytestmark = pytest.mark.unit


class TestPromptCompiler:
    """Test PromptCompiler functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.compiler = PromptCompiler()

    def test_substitute_parameters_simple(self):
        """Test simple parameter substitution."""
        content = "Hello ${input:name}!"
        params = {"name": "World"}

        result = self.compiler._substitute_parameters(content, params)

        assert result == "Hello World!"

    def test_substitute_parameters_multiple(self):
        """Test multiple parameter substitution."""
        content = "Service: ${input:service}, Environment: ${input:env}"
        params = {"service": "api", "env": "production"}

        result = self.compiler._substitute_parameters(content, params)

        assert result == "Service: api, Environment: production"

    def test_substitute_parameters_no_params(self):
        """Test content with no parameters to substitute."""
        content = "This is a simple prompt with no parameters."
        params = {}

        result = self.compiler._substitute_parameters(content, params)

        assert result == content

    def test_substitute_parameters_missing_param(self):
        """Test behavior when parameter is missing."""
        content = "Hello ${input:name}!"
        params = {}

        result = self.compiler._substitute_parameters(content, params)

        # Should leave placeholder unchanged when parameter is missing
        assert result == "Hello ${input:name}!"

    @patch("apm_cli.core.script_runner.Path.mkdir")
    @patch("apm_cli.core.script_runner.Path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_compile_with_frontmatter(self, mock_file, mock_exists, mock_mkdir):
        """Test compiling prompt file with frontmatter."""
        mock_exists.return_value = True

        # Mock file content with frontmatter
        file_content = """---
description: Test prompt
input:
  - name
---

# Test Prompt

Hello ${input:name}!"""

        mock_file.return_value.read.return_value = file_content

        result_path = self.compiler.compile("test.prompt.md", {"name": "World"})  # noqa: F841

        # Check that the compiled content was written correctly
        mock_file.return_value.write.assert_called_once()
        written_content = mock_file.return_value.write.call_args[0][0]
        assert "Hello World!" in written_content
        assert "---" not in written_content  # Frontmatter should be stripped

    @patch("apm_cli.core.script_runner.Path.mkdir")
    @patch("apm_cli.core.script_runner.Path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_compile_without_frontmatter(self, mock_file, mock_exists, mock_mkdir):
        """Test compiling prompt file without frontmatter."""
        mock_exists.return_value = True

        # Mock file content without frontmatter
        file_content = "Hello ${input:name}!"
        mock_file.return_value.read.return_value = file_content

        result_path = self.compiler.compile("test.prompt.md", {"name": "World"})  # noqa: F841

        # Check that the compiled content was written correctly
        mock_file.return_value.write.assert_called_once()
        written_content = mock_file.return_value.write.call_args[0][0]
        assert written_content == "Hello World!"

    @patch("apm_cli.core.script_runner.Path.exists")
    def test_compile_file_not_found(self, mock_exists):
        """Test compiling non-existent prompt file."""
        mock_exists.return_value = False

        with pytest.raises(
            FileNotFoundError,
            match="Prompt file 'nonexistent.prompt.md' not found",  # noqa: RUF043
        ):
            self.compiler.compile("nonexistent.prompt.md", {})


class TestExecuteRuntimeCommandResolution:
    """Test that _execute_runtime_command resolves executables on all platforms."""

    def setup_method(self):
        self.runner = ScriptRunner()

    @patch("subprocess.run")
    @patch("apm_cli.core.script_runner.find_runtime_binary", return_value=r"C:\npm\copilot.cmd")
    def test_resolves_executable(self, mock_find_runtime, mock_run):
        """The executable should be resolved via find_runtime_binary on all platforms."""
        mock_run.return_value = MagicMock(returncode=0)

        self.runner._execute_runtime_command(
            "copilot --log-level all", "prompt content", os.environ.copy()
        )

        mock_find_runtime.assert_called_once_with("copilot")
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == r"C:\npm\copilot.cmd"

    @patch("subprocess.run")
    @patch("apm_cli.core.script_runner.find_runtime_binary", return_value=None)
    def test_keeps_original_when_find_returns_none(self, mock_find_runtime, mock_run):
        """If find_runtime_binary can't find it, keep the original name."""
        mock_run.return_value = MagicMock(returncode=0)

        self.runner._execute_runtime_command("copilot -p", "prompt content", os.environ.copy())

        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "copilot"

    @patch("subprocess.run")
    @patch("apm_cli.core.script_runner.find_runtime_binary", return_value=None)
    def test_resolves_on_non_windows(self, mock_find_runtime, mock_run):
        """find_runtime_binary should be called on all platforms, not just Windows."""
        mock_run.return_value = MagicMock(returncode=0)

        self.runner._execute_runtime_command("copilot -p", "prompt content", os.environ.copy())

        mock_find_runtime.assert_called_once_with("copilot")
