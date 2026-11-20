"""Tests for the JetBrains Copilot MCP client adapter (pure logic cases)."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.intellij import (
    IntelliJClientAdapter,
    _intellij_config_dir,
    _legacy_intellij_config_dir,
)
from apm_cli.factory import ClientFactory
from apm_cli.utils.path_security import PathTraversalError

pytestmark = pytest.mark.unit


class TestIntelliJClientFactory:
    """Verify IntelliJClientAdapter is registered in ClientFactory."""

    def test_factory_creates_intellij_adapter(self):
        adapter = ClientFactory.create_client("intellij")
        assert isinstance(adapter, IntelliJClientAdapter)

    def test_supports_user_scope_is_true(self):
        adapter = ClientFactory.create_client("intellij")
        assert adapter.supports_user_scope is True

    def test_mcp_servers_key_is_servers(self):
        adapter = ClientFactory.create_client("intellij")
        assert adapter.mcp_servers_key == "servers"


class TestIntelliJConfigDir(unittest.TestCase):
    """Verify _intellij_config_dir returns the correct OS-specific path."""

    def test_macos_path_defaults_to_plugin_config_location(self):
        with (
            patch.object(sys, "platform", "darwin"),
            patch("pathlib.Path.home", return_value=Path("/Users/tester")),
            patch.dict("os.environ", {"XDG_CONFIG_HOME": ""}, clear=False),
        ):
            result = _intellij_config_dir()
        expected = Path("/Users/tester/.config/github-copilot/intellij")
        self.assertEqual(result, expected)

    def test_macos_path_honors_xdg_config_home(self):
        env = {"XDG_CONFIG_HOME": "/custom/config"}
        with (
            patch.object(sys, "platform", "darwin"),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _intellij_config_dir()
        expected = Path("/custom/config/github-copilot/intellij")
        self.assertEqual(result, expected)

    def test_linux_path_default(self):
        env = {"XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "/wrong/data"}
        with (
            patch.object(sys, "platform", "linux"),
            patch("pathlib.Path.home", return_value=Path("/home/tester")),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _intellij_config_dir()
        expected = Path("/home/tester/.config/github-copilot/intellij")
        self.assertEqual(result, expected)

    def test_linux_path_xdg_override(self):
        env = {
            "XDG_CONFIG_HOME": "/custom/config",
            "XDG_DATA_HOME": "/wrong/data",
        }
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _intellij_config_dir()
        expected = Path("/custom/config/github-copilot/intellij")
        self.assertEqual(result, expected)

    def test_linux_rejects_relative_xdg_config_home(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict("os.environ", {"XDG_CONFIG_HOME": "relative/config"}, clear=False),
            self.assertRaises(PathTraversalError),
        ):
            _intellij_config_dir()

    def test_linux_legacy_path_keeps_old_xdg_data_location(self):
        env = {"XDG_DATA_HOME": "/custom/data"}
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _legacy_intellij_config_dir()
        expected = Path("/custom/data/github-copilot/intellij")
        self.assertEqual(result, expected)

    def test_macos_legacy_path_keeps_old_application_support_location(self):
        with (
            patch.object(sys, "platform", "darwin"),
            patch("pathlib.Path.home", return_value=Path("/Users/tester")),
        ):
            result = _legacy_intellij_config_dir()
        expected = Path("/Users/tester/Library/Application Support/github-copilot/intellij")
        self.assertEqual(result, expected)

    @unittest.skipUnless(sys.platform == "win32", "Windows path separator only valid on win32")
    def test_windows_path(self):
        env = {"LOCALAPPDATA": "C:\\Users\\tester\\AppData\\Local"}
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _intellij_config_dir()
        expected = Path("C:\\Users\\tester\\AppData\\Local\\github-copilot\\intellij")
        self.assertEqual(result, expected)

    def test_windows_path_components(self):
        """Verify Windows path uses LOCALAPPDATA and correct subdirectories."""
        env = {
            "LOCALAPPDATA": r"C:\fake\local",
            "XDG_CONFIG_HOME": "/wrong/config",
            "XDG_DATA_HOME": "/wrong/data",
        }
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _intellij_config_dir()
        # On any platform, the components must include the expected dirs.
        self.assertIn("github-copilot", result.parts)
        self.assertIn("intellij", result.parts)
