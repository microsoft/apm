"""Tests for the Claude Code MCP client adapter and integrator wiring."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.claude import ClaudeClientAdapter
from apm_cli.core.scope import InstallScope
from apm_cli.factory import ClientFactory


class TestClaudeClientFactory(unittest.TestCase):
    """Factory wiring for the ``claude`` client type."""

    def test_factory_creates_claude_adapter(self):
        client = ClientFactory.create_client("claude")
        self.assertIsInstance(client, ClaudeClientAdapter)

    def test_factory_threads_user_scope(self):
        client = ClientFactory.create_client("claude", user_scope=True)
        self.assertTrue(client._is_user_scope())

    def test_factory_threads_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ClientFactory.create_client("claude", project_root=tmp)
            self.assertEqual(Path(client.get_config_path()), Path(tmp) / ".mcp.json")


class TestClaudeClientAdapterProject(unittest.TestCase):
    """Project scope: ``<root>/.mcp.json`` with top-level ``mcpServers``."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".claude").mkdir()
        self.adapter = ClaudeClientAdapter(project_root=self.root, user_scope=False)
        self.mcp_path = self.root / ".mcp.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_config_path_uses_project_root(self):
        self.assertEqual(Path(self.adapter.get_config_path()), self.mcp_path)

    def test_update_config_creates_mcp_json(self):
        ok = self.adapter.update_config({"srv": {"command": "node", "args": ["s.js"]}})
        self.assertTrue(ok)
        data = json.loads(self.mcp_path.read_text(encoding="utf-8"))
        self.assertIn("srv", data["mcpServers"])
        self.assertEqual(data["mcpServers"]["srv"]["command"], "node")

    def test_update_config_warns_and_returns_false_without_claude_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = ClaudeClientAdapter(project_root=tmp, user_scope=False)
            ok = adapter.update_config({"srv": {"command": "x"}})
            self.assertFalse(ok)
            self.assertFalse((Path(tmp) / ".mcp.json").exists())

    def test_update_config_normalizes_stdio_entry(self):
        """Stdio entries: drop Copilot-only ``tools``/``id``; rewrite
        Copilot's ``type: "local"`` to Claude Code's canonical
        ``type: "stdio"`` so ``claude mcp list`` renders identically
        to entries installed via ``claude mcp add --transport stdio``."""
        self.adapter.update_config(
            {"srv": {"command": "node", "type": "local", "tools": ["*"], "id": ""}}
        )
        data = json.loads(self.mcp_path.read_text(encoding="utf-8"))
        srv = data["mcpServers"]["srv"]
        self.assertEqual(srv["type"], "stdio")
        self.assertNotIn("tools", srv)
        self.assertNotIn("id", srv)

    def test_update_config_sets_explicit_stdio_type_when_missing(self):
        """An entry with ``command`` but no ``type`` (e.g. older
        registry data) must be rewritten with explicit
        ``type: "stdio"`` to match the canonical Claude Code shape."""
        self.adapter.update_config({"srv": {"command": "node", "args": ["s.js"]}})
        data = json.loads(self.mcp_path.read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["srv"]["type"], "stdio")

    def test_update_config_preserves_remote_type_url(self):
        self.adapter.update_config({"remote": {"type": "http", "url": "https://example.com/mcp"}})
        data = json.loads(self.mcp_path.read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["remote"]["type"], "http")
        self.assertEqual(data["mcpServers"]["remote"]["url"], "https://example.com/mcp")

    def test_get_current_config_returns_empty_when_missing(self):
        self.assertEqual(self.adapter.get_current_config(), {"mcpServers": {}})

    def test_update_config_idempotent(self):
        """Re-applying the same update produces a byte-equal file."""
        cfg = {"srv": {"command": "node", "args": ["s.js"]}}
        self.adapter.update_config(cfg)
        first = self.mcp_path.read_bytes()
        self.adapter.update_config(cfg)
        second = self.mcp_path.read_bytes()
        self.assertEqual(first, second)

    def test_update_config_preserves_other_servers(self):
        self.mcp_path.write_text(
            json.dumps({"mcpServers": {"keep": {"command": "k"}}}), encoding="utf-8"
        )
        self.adapter.update_config({"new": {"command": "n"}})
        data = json.loads(self.mcp_path.read_text(encoding="utf-8"))
        self.assertIn("keep", data["mcpServers"])
        self.assertIn("new", data["mcpServers"])

    def test_update_config_tolerates_malformed_project_mcp_json(self):
        """Corrupted .mcp.json must not abort install; treat as empty + rewrite."""
        self.mcp_path.write_text("{not valid json", encoding="utf-8")
        ok = self.adapter.update_config({"srv": {"command": "x"}})
        self.assertTrue(ok)
        data = json.loads(self.mcp_path.read_text(encoding="utf-8"))
        self.assertIn("srv", data["mcpServers"])


