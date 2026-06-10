"""Unit tests for HermesClientAdapter and its MCP integrator wiring.

Hermes reads MCP servers from a YAML ``mcp_servers:`` block in
``~/.hermes/config.yaml`` (snake_case key, distinct from the JSON
``mcpServers`` schema of Claude/Copilot).  These tests pin:

  * factory registration (``ClientFactory.create_client("hermes")``);
  * the copilot -> hermes entry conversion (stdio + http shapes);
  * YAML round-trip writes via ``utils.yaml_io`` that PRESERVE unrelated
    top-level config keys (model provider, telegram, ...);
  * idempotent merge semantics;
  * ``$HERMES_HOME`` path override.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from apm_cli.adapters.client.hermes import HermesClientAdapter
from apm_cli.factory import ClientFactory
from apm_cli.utils.yaml_io import dump_yaml, load_yaml


class TestHermesClientFactory(unittest.TestCase):
    """Factory registration for the hermes runtime."""

    def test_create_hermes_client(self):
        client = ClientFactory.create_client("hermes")
        self.assertIsInstance(client, HermesClientAdapter)

    def test_create_hermes_client_case_insensitive(self):
        client = ClientFactory.create_client("Hermes")
        self.assertIsInstance(client, HermesClientAdapter)

    def test_hermes_uses_snake_case_servers_key(self):
        self.assertEqual(HermesClientAdapter.mcp_servers_key, "mcp_servers")

    def test_hermes_supports_user_scope(self):
        self.assertTrue(HermesClientAdapter.supports_user_scope)

    def test_hermes_target_name(self):
        self.assertEqual(HermesClientAdapter.target_name, "hermes")


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


class TestHermesConfigPath(unittest.TestCase):
    """Config path resolution honours $HERMES_HOME and defaults to ~/.hermes."""

    def test_default_config_path(self):
        adapter = HermesClientAdapter()
        with patch.object(Path, "home", staticmethod(lambda: Path("/fake/home"))):
            path = Path(adapter.get_config_path())
        self.assertEqual(path, Path("/fake/home/.hermes/config.yaml"))

    def test_hermes_home_override(self):
        adapter = HermesClientAdapter()
        with patch.dict("os.environ", {"HERMES_HOME": "/custom/hermes"}):
            path = Path(adapter.get_config_path())
        self.assertEqual(path, Path("/custom/hermes/config.yaml"))


class TestHermesUpdateConfig(unittest.TestCase):
    """YAML write semantics: merge into mcp_servers, preserve siblings."""

    def _adapter_for(self, home: Path) -> HermesClientAdapter:
        adapter = HermesClientAdapter()
        adapter._hermes_config_path = home / ".hermes" / "config.yaml"  # type: ignore[attr-defined]
        return adapter

    def test_writes_mcp_servers_block(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = HermesClientAdapter()
                ok = adapter.update_config({"demo": {"command": "npx", "args": ["-y", "demo"]}})
                self.assertTrue(ok)
                cfg_path = home / ".hermes" / "config.yaml"
                self.assertTrue(cfg_path.is_file())
                data = load_yaml(cfg_path)
                self.assertIn("mcp_servers", data)
                self.assertIn("demo", data["mcp_servers"])
                self.assertEqual(data["mcp_servers"]["demo"]["command"], "npx")

    def test_preserves_unrelated_top_level_keys(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / ".hermes" / "config.yaml"
            cfg_path.parent.mkdir(parents=True)
            dump_yaml(
                {
                    "model": {"provider": "openai", "name": "test-model"},
                    "telegram": {"allowed_users": [424242]},
                    "mcp_servers": {"old": {"command": "old", "enabled": True}},
                },
                cfg_path,
            )
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = HermesClientAdapter()
                ok = adapter.update_config({"demo": {"command": "npx", "args": ["-y", "demo"]}})
                self.assertTrue(ok)
            data = load_yaml(cfg_path)
            # New server merged in...
            self.assertIn("demo", data["mcp_servers"])
            # ...without clobbering existing servers or sibling config keys.
            self.assertIn("old", data["mcp_servers"])
            self.assertEqual(data["model"]["name"], "test-model")
            self.assertEqual(data["telegram"]["allowed_users"], [424242])

    def test_idempotent_merge(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = HermesClientAdapter()
                payload = {"demo": {"command": "npx", "args": ["-y", "demo"]}}
                adapter.update_config(payload)
                adapter.update_config(payload)
            data = load_yaml(home / ".hermes" / "config.yaml")
            self.assertEqual(list(data["mcp_servers"].keys()), ["demo"])


if __name__ == "__main__":
    unittest.main()
