from __future__ import annotations

import subprocess
import sys
from pathlib import Path


CLI = [sys.executable, "-m", "apm_cli.cli", "compile", "--target", "copilot", "--single-agents"]


def run_cli(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(CLI, cwd=str(cwd), capture_output=True, text=True)


def test_compile_emits_copilot_root_instructions_and_is_idempotent(tmp_path: Path):
    (tmp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n", encoding="utf-8")
    instructions_dir = tmp_path / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    (instructions_dir / "contributing.instructions.md").write_text(
        "---\ndescription: Contributing guide\n---\n\n# Contributing\n\nRun focused tests first.\n",
        encoding="utf-8",
    )

    first = run_cli(tmp_path)
    assert first.returncode == 0, first.stderr or first.stdout

    copilot_root = tmp_path / ".github" / "copilot-instructions.md"
    assert copilot_root.exists()
    first_content = copilot_root.read_text(encoding="utf-8")
    assert "<!-- Build ID: " in first_content
    assert "# Contributing" in first_content
    assert "Run focused tests first." in first_content

    second = run_cli(tmp_path)
    assert second.returncode == 0, second.stderr or second.stdout
    second_content = copilot_root.read_text(encoding="utf-8")

    assert first_content == second_content


def test_compile_removes_stale_root_file_when_only_scoped_rules_remain(tmp_path: Path):
    (tmp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n", encoding="utf-8")
    instructions_dir = tmp_path / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    instruction_file = instructions_dir / "contributing.instructions.md"

    instruction_file.write_text(
        "---\ndescription: Contributing guide\n---\n\n# Contributing\n\nRun focused tests first.\n",
        encoding="utf-8",
    )
    first = run_cli(tmp_path)
    assert first.returncode == 0, first.stderr or first.stdout

    copilot_root = tmp_path / ".github" / "copilot-instructions.md"
    assert copilot_root.exists()

    instruction_file.write_text(
        "---\napplyTo: \"**/*.py\"\ndescription: Python guide\n---\n\nUse type hints.\n",
        encoding="utf-8",
    )
    second = run_cli(tmp_path)
    assert second.returncode == 0, second.stderr or second.stdout

    assert not copilot_root.exists()
