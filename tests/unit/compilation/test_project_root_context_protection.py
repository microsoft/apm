"""Regression tests for hand-authored project root context files."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = pytest.mark.component

_MANUAL_CLAUDE = "# CLAUDE.md\n\nHand-authored project memory.\n"
_MANUAL_AGENTS = "# AGENTS.md\n\nHand-authored agent guidance.\n"


def _seed_project(project: Path) -> None:
    """Create a compilable project with hand-authored root context files."""
    (project / "apm.yml").write_text(
        "name: root-context-protection\n"
        "version: 1.0.0\n"
        "targets: [claude, codex]\n"
        "dependencies:\n"
        "  apm: []\n"
        "  mcp: []\n",
        encoding="utf-8",
    )
    instructions = project / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "standards.instructions.md").write_text(
        "---\ndescription: standards\n---\n# Standards\nUse US English.\n",
        encoding="utf-8",
    )
    (project / "CLAUDE.md").write_text(_MANUAL_CLAUDE, encoding="utf-8")
    (project / "AGENTS.md").write_text(_MANUAL_AGENTS, encoding="utf-8")


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("single_agents", [False, True])
def test_compile_preserves_hand_authored_project_root_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
    single_agents: bool,
) -> None:
    """Distributed and single-file compiles must protect hand-authored roots."""
    _seed_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    args = ["compile"]
    if single_agents:
        args.append("--single-agents")
    if dry_run:
        args.append("--dry-run")

    result = CliRunner().invoke(cli, args, catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == _MANUAL_CLAUDE
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == _MANUAL_AGENTS
    assert result.output.count("hand-authored file will not be overwritten") >= 2
    assert "Skipped CLAUDE.md" in result.output
    assert "Skipped AGENTS.md" in result.output


def test_compile_root_preserves_hand_authored_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirected compile root must receive the same overwrite protection."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_project(source)
    (destination / "CLAUDE.md").write_text(_MANUAL_CLAUDE, encoding="utf-8")
    monkeypatch.chdir(source)

    result = CliRunner().invoke(
        cli,
        ["compile", "--target", "claude", "--root", str(destination)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert (destination / "CLAUDE.md").read_text(encoding="utf-8") == _MANUAL_CLAUDE
    assert "Skipped CLAUDE.md" in result.output
