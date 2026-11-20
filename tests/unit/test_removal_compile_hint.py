"""Removing a package whose instructions were compiled must point at ``apm compile``.

Codex, Gemini and OpenCode read dependency instructions only from the compiled
root context file (AGENTS.md / GEMINI.md).  ``apm uninstall`` and ``apm prune``
do not recompile, so they reuse the install compile hint to say which file is
now stale.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

_HINT = "Instructions removed for codex. Run 'apm compile' to update AGENTS.md."


def _write_project(root: Path, *, target: str, declared: bool, instructions: bool) -> None:
    data = {
        "name": "test-project",
        "version": "1.0.0",
        "targets": [target],
        "dependencies": {"apm": ["acme/tools"] if declared else []},
    }
    (root / "apm.yml").write_text(yaml.safe_dump(data), encoding="utf-8")
    package = root / "apm_modules" / "acme" / "tools"
    (package / ".apm" / "instructions").mkdir(parents=True)
    (package / "apm.yml").write_text("name: tools\nversion: 1.0.0\n", encoding="utf-8")
    if instructions:
        (package / ".apm" / "instructions" / "glossary.instructions.md").write_text(
            "---\napplyTo: '**'\n---\nUse the agreed team glossary.\n", encoding="utf-8"
        )


def _remove(command: str) -> list[str]:
    return ["uninstall", "acme/tools"] if command == "uninstall" else ["prune"]


@pytest.mark.parametrize("command", ["uninstall", "prune"])
def test_removal_hints_compile_for_compiled_instructions(
    tmp_path: Path, monkeypatch, command: str
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, target="codex", declared=command == "uninstall", instructions=True)
    from apm_cli.cli import cli

    result = CliRunner().invoke(cli, _remove(command))

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "apm_modules" / "acme" / "tools").exists()
    assert result.output.count(_HINT) == 1, result.output


@pytest.mark.parametrize(
    ("command", "target", "instructions"),
    [
        ("uninstall", "copilot", True),
        ("prune", "cursor", True),
        ("uninstall", "codex", False),
    ],
)
def test_removal_skips_hint_without_compiled_instructions(
    tmp_path: Path, monkeypatch, command: str, target: str, instructions: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_project(
        tmp_path, target=target, declared=command == "uninstall", instructions=instructions
    )
    from apm_cli.cli import cli

    result = CliRunner().invoke(cli, _remove(command))

    assert result.exit_code == 0, result.output
    assert "Run 'apm compile'" not in result.output, result.output
