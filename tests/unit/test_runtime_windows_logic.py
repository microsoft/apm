"""Tests for Windows platform support in RuntimeManager and ScriptRunner (pure logic cases)."""

import sys  # noqa: F401
from unittest.mock import patch

import pytest

# Import modules at module level BEFORE any sys.platform patching,
# to avoid triggering Windows-only import paths (msvcrt, CREATE_NO_WINDOW) on Unix.
from apm_cli.runtime.manager import RuntimeManager

pytestmark = pytest.mark.unit


def _make_manager(platform: str) -> RuntimeManager:
    """Create a RuntimeManager with a specific platform."""
    with patch("sys.platform", platform):
        return RuntimeManager()


class TestRuntimeManagerPlatformDetection:
    """Test RuntimeManager selects correct scripts per platform."""

    def test_selects_ps1_scripts_on_windows(self):
        manager = _make_manager("win32")
        for name, runtime_info in manager.supported_runtimes.items():
            assert runtime_info["script"].endswith(".ps1"), (
                f"Runtime '{name}' should use .ps1 on Windows, got {runtime_info['script']}"
            )

    def test_selects_sh_scripts_on_unix(self):
        manager = _make_manager("darwin")
        for name, runtime_info in manager.supported_runtimes.items():
            assert runtime_info["script"].endswith(".sh"), (
                f"Runtime '{name}' should use .sh on Unix, got {runtime_info['script']}"
            )

    def test_selects_sh_scripts_on_linux(self):
        manager = _make_manager("linux")
        for name, runtime_info in manager.supported_runtimes.items():
            assert runtime_info["script"].endswith(".sh"), (
                f"Runtime '{name}' should use .sh on Linux, got {runtime_info['script']}"
            )

    def test_common_script_is_ps1_on_windows(self):
        manager = _make_manager("win32")
        with (
            patch("sys.platform", "win32"),
            patch.object(manager, "get_embedded_script", return_value="# ps1 content") as mock,
        ):
            manager.get_common_script()
            mock.assert_called_once_with("setup-common.ps1")

    def test_common_script_is_sh_on_unix(self):
        manager = _make_manager("darwin")
        with (
            patch("sys.platform", "darwin"),
            patch.object(manager, "get_embedded_script", return_value="# sh content") as mock,
        ):
            manager.get_common_script()
            mock.assert_called_once_with("setup-common.sh")


class TestRuntimeManagerTokenHelper:
    """Test token helper script platform behavior."""

    def test_token_helper_returns_empty_on_windows(self):
        manager = _make_manager("win32")
        with patch("sys.platform", "win32"):
            result = manager.get_token_helper_script()
        assert result == "", "Token helper should return empty string on Windows"

    def test_token_helper_loads_script_on_unix(self):
        manager = _make_manager("darwin")
        with (
            patch("sys.platform", "darwin"),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value="#!/bin/bash\n# token helper"),
        ):
            result = manager.get_token_helper_script()
            assert result == "#!/bin/bash\n# token helper"


class TestIsWindowsProperty:
    """Test _is_windows property on RuntimeManager."""

    def test_is_windows_true(self):
        manager = _make_manager("win32")
        with patch("sys.platform", "win32"):
            assert manager._is_windows is True

    def test_is_windows_false_on_macos(self):
        manager = _make_manager("darwin")
        with patch("sys.platform", "darwin"):
            assert manager._is_windows is False

    def test_is_windows_false_on_linux(self):
        manager = _make_manager("linux")
        with patch("sys.platform", "linux"):
            assert manager._is_windows is False