class TestClaudeClientAdapterUser(unittest.TestCase):
    """User scope: ``~/.claude.json`` top-level ``mcpServers``."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.claude_json = self.home / ".claude.json"
        self.adapter = ClaudeClientAdapter(user_scope=True)
        self._home = patch.object(Path, "home", return_value=self.home)
        self._home.start()

    def tearDown(self):
        self._home.stop()
        self.tmp.cleanup()

    def test_merge_user_claude_json_preserves_other_keys(self):
        self.claude_json.write_text(
            json.dumps({"projects": {}, "mcpServers": {"x": {"command": "a"}}}),
            encoding="utf-8",
        )
        self.adapter.update_config({"y": {"command": "b"}})
        data = json.loads(self.claude_json.read_text(encoding="utf-8"))
        self.assertIn("projects", data)
        self.assertIn("x", data["mcpServers"])
        self.assertIn("y", data["mcpServers"])

    def test_new_user_claude_json_created_with_0600_perms(self):
        """New ~/.claude.json must be created with 0o600 to avoid leaking OAuth state."""
        if os.name != "posix":
            self.skipTest("POSIX permission bits not observable on Windows")
        self.assertFalse(self.claude_json.exists())
        ok = self.adapter.update_config({"srv": {"command": "x"}})
        self.assertTrue(ok)
        mode = stat.S_IMODE(os.stat(self.claude_json).st_mode)
        self.assertEqual(mode, 0o600)

    def test_user_scope_write_is_atomic_no_temp_left_behind(self):
        self.adapter.update_config({"srv": {"command": "x"}})
        leftovers = list(self.home.glob(".claude.json.*")) + list(self.home.glob("apm-atomic-*"))
        self.assertEqual(leftovers, [])

    def test_update_config_tolerates_malformed_user_claude_json(self):
        """Corrupted ~/.claude.json must not abort install; treat as empty + rewrite."""
        self.claude_json.write_text("{not valid json", encoding="utf-8")
        ok = self.adapter.update_config({"srv": {"command": "x"}})
        self.assertTrue(ok)
        data = json.loads(self.claude_json.read_text(encoding="utf-8"))
        self.assertIn("srv", data["mcpServers"])

    def test_configure_mcp_server_returns_false_when_update_fails(self):
        """If update_config returns False, configure_mcp_server must surface the failure."""
        from unittest.mock import MagicMock

        self.adapter.registry_client = MagicMock()
        self.adapter.registry_client.find_server_by_reference.return_value = {
            "name": "srv",
            "packages": [{"registry_name": "npm", "name": "x", "version": "1.0.0"}],
        }
        with patch.object(self.adapter, "update_config", return_value=False):
            ok = self.adapter.configure_mcp_server("srv")
        self.assertFalse(ok)


@pytest.mark.parametrize(
    "transport",
    [
        {"type": "sse", "url": "https://example.com/sse"},
        {"type": "streamable-http", "url": "https://example.com/sh"},
    ],
)
def test_normalize_sse_and_streamable_http(transport):
    """Remote SSE / streamable-http entries keep ``type`` and ``url``."""
    out = ClaudeClientAdapter._normalize_mcp_entry_for_claude_code(
        {**transport, "id": "", "tools": ["*"]}
    )
    assert out["type"] == transport["type"]
    assert out["url"] == transport["url"]
    assert "id" not in out
    assert "tools" not in out


class TestMCPIntegratorClaudeStaleCleanup(unittest.TestCase):
    """``MCPIntegrator.remove_stale`` for Claude project / user files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_remove_stale_claude_project_mcp_json(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        (self.root / ".claude").mkdir()
        mcp = self.root / ".mcp.json"
        mcp.write_text(
            json.dumps({"mcpServers": {"keep": {"command": "k"}, "stale": {"command": "s"}}}),
            encoding="utf-8",
        )
        MCPIntegrator.remove_stale(
            {"stale"},
            runtime="claude",
            project_root=self.root,
            scope=InstallScope.PROJECT,
        )
        data = json.loads(mcp.read_text(encoding="utf-8"))
        self.assertIn("keep", data["mcpServers"])
        self.assertNotIn("stale", data["mcpServers"])

    def test_remove_stale_claude_project_skips_without_claude_dir(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        mcp = self.root / ".mcp.json"
        raw = json.dumps({"mcpServers": {"keep": {"command": "k"}, "stale": {"command": "s"}}})
        mcp.write_text(raw, encoding="utf-8")
        MCPIntegrator.remove_stale(
            {"stale"},
            runtime="claude",
            project_root=self.root,
            scope=InstallScope.PROJECT,
        )
        self.assertEqual(mcp.read_text(encoding="utf-8"), raw)

    def test_remove_stale_claude_user_claude_json(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with patch.object(Path, "home", return_value=self.root):
            cfg = self.root / ".claude.json"
            cfg.write_text(
                json.dumps({"mcpServers": {"keep": {"command": "k"}, "stale": {"command": "s"}}}),
                encoding="utf-8",
            )
            MCPIntegrator.remove_stale({"stale"}, runtime="claude", scope=InstallScope.USER)
            data = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertIn("keep", data["mcpServers"])
            self.assertNotIn("stale", data["mcpServers"])

    def test_remove_stale_scope_none_defaults_safely(self):
        """When scope is unspecified, only project .mcp.json is touched."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        (self.root / ".claude").mkdir()
        proj = self.root / ".mcp.json"
        proj.write_text(json.dumps({"mcpServers": {"stale": {"command": "s"}}}), encoding="utf-8")

        with patch.object(Path, "home", return_value=self.root):
            user_cfg = self.root / ".claude.json"
            user_raw = json.dumps({"mcpServers": {"stale": {"command": "u"}}})
            user_cfg.write_text(user_raw, encoding="utf-8")

            MCPIntegrator.remove_stale(
                {"stale"}, runtime="claude", project_root=self.root, scope=None
            )

            self.assertNotIn(
                "stale",
                json.loads(proj.read_text(encoding="utf-8")).get("mcpServers", {}),
            )
            self.assertEqual(user_cfg.read_text(encoding="utf-8"), user_raw)


class TestClaudeAutoDetection(unittest.TestCase):
    """Auto-detection gating for Claude in MCPIntegrator."""

    def test_claude_not_auto_targeted_when_binary_absent_and_no_claude_dir(self):
        """Claude must NOT appear in filtered runtimes when neither gate is satisfied."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        scripts = {"start": "claude --interactive"}
        detected = MCPIntegrator._detect_runtimes(scripts)
        self.assertIn("claude", detected)
        with patch("apm_cli.integration.mcp_integrator.shutil.which", return_value=None):
            filtered = MCPIntegrator._filter_runtimes(detected)
            self.assertNotIn("claude", filtered)


if __name__ == "__main__":
    unittest.main()
