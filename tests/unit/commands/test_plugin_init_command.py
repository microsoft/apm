"""Tests for the new ``apm plugin init`` surface and deprecation
warnings on ``apm init --plugin`` / ``apm init --marketplace``.

Wave 3 v3: noun-verb consolidation. Legacy flags continue to work
during the deprecation window (removal scheduled for v0.16).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from click.testing import CliRunner

from apm_cli.cli import cli


class TestPluginInitCommand:
    """``apm plugin init`` behaves like ``apm init --plugin``."""

    def setup_method(self) -> None:
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).resolve().parents[3])
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            os.chdir(str(Path(__file__).resolve().parents[3]))

    def test_plugin_init_creates_plugin_json_and_apm_yml(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(cli, ["plugin", "init", "demo", "--yes"])
                assert result.exit_code == 0, result.output
                # `apm init <name>` chdirs into the new project dir.
                assert Path("apm.yml").exists()
                assert Path("plugin.json").exists()
                # Plugin-author next-steps surface
                assert "apm install --dev" in result.output
                assert "apm pack --format agent-plugin" in result.output
                assert "apm pack --format claude-plugin" in result.output
                assert "apm pack --plugin" not in result.output
                # Consumer-only hints absent in plugin mode
                assert "apm marketplace init" not in result.output
            finally:
                os.chdir(self.original_dir)

    def test_plugin_init_current_directory(self):
        # tmp dir basenames contain underscores which fail kebab validation,
        # so create a kebab-safe child dir to exercise the current-dir path.
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "demo-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                result = self.runner.invoke(cli, ["plugin", "init", "--yes"])
                assert result.exit_code == 0, result.output
                assert Path("apm.yml").exists()
                assert Path("plugin.json").exists()
            finally:
                os.chdir(self.original_dir)

    def test_plugin_init_help_advertises_apm_marketplace_init(self):
        """Group help points users at the sibling marketplace verb."""
        result = self.runner.invoke(cli, ["plugin", "--help"])
        assert result.exit_code == 0
        assert "init" in result.output
        assert "plugin" in result.output.lower()

    def test_plugin_init_defaults_to_legacy_claude_scaffold(self):
        """No selector preserves the shipped legacy Claude scaffold."""
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(cli, ["plugin", "init", "demo", "--yes"])
                assert result.exit_code == 0, result.output
                assert Path("plugin.json").exists()
                import json

                pj = json.loads(Path("plugin.json").read_text(encoding="utf-8"))
                assert "$schema" not in pj
                assert "extensions" not in pj
                assert not Path("mcp.json").exists()
            finally:
                os.chdir(self.original_dir)

    def test_plugin_init_explicit_agent_plugin_format_creates_complete_native_scaffold(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(
                    cli,
                    ["plugin", "init", "demo", "--format", "agent-plugin", "--yes"],
                )
                assert result.exit_code == 0, result.output
                import json

                pj = json.loads(Path("plugin.json").read_text(encoding="utf-8"))
                assert "extensions" in pj and "com.microsoft.apm" in pj["extensions"]
                assert pj["extensions"]["com.microsoft.apm"]["schemaVersion"] == "1"
                assert Path("mcp.json").is_file()
                assert "Created Files" in result.output
                assert "apm.yml" in result.output
                assert "plugin.json" in result.output
                assert "mcp.json" in result.output
                from apm_cli.agent_plugins import load_agent_plugin

                loaded = load_agent_plugin(Path.cwd())
                assert loaded.identity.name == "demo"
                assert loaded.identity.version == "0.1.0"
                assert loaded.components.mcp_servers == ()
            finally:
                os.chdir(self.original_dir)

    def test_plugin_init_claude_mode_preserves_legacy_scaffold(self):
        """`--claude-plugin` must produce the legacy Claude-compatible plugin.json."""
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(
                    cli, ["plugin", "init", "demo", "--claude-plugin", "--yes"]
                )
                assert result.exit_code == 0, result.output
                import json

                pj = json.loads(Path("plugin.json").read_text(encoding="utf-8"))
                # Legacy Claude scaffold does not include Agent Plugins $schema or extensions
                assert "$schema" not in pj
                assert "extensions" not in pj
            finally:
                os.chdir(self.original_dir)

    def test_plugin_init_plugin_format_alias_preserves_legacy_scaffold(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(
                    cli,
                    ["plugin", "init", "demo", "--format", "plugin", "--yes"],
                )
                assert result.exit_code == 0, result.output
                import json

                pj = json.loads(Path("plugin.json").read_text(encoding="utf-8"))
                assert "$schema" not in pj
                assert not Path("mcp.json").exists()
            finally:
                os.chdir(self.original_dir)

    def test_plugin_init_conflicting_selectors_is_usage_error(self):
        """Multiple plugin format selectors produce a clear usage error."""
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(
                    cli,
                    [
                        "plugin",
                        "init",
                        "demo",
                        "--format",
                        "agent-plugin",
                        "--claude-plugin",
                        "--yes",
                    ],
                )
                assert result.exit_code == 2
                assert "choose one bundle format selector" in result.output.lower()
            finally:
                os.chdir(self.original_dir)

    def test_plugin_init_redundant_claude_selectors_is_usage_error(self):
        result = self.runner.invoke(
            cli,
            [
                "plugin",
                "init",
                "demo",
                "--format",
                "plugin",
                "--claude-plugin",
                "--yes",
            ],
        )
        assert result.exit_code == 2
        assert "Choose one bundle format selector" in result.output

    def test_plugin_init_removed_plugin_shortcut_is_rejected(self):
        result = self.runner.invoke(cli, ["plugin", "init", "demo", "--plugin", "--yes"])
        assert result.exit_code == 2
        assert "No such option: --plugin" in result.output

    def test_plugin_init_native_loader_failure_prevents_success(self, monkeypatch):
        """Explicit native scaffold cannot report success when canonical reload fails."""

        def _reject(_root):
            raise ValueError("canonical scaffold rejection")

        monkeypatch.setattr("apm_cli.agent_plugins.load_agent_plugin", _reject)
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(
                    cli,
                    ["plugin", "init", "demo", "--format", "agent-plugin", "--yes"],
                )
                assert result.exit_code == 1
                assert "canonical scaffold rejection" in result.output
                assert "initialized successfully" not in result.output
                assert not Path("apm.yml").exists()
                assert not Path("plugin.json").exists()
                assert not Path("mcp.json").exists()
            finally:
                os.chdir(self.original_dir)

    def test_plugin_init_native_loader_failure_preserves_existing_generated_files(
        self, monkeypatch
    ):
        """Brownfield --yes leaves every generated file byte-identical on rejection."""

        def _reject(_root):
            raise ValueError("canonical scaffold rejection")

        monkeypatch.setattr("apm_cli.agent_plugins.load_agent_plugin", _reject)
        existing = {
            "apm.yml": b"name: existing\n",
            "plugin.json": b'{"name":"existing"}\n',
            "mcp.json": b'{"mcpServers":{"existing":{}}}\n',
        }
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                project = Path("demo")
                project.mkdir()
                for filename, payload in existing.items():
                    (project / filename).write_bytes(payload)
                result = self.runner.invoke(
                    cli,
                    ["plugin", "init", "demo", "--format", "agent-plugin", "--yes"],
                )
                assert result.exit_code == 1
                assert "canonical scaffold rejection" in result.output
                assert (
                    "--yes specified, overwriting: apm.yml, plugin.json, mcp.json" in result.output
                )
                assert {filename: Path(filename).read_bytes() for filename in existing} == existing
            finally:
                os.chdir(self.original_dir)

    def test_plugin_init_native_refuses_existing_outputs_without_confirmation(self):
        """Native scaffold files are not overwritten when confirmation is declined."""
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                project = Path("demo")
                project.mkdir()
                (project / "mcp.json").write_text("sentinel\n", encoding="utf-8")
                result = self.runner.invoke(
                    cli,
                    ["plugin", "init", "demo", "--format", "agent-plugin"],
                    input="n\n",
                )
                assert result.exit_code == 0, result.output
                assert "mcp.json" in result.output
                assert Path("mcp.json").read_text(encoding="utf-8") == "sentinel\n"
                assert not Path("plugin.json").exists()
                assert not Path("apm.yml").exists()
            finally:
                os.chdir(self.original_dir)


class TestInitDeprecationWarnings:
    """Legacy ``apm init --plugin`` / ``--marketplace`` flags still work
    but print a one-line deprecation redirect on stderr.
    """

    def setup_method(self) -> None:
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).resolve().parents[3])
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            os.chdir(str(Path(__file__).resolve().parents[3]))

    def test_init_plugin_flag_prints_deprecation(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(cli, ["init", "demo", "--plugin", "--yes"])
                assert result.exit_code == 0, result.output
                # Deprecation lives on stderr so it does not pollute pipes
                assert "deprecated" in result.stderr.lower()
                assert "apm plugin init" in result.stderr
                assert "v0.16" in result.stderr
                # And the legacy flag STILL works (cwd is now demo/)
                assert Path("plugin.json").exists()
                # The deprecated producer flag keeps shipped legacy behavior.
                import json

                pj = json.loads(Path("plugin.json").read_text(encoding="utf-8"))
                assert "$schema" not in pj
                assert "extensions" not in pj
            finally:
                os.chdir(self.original_dir)

    def test_init_marketplace_flag_prints_deprecation(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(cli, ["init", "demo", "--marketplace", "--yes"])
                assert result.exit_code == 0, result.output
                assert "deprecated" in result.stderr.lower()
                assert "apm marketplace init" in result.stderr
                assert "v0.16" in result.stderr
                # And the legacy flag STILL writes the marketplace block (cwd is now demo/)
                content = Path("apm.yml").read_text()
                assert "marketplace:" in content
            finally:
                os.chdir(self.original_dir)

    def test_init_without_flags_does_not_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(cli, ["init", "demo", "--yes"])
                assert result.exit_code == 0, result.output
                assert "deprecated" not in result.stderr.lower()
            finally:
                os.chdir(self.original_dir)


class TestInitConsumerNextSteps:
    """Consumer-mode ``apm init`` teaches the noun-verb namespace."""

    def setup_method(self) -> None:
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).resolve().parents[3])
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            os.chdir(str(Path(__file__).resolve().parents[3]))

    def test_consumer_init_surfaces_namespace_pointers(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(cli, ["init", "--yes"])
                assert result.exit_code == 0, result.output
                assert "apm install" in result.output
                assert "apm run" in result.output
                assert "apm plugin init" in result.output
                assert "apm marketplace init" in result.output
            finally:
                os.chdir(self.original_dir)
