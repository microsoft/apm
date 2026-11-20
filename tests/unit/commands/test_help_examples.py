"""Help examples stay copy-pasteable (#3994).

Click rewraps help paragraphs unless a ``\\b`` marker opens them; a marker that
does not open a paragraph is printed as a literal backspace byte.
"""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = pytest.mark.component


def _help_pages(group: click.Group, path: tuple[str, ...] = ()):
    ctx = click.Context(group)
    for name in group.list_commands(ctx):
        command = group.get_command(ctx, name)
        if command is None or command.hidden:
            continue
        yield (*path, name)
        if isinstance(command, click.Group):
            yield from _help_pages(command, (*path, name))


def test_no_help_page_prints_a_control_byte():
    runner = CliRunner()
    for page in _help_pages(cli):
        output = runner.invoke(cli, [*page, "--help"]).output
        assert not any(ord(ch) < 32 and ch not in "\n\t" for ch in output), page


def test_examples_print_one_command_per_line():
    runner = CliRunner()
    why = runner.invoke(cli, ["deps", "why", "--help"]).output.splitlines()
    assert "    apm deps why acme-org/shared-utils --json" in why
    publish = runner.invoke(cli, ["publish", "--help"]).output.splitlines()
    assert "    apm publish --package acme/my-skill" in publish
    assert "    apm experimental enable registries" in publish
