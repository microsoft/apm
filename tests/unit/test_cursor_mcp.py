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
        self.adapter.update_config(
            {"my-server": {"command": "npx", "args": ["-y", "pkg"]}}
        )
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

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    @patch("apm_cli.adapters.client.copilot.GitHubTokenManager")
    def test_github_remote_server_gets_auth_header(self, mock_token_mgr, mock_find):
        """GitHub MCP servers must use env-var reference for auth in Cursor config.

        Security: Cursor stores config in .cursor/mcp.json which may be committed
        to git. The adapter replaces literal tokens with ${env:GITHUB_TOKEN}
        references so secrets never touch disk.
        """
        mock_token_mgr.return_value.get_token_for_purpose.return_value = (
            "ghp_test_token_12345"
        )
        mock_find.return_value = {
            "id": "github-uuid",
            "name": "github-mcp-server",
            "packages": [],
            "remotes": [
                {
                    "url": "https://api.github.com/mcp",
                    "transport_type": "http",
                    "headers": [],
                }
            ],
        }
        ok = self.adapter.configure_mcp_server("github-mcp-server", "gh-mcp")
        self.assertTrue(ok)
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        server_cfg = data["mcpServers"]["gh-mcp"]
        self.assertEqual(server_cfg["type"], "http")
        self.assertIn("Authorization", server_cfg["headers"])
        # Security: token must be env-var reference, not literal
        self.assertEqual(
            server_cfg["headers"]["Authorization"], "Bearer ${env:GITHUB_TOKEN}"
        )
        self.assertNotIn("tools", server_cfg)
        self.assertNotIn("id", server_cfg)

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_sse_transport_normalized_to_http(self, mock_find):
        """SSE transport type must be normalized to 'http' for Cursor."""
        mock_find.return_value = {
            "id": "sse-uuid",
            "name": "sse-srv",
            "packages": [],
            "remotes": [
                {
                    "url": "https://example.com/sse",
                    "transport_type": "sse",
                    "headers": [{"name": "X-API-Key", "value": "test-key"}],
                }
            ],
        }
        ok = self.adapter.configure_mcp_server("sse-srv", "sse-mcp")
        self.assertTrue(ok)
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["sse-mcp"]["type"], "http")
        self.assertIn("X-API-Key", data["mcpServers"]["sse-mcp"]["headers"])

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_streamable_http_transport_normalized_to_http(self, mock_find):
        """streamable-http transport type must be normalized to 'http' for Cursor."""
        mock_find.return_value = {
            "id": "stream-uuid",
            "name": "stream-srv",
            "packages": [],
            "remotes": [
                {
                    "url": "https://example.com/mcp",
                    "transport_type": "streamable-http",
                    "headers": [],
                }
            ],
        }
        ok = self.adapter.configure_mcp_server("stream-srv", "stream-mcp")
        self.assertTrue(ok)
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["stream-mcp"]["type"], "http")

    def test_stdio_warns_on_input_variables(self):
        """_warn_input_variables should be called for ${input:...} in env vars."""
        server_info = {
            "name": "my-cli",
            "_raw_stdio": {
                "command": "./my-cli",
                "args": ["mcp"],
                "env": {"API_TOKEN": "${input:api-token}"},
            },
        }
        # Should not raise; the warning is printed to stdout
        config = self.adapter._format_server_config(server_info)
        self.assertEqual(config["type"], "stdio")
        self.assertIn("env", config)

    @patch("apm_cli.adapters.client.copilot.GitHubTokenManager")
    def test_format_server_config_delegates_to_parent(self, mock_token_mgr):
        """Verify that _format_server_config calls parent and transforms result."""
        mock_token_mgr.return_value.get_token_for_purpose.return_value = None
        server_info = {
            "id": "test-id",
            "name": "github-mcp-server",
            "packages": [],
            "remotes": [
                {
                    "url": "https://api.github.com/mcp",
                    "transport_type": "http",
                    "headers": [{"name": "X-Custom", "value": "val"}],
                }
            ],
        }
        config = self.adapter._format_server_config(server_info)
        # Parent resolves headers, so X-Custom should be present
        self.assertIn("headers", config)
        self.assertIn("X-Custom", config["headers"])
        # Cursor-specific transformations applied
        self.assertEqual(config["type"], "http")
        self.assertNotIn("tools", config)
        self.assertNotIn("id", config)

    def test_tools_override_stripped_for_cursor(self):
        """_apm_tools_override should be applied by parent but stripped for Cursor."""
        server_info = {
            "name": "my-cli",
            "_raw_stdio": {"command": "./cli", "args": ["mcp"]},
            "_apm_tools_override": ["specific-tool"],
        }
        config = self.adapter._format_server_config(server_info)
        self.assertNotIn("tools", config)

    def test_empty_env_omitted_from_stdio_output(self):
        """Empty env dict should not appear in stdio config output."""
        server_info = {
            "name": "my-cli",
            "_raw_stdio": {
                "command": "./my-cli",
                "args": ["mcp"],
            },
        }
        config = self.adapter._format_server_config(server_info)
        self.assertEqual(config["type"], "stdio")
        self.assertNotIn("env", config)

    def test_runtime_label_is_cursor(self):
        """Cursor adapter must report 'Cursor' as its runtime label."""
        self.assertEqual(self.adapter._runtime_label, "Cursor")

    def test_warn_input_variables_uses_cursor_label(self):
        """_warn_input_variables should reference 'Cursor', not 'Copilot CLI'."""
        server_info = {
            "name": "my-cli",
            "_raw_stdio": {
                "command": "./my-cli",
                "args": ["mcp"],
                "env": {"API_TOKEN": "${input:api-token}"},
            },
        }
        import io
        from unittest.mock import patch as _patch

        buf = io.StringIO()
        with _patch("sys.stdout", buf):
            self.adapter._format_server_config(server_info)
        output = buf.getvalue()
        self.assertIn("Cursor", output)
        self.assertNotIn("Copilot CLI", output)

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_no_packages_no_remotes_returns_false(self, mock_find):
        """Server with no packages and no remotes should fail gracefully."""
        mock_find.return_value = {
            "id": "empty-uuid",
            "name": "empty-srv",
            "packages": [],
            "remotes": [],
        }
        ok = self.adapter.configure_mcp_server("empty-srv", "empty-srv")
        self.assertFalse(ok)


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
            json.dumps(
                {"mcpServers": {"keep": {"command": "k"}, "stale": {"command": "s"}}}
            ),
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


if __name__ == "__main__":
    unittest.main()
