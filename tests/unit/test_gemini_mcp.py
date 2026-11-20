"""Tests for the Gemini CLI MCP client adapter."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.adapters.client.gemini import GeminiClientAdapter
from apm_cli.factory import ClientFactory


class TestGeminiClientFactory:
    """Verify GeminiClientAdapter is registered in ClientFactory."""

    def test_factory_creates_gemini_adapter(self):
        adapter = ClientFactory.create_client("gemini")
        assert isinstance(adapter, GeminiClientAdapter)


class TestGeminiClientAdapter(unittest.TestCase):
    """Core config operations for GeminiClientAdapter under project scope."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.gemini_dir = self.project_root / ".gemini"
        self.gemini_dir.mkdir()
        self.settings_json = self.gemini_dir / "settings.json"
        self.adapter = GeminiClientAdapter(project_root=self.project_root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_path(self):
        expected = str(self.project_root / ".gemini" / "settings.json")
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

    def test_get_current_config_returns_empty_dict_when_no_dir(self):
        """get_current_config returns {} when the .gemini directory does not exist."""
        adapter = GeminiClientAdapter(project_root=Path(tempfile.mkdtemp()))
        config = adapter.get_current_config()
        self.assertEqual(config, {})

    def test_update_config_creates_file(self):
        self.adapter.update_config({"my-server": {"command": "npx", "args": ["-y", "pkg"]}})
        data = json.loads(self.settings_json.read_text())
        self.assertIn("mcpServers", data)
        self.assertIn("my-server", data["mcpServers"])
        self.assertEqual(data["mcpServers"]["my-server"]["command"], "npx")

    def test_update_config_preserves_existing_keys(self):
        self.settings_json.write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "tools": {"sandbox": "docker"},
                }
            )
        )
        self.adapter.update_config({"server-a": {"command": "node", "args": ["server.js"]}})
        data = json.loads(self.settings_json.read_text())
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["tools"], {"sandbox": "docker"})
        self.assertIn("server-a", data["mcpServers"])

    def test_update_config_merges_servers(self):
        self.settings_json.write_text(json.dumps({"mcpServers": {"existing": {"command": "old"}}}))
        self.adapter.update_config({"new-server": {"command": "new"}})
        data = json.loads(self.settings_json.read_text())
        self.assertIn("existing", data["mcpServers"])
        self.assertIn("new-server", data["mcpServers"])

    def test_update_config_noop_when_no_gemini_dir(self):
        shutil.rmtree(self.gemini_dir)
        self.adapter.update_config({"server": {"command": "npx"}})
        self.assertFalse(self.settings_json.exists())


class TestGeminiProjectRootRouting(unittest.TestCase):
    """Regression coverage for #1299: adapter must honour ``project_root`` and
    never read or write through ``os.getcwd()``."""

    def setUp(self):
        self.project_tmp = tempfile.TemporaryDirectory()
        self.cwd_tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.project_tmp.name)
        self.cwd_root = Path(self.cwd_tmp.name)
        (self.project_root / ".gemini").mkdir()

    def tearDown(self):
        self.project_tmp.cleanup()
        self.cwd_tmp.cleanup()

    def test_writes_to_project_root_when_cwd_lacks_gemini(self):
        with patch("os.getcwd", return_value=str(self.cwd_root)):
            adapter = GeminiClientAdapter(project_root=self.project_root)
            adapter.update_config({"srv": {"command": "node"}})

        project_settings = self.project_root / ".gemini" / "settings.json"
        self.assertTrue(project_settings.exists())
        data = json.loads(project_settings.read_text())
        self.assertEqual(data["mcpServers"]["srv"]["command"], "node")

    def test_does_not_pollute_cwd_when_cwd_also_has_gemini(self):
        (self.cwd_root / ".gemini").mkdir()
        with patch("os.getcwd", return_value=str(self.cwd_root)):
            adapter = GeminiClientAdapter(project_root=self.project_root)
            adapter.update_config({"srv": {"command": "node"}})

        self.assertTrue((self.project_root / ".gemini" / "settings.json").exists())
        self.assertFalse((self.cwd_root / ".gemini" / "settings.json").exists())

    def test_falls_back_to_cwd_when_project_root_not_passed(self):
        (self.cwd_root / ".gemini").mkdir()
        with patch("os.getcwd", return_value=str(self.cwd_root)):
            adapter = GeminiClientAdapter()
            adapter.update_config({"srv": {"command": "node"}})

        self.assertTrue((self.cwd_root / ".gemini" / "settings.json").exists())


