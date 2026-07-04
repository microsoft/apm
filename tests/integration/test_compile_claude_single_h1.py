"""Regression coverage for CLAUDE.md heading structure."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli


def _h1_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("# ")]


def test_compile_claude_project_standards_is_not_duplicate_h1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE.md keeps Project Standards below the file title."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text(
        "name: single-h1-repro\nversion: 1.0.0\ntargets:\n  - claude\n  - codex\n",
        encoding="utf-8",
    )

    instructions_dir = tmp_path / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    (instructions_dir / "style.instructions.md").write_text(
        "---\ndescription: Style rule\napplyTo: '**/*.py'\n---\nUse type hints for Python code.\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["compile", "--no-links"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert _h1_lines(tmp_path / "CLAUDE.md") == ["# CLAUDE.md"]
    assert _h1_lines(tmp_path / "AGENTS.md") == ["# AGENTS.md"]

    claude_lines = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    assert "## Project Standards" in claude_lines
    assert "# Project Standards" not in claude_lines
