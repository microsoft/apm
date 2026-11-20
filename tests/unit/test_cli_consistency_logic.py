"""Regression tests for CLI help and output consistency (pure logic cases)."""

import click
import pytest

from apm_cli.cli import cli
from apm_cli.output.script_formatters import ScriptExecutionFormatter

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-process-interrupt-signals")]


def _walk_commands(group: click.Group, prefix: tuple[str, ...] = ()):
    """Yield (path_tuple, command) for every command reachable under group."""
    ctx = click.Context(group)
    for name in group.list_commands(ctx):
        cmd = group.get_command(ctx, name)
        if cmd is None:
            continue
        path = (*prefix, name)
        yield path, cmd
        if isinstance(cmd, click.Group):
            yield from _walk_commands(cmd, path)


def test_every_registered_command_has_explicit_help():
    """Silent-drift guard: no command may rely on the docstring fallback.

    The release binary is built with PyInstaller ``optimize=2`` (``python -OO``)
    to keep the PYZ string surface small (Defender ML false-positive mitigation;
    see #1407 and build/apm.spec). ``-OO`` strips ``__doc__``, so any Click
    command without an explicit ``help=`` renders with an empty summary and
    empty ``--help`` body in the binary -- exactly the regression that #1298
    reported for ``apm view``.

    Every command and sub-command registered under the top-level ``cli`` group
    must set ``help=`` (or ``short_help=``) explicitly.
    """
    missing: list[str] = []
    for path, cmd in _walk_commands(cli):
        if cmd.hidden:
            # Hidden aliases (e.g. ``apm info``) inherit help from their source
            # command; checking the visible command is sufficient.
            continue
        help_text = (cmd.help or "").strip() or (cmd.short_help or "").strip()
        if not help_text:
            missing.append(" ".join(path))
    assert not missing, (
        "Commands missing explicit help= (would render blank under "
        "PyInstaller optimize=2): " + ", ".join(sorted(missing))
    )


def test_script_run_header_uses_running_status_symbol():
    formatter = ScriptExecutionFormatter(use_color=False)

    assert formatter.format_script_header("build", {})[0] == "[>] Running script: build"
