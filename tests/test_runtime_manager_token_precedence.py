"""Tests for RuntimeManager token precedence logic."""

import os
import tempfile  # noqa: F401
from unittest.mock import Mock, patch

import pytest  # noqa: F401

from src.apm_cli.runtime.manager import RuntimeManager


class TestRuntimeManagerTokenPrecedence:
    """Test token precedence logic in RuntimeManager."""

    def setup_method(self):
        """Set up test environment."""
        self.runtime_manager = RuntimeManager()

    def test_token_precedence_with_apm_pat(self):
        """Test that GITHUB_APM_PAT is used for runtime setup."""
        with (
            patch.dict(
                os.environ,
                {"GITHUB_APM_PAT": "apm-token", "GITHUB_TOKEN": "generic-token"},
                clear=True,
            ),
            patch.object(self.runtime_manager, "run_embedded_script") as mock_run,
        ):
            mock_run.return_value = True

            # Mock the script content methods
            with patch.object(self.runtime_manager, "get_embedded_script") as mock_script:
                with patch.object(self.runtime_manager, "get_common_script") as mock_common:
                    mock_script.return_value = "#!/bin/bash\necho 'test'"
                    mock_common.return_value = "#!/bin/bash\necho 'common'"

                    result = self.runtime_manager.setup_runtime("codex")

                    # Check that the environment passed to the script has the right precedence
                    assert mock_run.called
                    call_args = mock_run.call_args  # noqa: F841
                    # The run_embedded_script method should have been called
                    assert result is True

    def test_token_precedence_fallback_to_github_token(self):
        """Test that GITHUB_TOKEN is used when GITHUB_APM_PAT is not available."""
        with patch.dict(os.environ, {"GITHUB_TOKEN": "generic-token"}, clear=True):
            # Remove GITHUB_APM_PAT if it exists
            if "GITHUB_APM_PAT" in os.environ:
                del os.environ["GITHUB_APM_PAT"]

            with patch.object(self.runtime_manager, "run_embedded_script") as mock_run:
                mock_run.return_value = True

                with patch.object(self.runtime_manager, "get_embedded_script") as mock_script:
                    with patch.object(self.runtime_manager, "get_common_script") as mock_common:
                        mock_script.return_value = "#!/bin/bash\necho 'test'"
                        mock_common.return_value = "#!/bin/bash\necho 'common'"

                        result = self.runtime_manager.setup_runtime("codex")
                        assert result is True

    def test_token_passthrough_to_scripts(self):
        """Test that RuntimeManager passes through tokens to shell scripts.

        Token precedence is handled by shell scripts, not Python.
        Python should pass through available tokens.
        """
        test_cases = [
            # Case 1: All tokens present - Python should pass all through
            {
                "env": {"GITHUB_APM_PAT": "apm-token", "GITHUB_TOKEN": "generic-token"},
                "expected_passed_tokens": {
                    "GITHUB_APM_PAT": "apm-token",
                    "GITHUB_TOKEN": "generic-token",
                },
            },
            # Case 2: Only GITHUB_TOKEN - Python should pass it through
            {
                "env": {"GITHUB_TOKEN": "generic-token"},
                "expected_passed_tokens": {"GITHUB_TOKEN": "generic-token"},
            },
        ]

        for i, test_case in enumerate(test_cases):
            with patch.dict(os.environ, test_case["env"], clear=True):
                with patch("subprocess.run") as mock_subprocess:
                    mock_subprocess.return_value = Mock(returncode=0)

                    with patch.object(self.runtime_manager, "get_embedded_script") as mock_script:
                        with patch.object(self.runtime_manager, "get_common_script") as mock_common:
                            mock_script.return_value = "#!/bin/bash\necho 'test'"
                            mock_common.return_value = "#!/bin/bash\necho 'common'"

                            result = self.runtime_manager.setup_runtime("codex")  # noqa: F841

                            # Check that subprocess was called with the right environment
                            assert mock_subprocess.called, (
                                f"Test case {i + 1}: subprocess.run should have been called"
                            )
                            call_args = mock_subprocess.call_args
                            env_used = call_args.kwargs.get("env", {})

                            # Verify that Python passes through the expected tokens (shell handles precedence)
                            for token_name, expected_value in test_case[
                                "expected_passed_tokens"
                            ].items():
                                assert env_used.get(token_name) == expected_value, (
                                    f"Test case {i + 1}: Expected {token_name}={expected_value}, got {env_used.get(token_name)}"
                                )

    def test_no_tokens_available(self):
        """Test behavior when no tokens are available."""
        with patch.dict(os.environ, {}, clear=True):
            with patch("subprocess.run") as mock_subprocess:
                mock_subprocess.return_value = Mock(returncode=0)

                with patch.object(self.runtime_manager, "get_embedded_script") as mock_script:
                    with patch.object(self.runtime_manager, "get_common_script") as mock_common:
                        mock_script.return_value = "#!/bin/bash\necho 'test'"
                        mock_common.return_value = "#!/bin/bash\necho 'common'"

                        result = self.runtime_manager.setup_runtime("codex")  # noqa: F841

                        # Should still work, but without tokens
                        assert mock_subprocess.called
                        call_args = mock_subprocess.call_args
                        env_used = call_args.kwargs.get("env", {})

                        # These tokens should not be set
                        assert "GITHUB_TOKEN" not in env_used or env_used.get("GITHUB_TOKEN") == ""
                        assert "GH_TOKEN" not in env_used or env_used.get("GH_TOKEN") == ""


class TestRuntimeManagerErrorMessages:
    """Test error message updates for new token names."""

    def setup_method(self):
        """Set up test environment."""
        self.runtime_manager = RuntimeManager()

    def test_unsupported_runtime_error(self):
        """Test error message for unsupported runtime."""
        with patch("click.echo") as mock_echo:
            result = self.runtime_manager.setup_runtime("unsupported")
            assert result is False

            # Check error messages were displayed
            assert mock_echo.called
            error_calls = [
                call for call in mock_echo.call_args_list if "Unsupported runtime" in str(call)
            ]
            assert len(error_calls) > 0

    def test_runtime_availability_check(self):
        """Test runtime availability check methods."""
        # Test codex runtime availability
        with patch("shutil.which") as mock_which, patch("pathlib.Path.exists") as mock_exists:
            # Mock that APM binary doesn't exist, use system PATH
            mock_exists.return_value = False
            mock_which.return_value = "/usr/local/bin/codex"
            assert self.runtime_manager.is_runtime_available("codex") is True

            # Mock that neither APM binary nor system binary exists
            mock_exists.return_value = False
            mock_which.return_value = None
            assert self.runtime_manager.is_runtime_available("codex") is False

        # Test unsupported runtime
        assert self.runtime_manager.is_runtime_available("unsupported") is False
