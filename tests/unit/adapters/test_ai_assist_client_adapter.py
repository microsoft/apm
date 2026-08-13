"""Unit tests for AiAssistClientAdapter and its MCP integrator wiring.

ai-assist reads MCP servers from a YAML ``servers:`` block in
``<config_dir>/mcp_servers.yaml``.  These tests pin:

  * factory registration (``ClientFactory.create_client("ai-assist")``);
  * the copilot -> ai-assist entry conversion (stdio + http shapes,
    transport mapping, readonly_tools / pagination passthrough);
  * YAML round-trip writes via ``utils.yaml_io``;
  * idempotent merge semantics;
  * ``$AI_ASSIST_CONFIG_DIR`` path override.
"""

from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from apm_cli.adapters.client.ai_assist import AiAssistClientAdapter
from apm_cli.factory import ClientFactory
from apm_cli.utils.yaml_io import dump_yaml, load_yaml


class TestAiAssistClientFactory(unittest.TestCase):
    """Factory registration for the ai-assist runtime."""

    def test_create_ai_assist_client(self):
        client = ClientFactory.create_client("ai-assist")
        self.assertIsInstance(client, AiAssistClientAdapter)

    def test_create_ai_assist_client_case_insensitive(self):
        client = ClientFactory.create_client("AI-Assist")
        self.assertIsInstance(client, AiAssistClientAdapter)

    def test_ai_assist_uses_servers_key(self):
        self.assertEqual(AiAssistClientAdapter.mcp_servers_key, "servers")

    def test_ai_assist_supports_user_scope(self):
        self.assertTrue(AiAssistClientAdapter.supports_user_scope)

    def test_ai_assist_target_name(self):
        self.assertEqual(AiAssistClientAdapter.target_name, "ai-assist")

    def test_ai_assist_supports_runtime_env_substitution(self):
        self.assertTrue(AiAssistClientAdapter._supports_runtime_env_substitution)


class TestToAiAssistFormat(unittest.TestCase):
    """_to_ai_assist_format static conversion logic."""

    def test_stdio_command_and_args(self):
        copilot = {"command": "npx", "args": ["-y", "some-pkg"]}
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertEqual(result["command"], "npx")
        self.assertEqual(result["args"], ["-y", "some-pkg"])
        self.assertTrue(result["enabled"])
        self.assertNotIn("url", result)

    def test_stdio_env_preserved(self):
        copilot = {"command": "npx", "args": [], "env": {"KEY": "val"}}
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertEqual(result["env"], {"KEY": "val"})

    def test_stdio_empty_env_omitted(self):
        copilot = {"command": "npx", "args": [], "env": {}}
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertNotIn("env", result)

    def test_enabled_false(self):
        copilot = {"command": "npx", "args": []}
        result = AiAssistClientAdapter._to_ai_assist_format(copilot, enabled=False)
        self.assertFalse(result["enabled"])

    def test_drops_copilot_only_keys(self):
        copilot = {
            "command": "npx",
            "args": [],
            "type": "local",
            "tools": ["*"],
            "id": "",
        }
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertNotIn("type", result)
        self.assertNotIn("tools", result)
        self.assertNotIn("id", result)

    def test_remote_type_without_url_omits_null(self):
        copilot = {"type": "http"}
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertNotIn("url", result)
        self.assertTrue(result["enabled"])

    def test_stdio_without_command_omits_null(self):
        copilot = {"args": ["x"]}
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertNotIn("command", result)
        self.assertTrue(result["enabled"])

    def test_http_basic(self):
        copilot = {"url": "https://example.com/mcp"}
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertEqual(result["url"], "https://example.com/mcp")
        self.assertTrue(result["enabled"])
        self.assertNotIn("command", result)
        self.assertNotIn("headers", result)

    def test_http_with_headers(self):
        copilot = {
            "url": "https://example.com/mcp",
            "headers": {"X-Custom-Header": "foo"},
        }
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertEqual(result["url"], "https://example.com/mcp")
        self.assertEqual(result["headers"], {"X-Custom-Header": "foo"})

    def test_transport_sse(self):
        copilot = {"url": "https://example.com/mcp", "type": "sse"}
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertEqual(result["transport"], "sse")

    def test_transport_streamable_http(self):
        copilot = {"url": "https://example.com/mcp", "type": "streamable-http"}
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertEqual(result["transport"], "streamablehttp")

    def test_explicit_transport_preserved(self):
        copilot = {"url": "https://example.com/mcp", "transport": "streamablehttp"}
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertEqual(result["transport"], "streamablehttp")

    def test_readonly_tools_passthrough(self):
        copilot = {"command": "npx", "args": [], "readonly_tools": ["search_*", "get_*"]}
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertEqual(result["readonly_tools"], ["search_*", "get_*"])

    def test_pagination_passthrough(self):
        pagination = {"offset_param": "offset", "limit_param": "limit", "default_page_size": 200}
        copilot = {"command": "npx", "args": [], "pagination": pagination}
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertEqual(result["pagination"], pagination)

    def test_empty_readonly_tools_omitted(self):
        copilot = {"command": "npx", "args": [], "readonly_tools": []}
        result = AiAssistClientAdapter._to_ai_assist_format(copilot)
        self.assertNotIn("readonly_tools", result)