class TestGeminiUserScope(unittest.TestCase):
    """Cover the user-scope path: ``~/.gemini/settings.json``."""

    def setUp(self):
        self.home_tmp = tempfile.TemporaryDirectory()
        self.home_root = Path(self.home_tmp.name)
        self._home_patcher = patch("pathlib.Path.home", return_value=self.home_root)
        self._home_patcher.start()

    def tearDown(self):
        self._home_patcher.stop()
        self.home_tmp.cleanup()

    def test_user_scope_config_path_points_at_home(self):
        adapter = GeminiClientAdapter(user_scope=True)
        expected = str(self.home_root / ".gemini" / "settings.json")
        self.assertEqual(adapter.get_config_path(), expected)

    def test_user_scope_writes_without_requiring_existing_dir(self):
        # ``~/.gemini/`` does not yet exist; user scope is not opt-in.
        adapter = GeminiClientAdapter(user_scope=True)
        adapter.update_config({"srv": {"command": "node"}})

        home_settings = self.home_root / ".gemini" / "settings.json"
        self.assertTrue(home_settings.exists())
        data = json.loads(home_settings.read_text())
        self.assertEqual(data["mcpServers"]["srv"]["command"], "node")

    def test_user_scope_ignores_project_root(self):
        project = self.home_root.parent / "elsewhere"
        adapter = GeminiClientAdapter(project_root=project, user_scope=True)
        self.assertEqual(
            adapter.get_config_path(),
            str(self.home_root / ".gemini" / "settings.json"),
        )

    def test_user_scope_configure_mcp_server_does_not_short_circuit(self):
        """``configure_mcp_server`` must not early-return in user scope just
        because ``~/.gemini/`` is missing -- user scope is not opt-in."""
        with (
            patch("apm_cli.adapters.client.copilot.SimpleRegistryClient") as registry_cls,
            patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
        ):
            registry = MagicMock()
            registry.find_server_by_reference.return_value = {
                "packages": [{"name": "pkg", "registry_name": "npm", "runtime_hint": "npx"}]
            }
            registry_cls.return_value = registry

            adapter = GeminiClientAdapter(user_scope=True)
            result = adapter.configure_mcp_server("some/server", server_name="srv")

            self.assertTrue(result)
            home_settings = self.home_root / ".gemini" / "settings.json"
            self.assertTrue(home_settings.exists())
            data = json.loads(home_settings.read_text())
            self.assertIn("srv", data["mcpServers"])


class TestGeminiConfigureMCPServer(unittest.TestCase):
    """Test configure_mcp_server() for GeminiClientAdapter."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.gemini_dir = self.project_root / ".gemini"
        self.gemini_dir.mkdir()
        self.settings_json = self.gemini_dir / "settings.json"

        self.mock_registry_patcher = patch("apm_cli.adapters.client.copilot.SimpleRegistryClient")
        self.mock_registry_class = self.mock_registry_patcher.start()
        self.mock_registry = MagicMock()
        self.mock_registry_class.return_value = self.mock_registry

        self.mock_integration_patcher = patch("apm_cli.adapters.client.copilot.RegistryIntegration")
        self.mock_integration_class = self.mock_integration_patcher.start()

        self.adapter = GeminiClientAdapter(project_root=self.project_root)

    def tearDown(self):
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
        cached = {
            "some/server": {
                "packages": [{"name": "pkg", "registry_name": "npm", "runtime_hint": "npx"}]
            }
        }
        result = self.adapter.configure_mcp_server(
            "some/server",
            server_info_cache=cached,
        )
        self.assertTrue(result)
        self.mock_registry.find_server_by_reference.assert_not_called()

    def test_extracts_server_name_from_url(self):
        self.mock_registry.find_server_by_reference.return_value = {
            "packages": [
                {"name": "@scope/mcp-server", "registry_name": "npm", "runtime_hint": "npx"}
            ]
        }
        result = self.adapter.configure_mcp_server("scope/mcp-server")
        self.assertTrue(result)
        data = json.loads(self.settings_json.read_text())
        self.assertIn("mcp-server", data["mcpServers"])

    def test_uses_explicit_server_name(self):
        self.mock_registry.find_server_by_reference.return_value = {
            "packages": [{"name": "pkg", "registry_name": "npm", "runtime_hint": "npx"}]
        }
        result = self.adapter.configure_mcp_server("some/server", server_name="custom-name")
        self.assertTrue(result)
        data = json.loads(self.settings_json.read_text())
        self.assertIn("custom-name", data["mcpServers"])

    def test_supports_user_scope_is_true(self):
        self.assertTrue(self.adapter.supports_user_scope)


class TestGeminiFormatServerConfig(unittest.TestCase):
    """Verify _format_server_config produces Gemini-valid schema."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.gemini_dir = self.project_root / ".gemini"
        self.gemini_dir.mkdir()

        self.mock_registry_patcher = patch("apm_cli.adapters.client.copilot.SimpleRegistryClient")
        self.mock_registry_class = self.mock_registry_patcher.start()

        self.mock_integration_patcher = patch("apm_cli.adapters.client.copilot.RegistryIntegration")
        self.mock_integration_class = self.mock_integration_patcher.start()

        self.adapter = GeminiClientAdapter(project_root=self.project_root)

    def tearDown(self):
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
            "packages": [
                {
                    "name": "@scope/mcp-server",
                    "registry_name": "npm",
                    "runtime_hint": "npx",
                }
            ],
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
            "remotes": [
                {
                    "url": "https://api.example.com/mcp",
                    "transport_type": "http",
                }
            ],
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
            "remotes": [
                {
                    "url": "https://api.example.com/sse",
                    "transport_type": "sse",
                }
            ],
            "name": "sse-server",
        }
        config = self.adapter._format_server_config(server_info)
        self.assertEqual(config["url"], "https://api.example.com/sse")
        self.assertNotIn("httpUrl", config)
        self.assertNotIn("type", config)


