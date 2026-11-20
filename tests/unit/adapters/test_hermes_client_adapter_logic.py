"""Unit tests for HermesClientAdapter and its MCP integrator wiring (pure logic cases)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.hermes import HermesClientAdapter
from apm_cli.factory import ClientFactory

pytestmark = pytest.mark.unit


class TestHermesClientFactory(unittest.TestCase):
    """Factory registration for the hermes runtime."""

    def test_create_hermes_client_case_insensitive(self):
        client = ClientFactory.create_client("Hermes")
        self.assertIsInstance(client, HermesClientAdapter)

    def test_hermes_uses_snake_case_servers_key(self):
        self.assertEqual(HermesClientAdapter.mcp_servers_key, "mcp_servers")

    def test_hermes_supports_user_scope(self):
        self.assertTrue(HermesClientAdapter.supports_user_scope)


class TestToHermesFormat(unittest.TestCase):
    """_to_hermes_format static conversion logic."""

    def test_stdio_command_and_args(self):
        copilot = {"command": "npx", "args": ["-y", "some-pkg"]}
        result = HermesClientAdapter._to_hermes_format(copilot)
        self.assertEqual(result["command"], "npx")
        self.assertEqual(result["args"], ["-y", "some-pkg"])
        self.assertTrue(result["enabled"])
        self.assertNotIn("url", result)

    def test_stdio_env_preserved(self):
        copilot = {"command": "npx", "args": [], "env": {"KEY": "val"}}
        result = HermesClientAdapter._to_hermes_format(copilot)
        self.assertEqual(result["env"], {"KEY": "val"})

    def test_stdio_empty_env_omitted(self):
        copilot = {"command": "npx", "args": [], "env": {}}
        result = HermesClientAdapter._to_hermes_format(copilot)
        self.assertNotIn("env", result)

    def test_enabled_false(self):
        copilot = {"command": "npx", "args": []}
        result = HermesClientAdapter._to_hermes_format(copilot, enabled=False)
        self.assertFalse(result["enabled"])

    def test_drops_copilot_only_keys(self):
        copilot = {
            "command": "npx",
            "args": [],
            "type": "local",
            "tools": ["*"],
            "id": "",
        }
        result = HermesClientAdapter._to_hermes_format(copilot)
        self.assertNotIn("type", result)
        self.assertNotIn("tools", result)
        self.assertNotIn("id", result)

    def test_remote_type_without_url_omits_null(self):
        # type signals remote but url is missing: must NOT emit `url: null`.
        copilot = {"type": "http"}
        result = HermesClientAdapter._to_hermes_format(copilot)
        self.assertNotIn("url", result)
        self.assertTrue(result["enabled"])

    def test_stdio_without_command_omits_null(self):
        # malformed stdio entry: must NOT emit `command: null`.
        copilot = {"args": ["x"]}
        result = HermesClientAdapter._to_hermes_format(copilot)
        self.assertNotIn("command", result)
        self.assertTrue(result["enabled"])

    def test_http_basic(self):
        copilot = {"url": "https://example.com/mcp"}
        result = HermesClientAdapter._to_hermes_format(copilot)
        self.assertEqual(result["url"], "https://example.com/mcp")
        self.assertTrue(result["enabled"])
        self.assertNotIn("command", result)
        self.assertNotIn("headers", result)

    def test_http_with_headers(self):
        copilot = {
            "url": "https://example.com/mcp",
            "headers": {"X-Custom-Header": "foo"},
        }
        result = HermesClientAdapter._to_hermes_format(copilot)
        self.assertEqual(result["url"], "https://example.com/mcp")
        self.assertEqual(result["headers"], {"X-Custom-Header": "foo"})


class TestHermesConfigPath(unittest.TestCase):
    """Config path resolution honours $HERMES_HOME and defaults to ~/.hermes."""

    def test_default_config_path(self):
        adapter = HermesClientAdapter()
        fake_home = Path("/fake/home")
        with patch.object(Path, "home", staticmethod(lambda: fake_home)):
            path = Path(adapter.get_config_path())
        expected = (fake_home / ".hermes" / "config.yaml").resolve(strict=False)
        self.assertEqual(path, expected)


# --- #3749: documented Hermes keys survive the render -------------------------


if __name__ == "__main__":
    unittest.main()
