"""Unit tests for MCPClientAdapter._should_skip_env_prompts."""

import os
import unittest
from unittest.mock import patch

from apm_cli.adapters.client.base import MCPClientAdapter


class TestShouldSkipEnvPrompts(unittest.TestCase):
    """Verify the three-branch TTY/CI/managed-mode policy."""

    def test_returns_true_when_env_overrides_provided(self):
        """Managed mode: caller already collected env vars."""
        self.assertTrue(MCPClientAdapter._should_skip_env_prompts({"TOKEN": "val"}))

    @patch.dict(os.environ, {"APM_E2E_TESTS": "1"})
    def test_returns_true_when_e2e_tests_flag_set(self):
        """CI mode: APM_E2E_TESTS=1 disables prompts."""
        self.assertTrue(MCPClientAdapter._should_skip_env_prompts({}))

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_true_when_stdin_not_tty(self):
        """Non-interactive: stdin is not a TTY."""
        with patch("sys.stdin") as mock_stdin, patch("sys.stdout") as mock_stdout:
            mock_stdin.isatty.return_value = False
            mock_stdout.isatty.return_value = True
            self.assertTrue(MCPClientAdapter._should_skip_env_prompts({}))

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_true_when_stdout_not_tty(self):
        """Non-interactive: stdout is not a TTY."""
        with patch("sys.stdin") as mock_stdin, patch("sys.stdout") as mock_stdout:
            mock_stdin.isatty.return_value = True
            mock_stdout.isatty.return_value = False
            self.assertTrue(MCPClientAdapter._should_skip_env_prompts({}))

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_false_when_interactive_tty(self):
        """Interactive: both stdin and stdout are TTYs, no overrides, no CI flag."""
        with patch("sys.stdin") as mock_stdin, patch("sys.stdout") as mock_stdout:
            mock_stdin.isatty.return_value = True
            mock_stdout.isatty.return_value = True
            self.assertFalse(MCPClientAdapter._should_skip_env_prompts({}))

    def test_returns_true_with_empty_overrides_is_false(self):
        """Empty dict is falsy — should NOT skip on overrides alone."""
        # With an empty dict, only TTY/CI determines the result.
        # This test just confirms {} is treated as "no overrides".
        with patch("sys.stdin") as mock_stdin, patch("sys.stdout") as mock_stdout:
            mock_stdin.isatty.return_value = True
            mock_stdout.isatty.return_value = True
            with patch.dict(os.environ, {}, clear=True):
                self.assertFalse(MCPClientAdapter._should_skip_env_prompts({}))
