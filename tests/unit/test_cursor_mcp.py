"""Unit tests for CursorClientAdapter and its MCP integrator wiring."""

import json
import os  # noqa: F401
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

    def test_remove_stale_cursor_uses_explicit_project_root(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        other_root = Path(self.tmp.name) / "nested-project"
        cursor_dir = other_root / ".cursor"
        cursor_dir.mkdir(parents=True)
        mcp_json = cursor_dir / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"keep": {"command": "k"}, "stale": {"command": "s"}}}),
            encoding="utf-8",
        )

        MCPIntegrator.remove_stale(
            {"stale"},
            runtime="cursor",
            project_root=other_root,
        )

        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        self.assertIn("keep", data["mcpServers"])
        self.assertNotIn("stale", data["mcpServers"])


class TestCursorFormatServerConfig(unittest.TestCase):
    """CursorClientAdapter._format_server_config emits Cursor-native schema."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cursor_dir = Path(self.tmp.name) / ".cursor"
        self.cursor_dir.mkdir()

        self.adapter = CursorClientAdapter()
        self._cwd_patcher = patch("os.getcwd", return_value=self.tmp.name)
        self._cwd_patcher.start()

    def tearDown(self):
        self._cwd_patcher.stop()
        self.tmp.cleanup()

    # -- helpers --

    _COPILOT_ONLY_KEYS = ("tools", "id")

    def _assert_no_copilot_fields(self, config):
        for key in self._COPILOT_ONLY_KEYS:
            self.assertNotIn(key, config, f"Cursor config must not contain '{key}'")

    # -- tests --

    def test_format_stdio_server_emits_type_stdio(self):
        """Raw stdio server produces type=stdio, command/args/env, no tools/id."""
        server_info = {
            "id": "abc-123",
            "name": "my-stdio-server",
            "_raw_stdio": {
                "command": "node",
                "args": ["server.js", "--port", "3000"],
                "env": {"API_KEY": "secret"},
            },
        }
        config = self.adapter._format_server_config(server_info)

        self.assertEqual(config["type"], "stdio")
        self.assertEqual(config["command"], "node")
        self.assertIn("args", config)
        self.assertIn("env", config)
        self._assert_no_copilot_fields(config)

    def test_format_remote_server_emits_type_http(self):
        """Remote (HTTP) server produces type=http, url, no tools/id."""
        server_info = {
            "id": "remote-456",
            "name": "my-remote-server",
            "remotes": [
                {
                    "url": "https://mcp.example.com/sse",
                    "transport_type": "http",
                },
            ],
        }
        config = self.adapter._format_server_config(server_info)

        self.assertEqual(config["type"], "http")
        self.assertEqual(config["url"], "https://mcp.example.com/sse")
        self._assert_no_copilot_fields(config)

    def test_format_npm_package_emits_type_stdio(self):
        """npm package server produces type=stdio, command=npx and args, no tools/id."""
        server_info = {
            "id": "npm-789",
            "name": "my-npm-server",
            "packages": [
                {
                    "registry_name": "npm",
                    "name": "@example/mcp-server",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                },
            ],
        }
        config = self.adapter._format_server_config(server_info)

        self.assertEqual(config["type"], "stdio")
        self.assertEqual(config["command"], "npx")
        self.assertIn("-y", config["args"])
        self.assertIn("@example/mcp-server", config["args"])
        self._assert_no_copilot_fields(config)


class TestCursorTokenInjection(unittest.TestCase):
    """Test GitHub token injection for Cursor remote servers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cursor_dir = Path(self.tmp.name) / ".cursor"
        self.cursor_dir.mkdir()

        self.adapter = CursorClientAdapter()
        self._cwd_patcher = patch("os.getcwd", return_value=self.tmp.name)
        self._cwd_patcher.start()

    def tearDown(self):
        self._cwd_patcher.stop()
        self.tmp.cleanup()

    def test_github_remote_injects_token(self):
        """Legitimate GitHub remote must get Authorization header."""
        server_info = {
            "name": "github-mcp-server",
            "remotes": [
                {"url": "https://api.github.com/v1", "transport_type": "http"},
            ],
        }
        with patch("apm_cli.adapters.client.cursor.GitHubTokenManager") as mock_tm:
            mock_tm.return_value.get_token_for_purpose.return_value = "test-tok"
            config = self.adapter._format_server_config(server_info)
        self.assertEqual(config.get("headers", {}).get("Authorization"), "Bearer test-tok")

    def test_non_github_remote_no_token(self):
        """Non-GitHub remote must NOT get Authorization header."""
        server_info = {
            "name": "my-custom-server",
            "remotes": [
                {"url": "https://evil.example.com/v1", "transport_type": "http"},
            ],
        }
        config = self.adapter._format_server_config(server_info)
        self.assertNotIn("Authorization", config.get("headers", {}))

    def test_registry_header_cannot_override_github_token(self):
        """Registry-supplied Authorization must not clobber injected GitHub token."""
        server_info = {
            "name": "github-mcp-server",
            "remotes": [
                {
                    "url": "https://api.github.com/v1",
                    "transport_type": "http",
                    "headers": [
                        {"name": "Authorization", "value": "Bearer evil-token"},
                    ],
                },
            ],
        }
        with patch("apm_cli.adapters.client.cursor.GitHubTokenManager") as mock_tm:
            mock_tm.return_value.get_token_for_purpose.return_value = "legit-tok"
            config = self.adapter._format_server_config(server_info)
        self.assertEqual(config["headers"]["Authorization"], "Bearer legit-tok")

    def test_unsupported_packages_raises_valueerror(self):
        """When _select_best_package returns None, raise ValueError instead of silent {}."""
        server_info = {
            "name": "weird-server",
            "packages": [
                {"registry_name": "unsupported-registry", "name": "pkg"},
            ],
        }
        with patch.object(self.adapter, "_select_best_package", return_value=None):
            with self.assertRaises(ValueError) as ctx:
                self.adapter._format_server_config(server_info)
        self.assertIn("No supported package type", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
