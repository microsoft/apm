"""Unit tests for ClaudeClientAdapter and Claude MCP integrator wiring."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apm_cli.adapters.client.claude import ClaudeClientAdapter
from apm_cli.core.scope import InstallScope
from apm_cli.factory import ClientFactory


class TestClaudeClientFactory(unittest.TestCase):
    def test_create_claude_client(self):
        """ClientFactory returns ClaudeClientAdapter for runtime 'claude'."""
        client = ClientFactory.create_client("claude")
        self.assertIsInstance(client, ClaudeClientAdapter)

    def test_create_claude_client_case_insensitive(self):
        """Runtime name 'Claude' is accepted and maps to ClaudeClientAdapter."""
        client = ClientFactory.create_client("Claude")
        self.assertIsInstance(client, ClaudeClientAdapter)


class TestClaudeClientAdapterProject(unittest.TestCase):
    """Project scope: .mcp.json when .claude/ exists."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".claude").mkdir()
        self.mcp_json = self.root / ".mcp.json"
        self._prev_cwd = os.getcwd()
        os.chdir(self.root)
        self.adapter = ClaudeClientAdapter()
        self.adapter.mcp_install_scope = InstallScope.PROJECT

    def tearDown(self):
        os.chdir(self._prev_cwd)
        self.tmp.cleanup()

    def test_get_config_path_project(self):
        """Project scope config path resolves to .mcp.json under cwd."""
        self.assertEqual(
            Path(self.adapter.get_config_path()).resolve(), self.mcp_json.resolve()
        )

    def test_get_current_config_missing(self):
        """Missing .mcp.json yields empty mcpServers."""
        self.assertEqual(self.adapter.get_current_config(), {"mcpServers": {}})

    def test_update_config_merges(self):
        """update_config merges new servers into existing mcpServers."""
        self.mcp_json.write_text(
            json.dumps({"mcpServers": {"a": {"command": "x"}}}),
            encoding="utf-8",
        )
        self.adapter.update_config({"b": {"command": "y"}})
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        self.assertIn("a", data["mcpServers"])
        self.assertIn("b", data["mcpServers"])

    def test_update_config_preserves_plugin_only_keys_per_server(self):
        """Merge + Claude normalize: Copilot-only keys dropped; plugin extras kept."""
        self.mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "lightspeed-mcp": {
                            "type": "local",
                            "tools": ["*"],
                            "id": "",
                            "command": "podman",
                            "args": ["run", "--rm"],
                            "oauth": {"callbackPort": 8080},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.adapter.update_config(
            {
                "lightspeed-mcp": {
                    "command": "podman",
                    "args": ["run", "--rm", "-i"],
                    "tools": ["*"],
                }
            }
        )
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        srv = data["mcpServers"]["lightspeed-mcp"]
        self.assertNotIn("type", srv)
        self.assertNotIn("tools", srv)
        self.assertNotIn("id", srv)
        self.assertIn("oauth", srv)
        self.assertEqual(srv["args"], ["run", "--rm", "-i"])

    def test_update_config_noop_without_claude_dir(self):
        """Without .claude/, project update_config does not create .mcp.json."""
        (self.root / ".claude").rmdir()
        self.adapter.update_config({"s": {"command": "x"}})
        self.assertFalse(self.mcp_json.exists())

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server(self, mock_find):
        """configure_mcp_server writes normalized stdio entry from registry mock."""
        mock_find.return_value = {
            "id": "",
            "name": "test-server",
            "packages": [{"registry_name": "npm", "name": "test-pkg", "arguments": []}],
            "environment_variables": [],
        }
        ok = self.adapter.configure_mcp_server("test-server", "srv")
        self.assertTrue(ok)
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        self.assertIn("srv", data["mcpServers"])
        srv = data["mcpServers"]["srv"]
        self.assertNotIn("type", srv)
        self.assertNotIn("tools", srv)
        self.assertNotIn("id", srv)
        self.assertEqual(srv["command"], "npx")

    def test_normalize_keeps_http_remote_shape(self):
        """HTTP remote entries keep type/url/headers; strip empty id and default tools."""
        raw = {
            "type": "http",
            "url": "https://example.com/mcp",
            "tools": ["*"],
            "id": "",
            "headers": {"X": "y"},
        }
        norm = ClaudeClientAdapter._normalize_mcp_entry_for_claude_code(raw)
        self.assertEqual(norm["type"], "http")
        self.assertEqual(norm["url"], "https://example.com/mcp")
        self.assertIn("headers", norm)
        self.assertNotIn("id", norm)
        self.assertNotIn("tools", norm)

    def test_normalize_stdio_keeps_nonempty_registry_id(self):
        """Stdio normalize drops type/tools but keeps non-empty server id."""
        raw = {
            "type": "local",
            "tools": ["*"],
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "command": "npx",
            "args": ["-y", "pkg"],
        }
        norm = ClaudeClientAdapter._normalize_mcp_entry_for_claude_code(raw)
        self.assertNotIn("type", norm)
        self.assertNotIn("tools", norm)
        self.assertEqual(norm["id"], "550e8400-e29b-41d4-a716-446655440000")


class TestClaudeClientAdapterUser(unittest.TestCase):
    """User scope: ~/.claude.json top-level mcpServers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.claude_json = self.home / ".claude.json"
        self.adapter = ClaudeClientAdapter()
        self.adapter.mcp_install_scope = InstallScope.USER
        self._home = patch.object(Path, "home", return_value=self.home)
        self._home.start()

    def tearDown(self):
        self._home.stop()
        self.tmp.cleanup()

    def test_merge_user_claude_json(self):
        """User scope update_config merges mcpServers and preserves other top-level keys."""
        self.claude_json.write_text(
            json.dumps({"projects": {}, "mcpServers": {"x": {"command": "a"}}}),
            encoding="utf-8",
        )
        self.adapter.update_config({"y": {"command": "b"}})
        data = json.loads(self.claude_json.read_text(encoding="utf-8"))
        self.assertIn("projects", data)
        self.assertIn("x", data["mcpServers"])
        self.assertIn("y", data["mcpServers"])


class TestMCPIntegratorClaudeStaleCleanup(unittest.TestCase):
    """MCPIntegrator.remove_stale for Claude project .mcp.json and user .claude.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_remove_stale_claude_project_mcp_json(self):
        """remove_stale drops named servers from project .mcp.json at workspace_root."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        (self.root / ".claude").mkdir()
        mcp = self.root / ".mcp.json"
        mcp.write_text(
            json.dumps(
                {"mcpServers": {"keep": {"command": "k"}, "stale": {"command": "s"}}}
            ),
            encoding="utf-8",
        )
        MCPIntegrator.remove_stale(
            {"stale"},
            runtime="claude",
            workspace_root=self.root,
            install_scope=InstallScope.PROJECT,
        )
        data = json.loads(mcp.read_text(encoding="utf-8"))
        self.assertIn("keep", data["mcpServers"])
        self.assertNotIn("stale", data["mcpServers"])

    def test_remove_stale_claude_project_skips_without_claude_dir(self):
        """Project cleanup does not touch .mcp.json when .claude/ is absent (opt-in)."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        mcp = self.root / ".mcp.json"
        raw = json.dumps(
            {"mcpServers": {"keep": {"command": "k"}, "stale": {"command": "s"}}}
        )
        mcp.write_text(raw, encoding="utf-8")
        MCPIntegrator.remove_stale(
            {"stale"},
            runtime="claude",
            workspace_root=self.root,
            install_scope=InstallScope.PROJECT,
        )
        self.assertEqual(mcp.read_text(encoding="utf-8"), raw)

    def test_remove_stale_user_scope_skips_vscode_mcp_json(self):
        """USER-scope stale cleanup does not modify .vscode/mcp.json (CWD-based)."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        vscode_dir = self.root / ".vscode"
        vscode_dir.mkdir(parents=True)
        mcp_path = vscode_dir / "mcp.json"
        raw = json.dumps({"servers": {"stale": {"command": "x"}}})
        mcp_path.write_text(raw, encoding="utf-8")
        MCPIntegrator.remove_stale(
            {"stale"},
            workspace_root=self.root,
            install_scope=InstallScope.USER,
        )
        self.assertEqual(mcp_path.read_text(encoding="utf-8"), raw)

    def test_remove_stale_claude_user_claude_json(self):
        """remove_stale drops named servers from ~/.claude.json mcpServers."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with patch.object(Path, "home", return_value=self.root):
            cfg = self.root / ".claude.json"
            cfg.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "keep": {"command": "k"},
                            "stale": {"command": "s"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            MCPIntegrator.remove_stale(
                {"stale"}, runtime="claude", install_scope=InstallScope.USER
            )
            data = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertIn("keep", data["mcpServers"])
            self.assertNotIn("stale", data["mcpServers"])


class TestMCPIntegratorUserScopeInstall(unittest.TestCase):
    """USER-scope install rejects workspace-based --runtime."""

    def test_install_global_rejects_vscode_runtime(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with self.assertRaises(RuntimeError) as ctx:
            MCPIntegrator.install(
                ["ghcr.io/example/server"],
                runtime="vscode",
                install_scope=InstallScope.USER,
            )
        self.assertIn("Global MCP install", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
