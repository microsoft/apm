"""Tests for the Claude Code MCP client adapter and integrator wiring."""

import json
import os
import stat
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.claude import ClaudeClientAdapter
from apm_cli.adapters.client.copilot import CopilotClientAdapter
from apm_cli.core.scope import InstallScope
from apm_cli.factory import ClientFactory
from apm_cli.utils.path_security import PathTraversalError


@pytest.mark.parametrize("cwd", ["/fixture-cwd", "./relative", r"C:\fixture", "/with spaces"])
def test_managed_projection_is_nonmutating(tmp_path: Path, cwd: str) -> None:
    client = ClaudeClientAdapter(project_root=tmp_path)
    baseline = {"type": "stdio", "command": "python", "env": {"KEEP": "value"}}
    requested = {"type": "http", "url": "https://example.invalid/mcp"}
    native = {**deepcopy(baseline), "cwd": cwd, "oauthAccount": {"accountUuid": "keep"}}
    before = deepcopy((native, baseline, requested))

    projected = client.project_managed_server_config(native, baseline=baseline, requested=requested)

    assert projected == {key: value for key, value in native.items() if key != "cwd"}
    assert projected is not native
    assert (native, baseline, requested) == before


@pytest.mark.parametrize(
    "cwd",
    [None, True, 1, [], {}, "", " \t", "bad\x00path", "../other", r"..\other", "%2e%2e/other"],
)
def test_managed_projection_keeps_invalid_cwd(tmp_path: Path, cwd: object) -> None:
    client = ClaudeClientAdapter(project_root=tmp_path)
    baseline = {"type": "stdio", "command": "python"}
    native = {**baseline, "cwd": cwd}
    assert (
        client.project_managed_server_config(native, baseline=baseline, requested=baseline)
        is native
    )


@pytest.mark.parametrize(
    ("baseline", "requested", "native"),
    [
        ({"type": "stdio", "cwd": "/old"}, {"type": "http"}, {"type": "stdio", "cwd": "/new"}),
        ({"type": "stdio"}, {"type": "stdio", "cwd": "/same"}, {"type": "stdio", "cwd": "/same"}),
        ({"type": "http"}, {"type": "http"}, {"type": "http", "cwd": "/native"}),
        ({"type": "stdio"}, {"type": "http"}, {"type": "http", "cwd": "/native"}),
        ({"type": "stdio"}, {"type": "http"}, {"command": "python", "cwd": "/native"}),
        ({"type": "stdio"}, {"type": "http"}, []),
    ],
)
def test_managed_projection_does_not_expand_eligibility(
    tmp_path: Path, baseline: dict, requested: dict, native: object
) -> None:
    client = ClaudeClientAdapter(project_root=tmp_path)
    assert (
        client.project_managed_server_config(native, baseline=baseline, requested=requested)
        is native
    )


