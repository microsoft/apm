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
    assert result.output.count("hand-authored file will not be overwritten") == 2
    assert "Protected CLAUDE.md" in result.output
    assert "Protected AGENTS.md" in result.output
    assert "produced no output files" not in result.output


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
    assert "Protected CLAUDE.md" in result.output


def test_single_file_managed_section_updates_hand_authored_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed-section mode must update only the explicitly owned block."""
    _seed_project(tmp_path)
    (tmp_path / "AGENTS.md").write_text(
        "# Team guidance\n\n"
        "Human-authored content.\n\n"
        "<!-- apm:start -->\nOld APM block.\n<!-- apm:end -->\n",
        encoding="utf-8",
    )
    with (tmp_path / "apm.yml").open("a", encoding="utf-8") as handle:
        handle.write(
            "compilation:\n"
            "  agents_md:\n"
            "    mode: managed_section\n"
            '    start_marker: "<!-- apm:start -->"\n'
            '    end_marker: "<!-- apm:end -->"\n'
        )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["compile", "--single-agents"], catch_exceptions=False)

    written = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert result.exit_code == 0, result.output
    assert "Human-authored content." in written
    assert "Use US English." in written
    assert "Old APM block." not in written


@pytest.mark.parametrize("first_single", [False, True])
def test_compile_switches_between_generated_agents_formats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_single: bool,
) -> None:
    """Both APM markers must remain writable when compile strategy changes."""
    _seed_project(tmp_path)
    (tmp_path / "AGENTS.md").unlink()
    monkeypatch.chdir(tmp_path)
    first_args = ["compile", "--single-agents"] if first_single else ["compile"]
    second_args = ["compile"] if first_single else ["compile", "--single-agents"]

    first = CliRunner().invoke(cli, first_args, catch_exceptions=False)
    second = CliRunner().invoke(cli, second_args, catch_exceptions=False)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "Protected AGENTS.md" not in second.output
    expected_marker = (
        "<!-- Generated by APM CLI from distributed .apm/ primitives -->"
        if first_single
        else "<!-- Generated by APM CLI from .apm/ primitives -->"
    )
    assert expected_marker in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


def test_custom_single_file_output_remains_replaceable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root protection must not change the explicit custom-output contract."""
    _seed_project(tmp_path)
    custom_output = tmp_path / "notes.md"
    custom_output.write_text("old custom output\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["compile", "--single-agents", "--output", "notes.md"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert custom_output.read_text(encoding="utf-8") != "old custom output\n"
    assert "Protected notes.md" not in result.output


@pytest.mark.parametrize("absolute", [False, True])
def test_aliased_output_path_preserves_hand_authored_root_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    absolute: bool,
) -> None:
    """Path aliases must not bypass root context protection."""
    _seed_project(tmp_path)
    (tmp_path / "alias").mkdir()
    monkeypatch.chdir(tmp_path)
    output = str(tmp_path / "AGENTS.md") if absolute else "alias/../AGENTS.md"

    result = CliRunner().invoke(
        cli,
        ["compile", "--single-agents", "--output", output],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == _MANUAL_AGENTS
    assert "Protected" in result.output
