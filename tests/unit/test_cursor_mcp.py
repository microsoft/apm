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


if __name__ == "__main__":
    unittest.main()