class TestGeminiSelfDefinedStdioEnvResolution(unittest.TestCase):
    """Regression coverage for issue #1266.

    Self-defined stdio MCP servers declared in apm.yml pass their env block
    through the adapter as a plain dict (the _raw_stdio["env"] shape).
    Before #1266, the Gemini adapter wrote that dict to disk verbatim, so
    placeholders like ${TOKEN} ended up as literal strings in
    .gemini/settings.json. The fix routes the dict through
    _resolve_environment_variables in legacy mode so all three placeholder
    syntaxes resolve to literal values from env_overrides -> os.environ at
    install time.
    """

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

    @staticmethod
    def _server_info_with_placeholders():
        return {
            "name": "bitbucket",
            "id": "",
            "_raw_stdio": {
                "command": "pnpx",
                "args": ["@aashari/mcp-server-atlassian-bitbucket@3.1.0"],
                "env": {
                    "TOKEN_DOLLAR": "${ATLASSIAN_API_TOKEN}",
                    "TOKEN_ENVPREFIX": "${env:ATLASSIAN_API_TOKEN}",
                    "TOKEN_ANGLE": "<ATLASSIAN_API_TOKEN>",
                    "LITERAL_EMAIL": "user@example.com",
                },
            },
        }

    def test_all_three_placeholder_syntaxes_resolve_to_literal(self):
        env_overrides = {"ATLASSIAN_API_TOKEN": "real-secret-xyz123"}
        with patch.object(self.adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = (
                self._server_info_with_placeholders()
            )
            ok = self.adapter.configure_mcp_server("bitbucket", env_overrides=env_overrides)

        self.assertTrue(ok)
        env_block = json.loads(self.settings_json.read_text())["mcpServers"]["bitbucket"]["env"]
        self.assertEqual(env_block["TOKEN_DOLLAR"], "real-secret-xyz123")
        self.assertEqual(env_block["TOKEN_ENVPREFIX"], "real-secret-xyz123")
        self.assertEqual(env_block["TOKEN_ANGLE"], "real-secret-xyz123")
        self.assertEqual(env_block["LITERAL_EMAIL"], "user@example.com")

    def test_unresolvable_placeholder_is_preserved(self):
        # patch.dict snapshots os.environ on enter and restores on exit, so
        # the pop is reverted automatically after the test.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATLASSIAN_API_TOKEN", None)
            with patch.object(self.adapter, "registry_client") as mock_registry:
                mock_registry.find_server_by_reference.return_value = (
                    self._server_info_with_placeholders()
                )
                self.adapter.configure_mcp_server("bitbucket")

        env_block = json.loads(self.settings_json.read_text())["mcpServers"]["bitbucket"]["env"]
        self.assertEqual(env_block["TOKEN_DOLLAR"], "${ATLASSIAN_API_TOKEN}")
        self.assertEqual(env_block["TOKEN_ENVPREFIX"], "${env:ATLASSIAN_API_TOKEN}")
        self.assertEqual(env_block["TOKEN_ANGLE"], "<ATLASSIAN_API_TOKEN>")

    def test_placeholders_in_args_also_resolve(self):
        server_info = {
            "name": "demo",
            "id": "",
            "_raw_stdio": {
                "command": "demo",
                "args": ["--token", "<API_TOKEN>"],
                "env": {"API_TOKEN": "<API_TOKEN>"},
            },
        }
        with patch.object(self.adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = server_info
            self.adapter.configure_mcp_server("demo", env_overrides={"API_TOKEN": "tok-abc"})

        srv = json.loads(self.settings_json.read_text())["mcpServers"]["demo"]
        self.assertEqual(srv["env"]["API_TOKEN"], "tok-abc")
        self.assertEqual(srv["args"], ["--token", "tok-abc"])
