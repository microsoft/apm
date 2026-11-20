"""Unit tests for the mcp command group (commands/mcp.py) (pure logic cases)."""

import click
import pytest

from apm_cli.commands.mcp import mcp

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# mcp search command
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# mcp show command
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# mcp list command
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# mcp group
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# `apm mcp install` alias forwarding tests (T-alias)
# ---------------------------------------------------------------------------


class TestMcpInstallAlias:
    """The `apm mcp install` subcommand is a thin alias that forwards to
    `apm install --mcp ...`. These tests verify forwarding semantics and the
    help surface; end-to-end install behaviour is owned by the install command
    tests.
    """

    def test_forwarded_options_are_borrowed_from_install(self):
        """#3588: forwarded options are apm install's own definitions, not prose."""
        from apm_cli.commands.install import install as install_command

        install_cmd = mcp.commands["install"]
        mcp_ctx = click.Context(install_cmd, info_name="install")
        install_ctx = click.Context(install_command, info_name="install")
        install_by_name = {p.name: p for p in install_command.params}
        forwarded = [
            p
            for p in install_cmd.get_params(mcp_ctx)
            if p.name not in {"name", "help"} and isinstance(p, click.Option)
        ]
        # Every install option except the alias-owned --mcp is parsed with
        # install's arity, so pass-through values stay attached.
        expected = {
            p.name
            for p in install_command.params
            if isinstance(p, click.Option) and p.name != "mcp_name"
        }
        assert {p.name for p in forwarded} == expected
        for param in forwarded:
            source = install_by_name[param.name]
            assert param.opts == source.opts
            assert param.secondary_opts == source.secondary_opts
            assert param.type is source.type
            assert param.help == source.help
            if not param.hidden:
                assert param.get_help_record(mcp_ctx) == source.get_help_record(install_ctx)

    def test_forwarded_options_shell_complete(self):
        """Forwarded options complete like apm install's (names and choices)."""
        from click.shell_completion import ShellComplete

        from apm_cli.cli import cli

        completer = ShellComplete(cli, {}, "apm", "_APM_COMPLETE")
        names = [c.value for c in completer.get_completions(["mcp", "install", "api"], "--tr")]
        assert "--transport" in names
        assert "--trust-transitive-mcp" in names
        choices = [
            c.value for c in completer.get_completions(["mcp", "install", "api", "--transport"], "")
        ]
        assert choices == ["stdio", "http", "sse", "streamable-http"]


# ---------------------------------------------------------------------------
# Registry env-var honouring (regression for #813)
# ---------------------------------------------------------------------------
