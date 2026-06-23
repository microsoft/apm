"""Regression tests for CLI help and output consistency."""

from unittest.mock import patch

import click
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.core.target_detection import (
    TARGET_ALL_HELP_EXPANSION,
    TARGET_HELP_EXAMPLE_CSV,
    TARGET_HELP_VALUES_CSV,
)
from apm_cli.output.script_formatters import ScriptExecutionFormatter


def _walk_commands(group: click.Group, prefix: tuple[str, ...] = ()):
    """Yield (path_tuple, command) for every command reachable under group."""
    for name, cmd in group.commands.items():
        path = (*prefix, name)
        yield path, cmd
        if isinstance(cmd, click.Group):
            yield from _walk_commands(cmd, path)


def _option_help(path: tuple[str, ...], option_name: str) -> str:
    command: click.Command = cli
    for name in path:
        assert isinstance(command, click.Group)
        command = command.commands[name]
    for param in command.params:
        if isinstance(param, click.Option) and option_name in param.opts:
            return param.help or ""
    raise AssertionError(f"{' '.join(path)} is missing {option_name}")


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


def test_experimental_subcommand_help_is_specific():
    runner = CliRunner()

    list_result = runner.invoke(cli, ["experimental", "list", "--help"])
    assert list_result.exit_code == 0
    assert "Usage: cli experimental list [OPTIONS]" in list_result.output
    assert "--enabled" in list_result.output
    assert "--disabled" in list_result.output
    assert "--json" in list_result.output

    enable_result = runner.invoke(cli, ["experimental", "enable", "--help"])
    assert enable_result.exit_code == 0
    assert "Usage: cli experimental enable [OPTIONS] NAME" in enable_result.output

    disable_result = runner.invoke(cli, ["experimental", "disable", "--help"])
    assert disable_result.exit_code == 0
    assert "Usage: cli experimental disable [OPTIONS] NAME" in disable_result.output

    reset_result = runner.invoke(cli, ["experimental", "reset", "--help"])
    assert reset_result.exit_code == 0
    assert "Usage: cli experimental reset [OPTIONS] [NAME]" in reset_result.output
    assert "-y, --yes" in reset_result.output


def test_runtime_remove_help_includes_short_yes_alias():
    result = CliRunner().invoke(cli, ["runtime", "remove", "--help"])

    assert result.exit_code == 0
    assert "-y, --yes" in result.output


def test_mcp_install_forwards_unknown_options_before_double_dash():
    runner = CliRunner()

    with (
        runner.isolated_filesystem(),
        patch(
            "apm_cli.commands.install._get_invocation_argv",
            return_value=[
                "apm",
                "mcp",
                "install",
                "myserver",
                "--target",
                "cursor",
                "--dry-run",
                "--",
                "npx",
                "-y",
                "pkg",
            ],
        ),
    ):
        result = runner.invoke(
            cli,
            [
                "mcp",
                "install",
                "myserver",
                "--target",
                "cursor",
                "--dry-run",
                "--",
                "npx",
                "-y",
                "pkg",
            ],
        )

    assert result.exit_code == 0
    assert "would add MCP server 'myserver'" in result.output


def test_target_help_uses_shared_display_fragments():
    """Keep target help examples aligned with target_detection.py."""
    for path in (("install",), ("compile",), ("deps", "update")):
        help_text = _option_help(path, "--target")
        assert TARGET_HELP_VALUES_CSV in help_text
        assert TARGET_ALL_HELP_EXPANSION in help_text

    install_help = _option_help(("install",), "--target")
    assert "apm config target" in install_help

    compile_help = _option_help(("compile",), "--target")
    assert "'--target all' is deprecated; use '--all' instead" in compile_help

    update_help = _option_help(("update",), "--target")
    assert TARGET_HELP_EXAMPLE_CSV in update_help


def test_pack_unpack_dry_run_help_has_no_trailing_period():
    runner = CliRunner()

    pack_result = runner.invoke(cli, ["pack", "--help"])
    assert pack_result.exit_code == 0
    assert "Show what would be packed without writing." not in pack_result.output
    assert "Show what would be packed without writing" in pack_result.output

    unpack_result = runner.invoke(cli, ["unpack", "--help"])
    assert unpack_result.exit_code == 0
    assert "Show what would be unpacked without writing." not in unpack_result.output
    assert "Show what would be unpacked without writing" in unpack_result.output


def test_outdated_top_level_help_description_has_no_trailing_period():
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "Show outdated locked dependencies." not in result.output
    assert "Show outdated locked dependencies" in result.output


def test_script_run_header_uses_running_status_symbol():
    formatter = ScriptExecutionFormatter(use_color=False)

    assert formatter.format_script_header("build", {})[0] == "[>] Running script: build"
