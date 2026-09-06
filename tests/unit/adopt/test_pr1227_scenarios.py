"""Scenarios ported from microsoft/apm PR #1227 (the first brownfield prototype).

Kept as regression coverage for the behaviours that PR exercised: multi-tool
preview, manifest creation on --write, machine-readable JSON, and detection of
every harness family from its canonical directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli


def _seed(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


@pytest.mark.component
def test_preview_multi_agent_reports_all_tools(project: Path):
    _seed(
        project,
        {
            ".claude/commands/review.md": "review",
            ".codex/config.toml": "[mcp_servers]\n",
            ".cursor/rules/style.md": "cursor rule",
        },
    )
    result = CliRunner().invoke(cli, ["init", "--discover", "--yes"])
    assert result.exit_code == 0, result.output
    for tool in ("claude", "codex", "cursor"):
        assert tool in result.output
    assert "Re-run with --apply" in result.output
    assert not (project / "apm.yml").exists()


@pytest.mark.component
def test_write_creates_well_formed_apm_yml(project: Path):
    _seed(
        project,
        {
            ".codex/config.toml": "[mcp_servers]\n",
            ".codex/agents/coder.toml": 'name = "coder"\ndescription = "d"\ndeveloper_instructions = "Write code"\n',
        },
    )
    result = CliRunner().invoke(cli, ["init", "--discover", "--write", "--yes"])
    assert result.exit_code == 0, result.output
    config = yaml.safe_load((project / "apm.yml").read_text(encoding="utf-8"))
    assert {"name", "version", "targets", "dependencies"} <= set(config)
    assert config["targets"] == ["codex"]
    assert isinstance(config["dependencies"], dict)
    assert (project / ".apm/agents/coder.agent.md").is_file()


@pytest.mark.component
def test_json_output_is_machine_parseable(project: Path):
    _seed(project, {".windsurf/rules/python.md": "windsurf rule"})
    result = CliRunner().invoke(cli, ["init", "--discover", "--yes", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    findings = [f for f in payload["findings"] if f["kind"] != "unknown"]
    assert len(findings) == 1
    assert findings[0]["tool"] == "windsurf"
    assert findings[0]["importability"] == "convertible"
    assert not (project / "apm.yml").exists()


@pytest.mark.component
def test_all_agents_found_in_one_project(project: Path):
    _seed(
        project,
        {
            ".claude/commands/fix.md": "claude fix",
            ".codex/agents/coder.md": "codex agent",
            ".cursor/rules/style.md": "cursor rule",
            ".opencode/agents/reviewer.md": "opencode agent",
            ".windsurf/rules/style.md": "windsurf rule",
            ".gemini/commands/review.md": "gemini command",
            ".github/copilot-instructions.md": "copilot instructions",
        },
    )
    result = CliRunner().invoke(cli, ["init", "--discover", "--yes", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert {"claude", "codex", "cursor", "opencode", "windsurf", "gemini", "copilot"} <= set(
        payload["detected_tools"]
    )
    found_tools = {f["tool"] for f in payload["findings"]}
    assert {"claude", "codex", "cursor", "opencode", "windsurf", "gemini", "copilot"} <= found_tools
    # Non-canonical extensions are surfaced rather than silently skipped.
    unknown = {f["path"] for f in payload["findings"] if f["kind"] == "unknown"}
    assert {
        ".codex/agents/coder.md",
        ".cursor/rules/style.md",
        ".gemini/commands/review.md",
    } <= unknown