@pytest.mark.parametrize("target", ["copilot", "codex"])
def test_other_clients_keep_identity_projection(tmp_path: Path, target: str) -> None:
    client = ClientFactory.create_client(target, project_root=tmp_path)
    native = {"command": "python", "cwd": "/native", "extra": {"keep": True}}
    before = deepcopy(native)
    assert client.project_managed_server_config(native, baseline={}, requested={}) is native
    assert native == before


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

    def test_self_defined_skill_command_rewritten_to_claude_skills(self):
        """Skill-shipped MCP launchers declare their command using the
        cross-tool ``.agents/skills/<name>/...`` convention.  Claude does not
        converge to ``.agents/skills/`` (see install/skill_path_migration.py),
        so the prefix must be rewritten to ``.claude/skills/`` -- otherwise
        ``.mcp.json`` points at a path that does not exist on disk.
        """
        with patch.object(self.adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = {
                "name": "chrome-devtools",
                "_raw_stdio": {
                    "command": ".agents/skills/nix-chrome-devtools-mcp/bin/serve",
                    "args": [],
                    "env": {},
                },
            }
            ok = self.adapter.configure_mcp_server("chrome-devtools")

        self.assertTrue(ok)
        data = json.loads(self.mcp_path.read_text(encoding="utf-8"))
        self.assertEqual(
            data["mcpServers"]["chrome-devtools"]["command"],
            ".claude/skills/nix-chrome-devtools-mcp/bin/serve",
        )

    def test_self_defined_non_skill_command_left_unchanged(self):
        """Only the ``.agents/skills/<name>/...`` prefix is rewritten;
        bare binaries (``npx``, ``node``), absolute paths, and unrelated
        relative paths must pass through verbatim.
        """
        for raw_command in (
            "npx",
            "/usr/local/bin/mcp-server",
            "./scripts/run-mcp.sh",
            ".agents/other/path",
        ):
            with self.subTest(command=raw_command):
                self.assertEqual(
                    self.adapter._rewrite_self_defined_skill_command(raw_command),
                    raw_command,
                )

    def test_configure_self_defined_stdio_preserves_env(self):
        with patch.object(self.adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = {
                "name": "env-demo",
                "_raw_stdio": {
                    "command": "npx",
                    "args": ["-y", "example-mcp"],
                    "env": {
                        "DEMO_ENV": "demo-value",
                        "PORT": 3000,
                        "DEBUG": False,
                        "RATE": 0.5,
                    },
                },
            }

            ok = self.adapter.configure_mcp_server("env-demo")

        self.assertTrue(ok)
        data = json.loads(self.mcp_path.read_text(encoding="utf-8"))
        self.assertEqual(
            data["mcpServers"]["env-demo"]["env"],
            {
                "DEMO_ENV": "demo-value",
                "PORT": "3000",
                "DEBUG": "false",
                "RATE": "0.5",
            },
        )


class TestClaudeProjectEnvPlaceholders(unittest.TestCase):
    """#3252: the committed ``.mcp.json`` keeps ``${VAR}`` references.

    Claude Code expands them when it starts the server, so APM must not
    resolve secrets at install time and bake them into the project.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".claude").mkdir()
        self.mcp_path = self.root / ".mcp.json"
        ClaudeClientAdapter.reset_install_run_state()
        CopilotClientAdapter.reset_install_run_state()

    def tearDown(self):
        ClaudeClientAdapter.reset_install_run_state()
        CopilotClientAdapter.reset_install_run_state()
        self.tmp.cleanup()

    def _configure(self, adapter, server_info):
        with patch.object(adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = server_info
            self.assertTrue(adapter.configure_mcp_server(server_info["name"]))

    def _configure_project(self, server_info):
        self._configure(ClaudeClientAdapter(project_root=self.root), server_info)
        return self.mcp_path.read_text(encoding="utf-8")

    @staticmethod
    def _stdio_info(name, env):
        return {"name": name, "_raw_stdio": {"command": "uvx", "args": [name], "env": env}}

    def test_self_defined_env_references_stay_placeholders(self):
        env = {
            "JIRA_TOKEN": "${JIRA_TOKEN}",
            "JIRA_URL": "${env:JIRA_URL}",
            "LEGACY": "<LEGACY_TOKEN>",
            "LOG_LEVEL": "debug",
        }
        secrets = {
            "JIRA_TOKEN": "token-sentinel",
            "JIRA_URL": "https://jira.invalid",
            "LEGACY_TOKEN": "legacy-sentinel",
        }
        with patch.dict(os.environ, secrets):
            text = self._configure_project(self._stdio_info("atlassian", env))

        self.assertEqual(
            json.loads(text)["mcpServers"]["atlassian"]["env"],
            {
                "JIRA_TOKEN": "${JIRA_TOKEN}",
                "JIRA_URL": "${JIRA_URL}",
                "LEGACY": "${LEGACY_TOKEN}",
                "LOG_LEVEL": "debug",
            },
        )
        for value in secrets.values():
            self.assertNotIn(value, text)

    def test_registry_required_env_becomes_placeholder(self):
        info = {
            "name": "npm-srv",
            "packages": [
                {
                    "registry_name": "npm",
                    "name": "@scope/tool",
                    "environment_variables": [{"name": "API_KEY", "required": True}],
                }
            ],
        }
        with patch.dict(os.environ, {"API_KEY": "token-sentinel"}):
            text = self._configure_project(info)

        self.assertEqual(json.loads(text)["mcpServers"]["npm-srv"]["env"]["API_KEY"], "${API_KEY}")
        self.assertNotIn("token-sentinel", text)

    def test_reinstall_over_baked_secret_reports_claude_diagnostics(self):
        self.mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "atlassian": {
                            "type": "stdio",
                            "command": "uvx",
                            "env": {"JIRA_TOKEN": "baked-sentinel"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ):
            os.environ.pop("JIRA_TOKEN", None)
            text = self._configure_project(
                self._stdio_info("atlassian", {"JIRA_TOKEN": "${JIRA_TOKEN}"})
            )
            with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
                ClaudeClientAdapter.emit_install_run_summary()

        self.assertNotIn("baked-sentinel", text)
        joined = "\n".join(call.args[0] for call in mock_warn.call_args_list)
        self.assertIn("previously stored as plaintext in .mcp.json", joined)
        self.assertIn("Claude Code will resolve 1 environment variable", joined)
        self.assertIn("before running 'claude'", joined)
        self.assertIn("JIRA_TOKEN", joined)
        # Claude's diagnostics must not leak into Copilot CLI's summary.
        self.assertEqual(CopilotClientAdapter._security_upgraded_keys, set())
        self.assertEqual(CopilotClientAdapter._unset_env_keys_by_server, {})

    def test_user_scope_keeps_install_time_resolution(self):
        home = self.root / "home"
        home.mkdir()
        with (
            patch.object(Path, "home", return_value=home),
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "", "TOK": "user-secret"}),
        ):
            self._configure(
                ClaudeClientAdapter(user_scope=True), self._stdio_info("srv", {"TOK": "${TOK}"})
            )

        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["srv"]["env"], {"TOK": "user-secret"})

    def test_command_and_url_references_use_claude_syntax(self):
        stdio = {
            "name": "cmd-demo",
            "_raw_stdio": {"command": "${env:DEMO_CMD}", "args": [], "env": {}},
        }
        remote = {
            "name": "url-demo",
            "remotes": [{"url": "https://<DEMO_HOST>/${env:DEMO_PATH}", "transport_type": "http"}],
        }
        self._configure_project(stdio)
        data = json.loads(self._configure_project(remote))["mcpServers"]

        self.assertEqual(data["cmd-demo"]["command"], "${DEMO_CMD}")
        self.assertEqual(data["url-demo"]["url"], "https://${DEMO_HOST}/${DEMO_PATH}")

    def test_defaults_are_kept_in_every_field(self):
        stdio = {
            "name": "defaults",
            "_raw_stdio": {
                "command": "${DEMO_CMD:-uvx}",
                "args": ["--mode=${DEMO_MODE:-safe}"],
                "env": {"LEVEL": "${DEMO_LEVEL:-info}"},
            },
        }
        remote = {
            "name": "remote-defaults",
            "remotes": [
                {
                    "url": "https://${DEMO_HOST:-mcp.example.invalid}/mcp",
                    "transport_type": "http",
                    "headers": [{"name": "X-Region", "value": "${DEMO_REGION:-eu}"}],
                }
            ],
        }
        self._configure_project(stdio)
        data = json.loads(self._configure_project(remote))["mcpServers"]

        self.assertEqual(data["defaults"]["command"], "${DEMO_CMD:-uvx}")
        self.assertEqual(data["defaults"]["args"], ["--mode=${DEMO_MODE:-safe}"])
        self.assertEqual(data["defaults"]["env"], {"LEVEL": "${DEMO_LEVEL:-info}"})
        self.assertEqual(
            data["remote-defaults"]["url"], "https://${DEMO_HOST:-mcp.example.invalid}/mcp"
        )
        self.assertEqual(data["remote-defaults"]["headers"], {"X-Region": "${DEMO_REGION:-eu}"})

    def test_missing_variable_summary_covers_every_field_but_skips_defaults(self):
        info = {
            "name": "missing",
            "_raw_stdio": {
                "command": "${MISSING_CMD}",
                "args": ["--token=${MISSING_ARG}", "--mode=${DEFAULTED_ARG:-safe}"],
                "env": {"TOKEN": "${MISSING_ENV}"},
            },
        }
        with patch.dict(os.environ):
            for name in ("MISSING_CMD", "MISSING_ARG", "DEFAULTED_ARG", "MISSING_ENV"):
                os.environ.pop(name, None)
            self._configure_project(info)
            with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
                ClaudeClientAdapter.emit_install_run_summary()

        joined = "\n".join(call.args[0] for call in mock_warn.call_args_list)
        self.assertIn("MISSING_ARG, MISSING_CMD, MISSING_ENV", joined)
        self.assertNotIn("DEFAULTED_ARG", joined)

    def test_upgrade_notice_names_values_replaced_in_args_and_headers(self):
        self.mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "type": "http",
                            "url": "https://mcp.example.invalid/mcp",
                            "headers": {"Authorization": "Bearer baked-sentinel"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        remote = {
            "name": "remote",
            "remotes": [
                {
                    "url": "https://mcp.example.invalid/mcp",
                    "transport_type": "http",
                    "headers": [{"name": "Authorization", "value": "Bearer ${REMOTE_TOKEN}"}],
                }
            ],
        }
        with patch.dict(os.environ, {"REMOTE_TOKEN": "baked-sentinel"}):
            text = self._configure_project(remote)
            with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
                ClaudeClientAdapter.emit_install_run_summary()

        self.assertNotIn("baked-sentinel", text)
        joined = "\n".join(call.args[0] for call in mock_warn.call_args_list)
        self.assertIn("Affected: REMOTE_TOKEN", joined)
        self.assertIn("rotate these credentials", joined)
        self.assertNotIn("baked-sentinel", joined)


_LEGACY_EXPECTED = {
    "type": "stdio",
    "command": "uvx",
    "args": ["--token=${OLD_TOKEN}"],
    "env": {"TOKEN": "${OLD_TOKEN}", "MODE": "${OLD_MODE:-safe}", "STATIC": "debug"},
}


class TestClaudeLegacyEntries(unittest.TestCase):
    """Entries written before #3252 kept expanded values in ``.mcp.json``."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".claude").mkdir()
        self.adapter = ClaudeClientAdapter(project_root=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _native(self, value):
        return json.loads(json.dumps(_LEGACY_EXPECTED).replace("${OLD_TOKEN}", value))

    def test_expansion_from_current_environment_is_recognized(self):
        with patch.dict(os.environ, {"OLD_TOKEN": "old-sentinel"}):
            self.assertTrue(
                self.adapter.matches_legacy_managed_config(
                    self._native("old-sentinel"), _LEGACY_EXPECTED
                )
            )

    def test_other_values_are_not_recognized(self):
        with patch.dict(os.environ, {"OLD_TOKEN": "rotated"}):
            self.assertFalse(
                self.adapter.matches_legacy_managed_config(
                    self._native("old-sentinel"), _LEGACY_EXPECTED
                )
            )

    def test_unresolved_reference_in_old_spelling_is_recognized(self):
        with patch.dict(os.environ):
            os.environ.pop("OLD_TOKEN", None)
            native = self._native("${env:OLD_TOKEN}")
            self.assertTrue(self.adapter.matches_legacy_managed_config(native, _LEGACY_EXPECTED))

    def test_unresolved_registry_variable_written_empty_is_recognized(self):
        expected = {"type": "stdio", "command": "npx", "args": [], "env": {"API_KEY": "${API_KEY}"}}
        native = {"type": "stdio", "command": "npx", "args": [], "env": {"API_KEY": ""}}
        with patch.dict(os.environ):
            os.environ.pop("API_KEY", None)
            self.assertTrue(self.adapter.matches_legacy_managed_config(native, expected))

    def test_extra_or_missing_keys_are_not_recognized(self):
        native = self._native("old-sentinel")
        native["env"]["EXTRA"] = "user-added"
        with patch.dict(os.environ, {"OLD_TOKEN": "old-sentinel"}):
            self.assertFalse(self.adapter.matches_legacy_managed_config(native, _LEGACY_EXPECTED))

    def test_conflict_guidance_names_fields_without_values(self):
        native = self._native("someone-elses-value")
        self.adapter.update_config({"legacy": native})
        guidance = self.adapter.native_conflict_guidance("legacy", _LEGACY_EXPECTED)

        self.assertIn("Fields args[0], env.TOKEN hold values", guidance)
        self.assertIn("rotate those credentials", guidance)
        self.assertNotIn("someone-elses-value", guidance)

    def test_conflict_guidance_is_silent_without_expanded_fields(self):
        self.adapter.update_config({"legacy": dict(_LEGACY_EXPECTED, command="other")})
        self.assertIsNone(self.adapter.native_conflict_guidance("legacy", _LEGACY_EXPECTED))


class TestClaudeClientAdapterUser(unittest.TestCase):
    """User scope: ``~/.claude.json`` top-level ``mcpServers``."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.claude_json = self.home / ".claude.json"
        self.adapter = ClaudeClientAdapter(user_scope=True)
        self._home = patch.object(Path, "home", return_value=self.home)
        self._config_dir = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""})
        self._home.start()
        self._config_dir.start()

    def tearDown(self):
        self._config_dir.stop()
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

    def test_get_config_path_honors_claude_config_dir(self):
        config_dir = self.home / "custom-claude"
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}):
            self.assertEqual(
                Path(self.adapter.get_config_path()),
                config_dir.resolve() / ".claude.json",
            )

    def test_get_config_path_rejects_relative_claude_config_dir(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "../custom-claude"}):
            with self.assertRaises(PathTraversalError):
                self.adapter.get_config_path()

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


class TestClaudeTransportChange(unittest.TestCase):
    """Redeclaring one server under another transport rewrites its shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".claude").mkdir()
        self.adapter = ClaudeClientAdapter(project_root=self.root, user_scope=False)
        self.mcp_path = self.root / ".mcp.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _entry(self, name="srv"):
        return json.loads(self.mcp_path.read_text(encoding="utf-8"))["mcpServers"][name]

    def test_remote_to_stdio_drops_url_and_headers(self):
        """A remote entry redeclared as stdio keeps no URL or credential.

        A surviving ``url`` also re-classifies the entry as remote, so the
        stdio normalisation would never run and Copilot's ``type: "local"``
        would reach disk.
        """
        self.adapter.update_config(
            {
                "srv": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer secret"},
                }
            }
        )
        self.adapter.update_config(
            {"srv": {"type": "local", "command": "npx", "args": ["-y", "srv-mcp"]}}
        )
        srv = self._entry()
        self.assertEqual(srv["type"], "stdio")
        self.assertEqual(srv["command"], "npx")
        self.assertNotIn("url", srv)
        self.assertNotIn("headers", srv)

    def test_stdio_to_remote_drops_command_args_env_cwd(self):
        self.adapter.update_config(
            {
                "srv": {
                    "type": "local",
                    "command": "npx",
                    "args": ["-y", "srv-mcp"],
                    "env": {"TOKEN": "secret"},
                    "cwd": "/tmp/srv",
                }
            }
        )
        self.adapter.update_config({"srv": {"type": "http", "url": "https://example.com/mcp"}})
        srv = self._entry()
        self.assertEqual(srv["type"], "http")
        self.assertEqual(srv["url"], "https://example.com/mcp")
        for key in ("command", "args", "env", "cwd"):
            self.assertNotIn(key, srv)

    def test_transport_change_preserves_unmanaged_keys(self):
        """Hand-authored keys describe no transport, so they survive."""
        self.mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "type": "http",
                            "url": "https://example.com/mcp",
                            "oauthAccount": {"accountUuid": "abc"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.adapter.update_config({"srv": {"type": "local", "command": "npx"}})
        srv = self._entry()
        self.assertEqual(srv["oauthAccount"], {"accountUuid": "abc"})
        self.assertNotIn("url", srv)

    def test_mixed_entry_from_earlier_release_is_repaired(self):
        """An entry already carrying both transports is cleaned, not matched.

        Releases that merged unconditionally left entries describing both
        transports at once. Such an entry classifies as remote on its own
        ``url``, so a comparison against the update would call the transport
        unchanged and keep the stdio keys forever.
        """
        self.mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "type": "http",
                            "url": "https://example.com/mcp",
                            "command": "npx",
                            "args": ["-y", "srv-mcp"],
                            "env": {"TOKEN": "leftover"},
                            "cwd": "/tmp/srv",
                            "oauthAccount": {"accountUuid": "abc"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.adapter.update_config({"srv": {"type": "http", "url": "https://example.com/mcp"}})
        srv = self._entry()
        self.assertEqual(srv["type"], "http")
        self.assertEqual(srv["oauthAccount"], {"accountUuid": "abc"})
        for key in ("command", "args", "env", "cwd"):
            self.assertNotIn(key, srv)

    def test_mixed_entry_repaired_towards_stdio(self):
        """The same repair applies when the update declares stdio."""
        self.mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "type": "local",
                            "url": "https://example.com/mcp",
                            "headers": {"Authorization": "Bearer leftover"},
                            "command": "npx",
                            "args": ["-y", "srv-mcp"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.adapter.update_config({"srv": {"type": "local", "command": "npx"}})
        srv = self._entry()
        self.assertEqual(srv["type"], "stdio")
        self.assertNotIn("url", srv)
        self.assertNotIn("headers", srv)

    def test_unchanged_transport_still_shallow_merges(self):
        """Without a transport change the entry keeps keys the update omits."""
        self.mcp_path.write_text(
            json.dumps(
                {"mcpServers": {"srv": {"type": "stdio", "command": "old", "cwd": "/tmp/srv"}}}
            ),
            encoding="utf-8",
        )
        self.adapter.update_config({"srv": {"type": "local", "command": "new"}})
        srv = self._entry()
        self.assertEqual(srv["command"], "new")
        self.assertEqual(srv["cwd"], "/tmp/srv")


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize(
    ("previous", "update", "expected"),
    [
        (
            {"type": "http", "url": "https://example.invalid/mcp", "headers": {"X-Key": "old"}},
            {"headers": {"X-Key": "new"}},
            {"type": "http", "url": "https://example.invalid/mcp", "headers": {"X-Key": "new"}},
        ),
        (
            {"type": "sse", "url": "https://example.invalid/old"},
            {"url": "https://example.invalid/new"},
            {"type": "sse", "url": "https://example.invalid/new"},
        ),
        (
            {"type": "stdio", "command": "python", "args": ["old"], "cwd": "/fixture"},
            {"args": ["new"]},
            {"type": "stdio", "command": "python", "args": ["new"], "cwd": "/fixture"},
        ),
    ],
    ids=["http-headers", "sse-url", "stdio-args"],
)
def test_partial_updates_preserve_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    user_scope: bool,
    previous: dict,
    update: dict,
    expected: dict,
) -> None:
    """Partial and repeated writes preserve compatible fields and their order."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    adapter = ClaudeClientAdapter(project_root=tmp_path, user_scope=user_scope)
    assert adapter.update_config({"srv": previous}) is True
    config_path = Path(adapter.get_config_path())
    initial_bytes = config_path.read_bytes()
    assert adapter.update_config({"srv": previous}) is True
    assert config_path.read_bytes() == initial_bytes
    assert adapter.update_config({"srv": update}) is True
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert document["mcpServers"]["srv"] == expected
    assert tuple(document["mcpServers"]["srv"]) == tuple(expected)


class TestMCPIntegratorClaudeStaleCleanup(unittest.TestCase):
    """``MCPIntegrator.remove_stale`` for Claude project / user files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stored_configs = {
            "stale": {"name": "stale", "registry": False, "transport": "stdio", "command": "s"}
        }
        self.managed_entry = {"type": "stdio", "command": "s", "args": []}

    def tearDown(self):
        self.tmp.cleanup()

    def test_remove_stale_claude_project_mcp_json(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        (self.root / ".claude").mkdir()
        mcp = self.root / ".mcp.json"
        mcp.write_text(
            json.dumps({"mcpServers": {"keep": {"command": "k"}, "stale": self.managed_entry}}),
            encoding="utf-8",
        )
        MCPIntegrator.remove_stale(
            {"stale"},
            runtime="claude",
            project_root=self.root,
            scope=InstallScope.PROJECT,
            stored_configs=self.stored_configs,
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

        with (
            patch.object(Path, "home", return_value=self.root),
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}),
        ):
            cfg = self.root / ".claude.json"
            cfg.write_text(
                json.dumps({"mcpServers": {"keep": {"command": "k"}, "stale": self.managed_entry}}),
                encoding="utf-8",
            )
            MCPIntegrator.remove_stale(
                {"stale"},
                runtime="claude",
                scope=InstallScope.USER,
                stored_configs=self.stored_configs,
            )
            data = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertIn("keep", data["mcpServers"])
            self.assertNotIn("stale", data["mcpServers"])

    def test_remove_stale_claude_user_honors_claude_config_dir(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        custom_claude_dir = self.root / "custom-claude"
        custom_claude_dir.mkdir()
        custom_cfg = custom_claude_dir / ".claude.json"
        custom_cfg.write_text(
            json.dumps({"mcpServers": {"keep": {"command": "k"}, "stale": self.managed_entry}}),
            encoding="utf-8",
        )

        with patch.object(Path, "home", return_value=self.root):
            default_cfg = self.root / ".claude.json"
            default_cfg.write_text(
                json.dumps({"mcpServers": {"default_srv": {"command": "d"}}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(custom_claude_dir)}):
                MCPIntegrator.remove_stale(
                    {"stale"},
                    runtime="claude",
                    scope=InstallScope.USER,
                    stored_configs=self.stored_configs,
                )

            custom_data = json.loads(custom_cfg.read_text(encoding="utf-8"))
            self.assertIn("keep", custom_data["mcpServers"])
            self.assertNotIn("stale", custom_data["mcpServers"])

            default_data = json.loads(default_cfg.read_text(encoding="utf-8"))
            self.assertIn("default_srv", default_data["mcpServers"])

    def test_remove_stale_claude_user_preserves_unrelated_settings(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        custom_claude_dir = self.root / "custom-claude"
        custom_claude_dir.mkdir()
        custom_cfg = custom_claude_dir / ".claude.json"
        custom_cfg.write_text(
            json.dumps(
                {
                    "projects": {"/path/to/project": {"allowedTools": ["Bash"]}},
                    "mcpServers": {
                        "keep": {"command": "k"},
                        "stale": self.managed_entry,
                    },
                    "theme": "dark",
                }
            ),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(custom_claude_dir)}):
            MCPIntegrator.remove_stale(
                {"stale"},
                runtime="claude",
                scope=InstallScope.USER,
                stored_configs=self.stored_configs,
            )

        data = json.loads(custom_cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["projects"], {"/path/to/project": {"allowedTools": ["Bash"]}})
        self.assertEqual(data["theme"], "dark")
        self.assertIn("keep", data["mcpServers"])
        self.assertNotIn("stale", data["mcpServers"])

    def test_remove_stale_claude_user_rejects_relative_claude_config_dir(self):
        from apm_cli.install.errors import RequiredIntegrationError
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "../relative-claude"}):
            with self.assertRaises(RequiredIntegrationError):
                MCPIntegrator.remove_stale(
                    {"stale"},
                    runtime="claude",
                    scope=InstallScope.USER,
                    fail_on_write_error=True,
                )

    def test_remove_stale_claude_user_rejects_symlink_config(self):
        if os.name == "nt":
            self.skipTest("Symlinks require elevated privileges on Windows")
        from apm_cli.install.errors import RequiredIntegrationError
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        custom_claude_dir = self.root / "custom-claude"
        custom_claude_dir.mkdir()
        target = self.root / "target.json"
        target_bytes = b'{"mcpServers": {"stale": {"command": "s"}}}'
        target.write_bytes(target_bytes)
        symlink_cfg = custom_claude_dir / ".claude.json"
        symlink_cfg.symlink_to(target)

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(custom_claude_dir)}):
            with self.assertRaises(RequiredIntegrationError) as ctx:
                MCPIntegrator.remove_stale(
                    {"stale"},
                    runtime="claude",
                    scope=InstallScope.USER,
                    fail_on_write_error=True,
                )
            self.assertIn("symlinked MCP config", str(ctx.exception))

        self.assertTrue(symlink_cfg.is_symlink())
        self.assertEqual(target.read_bytes(), target_bytes)

    def test_remove_stale_claude_user_rejects_symlinked_ancestor(self):
        if os.name == "nt":
            self.skipTest("Symlinks require elevated privileges on Windows")
        from apm_cli.install.errors import RequiredIntegrationError
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        real_dir = self.root / "real-claude"
        real_dir.mkdir()
        real_cfg = real_dir / ".claude.json"
        target_bytes = b'{"mcpServers": {"stale": {"command": "s"}}}'
        real_cfg.write_bytes(target_bytes)

        symlink_dir = self.root / "symlink-claude"
        symlink_dir.symlink_to(real_dir, target_is_directory=True)

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(symlink_dir)}):
            with self.assertRaises(RequiredIntegrationError) as ctx:
                MCPIntegrator.remove_stale(
                    {"stale"},
                    runtime="claude",
                    scope=InstallScope.USER,
                    fail_on_write_error=True,
                )
            self.assertIn("symlinked MCP config", str(ctx.exception))

        self.assertTrue(symlink_dir.is_symlink())
        self.assertEqual(real_cfg.read_bytes(), target_bytes)

    def test_remove_stale_scope_none_defaults_safely(self):
        """When scope is unspecified, only project .mcp.json is touched."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        (self.root / ".claude").mkdir()
        proj = self.root / ".mcp.json"
        proj.write_text(json.dumps({"mcpServers": {"stale": self.managed_entry}}), encoding="utf-8")

        with patch.object(Path, "home", return_value=self.root):
            user_cfg = self.root / ".claude.json"
            user_raw = json.dumps({"mcpServers": {"stale": {"command": "u"}}})
            user_cfg.write_text(user_raw, encoding="utf-8")

            MCPIntegrator.remove_stale(
                {"stale"},
                runtime="claude",
                project_root=self.root,
                scope=None,
                stored_configs=self.stored_configs,
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
