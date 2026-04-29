"""Unit tests for OpenCodeClientAdapter and its MCP integrator wiring."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.adapters.client.opencode import OpenCodeClientAdapter
from apm_cli.factory import ClientFactory


class TestOpenCodeClientFactory(unittest.TestCase):
    """Factory registration for the opencode runtime."""

    def test_create_opencode_client(self):
        client = ClientFactory.create_client("opencode")
        self.assertIsInstance(client, OpenCodeClientAdapter)

    def test_create_opencode_client_case_insensitive(self):
        client = ClientFactory.create_client("OpenCode")
        self.assertIsInstance(client, OpenCodeClientAdapter)


class TestToOpencodeFormat(unittest.TestCase):
    """_to_opencode_format static conversion logic."""

    # -- local (stdio) entries --

    def test_local_command_and_args(self):
        copilot = {"command": "npx", "args": ["-y", "some-pkg"]}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertEqual(result["type"], "local")
        self.assertEqual(result["command"], ["npx", "-y", "some-pkg"])
        self.assertTrue(result["enabled"])

    def test_local_env_mapped_to_environment(self):
        copilot = {"command": "npx", "args": [], "env": {"KEY": "val"}}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertEqual(result["environment"], {"KEY": "val"})

    def test_local_empty_env_omitted(self):
        copilot = {"command": "npx", "args": [], "env": {}}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertNotIn("environment", result)

    def test_enabled_false(self):
        copilot = {"command": "npx", "args": []}
        result = OpenCodeClientAdapter._to_opencode_format(copilot, enabled=False)
        self.assertFalse(result["enabled"])

    # -- remote entries --

    def test_remote_basic(self):
        copilot = {"url": "https://example.com/mcp"}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertEqual(result["type"], "remote")
        self.assertEqual(result["url"], "https://example.com/mcp")
        self.assertTrue(result["enabled"])
        self.assertNotIn("headers", result)

    def test_remote_with_headers(self):
        copilot = {
            "url": "https://example.com/mcp",
            "headers": {"X-Custom-Header": "foo"},
        }
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertEqual(result["type"], "remote")
        self.assertEqual(result["url"], "https://example.com/mcp")
        self.assertEqual(result["headers"], {"X-Custom-Header": "foo"})

    def test_remote_with_empty_headers_omitted(self):
        copilot = {"url": "https://example.com/mcp", "headers": {}}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertNotIn("headers", result)

    def test_remote_with_none_headers_omitted(self):
        copilot = {"url": "https://example.com/mcp", "headers": None}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertNotIn("headers", result)

    def test_remote_headers_not_mutated(self):
        original_headers = {"Authorization": "Bearer tok"}
        copilot = {"url": "https://example.com/mcp", "headers": original_headers}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        result["headers"]["new-key"] = "new-val"
        self.assertNotIn("new-key", original_headers)

    def test_no_command_no_url(self):
        copilot = {"env": {"KEY": "val"}}
        result = OpenCodeClientAdapter._to_opencode_format(copilot)
        self.assertEqual(result["type"], "local")
        self.assertNotIn("command", result)
        self.assertNotIn("url", result)
        self.assertEqual(result["environment"], {"KEY": "val"})


class TestOpenCodeClientAdapter(unittest.TestCase):
    """Core adapter behaviour for update_config / get_current_config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.opencode_dir = Path(self.tmp.name) / ".opencode"
        self.opencode_dir.mkdir()
        self.opencode_json = Path(self.tmp.name) / "opencode.json"

        self.adapter = OpenCodeClientAdapter()
        self._cwd_patcher = patch("os.getcwd", return_value=self.tmp.name)
        self._cwd_patcher.start()

    def tearDown(self):
        self._cwd_patcher.stop()
        self.tmp.cleanup()

    # -- config path --

    def test_config_path_is_repo_local(self):
        path = self.adapter.get_config_path()
        self.assertEqual(path, str(self.opencode_json))

    # -- get_current_config --

    def test_get_current_config_missing_file(self):
        self.assertEqual(self.adapter.get_current_config(), {})

    def test_get_current_config_existing_file(self):
        self.opencode_json.write_text(
            json.dumps({"mcp": {"my-server": {"type": "local", "command": ["x"]}}}),
            encoding="utf-8",
        )
        cfg = self.adapter.get_current_config()
        self.assertIn("mcp", cfg)
        self.assertIn("my-server", cfg["mcp"])

    def test_get_current_config_corrupt_json(self):
        self.opencode_json.write_text("{invalid json", encoding="utf-8")
        self.assertEqual(self.adapter.get_current_config(), {})

    # -- update_config --

    def test_update_config_creates_file(self):
        self.adapter.update_config(
            {"my-server": {"command": "npx", "args": ["-y", "pkg"]}}
        )
        data = json.loads(self.opencode_json.read_text(encoding="utf-8"))
        self.assertEqual(data["mcp"]["my-server"]["type"], "local")
        self.assertEqual(data["mcp"]["my-server"]["command"], ["npx", "-y", "pkg"])

    def test_update_config_merges_existing(self):
        self.opencode_json.write_text(
            json.dumps(
                {"mcp": {"old-server": {"type": "local", "command": ["old-cmd"]}}}
            ),
            encoding="utf-8",
        )
        self.adapter.update_config({"new-server": {"command": "new-cmd", "args": []}})
        data = json.loads(self.opencode_json.read_text(encoding="utf-8"))
        self.assertIn("old-server", data["mcp"])
        self.assertIn("new-server", data["mcp"])

    def test_update_config_noop_when_opencode_dir_missing(self):
        self.opencode_dir.rmdir()
        self.adapter.update_config({"s": {"command": "x", "args": []}})
        self.assertFalse(self.opencode_json.exists())

    def test_update_config_remote_with_headers(self):
        """End-to-end: Copilot-format remote entry with headers written to opencode.json."""
        copilot_entry = {
            "url": "https://example.com/mcp",
            "headers": {"X-Custom-Header": "foo"},
        }
        self.adapter.update_config({"my-server": copilot_entry})
        data = json.loads(self.opencode_json.read_text(encoding="utf-8"))
        server = data["mcp"]["my-server"]
        self.assertEqual(server["type"], "remote")
        self.assertEqual(server["url"], "https://example.com/mcp")
        self.assertEqual(server["headers"], {"X-Custom-Header": "foo"})

    def test_update_config_remote_without_headers(self):
        """Remote entry without headers should not include the headers key."""
        copilot_entry = {"url": "https://example.com/mcp"}
        self.adapter.update_config({"my-server": copilot_entry})
        data = json.loads(self.opencode_json.read_text(encoding="utf-8"))
        server = data["mcp"]["my-server"]
        self.assertEqual(server["type"], "remote")
        self.assertNotIn("headers", server)

    def test_update_config_local_with_env(self):
        copilot_entry = {"command": "npx", "args": ["-y", "pkg"], "env": {"KEY": "val"}}
        self.adapter.update_config({"my-server": copilot_entry})
        data = json.loads(self.opencode_json.read_text(encoding="utf-8"))
        server = data["mcp"]["my-server"]
        self.assertEqual(server["type"], "local")
        self.assertEqual(server["environment"], {"KEY": "val"})

    def test_update_config_enabled_false(self):
        copilot_entry = {"command": "npx", "args": []}
        self.adapter.update_config({"my-server": copilot_entry}, enabled=False)
        data = json.loads(self.opencode_json.read_text(encoding="utf-8"))
        self.assertFalse(data["mcp"]["my-server"]["enabled"])


class TestOpenCodeConfigureMCPServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.opencode_dir = Path(self.tmp.name) / ".opencode"
        self.opencode_dir.mkdir()
        self.opencode_json = Path(self.tmp.name) / "opencode.json"

        self.adapter = OpenCodeClientAdapter()
        self._cwd_patcher = patch("os.getcwd", return_value=self.tmp.name)
        self._cwd_patcher.start()

    def tearDown(self):
        self._cwd_patcher.stop()
        self.tmp.cleanup()

    def test_empty_server_url_returns_false(self):
        self.assertFalse(self.adapter.configure_mcp_server(""))
        self.assertFalse(self.adapter.configure_mcp_server(None))

    def test_returns_false_when_opencode_dir_missing(self):
        self.opencode_dir.rmdir()
        result = self.adapter.configure_mcp_server(
            "some-server", server_info_cache={"some-server": {"name": "x"}}
        )
        self.assertFalse(result)

    def test_server_not_found_returns_false(self):
        self.adapter.registry_client = MagicMock()
        self.adapter.registry_client.find_server_by_reference.return_value = None
        self.assertFalse(self.adapter.configure_mcp_server("unknown-server"))

    def test_config_key_uses_server_name_when_provided(self):
        server_info = {
            "name": "test-npm",
            "packages": [
                {
                    "name": "pkg",
                    "registry_name": "npm",
                    "runtime_hint": "npx",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        cache = {"io.github.org/test-npm": server_info}
        self.adapter.configure_mcp_server(
            "io.github.org/test-npm",
            server_name="custom-name",
            server_info_cache=cache,
        )
        data = json.loads(self.opencode_json.read_text(encoding="utf-8"))
        self.assertIn("custom-name", data["mcp"])

    def test_config_key_derived_from_last_segment(self):
        server_info = {
            "name": "test-npm",
            "packages": [
                {
                    "name": "pkg",
                    "registry_name": "npm",
                    "runtime_hint": "npx",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        cache = {"io.github.org/test-npm": server_info}
        self.adapter.configure_mcp_server(
            "io.github.org/test-npm",
            server_info_cache=cache,
        )
        data = json.loads(self.opencode_json.read_text(encoding="utf-8"))
        self.assertIn("test-npm", data["mcp"])

    def test_config_key_uses_full_url_when_no_slash(self):
        server_info = {
            "name": "simple",
            "packages": [
                {
                    "name": "pkg",
                    "registry_name": "npm",
                    "runtime_hint": "npx",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        cache = {"simple": server_info}
        self.adapter.configure_mcp_server("simple", server_info_cache=cache)
        data = json.loads(self.opencode_json.read_text(encoding="utf-8"))
        self.assertIn("simple", data["mcp"])

    def test_env_overrides_written_for_local_server(self):
        server_info = {
            "name": "test-npm",
            "packages": [
                {
                    "name": "some-mcp-pkg",
                    "registry_name": "npm",
                    "runtime_hint": "npx",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [
                        {"name": "MY_TOKEN", "description": "", "required": True},
                    ],
                }
            ],
        }
        cache = {"test-npm": server_info}
        self.adapter.configure_mcp_server(
            "test-npm",
            server_name="test-npm",
            env_overrides={"MY_TOKEN": "tok"},
            server_info_cache=cache,
        )
        data = json.loads(self.opencode_json.read_text(encoding="utf-8"))
        server = data["mcp"]["test-npm"]
        self.assertEqual(server["type"], "local")
        self.assertEqual(server["environment"], {"MY_TOKEN": "tok"})


class TestMCPIntegratorOpenCodeStaleCleanup(unittest.TestCase):
    """remove_stale() cleans opencode.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.opencode_dir = Path(self.tmp.name) / ".opencode"
        self.opencode_dir.mkdir()
        self.opencode_json = Path(self.tmp.name) / "opencode.json"

        self._cwd_patcher = patch(
            "apm_cli.integration.mcp_integrator.Path.cwd",
            return_value=Path(self.tmp.name),
        )
        self._cwd_patcher.start()

    def tearDown(self):
        self._cwd_patcher.stop()
        self.tmp.cleanup()

    def test_remove_stale_opencode(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        self.opencode_json.write_text(
            json.dumps(
                {"mcp": {"keep": {"type": "local"}, "stale": {"type": "remote"}}}
            ),
            encoding="utf-8",
        )
        MCPIntegrator.remove_stale({"stale"}, runtime="opencode")
        data = json.loads(self.opencode_json.read_text(encoding="utf-8"))
        self.assertIn("keep", data["mcp"])
        self.assertNotIn("stale", data["mcp"])

    def test_remove_stale_opencode_noop_when_no_file(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        MCPIntegrator.remove_stale({"stale"}, runtime="opencode")
        # No exception is the assertion

    def test_remove_stale_opencode_uses_explicit_project_root(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        other_root = Path(self.tmp.name) / "nested-project"
        (other_root / ".opencode").mkdir(parents=True)
        opencode_json = other_root / "opencode.json"
        opencode_json.write_text(
            json.dumps(
                {"mcp": {"keep": {"type": "local"}, "stale": {"type": "remote"}}}
            ),
            encoding="utf-8",
        )

        MCPIntegrator.remove_stale(
            {"stale"},
            runtime="opencode",
            project_root=other_root,
        )

        data = json.loads(opencode_json.read_text(encoding="utf-8"))
        self.assertIn("keep", data["mcp"])
        self.assertNotIn("stale", data["mcp"])


if __name__ == "__main__":
    unittest.main()
