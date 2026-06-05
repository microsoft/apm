"""Tests for the JetBrains Copilot MCP client adapter."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.adapters.client.intellij import IntelliJClientAdapter, _intellij_config_dir
from apm_cli.factory import ClientFactory


class TestIntelliJClientFactory:
    """Verify IntelliJClientAdapter is registered in ClientFactory."""

    def test_factory_creates_intellij_adapter(self):
        adapter = ClientFactory.create_client("intellij")
        assert isinstance(adapter, IntelliJClientAdapter)

    def test_supports_user_scope_is_true(self):
        adapter = ClientFactory.create_client("intellij")
        assert adapter.supports_user_scope is True

    def test_mcp_servers_key_is_servers(self):
        adapter = ClientFactory.create_client("intellij")
        assert adapter.mcp_servers_key == "servers"


class TestIntelliJConfigDir(unittest.TestCase):
    """Verify _intellij_config_dir returns the correct OS-specific path."""

    def test_macos_path(self):
        with (
            patch.object(sys, "platform", "darwin"),
            patch("pathlib.Path.home", return_value=Path("/Users/tester")),
        ):
            result = _intellij_config_dir()
        expected = Path("/Users/tester/Library/Application Support/github-copilot/intellij")
        self.assertEqual(result, expected)

    def test_linux_path_default(self):
        env = {"XDG_DATA_HOME": ""}
        with (
            patch.object(sys, "platform", "linux"),
            patch("pathlib.Path.home", return_value=Path("/home/tester")),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _intellij_config_dir()
        expected = Path("/home/tester/.local/share/github-copilot/intellij")
        self.assertEqual(result, expected)

    def test_linux_path_xdg_override(self):
        env = {"XDG_DATA_HOME": "/custom/data"}
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _intellij_config_dir()
        expected = Path("/custom/data/github-copilot/intellij")
        self.assertEqual(result, expected)

    @unittest.skipUnless(sys.platform == "win32", "Windows path separator only valid on win32")
    def test_windows_path(self):
        env = {"LOCALAPPDATA": "C:\\Users\\tester\\AppData\\Local"}
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _intellij_config_dir()
        expected = Path("C:\\Users\\tester\\AppData\\Local\\github-copilot\\intellij")
        self.assertEqual(result, expected)

    def test_windows_path_components(self):
        """Verify Windows path uses LOCALAPPDATA and correct subdirectories."""
        env = {"LOCALAPPDATA": "/fake/local"}
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _intellij_config_dir()
        # On any platform, the components must include the expected dirs.
        self.assertIn("github-copilot", result.parts)
        self.assertIn("intellij", result.parts)


class TestIntelliJClientAdapter(unittest.TestCase):
    """Core config operations for IntelliJClientAdapter."""

    def setUp(self):
        self.home_tmp = tempfile.TemporaryDirectory()
        self.home_root = Path(self.home_tmp.name)
        self._home_patcher = patch("pathlib.Path.home", return_value=self.home_root)
        self._home_patcher.start()
        # macOS path under the fake home
        self.config_dir = (
            self.home_root / "Library" / "Application Support" / "github-copilot" / "intellij"
        )
        self.config_dir.mkdir(parents=True)
        self.mcp_json = self.config_dir / "mcp.json"
        self.adapter = IntelliJClientAdapter()

    def tearDown(self):
        self._home_patcher.stop()
        self.home_tmp.cleanup()

    def _patch_config_path(self):
        """Patch get_config_path to return our temp mcp.json."""
        return patch.object(self.adapter, "get_config_path", return_value=str(self.mcp_json))

    def test_get_current_config_empty(self):
        with self._patch_config_path():
            config = self.adapter.get_current_config()
        self.assertEqual(config, {})

    def test_get_current_config_existing(self):
        self.mcp_json.write_text(json.dumps({"servers": {"my-srv": {"command": "node"}}}))
        with self._patch_config_path():
            config = self.adapter.get_current_config()
        self.assertIn("servers", config)

    def test_get_current_config_invalid_json(self):
        self.mcp_json.write_text("not json")
        with self._patch_config_path():
            config = self.adapter.get_current_config()
        self.assertEqual(config, {})

    def test_update_config_creates_servers_key(self):
        with self._patch_config_path():
            self.adapter.update_config({"my-server": {"command": "npx", "args": ["-y", "pkg"]}})
        data = json.loads(self.mcp_json.read_text())
        self.assertIn("servers", data)
        self.assertNotIn("mcpServers", data)
        self.assertIn("my-server", data["servers"])
        self.assertEqual(data["servers"]["my-server"]["command"], "npx")

    def test_update_config_preserves_existing_keys(self):
        self.mcp_json.write_text(json.dumps({"theme": "dark"}))
        with self._patch_config_path():
            self.adapter.update_config({"srv": {"command": "node"}})
        data = json.loads(self.mcp_json.read_text())
        self.assertEqual(data["theme"], "dark")
        self.assertIn("srv", data["servers"])

    def test_update_config_merges_servers(self):
        self.mcp_json.write_text(json.dumps({"servers": {"existing": {"command": "old"}}}))
        with self._patch_config_path():
            self.adapter.update_config({"new-server": {"command": "new"}})
        data = json.loads(self.mcp_json.read_text())
        self.assertIn("existing", data["servers"])
        self.assertIn("new-server", data["servers"])

    def test_update_config_creates_parent_dir(self):
        import shutil

        shutil.rmtree(self.config_dir)
        with self._patch_config_path():
            self.adapter.update_config({"srv": {"command": "node"}})
        self.assertTrue(self.mcp_json.exists())
        data = json.loads(self.mcp_json.read_text())
        self.assertIn("srv", data["servers"])

    def test_remote_header_preserves_env_prefix_placeholder(self):
        server_info = {
            "id": "remote-secret",
            "name": "remote-secret",
            "remotes": [
                {
                    "transport_type": "http",
                    "url": "https://example.com/mcp",
                    "headers": [{"name": "X-Token", "value": "${env:MY_TOKEN}"}],
                }
            ],
        }

        with patch.dict(os.environ, {"MY_TOKEN": "literal-secret"}, clear=False):
            config = self.adapter._format_server_config(server_info)

        self.assertEqual(config["headers"]["X-Token"], "${env:MY_TOKEN}")
        self.assertNotIn("literal-secret", json.dumps(config))
        self.assertIn("MY_TOKEN", self.adapter._last_env_placeholder_keys)

    def test_dict_env_literal_uses_env_prefix_placeholder(self):
        with patch.dict(os.environ, {"MY_TOKEN": "ignored-os-env"}, clear=False):
            result = self.adapter._resolve_environment_variables(
                {"MY_TOKEN": "literal-value-from-apm-yml"}, env_overrides=None
            )

        self.assertEqual(result["MY_TOKEN"], "${env:MY_TOKEN}")
        self.assertNotIn("literal-value-from-apm-yml", json.dumps(result))
        self.assertNotIn("ignored-os-env", json.dumps(result))
        self.assertIn("MY_TOKEN", self.adapter._last_env_placeholder_keys)

    def test_dict_env_translates_all_placeholder_syntaxes_to_env_prefix(self):
        result = self.adapter._resolve_environment_variables(
            {
                "PRIMARY_TOKEN": "${MY_STDIO_TOKEN}",
                "PREFIXED_TOKEN": "${env:MY_STDIO_TOKEN}",
                "LEGACY_TOKEN": "<MY_LEGACY_VAR>",
            },
            env_overrides=None,
        )

        self.assertEqual(result["PRIMARY_TOKEN"], "${env:MY_STDIO_TOKEN}")
        self.assertEqual(result["PREFIXED_TOKEN"], "${env:MY_STDIO_TOKEN}")
        self.assertEqual(result["LEGACY_TOKEN"], "${env:MY_LEGACY_VAR}")


class TestIntelliJCollectBakedKeys(unittest.TestCase):
    """Verify _collect_previously_baked_keys reads from 'servers' not 'mcpServers'."""

    def setUp(self):
        self.home_tmp = tempfile.TemporaryDirectory()
        self.home_root = Path(self.home_tmp.name)
        self._home_patcher = patch("pathlib.Path.home", return_value=self.home_root)
        self._home_patcher.start()
        self.config_dir = (
            self.home_root / "Library" / "Application Support" / "github-copilot" / "intellij"
        )
        self.config_dir.mkdir(parents=True)
        self.mcp_json = self.config_dir / "mcp.json"
        self.adapter = IntelliJClientAdapter()

    def tearDown(self):
        self._home_patcher.stop()
        self.home_tmp.cleanup()

    def test_reads_from_servers_key(self):
        self.mcp_json.write_text(
            json.dumps(
                {
                    "servers": {
                        "my-srv": {
                            "command": "node",
                            "env": {"MY_TOKEN": "literal-value"},
                        }
                    }
                }
            )
        )
        with patch.object(self.adapter, "get_config_path", return_value=str(self.mcp_json)):
            baked, headers_baked = self.adapter._collect_previously_baked_keys("", "my-srv")
        self.assertIn("MY_TOKEN", baked)
        self.assertFalse(headers_baked)

    def test_ignores_placeholder_values(self):
        self.mcp_json.write_text(
            json.dumps(
                {
                    "servers": {
                        "my-srv": {
                            "command": "node",
                            "env": {"MY_TOKEN": "${MY_TOKEN}"},
                        }
                    }
                }
            )
        )
        with patch.object(self.adapter, "get_config_path", return_value=str(self.mcp_json)):
            baked, _ = self.adapter._collect_previously_baked_keys("", "my-srv")
        self.assertNotIn("MY_TOKEN", baked)

    def test_missing_server_returns_empty(self):
        self.mcp_json.write_text(json.dumps({"servers": {}}))
        with patch.object(self.adapter, "get_config_path", return_value=str(self.mcp_json)):
            baked, headers_baked = self.adapter._collect_previously_baked_keys("", "missing")
        self.assertEqual(baked, set())
        self.assertFalse(headers_baked)


class TestIntelliJConfigureMCPServer(unittest.TestCase):
    """Test configure_mcp_server() writes 'servers' key for IntelliJClientAdapter."""

    def setUp(self):
        self.home_tmp = tempfile.TemporaryDirectory()
        self.home_root = Path(self.home_tmp.name)
        self._home_patcher = patch("pathlib.Path.home", return_value=self.home_root)
        self._home_patcher.start()
        self.config_dir = (
            self.home_root / "Library" / "Application Support" / "github-copilot" / "intellij"
        )
        self.config_dir.mkdir(parents=True)
        self.mcp_json = self.config_dir / "mcp.json"

        self.registry_patcher = patch("apm_cli.adapters.client.copilot.SimpleRegistryClient")
        self.registry_class = self.registry_patcher.start()
        self.registry = MagicMock()
        self.registry_class.return_value = self.registry

        self.integration_patcher = patch("apm_cli.adapters.client.copilot.RegistryIntegration")
        self.integration_patcher.start()

        self.adapter = IntelliJClientAdapter()

    def tearDown(self):
        self.registry_patcher.stop()
        self.integration_patcher.stop()
        self._home_patcher.stop()
        self.home_tmp.cleanup()

    def test_returns_false_for_empty_url(self):
        result = self.adapter.configure_mcp_server("")
        self.assertFalse(result)

    def test_returns_false_when_server_not_found(self):
        self.registry.find_server_by_reference.return_value = None
        result = self.adapter.configure_mcp_server("unknown/server")
        self.assertFalse(result)

    def test_writes_servers_key_not_mcp_servers(self):
        self.registry.find_server_by_reference.return_value = {
            "packages": [{"name": "pkg", "registry_name": "npm", "runtime_hint": "npx"}]
        }
        with patch.object(self.adapter, "get_config_path", return_value=str(self.mcp_json)):
            result = self.adapter.configure_mcp_server("my/server", server_name="my-srv")
        self.assertTrue(result)
        data = json.loads(self.mcp_json.read_text())
        self.assertIn("servers", data)
        self.assertNotIn("mcpServers", data)
        self.assertIn("my-srv", data["servers"])
