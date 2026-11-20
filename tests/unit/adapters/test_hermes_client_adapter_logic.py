"""Unit tests for HermesClientAdapter and its MCP integrator wiring (pure logic cases)."""

from __future__ import annotations

import unittest

import pytest

from apm_cli.adapters.client.hermes import HermesClientAdapter

pytestmark = pytest.mark.unit


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


# --- #3749: documented Hermes keys survive the render -------------------------


if __name__ == "__main__":
    unittest.main()