class TestAiAssistConfigPath(unittest.TestCase):
    """Config path resolution honours $AI_ASSIST_CONFIG_DIR and defaults to ~/.ai-assist."""

    def test_default_config_path(self):
        adapter = AiAssistClientAdapter()
        fake_home = Path("/fake/home")
        with patch.object(Path, "home", staticmethod(lambda: fake_home)):
            path = Path(adapter.get_config_path())
        expected = (fake_home / ".ai-assist" / "mcp_servers.yaml").resolve(strict=False)
        self.assertEqual(path, expected)

    def test_config_dir_override(self):
        adapter = AiAssistClientAdapter()
        with patch.dict("os.environ", {"AI_ASSIST_CONFIG_DIR": "/custom/ai-assist"}):
            path = Path(adapter.get_config_path())
        expected = (
            Path("/custom/ai-assist").expanduser().resolve(strict=False) / "mcp_servers.yaml"
        )
        self.assertEqual(path, expected)


class TestAiAssistUpdateConfig(unittest.TestCase):
    """YAML write semantics: merge into servers block."""

    def test_writes_servers_block(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = AiAssistClientAdapter()
                ok = adapter.update_config({"demo": {"command": "npx", "args": ["-y", "demo"]}})
                self.assertTrue(ok)
                cfg_path = home / ".ai-assist" / "mcp_servers.yaml"
                self.assertTrue(cfg_path.is_file())
                data = load_yaml(cfg_path)
                self.assertIn("servers", data)
                self.assertIn("demo", data["servers"])
                self.assertEqual(data["servers"]["demo"]["command"], "npx")

    def test_preserves_unrelated_top_level_keys(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / ".ai-assist" / "mcp_servers.yaml"
            cfg_path.parent.mkdir(parents=True)
            dump_yaml(
                {
                    "some_future_key": "keep-me",
                    "servers": {"old": {"command": "old", "enabled": True}},
                },
                cfg_path,
            )
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = AiAssistClientAdapter()
                ok = adapter.update_config({"demo": {"command": "npx", "args": ["-y", "demo"]}})
                self.assertTrue(ok)
            data = load_yaml(cfg_path)
            self.assertIn("demo", data["servers"])
            self.assertIn("old", data["servers"])
            self.assertEqual(data["some_future_key"], "keep-me")

    def test_idempotent_merge(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = AiAssistClientAdapter()
                payload = {"demo": {"command": "npx", "args": ["-y", "demo"]}}
                adapter.update_config(payload)
                adapter.update_config(payload)
            data = load_yaml(home / ".ai-assist" / "mcp_servers.yaml")
            self.assertEqual(list(data["servers"].keys()), ["demo"])


class TestAiAssistConfigSecurity(unittest.TestCase):
    """Config file is written 0o600 and never clobbered."""

    @unittest.skipUnless(hasattr(os, "fchmod"), "POSIX mode bits required")
    def test_new_config_written_owner_only(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = AiAssistClientAdapter()
                self.assertTrue(adapter.update_config({"demo": {"command": "npx"}}))
            cfg_path = home / ".ai-assist" / "mcp_servers.yaml"
            mode = stat.S_IMODE(cfg_path.stat().st_mode)
            self.assertEqual(mode, 0o600, f"expected 0o600, got {oct(mode)}")

    @unittest.skipUnless(hasattr(os, "fchmod"), "POSIX mode bits required")
    def test_existing_loose_config_tightened_to_owner_only(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / ".ai-assist" / "mcp_servers.yaml"
            cfg_path.parent.mkdir(parents=True)
            dump_yaml({"servers": {}}, cfg_path)
            os.chmod(cfg_path, 0o644)
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = AiAssistClientAdapter()
                self.assertTrue(adapter.update_config({"demo": {"command": "npx"}}))
            mode = stat.S_IMODE(cfg_path.stat().st_mode)
            self.assertEqual(mode, 0o600, f"expected tightened 0o600, got {oct(mode)}")

    def test_malformed_config_is_not_overwritten(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / ".ai-assist" / "mcp_servers.yaml"
            cfg_path.parent.mkdir(parents=True)
            original = "servers: {old: {command: old\nSECRET: keep-me\n"
            cfg_path.write_text(original, encoding="utf-8")
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = AiAssistClientAdapter()
                ok = adapter.update_config({"demo": {"command": "npx"}})
            self.assertFalse(ok, "must refuse to overwrite a malformed config")
            self.assertEqual(cfg_path.read_text(encoding="utf-8"), original)

    def test_non_mapping_config_is_not_overwritten(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / ".ai-assist" / "mcp_servers.yaml"
            cfg_path.parent.mkdir(parents=True)
            original = "- just\n- a\n- list\n"
            cfg_path.write_text(original, encoding="utf-8")
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = AiAssistClientAdapter()
                ok = adapter.update_config({"demo": {"command": "npx"}})
            self.assertFalse(ok)
            self.assertEqual(cfg_path.read_text(encoding="utf-8"), original)

    def test_empty_config_file_is_writable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / ".ai-assist" / "mcp_servers.yaml"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text("", encoding="utf-8")
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = AiAssistClientAdapter()
                self.assertTrue(adapter.update_config({"demo": {"command": "npx"}}))
            data = load_yaml(cfg_path)
            self.assertIn("demo", data["servers"])


if __name__ == "__main__":
    unittest.main()
