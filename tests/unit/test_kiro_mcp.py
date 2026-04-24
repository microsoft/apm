"""Unit tests for KiroClientAdapter and its MCP integrator wiring."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apm_cli.adapters.client.kiro import KiroClientAdapter
from apm_cli.factory import ClientFactory


class TestKiroClientFactory(unittest.TestCase):
    """Factory registration for the kiro runtime."""

    def test_create_kiro_client(self):
        client = ClientFactory.create_client("kiro")
        self.assertIsInstance(client, KiroClientAdapter)

    def test_create_kiro_client_case_insensitive(self):
        client = ClientFactory.create_client("Kiro")
        self.assertIsInstance(client, KiroClientAdapter)


class TestKiroClientAdapter(unittest.TestCase):
    """Core adapter behaviour."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kiro_dir = Path(self.tmp.name) / ".kiro"
        self.kiro_dir.mkdir()
        self.mcp_json = self.kiro_dir / "mcp.json"

        self.adapter = KiroClientAdapter()
        self._cwd_patcher = patch("os.getcwd", return_value=self.tmp.name)
        self._cwd_patcher.start()

    def tearDown(self):
        self._cwd_patcher.stop()
        self.tmp.cleanup()

    # -- config path --

    def test_config_path_is_repo_local(self):
        path = self.adapter.get_config_path()
        self.assertEqual(path, str(self.mcp_json))

    def test_supports_user_scope_is_false(self):
        self.assertFalse(self.adapter.supports_user_scope)

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
        self.assertIn("old", data["mcpServers"])
        self.assertIn("new", data["mcpServers"])

    def test_update_config_noop_when_kiro_dir_missing(self):
        """If .kiro/ doesn't exist, update_config should silently skip."""
        self.kiro_dir.rmdir()
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
        self.assertIn("my-mcp-server", data["mcpServers"])

    def test_configure_mcp_server_skips_when_no_kiro_dir(self):
        """Should return True (not an error) when .kiro/ doesn't exist."""
        self.kiro_dir.rmdir()
        result = self.adapter.configure_mcp_server("some-server")
        self.assertTrue(result)

    def test_configure_mcp_server_empty_url(self):
        result = self.adapter.configure_mcp_server("")
        self.assertFalse(result)


class TestMCPIntegratorKiroStaleCleanup(unittest.TestCase):
    """remove_stale() cleans .kiro/mcp.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kiro_dir = Path(self.tmp.name) / ".kiro"
        self.kiro_dir.mkdir()
        self.mcp_json = self.kiro_dir / "mcp.json"

        self._cwd_patcher = patch(
            "apm_cli.integration.mcp_integrator.Path.cwd",
            return_value=Path(self.tmp.name),
        )
        self._cwd_patcher.start()

    def tearDown(self):
        self._cwd_patcher.stop()
        self.tmp.cleanup()

    def test_remove_stale_kiro(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        self.mcp_json.write_text(
            json.dumps({"mcpServers": {"keep": {"command": "k"}, "stale": {"command": "s"}}}),
            encoding="utf-8",
        )
        MCPIntegrator.remove_stale({"stale"}, runtime="kiro")
        data = json.loads(self.mcp_json.read_text(encoding="utf-8"))
        self.assertIn("keep", data["mcpServers"])
        self.assertNotIn("stale", data["mcpServers"])

    def test_remove_stale_kiro_noop_when_no_file(self):
        """Should not fail when .kiro/mcp.json doesn't exist."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        MCPIntegrator.remove_stale({"stale"}, runtime="kiro")
        # No exception is the assertion


