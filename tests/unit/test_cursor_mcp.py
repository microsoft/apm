"""Unit tests for CursorClientAdapter and its MCP integrator wiring."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apm_cli.adapters.client.cursor import CursorClientAdapter
from apm_cli.factory import ClientFactory


class TestCursorClientFactory(unittest.TestCase):
    """Factory registration for the cursor runtime."""

    def test_create_cursor_client(self):
        client = ClientFactory.create_client("cursor")
        self.assertIsInstance(client, CursorClientAdapter)

    def test_create_cursor_client_case_insensitive(self):
        client = ClientFactory.create_client("Cursor")
        self.assertIsInstance(client, CursorClientAdapter)


class TestCursorClientAdapter(unittest.TestCase):
    """Core adapter behaviour."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cursor_dir = Path(self.tmp.name) / ".cursor"
        self.cursor_dir.mkdir()
        self.mcp_json = self.cursor_dir / "mcp.json"

        self.adapter = CursorClientAdapter()
        # Patch cwd so the adapter resolves to our temp directory
        self._cwd_patcher = patch("os.getcwd", return_value=self.tmp.name)
        self._cwd_patcher.start()

    def tearDown(self):
        self._cwd_patcher.stop()
        self.tmp.cleanup()

    # -- config path --

    def test_config_path_is_repo_local(self):
        path = self.adapter.get_config_path()
        self.assertEqual(path, str(self.mcp_json))

    # -- get_current_config --

    def test_get_current_config_missing_file(self):
        self.assertEqual(self.adapter.get_current_config(), {})

    def test_get_current_config_existing_file(self):
        self.mcp_json.write_text(
            json.dumps({"mcpServers": {"s": {"command": "x"}}}),
            encoding="utf-8",
        )
        cfg = self.adapter.get_current_config()
        self.assertIn("mcpServers", cfg)
        self.assertIn("s", cfg["mcpServers"])

    # -- update_config --

    def test_update_config_creates_file(self):
        self.adapter.update_config({"my-server": {"command": "npx", "args": ["-y", "pkg"]}})
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["my-server"]["command"], "npx")

    def test_update_config_merges_existing(self):
        self.mcp_json.write_text(
            json.dumps({"mcpServers": {"old": {"command": "old-cmd"}}}),
            encoding="utf-8",
        )
        self.adapter.update_config({"new": {"command": "new-cmd"}})
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        # Both entries must be present
        self.assertIn("old", data["mcpServers"])
        self.assertIn("new", data["mcpServers"])

    def test_update_config_noop_when_cursor_dir_missing(self):
        """If .cursor/ doesn't exist, update_config should silently skip."""
        self.cursor_dir.rmdir()  # remove the directory
        self.adapter.update_config({"s": {"command": "x"}})
        self.assertFalse(self.mcp_json.exists())

    # -- configure_mcp_server --

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server_basic(self, mock_find):
        mock_find.return_value = {
            "id": "test-id",
            "name": "test-server",
            "packages": [{"registry_name": "npm", "name": "test-pkg", "arguments": []}],
            "environment_variables": [],
        }
        ok = self.adapter.configure_mcp_server("test-server", "my-srv")
        self.assertTrue(ok)
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        self.assertIn("my-srv", data["mcpServers"])
        self.assertEqual(data["mcpServers"]["my-srv"]["command"], "npx")

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server_name_extraction(self, mock_find):
        mock_find.return_value = {
            "id": "id",
            "name": "srv",
            "packages": [{"registry_name": "npm", "name": "pkg"}],
            "environment_variables": [],
        }
        self.adapter.configure_mcp_server("org/my-mcp-server")
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        # Should use last segment as key
        self.assertIn("my-mcp-server", data["mcpServers"])

    def test_configure_mcp_server_skips_when_no_cursor_dir(self):
        """Should return True (not an error) when .cursor/ doesn't exist."""
        self.cursor_dir.rmdir()
        result = self.adapter.configure_mcp_server("some-server")
        self.assertTrue(result)

    # -- _format_server_config --

    def test_stdio_server_outputs_type_stdio(self):
        """Self-defined stdio deps must emit type=stdio, not type=local."""
        server_info = {
            "name": "my-cli",
            "_raw_stdio": {
                "command": "./my-cli",
                "args": ["mcp"],
                "env": {"API_KEY": "secret"},
            },
        }
        config = self.adapter._format_server_config(server_info)
        self.assertEqual(config["type"], "stdio")
        self.assertEqual(config["command"], "./my-cli")
        self.assertEqual(config["args"], ["mcp"])
        self.assertEqual(config["env"], {"API_KEY": "secret"})

    def test_stdio_server_no_copilot_fields(self):
        """Cursor config must NOT emit 'tools' or 'id' fields (Copilot-specific)."""
        server_info = {
            "id": "registry-uuid-12345",
            "name": "my-cli",
            "_raw_stdio": {
                "command": "./my-cli",
                "args": ["mcp"],
            },
        }
        config = self.adapter._format_server_config(server_info)
        self.assertNotIn("tools", config)
        self.assertNotIn("id", config)

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_http_server_outputs_type_http(self, mock_find):
        """Remote servers must emit type=http, not type=local."""
        mock_find.return_value = {
            "id": "remote-uuid",
            "name": "remote-srv",
            "packages": [],
            "remotes": [
                {
                    "url": "https://example.com/mcp",
                    "transport_type": "http",
                    "headers": [{"name": "Authorization", "value": "Bearer token"}],
                }
            ],
        }
        ok = self.adapter.configure_mcp_server("remote-srv", "remote-srv")
        self.assertTrue(ok)
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["remote-srv"]["type"], "http")
        self.assertNotIn("tools", data["mcpServers"]["remote-srv"])
        self.assertNotIn("id", data["mcpServers"]["remote-srv"])

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_stdio_with_packages_outputs_type_stdio(self, mock_find):
        """NPM/docker packages must also emit type=stdio, not type=local."""
        mock_find.return_value = {
            "id": "pkg-uuid",
            "name": "npm-pkg",
            "packages": [
                {
                    "registry_name": "npm",
                    "name": "some-npm-pkg",
                    "runtime_hint": "npx",
                    "arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        ok = self.adapter.configure_mcp_server("npm-pkg", "npm-pkg")
        self.assertTrue(ok)
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["npm-pkg"]["type"], "stdio")
        self.assertNotIn("tools", data["mcpServers"]["npm-pkg"])
        self.assertNotIn("id", data["mcpServers"]["npm-pkg"])


class TestMCPIntegratorCursorStaleCleanup(unittest.TestCase):
    """remove_stale() cleans .cursor/mcp.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cursor_dir = Path(self.tmp.name) / ".cursor"
        self.cursor_dir.mkdir()
        self.mcp_json = self.cursor_dir / "mcp.json"

        self._cwd_patcher = patch(
            "apm_cli.integration.mcp_integrator.Path.cwd",
            return_value=Path(self.tmp.name),
        )
        self._cwd_patcher.start()

    def tearDown(self):
        self._cwd_patcher.stop()
        self.tmp.cleanup()

    def test_remove_stale_cursor(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        self.mcp_json.write_text(
            json.dumps({"mcpServers": {"keep": {"command": "k"}, "stale": {"command": "s"}}}),
            encoding="utf-8",
        )
        MCPIntegrator.remove_stale({"stale"}, runtime="cursor")
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        self.assertIn("keep", data["mcpServers"])
        self.assertNotIn("stale", data["mcpServers"])

    def test_remove_stale_cursor_noop_when_no_file(self):
        """Should not fail when .cursor/mcp.json doesn't exist."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        MCPIntegrator.remove_stale({"stale"}, runtime="cursor")
        # No exception is the assertion


if __name__ == "__main__":
    unittest.main()
