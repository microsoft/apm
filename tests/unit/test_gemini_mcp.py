"""Tests for the Gemini CLI MCP client adapter."""

import json
import os
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from apm_cli.adapters.client.gemini import GeminiClientAdapter
from apm_cli.factory import ClientFactory


class TestGeminiClientFactory:
    """Verify GeminiClientAdapter is registered in ClientFactory."""

    def test_factory_creates_gemini_adapter(self):
        adapter = ClientFactory.create_client("gemini")
        assert isinstance(adapter, GeminiClientAdapter)


class TestGeminiClientAdapter(unittest.TestCase):
    """Core config operations for GeminiClientAdapter."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gemini_dir = Path(self.tmp.name) / ".gemini"
        self.gemini_dir.mkdir()
        self.settings_json = self.gemini_dir / "settings.json"
        self._cwd_patcher = patch("os.getcwd", return_value=self.tmp.name)
        self._cwd_patcher.start()
        self.adapter = GeminiClientAdapter()

    def tearDown(self):
        self._cwd_patcher.stop()
        self.tmp.cleanup()

    def test_config_path(self):
        expected = str(Path(self.tmp.name) / ".gemini" / "settings.json")
        self.assertEqual(self.adapter.get_config_path(), expected)

    def test_get_current_config_empty(self):
        config = self.adapter.get_current_config()
        self.assertEqual(config, {})

    def test_get_current_config_existing(self):
        self.settings_json.write_text('{"theme": "dark"}')
        config = self.adapter.get_current_config()
        self.assertEqual(config, {"theme": "dark"})

    def test_get_current_config_invalid_json(self):
        self.settings_json.write_text("not json")
        config = self.adapter.get_current_config()
        self.assertEqual(config, {})

    def test_update_config_creates_file(self):
        self.adapter.update_config({"my-server": {"command": "npx", "args": ["-y", "pkg"]}})
        data = json.loads(self.settings_json.read_text())
        self.assertIn("mcpServers", data)
        self.assertIn("my-server", data["mcpServers"])
        self.assertEqual(data["mcpServers"]["my-server"]["command"], "npx")

    def test_update_config_preserves_existing_keys(self):
        self.settings_json.write_text(json.dumps({
            "theme": "dark",
            "tools": {"sandbox": "docker"},
        }))
        self.adapter.update_config({"server-a": {"command": "node", "args": ["server.js"]}})
        data = json.loads(self.settings_json.read_text())
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["tools"], {"sandbox": "docker"})
        self.assertIn("server-a", data["mcpServers"])

    def test_update_config_merges_servers(self):
        self.settings_json.write_text(json.dumps({
            "mcpServers": {"existing": {"command": "old"}}
        }))
        self.adapter.update_config({"new-server": {"command": "new"}})
        data = json.loads(self.settings_json.read_text())
        self.assertIn("existing", data["mcpServers"])
        self.assertIn("new-server", data["mcpServers"])

    def test_update_config_noop_when_no_gemini_dir(self):
        shutil.rmtree(self.gemini_dir)
        self.adapter.update_config({"server": {"command": "npx"}})
        self.assertFalse(self.settings_json.exists())


class TestGeminiConfigureMCPServer(unittest.TestCase):
    """Test configure_mcp_server() for GeminiClientAdapter."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gemini_dir = Path(self.tmp.name) / ".gemini"
        self.gemini_dir.mkdir()
        self.settings_json = self.gemini_dir / "settings.json"
        self._cwd_patcher = patch("os.getcwd", return_value=self.tmp.name)
        self._cwd_patcher.start()

        self.mock_registry_patcher = patch(
            "apm_cli.adapters.client.copilot.SimpleRegistryClient"
        )
        self.mock_registry_class = self.mock_registry_patcher.start()
        self.mock_registry = MagicMock()
        self.mock_registry_class.return_value = self.mock_registry

        self.mock_integration_patcher = patch(
            "apm_cli.adapters.client.copilot.RegistryIntegration"
        )
        self.mock_integration_class = self.mock_integration_patcher.start()

        self.adapter = GeminiClientAdapter()

    def tearDown(self):
        self._cwd_patcher.stop()
        self.mock_registry_patcher.stop()
        self.mock_integration_patcher.stop()
        self.tmp.cleanup()

    def test_configure_mcp_server_skips_when_no_gemini_dir(self):
        """Should return True (not an error) when .gemini/ doesn't exist."""
        shutil.rmtree(self.gemini_dir)
        result = self.adapter.configure_mcp_server("some/server")
        self.assertTrue(result)

    def test_returns_false_for_empty_url(self):
        result = self.adapter.configure_mcp_server("")
        self.assertFalse(result)

    def test_returns_false_when_server_not_found(self):
        self.mock_registry.find_server_by_reference.return_value = None
        result = self.adapter.configure_mcp_server("unknown/server")
        self.assertFalse(result)

    def test_uses_cached_server_info(self):
        cached = {"some/server": {"packages": [{"name": "pkg", "registry_name": "npm", "runtime_hint": "npx"}]}}
        result = self.adapter.configure_mcp_server(
            "some/server",
            server_info_cache=cached,
        )
        self.assertTrue(result)
        self.mock_registry.find_server_by_reference.assert_not_called()

    def test_extracts_server_name_from_url(self):
        self.mock_registry.find_server_by_reference.return_value = {
            "packages": [{"name": "@scope/mcp-server", "registry_name": "npm", "runtime_hint": "npx"}]
        }
        result = self.adapter.configure_mcp_server("scope/mcp-server")
        self.assertTrue(result)
        data = json.loads(self.settings_json.read_text())
        self.assertIn("mcp-server", data["mcpServers"])

    def test_uses_explicit_server_name(self):
        self.mock_registry.find_server_by_reference.return_value = {
            "packages": [{"name": "pkg", "registry_name": "npm", "runtime_hint": "npx"}]
        }
        result = self.adapter.configure_mcp_server(
            "some/server", server_name="custom-name"
        )
        self.assertTrue(result)
        data = json.loads(self.settings_json.read_text())
        self.assertIn("custom-name", data["mcpServers"])

    def test_supports_user_scope_is_true(self):
        self.assertTrue(self.adapter.supports_user_scope)


