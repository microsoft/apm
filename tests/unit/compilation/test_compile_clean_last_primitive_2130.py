"""Regression test for orphan cleanup after the last primitive is removed."""

from pathlib import Path

from click.testing import CliRunner

from apm_cli.cli import cli


def test_clean_removes_claude_md_after_last_primitive_is_removed(
    tmp_path: Path, monkeypatch
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
