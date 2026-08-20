"""Tests for the JetBrains Copilot MCP client adapter."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.adapters.client.intellij import (
    IntelliJClientAdapter,
    IntelliJConfigError,
    _intellij_config_dir,
    _legacy_intellij_config_dir,
)
from apm_cli.cli import cli
from apm_cli.factory import ClientFactory
from apm_cli.utils.path_security import PathTraversalError


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

    def test_macos_path_defaults_to_plugin_config_location(self):
        with (
            patch.object(sys, "platform", "darwin"),
            patch("pathlib.Path.home", return_value=Path("/Users/tester")),
            patch.dict("os.environ", {"XDG_CONFIG_HOME": ""}, clear=False),
        ):
            result = _intellij_config_dir()
        expected = Path("/Users/tester/.config/github-copilot/intellij")
        self.assertEqual(result, expected)

    def test_macos_path_honors_xdg_config_home(self):
        env = {"XDG_CONFIG_HOME": "/custom/config"}
        with (
            patch.object(sys, "platform", "darwin"),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _intellij_config_dir()
        expected = Path("/custom/config/github-copilot/intellij")
        self.assertEqual(result, expected)

    def test_linux_path_default(self):
        env = {"XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "/wrong/data"}
        with (
            patch.object(sys, "platform", "linux"),
            patch("pathlib.Path.home", return_value=Path("/home/tester")),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _intellij_config_dir()
        expected = Path("/home/tester/.config/github-copilot/intellij")
        self.assertEqual(result, expected)

    def test_linux_path_xdg_override(self):
        env = {
            "XDG_CONFIG_HOME": "/custom/config",
            "XDG_DATA_HOME": "/wrong/data",
        }
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _intellij_config_dir()
        expected = Path("/custom/config/github-copilot/intellij")
        self.assertEqual(result, expected)

    def test_linux_rejects_relative_xdg_config_home(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict("os.environ", {"XDG_CONFIG_HOME": "relative/config"}, clear=False),
            self.assertRaises(PathTraversalError),
        ):
            _intellij_config_dir()

    def test_linux_legacy_path_keeps_old_xdg_data_location(self):
        env = {"XDG_DATA_HOME": "/custom/data"}
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _legacy_intellij_config_dir()
        expected = Path("/custom/data/github-copilot/intellij")
        self.assertEqual(result, expected)

    def test_macos_legacy_path_keeps_old_application_support_location(self):
        with (
            patch.object(sys, "platform", "darwin"),
            patch("pathlib.Path.home", return_value=Path("/Users/tester")),
        ):
            result = _legacy_intellij_config_dir()
        expected = Path("/Users/tester/Library/Application Support/github-copilot/intellij")
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
        env = {
            "LOCALAPPDATA": r"C:\fake\local",
            "XDG_CONFIG_HOME": "/wrong/config",
            "XDG_DATA_HOME": "/wrong/data",
        }
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _intellij_config_dir()
        # On any platform, the components must include the expected dirs.
        self.assertIn("github-copilot", result.parts)
        self.assertIn("intellij", result.parts)

    @pytest.mark.windows_compat
    def test_platform_paths_append_native_components(self):
        """Path construction must use pathlib rather than authored separators."""
        platform_root = Path.cwd().anchor or str(Path.cwd())
        env = {
            "LOCALAPPDATA": platform_root,
            "XDG_CONFIG_HOME": platform_root,
        }
        with patch.dict("os.environ", env, clear=False):
            result = _intellij_config_dir()
        self.assertEqual(result.parts[-2:], ("github-copilot", "intellij"))


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
        with (
            self._patch_config_path(),
            pytest.raises(IntelliJConfigError, match="line 1 column 1"),
        ):
            self.adapter.get_current_config()

    def test_get_current_config_rejects_non_object_servers(self):
        self.mcp_json.write_text('{"servers": null}', encoding="utf-8")
        with (
            self._patch_config_path(),
            pytest.raises(IntelliJConfigError, match="non-object"),
        ):
            self.adapter.get_current_config()

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

    def test_update_config_writes_deterministic_atomic_bytes(self):
        self.mcp_json.write_bytes(
            b'{\n  "theme": "dark",\n  "servers": {\n    "existing": {\n'
            b'      "command": "old"\n    }\n  }\n}\n'
        )
        with self._patch_config_path():
            self.adapter.update_config({"new-server": {"command": "new"}})
        assert self.mcp_json.read_bytes() == (
            b'{\n  "theme": "dark",\n  "servers": {\n    "existing": {\n'
            b'      "command": "old"\n    },\n    "new-server": {\n'
            b'      "command": "new"\n    }\n  }\n}\n'
        )

    def test_update_config_rejects_malformed_destination_without_rewrite(self):
        original = b"{malformed\n"
        self.mcp_json.write_bytes(original)
        with (
            self._patch_config_path(),
            pytest.raises(IntelliJConfigError, match="malformed"),
        ):
            self.adapter.update_config({"new-server": {"command": "new"}})
        assert self.mcp_json.read_bytes() == original

    def test_update_config_atomic_failure_preserves_original_bytes(self):
        original = b'{"servers":{"existing":{"command":"old"}}}\n'
        self.mcp_json.write_bytes(original)
        with (
            self._patch_config_path(),
            patch(
                "apm_cli.adapters.client.intellij.atomic_write_text",
                side_effect=OSError("disk full"),
            ),
            pytest.raises(IntelliJConfigError, match="write"),
        ):
            self.adapter.update_config({"new-server": {"command": "new"}})
        assert self.mcp_json.read_bytes() == original

    def test_write_config_rejects_path_outside_adapter_owners(self):
        outside = self.home_root / "outside.json"
        with (
            patch.object(self.adapter, "get_config_path", return_value=str(self.mcp_json)),
            patch.object(self.adapter, "get_legacy_config_path", return_value=None),
            pytest.raises(IntelliJConfigError, match="outside adapter-owned paths"),
        ):
            self.adapter._write_config(outside, {"servers": {}})
        assert not outside.exists()

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliable on Windows CI")
    def test_write_config_rejects_symlink_alias_to_owned_file(self):
        alias = self.home_root / "alias.json"
        self.mcp_json.write_text('{"servers": {}}\n', encoding="utf-8")
        alias.symlink_to(self.mcp_json)
        with (
            patch.object(self.adapter, "get_config_path", return_value=str(self.mcp_json)),
            patch.object(self.adapter, "get_legacy_config_path", return_value=None),
            pytest.raises(IntelliJConfigError, match="outside adapter-owned paths"),
        ):
            self.adapter._write_config(alias, {"servers": {"new": {}}})
        assert alias.is_symlink()
        assert self.mcp_json.read_text(encoding="utf-8") == '{"servers": {}}\n'

    def test_get_current_config_unreadable_is_explicit(self):
        self.mcp_json.write_text('{"servers": {}}', encoding="utf-8")
        with (
            self._patch_config_path(),
            patch("pathlib.Path.read_text", side_effect=PermissionError("denied")),
            pytest.raises(IntelliJConfigError, match="Cannot read"),
        ):
            self.adapter.get_current_config()

    def test_update_config_creates_parent_dir(self):
        import shutil

        shutil.rmtree(self.config_dir)
        with self._patch_config_path():
            self.adapter.update_config({"srv": {"command": "node"}})
        self.assertTrue(self.mcp_json.exists())
        data = json.loads(self.mcp_json.read_text())
        self.assertIn("srv", data["servers"])

    def test_migration_projects_owned_entries_and_preserves_user_content(self):
        canonical = self.home_root / "canonical" / "mcp.json"
        legacy = self.home_root / "legacy" / "mcp.json"
        canonical.parent.mkdir()
        legacy.parent.mkdir()
        canonical.write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "servers": {
                        "canonical-user": {"command": "user"},
                        "collision": {"command": "canonical-user"},
                    },
                }
            ),
            encoding="utf-8",
        )
        legacy.write_text(
            json.dumps(
                {
                    "legacySetting": True,
                    "servers": {
                        "managed": {"command": "apm"},
                        "collision": {"command": "old-apm"},
                        "server": {"command": "user-short-name"},
                        "legacy-user": {"command": "user"},
                    },
                }
            ),
            encoding="utf-8",
        )

        with (
            patch.object(self.adapter, "get_config_path", return_value=str(canonical)),
            patch.object(self.adapter, "get_legacy_config_path", return_value=str(legacy)),
        ):
            migrated = self.adapter.migrate_legacy_managed_servers(
                {"managed", "collision", "org/server"}
            )

        assert migrated == {"managed", "collision"}
        canonical_data = json.loads(canonical.read_text(encoding="utf-8"))
        legacy_data = json.loads(legacy.read_text(encoding="utf-8"))
        assert canonical_data == {
            "theme": "dark",
            "servers": {
                "canonical-user": {"command": "user"},
                "collision": {"command": "canonical-user"},
                "managed": {"command": "apm"},
            },
        }
        assert legacy_data == {
            "legacySetting": True,
            "servers": {
                "server": {"command": "user-short-name"},
                "legacy-user": {"command": "user"},
            },
        }

    def test_migration_without_ownership_preserves_legacy_file_exactly(self):
        canonical = self.home_root / "canonical" / "mcp.json"
        legacy = self.home_root / "legacy" / "mcp.json"
        legacy.parent.mkdir()
        original = b'{"servers":{"user-authored":{"command":"user"}}}\n'
        legacy.write_bytes(original)

        with (
            patch.object(self.adapter, "get_config_path", return_value=str(canonical)),
            patch.object(self.adapter, "get_legacy_config_path", return_value=str(legacy)),
        ):
            migrated = self.adapter.migrate_legacy_managed_servers(set())

        assert migrated == set()
        assert not canonical.exists()
        assert legacy.read_bytes() == original

    def test_remove_managed_servers_preserves_user_entries_in_both_files(self):
        canonical = self.home_root / "canonical" / "mcp.json"
        legacy = self.home_root / "legacy" / "mcp.json"
        canonical.parent.mkdir()
        legacy.parent.mkdir()
        canonical.write_text(
            json.dumps(
                {
                    "servers": {
                        "managed": {"command": "new"},
                        "canonical-user": {"command": "user"},
                    }
                }
            ),
            encoding="utf-8",
        )
        legacy.write_text(
            json.dumps(
                {
                    "legacySetting": True,
                    "servers": {
                        "managed": {"command": "old"},
                        "legacy-user": {"command": "user"},
                    },
                }
            ),
            encoding="utf-8",
        )

        with (
            patch.object(self.adapter, "get_config_path", return_value=str(canonical)),
            patch.object(self.adapter, "get_legacy_config_path", return_value=str(legacy)),
        ):
            removed = self.adapter.remove_managed_servers({"managed"})

        assert removed == {"managed"}
        assert json.loads(canonical.read_text(encoding="utf-8")) == {
            "servers": {"canonical-user": {"command": "user"}}
        }
        assert json.loads(legacy.read_text(encoding="utf-8")) == {
            "legacySetting": True,
            "servers": {"legacy-user": {"command": "user"}},
        }

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


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not reliable on Windows CI")
def test_config_path_rejects_suffix_symlink_escape(tmp_path: Path) -> None:
    """Containment validation must catch a config suffix escaping its root."""
    root = tmp_path / "config"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "github-copilot").symlink_to(outside, target_is_directory=True)

    with (
        patch.object(sys, "platform", "linux"),
        patch.dict("os.environ", {"XDG_CONFIG_HOME": str(root)}, clear=False),
        pytest.raises(PathTraversalError),
    ):
        IntelliJClientAdapter().get_config_path()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not reliable on Windows CI")
def test_config_path_rejects_file_symlink_escape(tmp_path: Path) -> None:
    """The final mcp.json path may not resolve outside its XDG root."""
    root = tmp_path / "config"
    config_dir = root / "github-copilot" / "intellij"
    config_dir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text('{"servers": {}}\n', encoding="utf-8")
    (config_dir / "mcp.json").symlink_to(outside)

    with (
        patch.object(sys, "platform", "linux"),
        patch.dict("os.environ", {"XDG_CONFIG_HOME": str(root)}, clear=False),
        pytest.raises(PathTraversalError),
    ):
        IntelliJClientAdapter().get_config_path()
    assert outside.read_text(encoding="utf-8") == '{"servers": {}}\n'


def test_cli_atomic_write_failure_is_nonzero_without_partial_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI must not report success when the atomic destination write fails."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: atomic-failure\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config" / "github-copilot" / "intellij" / "mcp.json"
    config_path.parent.mkdir(parents=True)
    original = b'{"servers":{"user":{"command":"keep"}}}\n'
    config_path.write_bytes(original)
    monkeypatch.chdir(project)

    with (
        patch.object(
            IntelliJClientAdapter,
            "get_config_path",
            return_value=str(config_path),
        ),
        patch.object(
            IntelliJClientAdapter,
            "get_legacy_config_path",
            return_value=None,
        ),
        patch(
            "apm_cli.adapters.client.intellij.atomic_write_text",
            side_effect=OSError("disk full"),
        ),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "install",
                "--mcp",
                "atomic-server",
                "--transport",
                "http",
                "--url",
                "https://example.invalid/mcp",
                "--target",
                "intellij",
                "--no-policy",
            ],
        )

    assert result.exit_code == 1
    assert "MCP configuration failed for selected runtime(s)" in result.output
    assert "Cannot write JetBrains Copilot MCP config" in result.output
    assert "Check permissions and free space" in result.output
    assert "MCP server written to apm.yml" not in result.output
    assert "Run with --verbose" not in result.output
    assert config_path.read_bytes() == original
    assert not tuple(config_path.parent.glob("apm-atomic-*"))