class TestKiroTargetProfile(unittest.TestCase):
    """Kiro entry in KNOWN_TARGETS."""

    def test_kiro_in_known_targets(self):
        from apm_cli.integration.targets import KNOWN_TARGETS

        self.assertIn("kiro", KNOWN_TARGETS)

    def test_kiro_root_dir(self):
        from apm_cli.integration.targets import KNOWN_TARGETS

        self.assertEqual(KNOWN_TARGETS["kiro"].root_dir, ".kiro")

    def test_kiro_supports_instructions(self):
        from apm_cli.integration.targets import KNOWN_TARGETS

        target = KNOWN_TARGETS["kiro"]
        self.assertIn("instructions", target.primitives)
        mapping = target.primitives["instructions"]
        self.assertEqual(mapping.subdir, "steering")
        self.assertEqual(mapping.extension, ".md")
        self.assertEqual(mapping.format_id, "kiro_steering")

    def test_kiro_supports_skills(self):
        from apm_cli.integration.targets import KNOWN_TARGETS

        target = KNOWN_TARGETS["kiro"]
        self.assertIn("skills", target.primitives)
        self.assertEqual(target.primitives["skills"].format_id, "skill_standard")

    def test_kiro_supports_hooks(self):
        from apm_cli.integration.targets import KNOWN_TARGETS

        target = KNOWN_TARGETS["kiro"]
        self.assertIn("hooks", target.primitives)

    def test_kiro_detect_by_dir(self):
        from apm_cli.integration.targets import KNOWN_TARGETS

        self.assertTrue(KNOWN_TARGETS["kiro"].detect_by_dir)

    def test_kiro_no_user_scope(self):
        from apm_cli.integration.targets import KNOWN_TARGETS

        self.assertFalse(KNOWN_TARGETS["kiro"].user_supported)


class TestKiroSteeringConverter(unittest.TestCase):
    """_convert_to_kiro_steering content transforms."""

    def _convert(self, content):
        from apm_cli.integration.instruction_integrator import InstructionIntegrator
        return InstructionIntegrator._convert_to_kiro_steering(content)

    def test_no_frontmatter_gets_always_inclusion(self):
        result = self._convert("# My rule\n\nsome content")
        self.assertIn("inclusion: always", result)
        self.assertIn("# My rule", result)

    def test_apply_to_maps_to_file_match(self):
        content = "---\napplyTo: '**/*.ts'\n---\n\n# TypeScript rules\n"
        result = self._convert(content)
        self.assertIn("inclusion: fileMatch", result)
        self.assertIn('fileMatchPattern: "**/*.ts"', result)
        self.assertIn("# TypeScript rules", result)

    def test_apply_to_not_present_gives_always(self):
        content = "---\ndescription: My rule\n---\n\nBody text\n"
        result = self._convert(content)
        self.assertIn("inclusion: always", result)
        self.assertNotIn("fileMatchPattern", result)

    def test_body_preserved(self):
        content = "---\napplyTo: '**/*.py'\n---\n\nDo not use globals.\n"
        result = self._convert(content)
        self.assertIn("Do not use globals.", result)


class TestTargetDetectionKiro(unittest.TestCase):
    """target_detection.py recognises .kiro/ folders."""

    def test_should_integrate_kiro(self):
        from apm_cli.core.target_detection import should_integrate_kiro
        self.assertTrue(should_integrate_kiro("kiro"))
        self.assertTrue(should_integrate_kiro("all"))
        self.assertFalse(should_integrate_kiro("claude"))
        self.assertFalse(should_integrate_kiro("vscode"))

    def test_kiro_in_all_canonical_targets(self):
        from apm_cli.core.target_detection import ALL_CANONICAL_TARGETS
        self.assertIn("kiro", ALL_CANONICAL_TARGETS)

    def test_detect_target_explicit_kiro(self):
        from pathlib import Path
        from apm_cli.core.target_detection import detect_target

        with tempfile.TemporaryDirectory() as tmp:
            target, reason = detect_target(Path(tmp), explicit_target="kiro")
        self.assertEqual(target, "kiro")
        self.assertIn("explicit", reason)

    def test_detect_target_auto_kiro(self):
        from pathlib import Path
        from apm_cli.core.target_detection import detect_target

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".kiro").mkdir()
            target, reason = detect_target(Path(tmp))
        self.assertEqual(target, "kiro")
        self.assertIn(".kiro/", reason)

    def test_detect_target_kiro_in_all(self):
        from pathlib import Path
        from apm_cli.core.target_detection import detect_target

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".kiro").mkdir()
            (Path(tmp) / ".github").mkdir()
            target, _ = detect_target(Path(tmp))
        self.assertEqual(target, "all")


if __name__ == "__main__":
    unittest.main()
