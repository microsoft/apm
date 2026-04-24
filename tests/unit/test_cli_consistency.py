"""Regression tests for CLI help and output consistency."""

from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.output.script_formatters import ScriptExecutionFormatter


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
    assert "outdated      Show outdated locked dependencies." not in result.output
    assert "outdated      Show outdated locked dependencies" in result.output


def test_script_run_header_uses_running_status_symbol():
    formatter = ScriptExecutionFormatter(use_color=False)

    assert formatter.format_script_header("build", {})[0] == "[>] Running script: build"
