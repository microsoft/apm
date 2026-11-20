"""Tests for the ``apm plugin init`` surface and the refusal of the
``apm init --plugin`` / ``apm init --marketplace`` flags removed in 0.37
(#4073).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = pytest.mark.component


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
                # Both advertised formats pack the fresh scaffold as is (#3994).
                for fmt in ("agent-plugin", "claude-plugin"):
                    packed = self.runner.invoke(cli, ["pack", "--format", fmt, "-o", f"out-{fmt}"])
                    assert packed.exit_code == 0, packed.output
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
                assert "license" not in pj
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


class TestRemovedInitFlags:
    """``apm init --plugin`` / ``--marketplace`` were removed in 0.37 (#4073).

    They stay as hidden stubs that write nothing, exit 2 and name the
    replacement command.
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

    @pytest.mark.parametrize(
        ("flag", "replacement"),
        [("--plugin", "apm plugin init"), ("--marketplace", "apm marketplace init")],
    )
    def test_removed_flag_exits_2_names_replacement_and_writes_nothing(
        self, flag: str, replacement: str
    ):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(cli, ["init", "demo", flag, "--yes"])
                assert result.exit_code == 2, result.output
                assert f"Run: {replacement}" in result.stderr
                assert "removed" in result.stderr
                assert list(Path(tmp).iterdir()) == []
            finally:
                os.chdir(self.original_dir)

    def test_removed_flags_are_hidden_from_help(self):
        result = self.runner.invoke(cli, ["init", "--help"])
        assert result.exit_code == 0, result.output
        assert "--plugin" not in result.output
        assert "--marketplace" not in result.output

    def test_plugin_init_writes_metadata_flags(self):
        """``apm plugin init`` covers what ``apm init --plugin`` offered (#4073)."""
        import json

        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(
                    cli,
                    [
                        "plugin",
                        "init",
                        "demo",
                        "--yes",
                        "--version",
                        "2.3.4",
                        "--author",
                        "Ada",
                        "--description",
                        "Says hi",
                    ],
                )
                assert result.exit_code == 0, result.output
                manifest = yaml.safe_load(Path("apm.yml").read_text(encoding="utf-8"))
                assert manifest["version"] == "2.3.4"
                assert manifest["author"] == "Ada"
                assert manifest["description"] == "Says hi"
                plugin = json.loads(Path("plugin.json").read_text(encoding="utf-8"))
                assert plugin["version"] == "2.3.4"
                assert plugin["description"] == "Says hi"
                assert "Ada" in json.dumps(plugin["author"])
            finally:
                os.chdir(self.original_dir)

    def test_removed_plugin_flag_prints_the_full_replacement_command(self):
        """The refusal keeps the user's arguments so recovery is one paste."""
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(
                    cli,
                    [
                        "init",
                        "my-skill",
                        "--plugin",
                        "--yes",
                        "--target",
                        "claude",
                        "--version",
                        "2.0.0",
                        "--author",
                        "Ada L",
                    ],
                )
                assert result.exit_code == 2, result.output
                assert (
                    "Run: apm plugin init my-skill --yes --target claude "
                    "--version 2.0.0 --author 'Ada L'"
                ) in result.stderr
                assert list(Path(tmp).iterdir()) == []
            finally:
                os.chdir(self.original_dir)

    def test_both_removed_flags_print_both_replacement_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(
                    cli, ["init", "demo", "--plugin", "--marketplace", "--yes"]
                )
                assert result.exit_code == 2, result.output
                assert "'apm init --plugin' and 'apm init --marketplace'" in result.stderr
                assert "Run: apm plugin init demo --yes" in result.stderr
                assert "Then run (inside demo/): apm marketplace init" in result.stderr
                assert list(Path(tmp).iterdir()) == []
            finally:
                os.chdir(self.original_dir)

    @pytest.mark.parametrize(
        ("flags", "expected"),
        [
            (
                ["--version", "2.3.4", "--author", "Ada", "--description", "Says hi"],
                ("2.3.4", "Ada", "Says hi"),
            ),
            (["--author", "Ada", "--description", "Says hi"], ("1.0.0", "Ada", "Says hi")),
        ],
    )
    def test_plugin_init_metadata_flags_seed_interactive_prompts(self, flags, expected):
        """Outside --yes the flags seed the prompts; the default version stays 1.0.0."""
        import json

        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                # Accept the name, version, description and author defaults, then confirm.
                result = self.runner.invoke(
                    cli, ["plugin", "init", "demo", *flags], input="\n\n\n\ny\n"
                )
                assert result.exit_code == 0, result.output
                project = Path(tmp) / "demo"
                manifest = yaml.safe_load((project / "apm.yml").read_text(encoding="utf-8"))
                assert (manifest["version"], manifest["author"], manifest["description"]) == (
                    expected
                )
                plugin = json.loads((project / "plugin.json").read_text(encoding="utf-8"))
                assert (plugin["version"], plugin["author"]["name"], plugin["description"]) == (
                    expected
                )
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


def test_claude_only_plugin_scaffold_advertises_only_working_formats(tmp_path, monkeypatch):
    """Every pack command a Claude-only scaffold advertises succeeds (#3994)."""
    project = tmp_path / "claude-plugin-demo"
    project.mkdir()
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["plugin", "init", "--yes", "--target", "claude"])
    assert result.exit_code == 0, result.output
    assert "apm pack --format agent-plugin" not in result.output
    assert "apm pack --format claude-plugin" in result.output
    packed = runner.invoke(cli, ["pack", "--format", "claude-plugin"])
    assert packed.exit_code == 0, packed.output


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
                # Experimental 'apm run' is not a next step (#3994).
                assert "apm run" not in result.output
                assert "apm plugin init" in result.output
                assert "apm marketplace init" in result.output
            finally:
                os.chdir(self.original_dir)

    @pytest.mark.parametrize(("marker", "expect_flag"), [(None, True), (".claude", False)])
    def test_install_step_names_a_harness_only_when_none_resolves(
        self, marker, expect_flag, monkeypatch
    ):
        """A markerless project's install hint carries -t, or it exits 2 (#3994)."""
        monkeypatch.setattr("apm_cli.config.get_install_target", lambda **_kw: None)
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                if marker:
                    Path(tmp, marker).mkdir()
                result = self.runner.invoke(cli, ["init", "--yes"])
                assert result.exit_code == 0, result.output
                assert ("apm install <owner>/<repo> -t <harness>" in result.output) is expect_flag
            finally:
                os.chdir(self.original_dir)
