"""Regression test for orphan cleanup after the last primitive is removed."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli


def test_clean_removes_claude_md_after_last_primitive_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--clean reaches orphan removal when the project has no primitives left."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text(
        "name: clean-last-primitive\nversion: 1.0.0\ntargets:\n  - claude\n",
        encoding="utf-8",
    )
    instructions_dir = tmp_path / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    instruction = instructions_dir / "base.instructions.md"
    instruction.write_text(
        "---\ndescription: Test instruction\n---\nKeep responses concise.\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    initial = runner.invoke(cli, ["compile", "--target", "claude"])
    assert initial.exit_code == 0, initial.output
    claude_md = tmp_path / "CLAUDE.md"
    assert claude_md.is_file()

    instruction.unlink()
    cleaned = runner.invoke(cli, ["compile", "--target", "claude", "--clean"])

    assert cleaned.exit_code == 0, cleaned.output
    assert not claude_md.exists()
    assert "no source primitives remain" in cleaned.output
    assert "produced no output files" not in cleaned.output


def test_clean_validate_still_rejects_project_without_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--validate keeps requiring project content when combined with --clean."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text(
        "name: clean-validate-empty\nversion: 1.0.0\ntargets:\n  - claude\n",
        encoding="utf-8",
    )
    (tmp_path / ".apm" / "instructions").mkdir(parents=True)

    result = CliRunner().invoke(cli, ["compile", "--target", "claude", "--clean", "--validate"])

    assert result.exit_code == 1
    assert "No instruction files found in .apm/ directory" in result.output


def test_clean_preserves_hand_authored_claude_md_without_duplicate_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-project cleanup preserves user content without irrelevant advice."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text(
        "name: clean-hand-authored\nversion: 1.0.0\ntargets:\n  - claude\n",
        encoding="utf-8",
    )
    (tmp_path / ".apm" / "instructions").mkdir(parents=True)
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# User-authored context\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["compile", "--target", "claude", "--clean"])

    assert result.exit_code == 0, result.output
    assert claude_md.read_text(encoding="utf-8") == "# User-authored context\n"
    assert "hand-authored file will not be deleted" in result.output
    assert "duplicate context" not in result.output
