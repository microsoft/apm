"""Regression tests for CLI help and output consistency."""

from unittest.mock import patch

import click
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.output.script_formatters import ScriptExecutionFormatter


def _walk_commands(group: click.Group, prefix: tuple[str, ...] = ()):
    """Yield (path_tuple, command) for every command reachable under group."""
    for name, cmd in group.commands.items():
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


def test_audit_help_describes_security_and_integrity_modes():
    result = CliRunner().invoke(cli, ["audit", "--help"])

    assert result.exit_code == 0
    help_text = " ".join(result.output.split())
    assert (
        "Scan installed primitives for hidden Unicode, drift, and lockfile/policy violations"
    ) in help_text
    assert "Scan installed packages for hidden Unicode characters" not in result.output


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


def test_config_help_mentions_no_subcommand_and_list_alias():
    runner = CliRunner()

    group_result = runner.invoke(cli, ["config", "--help"])
    assert group_result.exit_code == 0
    assert "Run with no subcommand to show the merged project" in group_result.output
    assert "list" in group_result.output
    assert "List all configuration values" in group_result.output

    list_result = runner.invoke(cli, ["config", "list", "--help"])
    assert list_result.exit_code == 0
    assert "Usage: cli config list [OPTIONS]" in list_result.output
    assert "List all configuration values" in list_result.output


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


def test_deps_update_target_help_uses_current_catalog():
    """Regression for #2451: deps update --target must list catalog-driven targets.

    The help string was previously hardcoded and stale.  After the fix it is
    generated from target_help_fragment('update'), so targets added to the
    catalog are automatically reflected.  This test asserts that a sampling of
    targets that were absent in the old static string now appear.
    """
    result = CliRunner().invoke(cli, ["deps", "update", "--help"])

    assert result.exit_code == 0
    help_text = result.output
    # Targets absent from the old hardcoded string
    assert "grok-build" in help_text
    assert "agents" in help_text
    # Deprecation note directing users to apm update
    assert "apm update" in help_text


def test_compile_target_all_exclusion_lists_explicit_only_targets():
    """Compile help lists stable explicit-only targets excluded from ``all``."""
    result = CliRunner().invoke(cli, ["compile", "--help"])

    assert result.exit_code == 0
    help_text = result.output
    # Assert the full exclusion sentence (normalize whitespace from help-text wrapping)
    normalized = " ".join(help_text.split())
    assert (
        "excludes agent-skills, antigravity, hermes, experimental targets, and intellij"
        in normalized
    )


def test_mcp_install_help_lists_target_global_and_trust_transitive():
    """Regression for #2451: mcp install --help must enumerate --target, --global,
    and --trust-transitive-mcp in its forwarded-options block.
    """
    result = CliRunner().invoke(cli, ["mcp", "install", "--help"])

    assert result.exit_code == 0
    help_text = result.output
    assert "--target" in help_text
    assert "--global" in help_text
    assert "--trust-transitive-mcp" in help_text


def test_mcp_install_help_forwarded_options_complete():
    """Regression trap for FU-3: mcp install --help forwarded-options block must
    enumerate all flags declared in the block, not just the three added in #2451.

    This test prevents silent omission when new install flags are added: if a flag
    appears in mcp.py's forwarded-options block it must appear in the rendered help.
    """
    result = CliRunner().invoke(cli, ["mcp", "install", "--help"])

    assert result.exit_code == 0
    help_text = result.output
    # All flags currently declared in the forwarded-options block of mcp.py
    expected_flags = [
        "--transport",
        "--url",
        "--env",
        "--header",
        "--target",
        "--registry",
        "--mcp-version",
        "--global",
        "--trust-transitive-mcp",
        "--dev",
        "--dry-run",
        "--force",
        "--verbose",
        "--no-policy",
    ]
    for flag in expected_flags:
        assert flag in help_text, (
            f"mcp install --help missing forwarded flag: {flag!r}. "
            "Add it to the forwarded-options block in src/apm_cli/commands/mcp.py."
        )


def test_deps_update_target_help_values_catalog_sorted():
    """FU-5 regression trap: the target values in deps update --target help must
    appear in the same alphabetical order as the catalog generates via
    target_help_fragment('update'), keeping CLI help and docs sort order in sync.
    """
    result = CliRunner().invoke(cli, ["deps", "update", "--help"])
    assert result.exit_code == 0
    normalized = " ".join(result.output.split())

    # Locate the Values: ... fragment in the help text and check ordering within it
    values_start = normalized.find("Values:")
    assert values_start != -1, "Expected 'Values:' in deps update --help --target section"
    values_segment = normalized[values_start:]

    # Verify key alphabetical ordering pairs within the values fragment.
    # These are derived from the catalog's sorted output (target_help_fragment sorts
    # accepted_target_values alphabetically); any catalog-order regression fails here.
    ordering_pairs = [
        ("claude", "codex"),
        ("codex", "copilot"),
        ("grok-build", "intellij"),
        ("intellij", "kiro"),
        ("kiro", "opencode"),
        ("opencode", "vscode"),
        ("vscode", "windsurf"),
    ]
    for earlier, later in ordering_pairs:
        assert earlier in values_segment and later in values_segment, (
            f"deps update --help missing one of: {earlier!r}, {later!r} in Values fragment"
        )
        assert values_segment.index(earlier) < values_segment.index(later), (
            f"Sort order mismatch: '{earlier}' must precede '{later}' "
            "(catalog-driven alphabetical order)."
        )


def test_install_compile_and_deps_exclusion_clause_matches_catalog():
    """Regression: 'all' exclusion clause in install, compile, and deps help must match
    target_all_exclusion_help(), not a hand-maintained static string.

    Adding a new explicit-only or mcp-only target to the catalog must automatically
    update both help strings; this test catches any desync.
    """
    from apm_cli.core.target_catalog import target_all_exclusion_help

    expected_clause = target_all_exclusion_help()

    install_result = CliRunner().invoke(cli, ["install", "--help"])
    assert install_result.exit_code == 0
    assert expected_clause in " ".join(install_result.output.split())

    compile_result = CliRunner().invoke(cli, ["compile", "--help"])
    assert compile_result.exit_code == 0
    compile_normalized = " ".join(compile_result.output.split())
    assert expected_clause in compile_normalized, (
        f"compile --help exclusion clause mismatch; expected: {expected_clause!r}"
    )

    deps_result = CliRunner().invoke(cli, ["deps", "update", "--help"])
    assert deps_result.exit_code == 0
    deps_normalized = " ".join(deps_result.output.split())
    assert expected_clause in deps_normalized, (
        f"deps update --help exclusion clause mismatch; expected: {expected_clause!r}"
    )


def test_script_run_header_uses_running_status_symbol():
    formatter = ScriptExecutionFormatter(use_color=False)

    assert formatter.format_script_header("build", {})[0] == "[>] Running script: build"
