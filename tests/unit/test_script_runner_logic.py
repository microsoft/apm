"""Unit tests for script runner functionality (pure logic cases)."""

import shutil  # noqa: F401
from unittest.mock import mock_open, patch

import pytest

from apm_cli.core.script_runner import PromptCompiler

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
