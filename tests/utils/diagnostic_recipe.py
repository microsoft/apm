"""Extract and run the shell recipe a diagnostic prints.

Diagnostics that tell a user how to recover are only correct if the commands
they print actually work. Asserting on their wording pins prose; these helpers
let a test run the printed commands instead, so the message can be reworded
freely and the test still fails when the advice stops working.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_PROMPTS = ("$ ", "> ")


def shell_commands_in(message: str) -> list[str]:
    """Return the indented command lines of *message*, comments stripped.

    A recovery recipe is rendered as indented lines under a prose lead-in. Blank
    lines, unindented prose, and whole-line comments are not commands.
    """
    commands: list[str] = []
    for raw_line in message.splitlines():
        if not raw_line.startswith((" ", "\t")):
            continue
        line = raw_line.strip()
        for prompt in _PROMPTS:
            line = line.removeprefix(prompt)
        if not line or line.startswith("#"):
            continue
        commands.append(line)
    return commands


def run_recipe(commands: list[str], cwd: Path, *, only: str | None = None) -> None:
    """Run *commands* in *cwd*, failing the test on the first non-zero exit.

    ``only`` restricts execution to commands starting with that token, so a test
    can exercise the repair step without invoking a slower installer.
    """
    for command in commands:
        if only is not None and not command.startswith(only):
            continue
        result = subprocess.run(
            command,
            shell=True,  # the input is this project's own diagnostic text
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"recovery command from the diagnostic failed: {command!r}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
