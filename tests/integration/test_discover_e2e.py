"""
End-to-end sanity tests for apm init --discover (brownfield context discovery).

Uses the Click test runner against real temporary directories so the feature
can be verified locally without touching user config or network.
Run: python -m pytest tests/integration/test_discover_e2e.py -v
"""

import json
import os
import tempfile
from pathlib import Path

import yaml
from click.testing import CliRunner

from apm_cli.cli import cli


def _isolate(monkeypatch, tmp_dir: Path) -> None:
    """Point HOME and XDG_CONFIG_DIRS at empty dirs inside tmp_dir."""
    fake_home = tmp_dir / "home"
    fake_sys = tmp_dir / "sys"
    fake_home.mkdir()
    fake_sys.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(fake_sys))
    monkeypatch.setenv("APM_E2E_TESTS", "1")


class TestDiscoverSanityE2E:
    """Sanity E2E tests for apm init --discover in real tmp directories."""

    def setup_method(self):
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self):
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            os.chdir(str(Path(__file__).parent.parent.parent))

    def test_preview_multi_agent_reports_all_tools(self, monkeypatch):
        """Preview mode reports Claude, Codex, and Cursor files and does not write apm.yml."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project = Path(tmp_dir)
            os.chdir(tmp_dir)
            _isolate(monkeypatch, project)
            try:
                (project / ".claude" / "commands").mkdir(parents=True)
                (project / ".claude" / "commands" / "review.md").write_text(
                    "review", encoding="utf-8"
                )
                (project / ".codex").mkdir()
                (project / ".codex" / "config.toml").write_text("[mcp_servers]\n", encoding="utf-8")
                (project / ".cursor" / "rules").mkdir(parents=True)
                (project / ".cursor" / "rules" / "style.md").write_text(
                    "cursor rule", encoding="utf-8"
                )

                result = self.runner.invoke(cli, ["init", "--discover", "--yes"])

                assert result.exit_code == 0, result.output
                assert "claude" in result.output
                assert "codex" in result.output
                assert "cursor" in result.output
                assert "Preview only" in result.output or "Re-run with --write" in result.output
                assert not (project / "apm.yml").exists()
            finally:
                os.chdir(self.original_dir)

    def test_write_creates_well_formed_apm_yml(self, monkeypatch):
        """--discover --write creates a valid apm.yml with expected top-level keys."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project = Path(tmp_dir)
            os.chdir(tmp_dir)
            _isolate(monkeypatch, project)
            try:
                (project / ".codex").mkdir()
                (project / ".codex" / "config.toml").write_text("[mcp_servers]\n", encoding="utf-8")

                result = self.runner.invoke(cli, ["init", "--discover", "--write", "--yes"])

                assert result.exit_code == 0, result.output
                apm_yml = project / "apm.yml"
                assert apm_yml.exists(), "apm.yml was not created"
                config = yaml.safe_load(apm_yml.read_text(encoding="utf-8"))
                assert "name" in config
                assert "version" in config
                assert "target" in config
                assert "dependencies" in config
                assert isinstance(config["dependencies"], dict)
            finally:
                os.chdir(self.original_dir)

    def test_json_output_is_machine_parseable(self, monkeypatch):
        """--format json output round-trips through json.loads with correct structure."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project = Path(tmp_dir)
            os.chdir(tmp_dir)
            _isolate(monkeypatch, project)
            try:
                (project / ".windsurf" / "rules").mkdir(parents=True)
                (project / ".windsurf" / "rules" / "python.md").write_text(
                    "windsurf rule", encoding="utf-8"
                )

                result = self.runner.invoke(
                    cli, ["init", "--discover", "--yes", "--format", "json"]
                )

                assert result.exit_code == 0, result.output
                payload = json.loads(result.output)
                assert payload["summary"]["total_files"] == 1
                assert payload["files"][0]["tool"] == "windsurf"
                assert not (project / "apm.yml").exists()
            finally:
                os.chdir(self.original_dir)

    def test_all_agents_found_in_one_project(self, monkeypatch):
        """All seven agent families are discovered when their canonical files are present."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            project = Path(tmp_dir)
            os.chdir(tmp_dir)
            _isolate(monkeypatch, project)
            try:
                fixtures = [
                    (".claude/commands/fix.md", "claude fix"),
                    (".codex/agents/coder.md", "codex agent"),
                    (".cursor/rules/style.md", "cursor rule"),
                    (".opencode/agents/reviewer.md", "opencode agent"),
                    (".windsurf/rules/style.md", "windsurf rule"),
                    (".gemini/commands/review.md", "gemini command"),
                    (".github/copilot-instructions.md", "copilot instructions"),
                ]
                for rel, content in fixtures:
                    path = project / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")

                result = self.runner.invoke(
                    cli, ["init", "--discover", "--yes", "--format", "json"]
                )

                assert result.exit_code == 0, result.output
                payload = json.loads(result.output)
                found_tools = {f["tool"] for f in payload["files"]}
                assert {
                    "claude",
                    "codex",
                    "cursor",
                    "opencode",
                    "windsurf",
                    "gemini",
                    "copilot",
                }.issubset(found_tools)
            finally:
                os.chdir(self.original_dir)