class TestGeminiFormatServerConfig(unittest.TestCase):
    """Verify _format_server_config produces Gemini-valid schema."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gemini_dir = Path(self.tmp.name) / ".gemini"
        self.gemini_dir.mkdir()
        self._cwd_patcher = patch("os.getcwd", return_value=self.tmp.name)
        self._cwd_patcher.start()

        self.mock_registry_patcher = patch(
            "apm_cli.adapters.client.copilot.SimpleRegistryClient"
        )
        self.mock_registry_class = self.mock_registry_patcher.start()

        self.mock_integration_patcher = patch(
            "apm_cli.adapters.client.copilot.RegistryIntegration"
        )
        self.mock_integration_class = self.mock_integration_patcher.start()

        self.adapter = GeminiClientAdapter()

    def tearDown(self):
        self._cwd_patcher.stop()
        self.mock_registry_patcher.stop()
        self.mock_integration_patcher.stop()
        self.tmp.cleanup()

    def test_stdio_config_has_no_copilot_fields(self):
        """stdio config must not contain type, tools, or id."""
        server_info = {
            "_raw_stdio": {
                "command": "node",
                "args": ["server.js"],
                "env": {"KEY": "val"},
            },
            "name": "test-server",
        }
        config = self.adapter._format_server_config(server_info)
        self.assertEqual(config["command"], "node")
        self.assertEqual(config["args"], ["server.js"])
        self.assertEqual(config["env"], {"KEY": "val"})
        self.assertNotIn("type", config)
        self.assertNotIn("tools", config)
        self.assertNotIn("id", config)

    def test_npm_package_config_has_no_copilot_fields(self):
        """npm package config must not contain type, tools, or id."""
        server_info = {
            "packages": [{
                "name": "@scope/mcp-server",
                "registry_name": "npm",
                "runtime_hint": "npx",
            }],
            "name": "test-server",
        }
        config = self.adapter._format_server_config(server_info)
        self.assertEqual(config["command"], "npx")
        self.assertIn("@scope/mcp-server", config["args"])
        self.assertNotIn("type", config)
        self.assertNotIn("tools", config)
        self.assertNotIn("id", config)

    def test_remote_http_uses_httpUrl(self):
        """HTTP remotes must use httpUrl key, not url."""
        server_info = {
            "remotes": [{
                "url": "https://api.example.com/mcp",
                "transport_type": "http",
            }],
            "name": "remote-server",
        }
        config = self.adapter._format_server_config(server_info)
        self.assertEqual(config["httpUrl"], "https://api.example.com/mcp")
        self.assertNotIn("url", config)
        self.assertNotIn("type", config)
        self.assertNotIn("tools", config)
        self.assertNotIn("id", config)

    def test_remote_sse_uses_url(self):
        """SSE remotes must use url key, not httpUrl."""
        server_info = {
            "remotes": [{
                "url": "https://api.example.com/sse",
                "transport_type": "sse",
            }],
            "name": "sse-server",
        }
        config = self.adapter._format_server_config(server_info)
        self.assertEqual(config["url"], "https://api.example.com/sse")
        self.assertNotIn("httpUrl", config)
        self.assertNotIn("type", config)
