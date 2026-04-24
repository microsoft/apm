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
        self.settings_dir = self.kiro_dir / "settings"
        self.settings_dir.mkdir()
        self.mcp_json = self.settings_dir / "mcp.json"

        self.adapter = KiroClientAdapter()
        self._cwd_patcher = patch("os.getcwd", return_value=self.tmp.name)
        self._cwd_patcher.start()

    def tearDown(self):
        self._cwd_patcher.stop()
        self.tmp.cleanup()

    # -- supports_user_scope --

    def test_supports_user_scope_is_true(self):
        """Kiro supports both project and global (~/.kiro/) scope."""
        self.assertTrue(self.adapter.supports_user_scope)

    # -- config path --

    def test_config_path_is_in_settings_subdir(self):
        """MCP config lives at .kiro/settings/mcp.json, not .kiro/mcp.json."""
        path = self.adapter.get_config_path()
        self.assertEqual(path, str(self.mcp_json))

    def test_config_path_user_scope(self):
        expected = str(Path.home() / ".kiro" / "settings" / "mcp.json")
        self.assertEqual(self.adapter.get_config_path(user_scope=True), expected)

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

    def test_update_config_creates_file_in_settings_subdir(self):
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
        """If .kiro/ doesn't exist at all, update_config should silently skip."""
        import shutil
        shutil.rmtree(self.kiro_dir)
        self.adapter.update_config({"s": {"command": "x"}})
        self.assertFalse(self.mcp_json.exists())

    def test_update_config_creates_settings_subdir_if_missing(self):
        """settings/ subdir is auto-created inside an existing .kiro/."""
        import shutil
        shutil.rmtree(self.settings_dir)
        self.adapter.update_config({"s": {"command": "x"}})
        self.assertTrue(self.mcp_json.exists())

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
        import shutil
        shutil.rmtree(self.kiro_dir)
        result = self.adapter.configure_mcp_server("some-server")
        self.assertTrue(result)

    def test_configure_mcp_server_empty_url(self):
        result = self.adapter.configure_mcp_server("")
        self.assertFalse(result)


class TestMCPIntegratorKiroStaleCleanup(unittest.TestCase):
    """remove_stale() cleans .kiro/settings/mcp.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kiro_dir = Path(self.tmp.name) / ".kiro"
        self.settings_dir = self.kiro_dir / "settings"
        self.settings_dir.mkdir(parents=True)
        self.mcp_json = self.settings_dir / "mcp.json"

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
        """Should not fail when .kiro/settings/mcp.json doesn't exist."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        MCPIntegrator.remove_stale({"stale"}, runtime="kiro")


class TestKiroTargetProfile(unittest.TestCase):
    """Kiro entry in KNOWN_TARGETS."""

    def test_kiro_in_known_targets(self):
        from apm_cli.integration.targets import KNOWN_TARGETS
        self.assertIn("kiro", KNOWN_TARGETS)

    def test_kiro_root_dir(self):
        from apm_cli.integration.targets import KNOWN_TARGETS
        self.assertEqual(KNOWN_TARGETS["kiro"].root_dir, ".kiro")

    def test_kiro_supports_instructions_to_steering(self):
        from apm_cli.integration.targets import KNOWN_TARGETS
        target = KNOWN_TARGETS["kiro"]
        self.assertIn("instructions", target.primitives)
        m = target.primitives["instructions"]
        self.assertEqual(m.subdir, "steering")
        self.assertEqual(m.extension, ".md")
        self.assertEqual(m.format_id, "kiro_steering")

    def test_kiro_supports_agents(self):
        from apm_cli.integration.targets import KNOWN_TARGETS
        target = KNOWN_TARGETS["kiro"]
        self.assertIn("agents", target.primitives)
        m = target.primitives["agents"]
        self.assertEqual(m.subdir, "agents")
        self.assertEqual(m.extension, ".json")
        self.assertEqual(m.format_id, "kiro_agent")

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

    def test_kiro_user_scope_supported(self):
        from apm_cli.integration.targets import KNOWN_TARGETS
        self.assertTrue(KNOWN_TARGETS["kiro"].user_supported)

    def test_kiro_user_root_dir(self):
        from apm_cli.integration.targets import KNOWN_TARGETS
        self.assertEqual(KNOWN_TARGETS["kiro"].user_root_dir, ".kiro")

    def test_kiro_for_user_scope_returns_scoped_profile(self):
        from apm_cli.integration.targets import KNOWN_TARGETS
        scoped = KNOWN_TARGETS["kiro"].for_scope(user_scope=True)
        self.assertIsNotNone(scoped)
        self.assertEqual(scoped.root_dir, ".kiro")


