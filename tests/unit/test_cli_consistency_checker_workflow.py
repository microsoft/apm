"""Contracts for the cli-consistency-checker agentic workflow (#3523).

The checker must derive the command surface from ``apm --help`` instead of a
hand-written list, and must only point at documentation paths that exist.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "cli-consistency-checker.md"
CLI_REFERENCE_DIR = "docs/src/content/docs/reference/cli/"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _walker_awk_program() -> str:
    match = re.search(r"apm_subcommands\(\) \{\n\s+apm .*?\| awk '([^']+)'", _workflow_text())
    assert match, "apm_subcommands() awk parser not found in the checker workflow"
    return match.group(1)


def _groups(group: click.Group, prefix: tuple[str, ...] = ()):
    """Yield (path, group) for the root and every visible nested group."""
    yield prefix, group
    ctx = click.Context(group)
    for name in group.list_commands(ctx):
        cmd = group.get_command(ctx, name)
        if cmd is None or cmd.hidden or not isinstance(cmd, click.Group):
            continue
        yield from _groups(cmd, (*prefix, name))


def test_checker_references_only_existing_docs_paths() -> None:
    text = _workflow_text()
    assert CLI_REFERENCE_DIR in text
    referenced = {m.rstrip(".") for m in re.findall(r"docs/[\w./-]+", text)}
    missing = sorted(path for path in referenced if not (ROOT / path).exists())
    assert not missing, f"Checker references docs paths that do not exist: {missing}"


def test_checker_does_not_hardcode_subcommand_help_list() -> None:
    hardcoded = re.findall(r"(?m)^apm [a-z][\w-]*(?: [a-z][\w-]*)* --help$", _workflow_text())
    assert not hardcoded, (
        "Checker hardcodes per-command --help calls; derive them from the "
        f"apm --help walker instead: {hardcoded}"
    )


@pytest.mark.skipif(shutil.which("awk") is None, reason="awk not available")
def test_checker_walker_finds_every_visible_subcommand() -> None:
    """The workflow's awk parser must list exactly the Click tree's subcommands."""
    program = _walker_awk_program()
    runner = CliRunner()
    problems: list[str] = []
    for path, group in _groups(cli):
        # Nested --help runs the root callback; keep the update check offline.
        with patch("apm_cli.cli._check_and_notify_updates"):
            result = runner.invoke(cli, [*path, "--help"], terminal_width=80)
        assert result.exit_code == 0, result.output
        parsed = subprocess.run(
            ["awk", program],
            input=result.output,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        ctx = click.Context(group)
        expected = [
            name
            for name in group.list_commands(ctx)
            if (cmd := group.get_command(ctx, name)) is not None and not cmd.hidden
        ]
        if sorted(parsed) != sorted(expected):
            label = " ".join(("apm", *path))
            problems.append(f"{label}: parsed={sorted(parsed)} expected={sorted(expected)}")
    assert not problems, "Checker walker misses subcommands: " + "; ".join(problems)
