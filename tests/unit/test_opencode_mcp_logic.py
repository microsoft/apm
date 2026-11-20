"""Unit tests for OpenCodeClientAdapter and its MCP integrator wiring (pure logic cases)."""

import unittest

import pytest

from apm_cli.adapters.client.opencode import OpenCodeClientAdapter
from apm_cli.factory import ClientFactory

pytestmark = pytest.mark.unit


class TestOpenCodeClientFactory(unittest.TestCase):
    """Factory registration for the opencode runtime."""

    def test_create_opencode_client(self):
        client = ClientFactory.create_client("opencode")
        self.assertIsInstance(client, OpenCodeClientAdapter)

    def test_create_opencode_client_case_insensitive(self):
        client = ClientFactory.create_client("OpenCode")
        self.assertIsInstance(client, OpenCodeClientAdapter)


class TestToOpencodeFormat(unittest.TestCase):
    """_to_opencode_format static conversion logic."""

    # -- local (stdio) entries --

    def test_local_command_and_args(self):
        copilot = {"command": "npx", "args": ["-y", "some-pkg"]}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertEqual(result["type"], "local")
        self.assertEqual(result["command"], ["npx", "-y", "some-pkg"])
        self.assertTrue(result["enabled"])

    def test_local_env_mapped_to_environment(self):
        copilot = {"command": "npx", "args": [], "env": {"KEY": "val"}}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertEqual(result["environment"], {"KEY": "val"})

    def test_local_empty_env_omitted(self):
        copilot = {"command": "npx", "args": [], "env": {}}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertNotIn("environment", result)

    def test_enabled_false(self):
        copilot = {"command": "npx", "args": []}
        result = OpenCodeClientAdapter._to_opencode_format(copilot, enabled=False)
        self.assertFalse(result["enabled"])

    def test_explicit_enabled_values_keep_their_json_type(self):
        for enabled in (False, "false", None):
            with self.subTest(enabled=enabled):
                result = OpenCodeClientAdapter._to_opencode_format(
                    {"command": "npx", "args": []}, enabled=enabled
                )
                self.assertEqual(result["enabled"], enabled)
                self.assertIs(type(result["enabled"]), type(enabled))

    # -- remote entries --

    def test_remote_basic(self):
        copilot = {"url": "https://example.com/mcp"}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertEqual(result["type"], "remote")
        self.assertEqual(result["url"], "https://example.com/mcp")
        self.assertTrue(result["enabled"])
        self.assertNotIn("headers", result)

    def test_remote_with_headers(self):
        copilot = {
            "url": "https://example.com/mcp",
            "headers": {"X-Custom-Header": "foo"},
        }
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertEqual(result["type"], "remote")
        self.assertEqual(result["url"], "https://example.com/mcp")
        self.assertEqual(result["headers"], {"X-Custom-Header": "foo"})

    def test_remote_with_empty_headers_omitted(self):
        copilot = {"url": "https://example.com/mcp", "headers": {}}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertNotIn("headers", result)

    def test_remote_with_none_headers_omitted(self):
        copilot = {"url": "https://example.com/mcp", "headers": None}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertNotIn("headers", result)

    def test_remote_headers_not_mutated(self):
        original_headers = {"Authorization": "Bearer tok"}
        copilot = {"url": "https://example.com/mcp", "headers": original_headers}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        result["headers"]["new-key"] = "new-val"
        self.assertNotIn("new-key", original_headers)

    def test_passthrough_fields_preserved_without_copilot_only_keys(self):
        copilot = {
            "type": "http",
            "url": "https://example.com/mcp",
            "tools": ["*"],
            "id": "registry-id",
            "oauth": {"clientId": "client", "callbackPort": 3118},
            "myField": "somevalue",
            "environment": {"NODE_OPTIONS": "--require ./payload.js"},
        }
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertEqual(result["oauth"], {"clientId": "client", "callbackPort": 3118})
        self.assertEqual(result["myField"], "somevalue")
        self.assertNotIn("tools", result)
        self.assertNotIn("id", result)
        self.assertNotIn("environment", result)

    def test_no_command_no_url(self):
        copilot = {"env": {"KEY": "val"}}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertEqual(result["type"], "local")
        self.assertNotIn("command", result)
        self.assertNotIn("url", result)
        self.assertEqual(result["environment"], {"KEY": "val"})


if __name__ == "__main__":
    unittest.main()