class TestKiroSteeringIsVerbatim(unittest.TestCase):
    """kiro_steering format should copy content verbatim (Kiro uses applyTo: natively)."""

    def test_kiro_steering_preserves_apply_to(self):
        from apm_cli.integration.instruction_integrator import InstructionIntegrator
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "coding.instructions.md"
            dst = Path(tmp) / "coding.md"
            content = "---\napplyTo: '**/*.ts'\n---\n\nUse strict TypeScript.\n"
            src.write_text(content, encoding="utf-8")

            ii = InstructionIntegrator()
            ii.copy_instruction_kiro(src, dst)
            result = dst.read_text(encoding="utf-8")

        # applyTo: should be preserved verbatim — no conversion to inclusion/fileMatchPattern
        self.assertIn("applyTo:", result)
        self.assertNotIn("inclusion:", result)
        self.assertNotIn("fileMatchPattern:", result)
        self.assertIn("Use strict TypeScript.", result)

    def test_kiro_steering_preserves_no_frontmatter(self):
        from apm_cli.integration.instruction_integrator import InstructionIntegrator
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "general.instructions.md"
            dst = Path(tmp) / "general.md"
            content = "# General rules\n\nAlways write tests.\n"
            src.write_text(content, encoding="utf-8")

            ii = InstructionIntegrator()
            ii.copy_instruction_kiro(src, dst)
            result = dst.read_text(encoding="utf-8")

        self.assertEqual(result, content)


class TestKiroAgentWriter(unittest.TestCase):
    """_write_kiro_agent converts .agent.md to Kiro JSON agent format."""

    def test_basic_conversion(self):
        from apm_cli.integration.agent_integrator import AgentIntegrator
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "my-agent.agent.md"
            dst = Path(tmp) / "my-agent.json"
            src.write_text(
                "---\nname: My Agent\ndescription: A helpful agent\n---\n\nDo great things.\n",
                encoding="utf-8",
            )
            AgentIntegrator._write_kiro_agent(src, dst)
            data = json.loads(dst.read_text(encoding="utf-8"))

        self.assertEqual(data["name"], "My Agent")
        self.assertEqual(data["description"], "A helpful agent")
        self.assertIn("Do great things.", data["prompt"])

    def test_model_field_included_when_present(self):
        from apm_cli.integration.agent_integrator import AgentIntegrator
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "agent.agent.md"
            dst = Path(tmp) / "agent.json"
            src.write_text(
                "---\nname: agent\ndescription: desc\nmodel: claude-sonnet-4\n---\n\nBody.\n",
                encoding="utf-8",
            )
            AgentIntegrator._write_kiro_agent(src, dst)
            data = json.loads(dst.read_text(encoding="utf-8"))

        self.assertEqual(data["model"], "claude-sonnet-4")

    def test_no_frontmatter(self):
        from apm_cli.integration.agent_integrator import AgentIntegrator
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "plain-agent.agent.md"
            dst = Path(tmp) / "plain-agent.json"
            src.write_text("# Plain agent\n\nDo things.\n", encoding="utf-8")
            AgentIntegrator._write_kiro_agent(src, dst)
            data = json.loads(dst.read_text(encoding="utf-8"))

        self.assertEqual(data["name"], "plain-agent")
        self.assertIn("Plain agent", data["prompt"])

    def test_model_omitted_when_not_in_frontmatter(self):
        from apm_cli.integration.agent_integrator import AgentIntegrator
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "agent.agent.md"
            dst = Path(tmp) / "agent.json"
            src.write_text("---\nname: agent\ndescription: d\n---\n\nBody.\n", encoding="utf-8")
            AgentIntegrator._write_kiro_agent(src, dst)
            data = json.loads(dst.read_text(encoding="utf-8"))

        self.assertNotIn("model", data)


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
        with tempfile.TemporaryDirectory() as tmp:
            from apm_cli.core.target_detection import detect_target
            target, reason = detect_target(Path(tmp), explicit_target="kiro")
        self.assertEqual(target, "kiro")
        self.assertIn("explicit", reason)

    def test_detect_target_auto_kiro(self):
        with tempfile.TemporaryDirectory() as tmp:
            from apm_cli.core.target_detection import detect_target
            (Path(tmp) / ".kiro").mkdir()
            target, reason = detect_target(Path(tmp))
        self.assertEqual(target, "kiro")
        self.assertIn(".kiro/", reason)

    def test_detect_target_kiro_in_all_when_multiple_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            from apm_cli.core.target_detection import detect_target
            (Path(tmp) / ".kiro").mkdir()
            (Path(tmp) / ".github").mkdir()
            target, _ = detect_target(Path(tmp))
        self.assertEqual(target, "all")

    def test_kiro_target_description_contains_paths(self):
        from apm_cli.core.target_detection import get_target_description
        desc = get_target_description("kiro")
        self.assertIn(".kiro/", desc)


if __name__ == "__main__":
    unittest.main()
