"""Unit tests for the mcp command group (commands/mcp.py).

Tests cover: search, show, list commands with rich console and fallback paths,
error handling, edge cases.
"""

import re
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import click
import pytest
from click.testing import CliRunner

from apm_cli.commands.mcp import mcp

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s\[\]<>'\"]+")


def _printed_urls(printed: str) -> list:
    """Parse all http(s) URLs out of console-printed text.

    Returns a list of ``(scheme, hostname)`` tuples produced by
    ``urllib.parse.urlparse``.  Tests that need to assert a specific URL
    appears in CLI output should compare against this structured form
    rather than substring-matching on the raw blob -- the substring form
    is flagged by CodeQL's ``py/incomplete-url-substring-sanitization``
    rule as an unsafe sanitiser pattern.
    """
    out = []
    for match in _URL_RE.findall(printed):
        parsed = urlparse(match)
        out.append((parsed.scheme, parsed.hostname))
    return out


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

FAKE_SERVERS = [
    {"name": "io.github.acme/cool-server", "description": "A cool server", "version": "1.0.0"},
    {"name": "io.github.acme/other-server", "description": "Another server", "version": "2.0.0"},
]

FAKE_SERVER_DETAIL = {
    "name": "io.github.acme/cool-server",
    "description": "A cool server with extra details",
    "version": "1.0.0",
    "version_detail": {"version": "1.2.3"},
    "repository": {"url": "https://github.com/acme/cool-server"},
    "id": "abcdef1234567890",
    "remotes": [{"transport_type": "sse", "url": "https://remote.example.com/sse"}],
    "packages": [{"registry_name": "npm", "name": "cool-server-pkg", "runtime_hint": "node"}],
}


def make_runner():
    return CliRunner()


def patch_registry(search_result=None, list_result=None, detail_result=None, detail_raises=None):
    """Return a context manager that patches RegistryIntegration.

    RegistryIntegration is imported lazily inside each command function body,
    so we patch it at the canonical module location.
    """
    mock_reg = MagicMock()
    if search_result is not None:
        mock_reg.search_packages.return_value = search_result
    if list_result is not None:
        mock_reg.list_available_packages.return_value = list_result
    if detail_raises is not None:
        mock_reg.get_package_info.side_effect = detail_raises
    elif detail_result is not None:
        mock_reg.get_package_info.return_value = detail_result

    return patch(
        "apm_cli.registry.integration.RegistryIntegration",
        return_value=mock_reg,
    )


# ---------------------------------------------------------------------------
# mcp search command
# ---------------------------------------------------------------------------


class TestMcpSearch:
    def test_search_rich_with_results(self):
        """search with Rich console returns results table."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch_registry(search_result=FAKE_SERVERS),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("rich.table.Table", MagicMock()),
        ):
            result = runner.invoke(mcp, ["search", "cool"])

        assert result.exit_code == 0
        mock_console.print.assert_called()

    def test_search_rich_no_results(self):
        """search with no results shows warning message via Rich."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch_registry(search_result=[]),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
        ):
            result = runner.invoke(mcp, ["search", "nothing"])

        assert result.exit_code == 0
        # Should warn no results
        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "No MCP servers found" in printed

    def test_search_fallback_with_results(self):
        """search falls back to plain echo when no Rich console."""
        runner = make_runner()

        with (
            patch_registry(search_result=FAKE_SERVERS),
            patch("apm_cli.commands.mcp._get_console", return_value=None),
        ):
            result = runner.invoke(mcp, ["search", "cool"])

        assert result.exit_code == 0
        assert "cool-server" in result.output

    def test_search_fallback_no_results(self):
        """search fallback path warns when no results found."""
        runner = make_runner()

        with (
            patch_registry(search_result=[]),
            patch("apm_cli.commands.mcp._get_console", return_value=None),
        ):
            result = runner.invoke(mcp, ["search", "nothing"])

        assert result.exit_code == 0

    def test_search_limit_respected(self):
        """--limit option restricts the number of results returned."""
        runner = make_runner()
        many_servers = [
            {"name": f"server-{i}", "description": f"Server {i}", "version": "1.0"}
            for i in range(20)
        ]
        mock_console = MagicMock()
        table_instance = MagicMock()

        with (
            patch_registry(search_result=many_servers),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("rich.table.Table", return_value=table_instance),
        ):
            result = runner.invoke(mcp, ["search", "server", "--limit", "3"])

        assert result.exit_code == 0
        # Only 3 rows should have been added to the table
        assert table_instance.add_row.call_count == 3
        # Verify no servers beyond the limit appear in the rows
        row_names = [call.args[0] for call in table_instance.add_row.call_args_list]
        for i in range(3, 20):
            assert f"server-{i}" not in row_names

    def test_search_registry_exception_exits_1(self):
        """Registry errors cause exit code 1."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch(
                "apm_cli.registry.integration.RegistryIntegration",
                side_effect=RuntimeError("network error"),
            ),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
        ):
            result = runner.invoke(mcp, ["search", "cool"])

        assert result.exit_code == 1

    def test_search_verbose_flag(self):
        """--verbose flag is accepted without error."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch_registry(search_result=FAKE_SERVERS),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("rich.table.Table", MagicMock()),
        ):
            result = runner.invoke(mcp, ["search", "cool", "--verbose"])

        assert result.exit_code == 0

    def test_search_description_truncation(self):
        """Long descriptions are truncated in the Rich path."""
        runner = make_runner()
        long_desc = "x" * 200
        servers = [{"name": "srv", "description": long_desc, "version": "1.0"}]
        mock_console = MagicMock()
        mock_table = MagicMock()

        with (
            patch_registry(search_result=servers),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("rich.table.Table", return_value=mock_table),
        ):
            result = runner.invoke(mcp, ["search", "srv"])

        assert result.exit_code == 0
        # add_row should have been called
        mock_table.add_row.assert_called_once()
        # The description arg should be truncated
        args = mock_table.add_row.call_args[0]
        assert len(args[1]) <= 83  # 80 chars + "..."


# ---------------------------------------------------------------------------
# mcp show command
# ---------------------------------------------------------------------------


class TestMcpShow:
    def test_show_rich_success(self):
        """show with Rich console displays server info tables."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch_registry(detail_result=FAKE_SERVER_DETAIL),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("rich.table.Table", MagicMock()),
        ):
            result = runner.invoke(mcp, ["show", "io.github.acme/cool-server"])

        assert result.exit_code == 0

    def test_show_rich_not_found_exits_1(self):
        """show with Rich console exits 1 when server not found."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch_registry(detail_raises=ValueError("not found")),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
        ):
            result = runner.invoke(mcp, ["show", "nonexistent"])

        assert result.exit_code == 1

    def test_show_fallback_success(self):
        """show fallback path echoes server details."""
        runner = make_runner()

        with (
            patch_registry(detail_result=FAKE_SERVER_DETAIL),
            patch("apm_cli.commands.mcp._get_console", return_value=None),
        ):
            result = runner.invoke(mcp, ["show", "io.github.acme/cool-server"])

        assert result.exit_code == 0
        assert "cool-server" in result.output

    def test_show_fallback_not_found_exits_1(self):
        """show fallback path exits 1 when server not found."""
        runner = make_runner()

        with (
            patch_registry(detail_raises=ValueError("not found")),
            patch("apm_cli.commands.mcp._get_console", return_value=None),
        ):
            result = runner.invoke(mcp, ["show", "nonexistent"])

        assert result.exit_code == 1

    def test_show_registry_exception_exits_1(self):
        """Generic exception causes exit code 1."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch(
                "apm_cli.registry.integration.RegistryIntegration", side_effect=RuntimeError("oops")
            ),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
        ):
            result = runner.invoke(mcp, ["show", "something"])

        assert result.exit_code == 1

    def test_show_version_from_version_detail(self):
        """show extracts version from version_detail when present."""
        runner = make_runner()
        mock_console = MagicMock()
        detail = dict(FAKE_SERVER_DETAIL)  # has version_detail

        with (
            patch_registry(detail_result=detail),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("rich.table.Table", MagicMock()),
        ):
            result = runner.invoke(mcp, ["show", "cool"])

        assert result.exit_code == 0

    def test_show_version_fallback_to_version_key(self):
        """show falls back to top-level version when version_detail absent."""
        runner = make_runner()
        mock_console = MagicMock()
        detail = {
            "name": "minimal-server",
            "description": "minimal",
            "version": "0.9.0",
            "repository": {"url": "https://github.com/x"},
        }

        with (
            patch_registry(detail_result=detail),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("rich.table.Table", MagicMock()),
        ):
            result = runner.invoke(mcp, ["show", "minimal-server"])

        assert result.exit_code == 0

    def test_show_no_remotes_no_packages(self):
        """show handles server with no remotes or packages gracefully."""
        runner = make_runner()
        mock_console = MagicMock()
        detail = {
            "name": "bare-server",
            "description": "bare",
            "id": "abc123xyz",
        }

        with (
            patch_registry(detail_result=detail),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("rich.table.Table", MagicMock()),
        ):
            result = runner.invoke(mcp, ["show", "bare-server"])

        assert result.exit_code == 0

    def test_show_long_package_name_truncated(self):
        """Package name longer than 25 chars is truncated in table."""
        runner = make_runner()
        mock_console = MagicMock()
        mock_table = MagicMock()
        detail = {
            "name": "pkg-server",
            "description": "desc",
            "packages": [{"registry_name": "npm", "name": "a" * 30, "runtime_hint": "node"}],
        }

        with (
            patch_registry(detail_result=detail),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("rich.table.Table", return_value=mock_table),
        ):
            result = runner.invoke(mcp, ["show", "pkg-server"])

        assert result.exit_code == 0
        # Find the add_row call that includes the package name (4-arg calls are pkg_table rows)
        pkg_row_calls = [c for c in mock_table.add_row.call_args_list if len(c.args) == 4]
        assert pkg_row_calls, "Expected pkg_table.add_row to have been called"
        pkg_name_arg = pkg_row_calls[0].args[1]
        # The 30-char name must be truncated to at most 25 chars
        assert len(pkg_name_arg) <= 25
        assert pkg_name_arg.endswith("...")


# ---------------------------------------------------------------------------
# mcp list command
# ---------------------------------------------------------------------------


class TestMcpList:
    def test_list_rich_with_results(self):
        """list with Rich console shows catalog table."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch_registry(list_result=FAKE_SERVERS),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("rich.table.Table", MagicMock()),
        ):
            result = runner.invoke(mcp, ["list"])

        assert result.exit_code == 0

    def test_list_rich_empty_registry(self):
        """list shows warning when registry returns no servers."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch_registry(list_result=[]),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
        ):
            result = runner.invoke(mcp, ["list"])

        assert result.exit_code == 0
        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "No MCP servers" in printed

    def test_list_fallback_with_results(self):
        """list falls back to plain echo when no Rich console."""
        runner = make_runner()

        with (
            patch_registry(list_result=FAKE_SERVERS),
            patch("apm_cli.commands.mcp._get_console", return_value=None),
        ):
            result = runner.invoke(mcp, ["list"])

        assert result.exit_code == 0
        assert "cool-server" in result.output

    def test_list_fallback_empty_registry(self):
        """list fallback path warns when no servers found."""
        runner = make_runner()

        with (
            patch_registry(list_result=[]),
            patch("apm_cli.commands.mcp._get_console", return_value=None),
        ):
            result = runner.invoke(mcp, ["list"])

        assert result.exit_code == 0

    def test_list_limit_option(self):
        """--limit restricts number of servers displayed."""
        runner = make_runner()
        many_servers = [
            {"name": f"s{i}", "description": f"Srv {i}", "version": "1.0"} for i in range(30)
        ]
        mock_console = MagicMock()
        table_instance = MagicMock()

        with (
            patch_registry(list_result=many_servers),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("rich.table.Table", return_value=table_instance),
        ):
            result = runner.invoke(mcp, ["list", "--limit", "5"])

        assert result.exit_code == 0
        # Only 5 rows should have been added to the table
        assert table_instance.add_row.call_count == 5
        # Verify no servers beyond the limit appear in the rows
        row_names = [call.args[0] for call in table_instance.add_row.call_args_list]
        for i in range(5, 30):
            assert f"s{i}" not in row_names

    def test_list_registry_exception_exits_1(self):
        """Registry errors on list cause exit code 1."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch(
                "apm_cli.registry.integration.RegistryIntegration",
                side_effect=RuntimeError("failure"),
            ),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
        ):
            result = runner.invoke(mcp, ["list"])

        assert result.exit_code == 1

    def test_list_shows_hint_at_limit(self):
        """When results == limit, a 'use --limit' hint is shown."""
        runner = make_runner()
        mock_console = MagicMock()
        # Exactly 20 results (default limit)
        servers = [{"name": f"s{i}", "description": "x", "version": "1.0"} for i in range(20)]

        with (
            patch_registry(list_result=servers),
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("rich.table.Table", MagicMock()),
        ):
            result = runner.invoke(mcp, ["list"])

        assert result.exit_code == 0
        # Should show hint to use higher --limit
        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "--limit" in printed


# ---------------------------------------------------------------------------
# mcp group
# ---------------------------------------------------------------------------


class TestMcpGroup:
    def test_mcp_help(self):
        """mcp --help exits 0."""
        runner = make_runner()
        result = runner.invoke(mcp, ["--help"])
        assert result.exit_code == 0
        assert "search" in result.output
        assert "show" in result.output
        assert "list" in result.output


# ---------------------------------------------------------------------------
# `apm mcp install` alias forwarding tests (T-alias)
# ---------------------------------------------------------------------------


class TestMcpInstallAlias:
    """The `apm mcp install` subcommand is a thin alias that forwards to
    `apm install --mcp ...`. These tests verify forwarding semantics and the
    help surface; end-to-end install behaviour is owned by the install command
    tests.
    """

    def test_help_shows_alias_message_and_example(self):
        runner = make_runner()
        result = runner.invoke(mcp, ["install", "--help"])
        assert result.exit_code == 0
        assert "Alias for 'apm install --mcp'" in result.output
        assert "apm mcp install fetch" in result.output
        assert "Usage: mcp install [OPTIONS] NAME" in result.output
        assert "--transport" in result.output
        assert "--url" in result.output
        assert "--env" in result.output
        assert "--header" in result.output
        assert "--mcp-version" in result.output
        assert "--registry" in result.output
        # Inherited "(requires --mcp)" notes are explained, not rewritten.
        assert "'--mcp NAME' is implied" in " ".join(result.output.split())

    def test_epilog_does_not_reference_broken_mcp_help_flag(self):
        """Epilog must reference `apm install --help`, not `apm install --mcp --help`.

        `apm install --mcp --help` is rejected by conflicts.py because --help
        is parsed as the value for --mcp, causing UsageError. Regression for #1586.
        """
        runner = make_runner()
        result = runner.invoke(mcp, ["install", "--help"])
        assert result.exit_code == 0
        assert "install --mcp --help" not in result.output
        assert "install --help" in result.output

    def test_forwards_args_to_root_install_with_mcp_flag(self):
        """Verify the alias invokes the root `cli` with `install --mcp <argv>`."""
        runner = make_runner()
        with patch("apm_cli.cli.cli.main") as mock_main:
            mock_main.return_value = 0
            result = runner.invoke(
                mcp,
                ["install", "fetch", "--", "npx", "-y", "@modelcontextprotocol/server-fetch"],
            )
            assert result.exit_code == 0
            mock_main.assert_called_once()
            kwargs = mock_main.call_args.kwargs
            forwarded = kwargs.get("args") or mock_main.call_args.args[0]
            assert forwarded[0] == "install"
            assert forwarded[1] == "--mcp"
            assert "fetch" in forwarded
            assert "npx" in forwarded
            assert "@modelcontextprotocol/server-fetch" in forwarded

    @pytest.mark.parametrize("code", [0, 1, 2])
    def test_exit_code_comes_from_the_forwarded_install(self, code):
        """#3666: the alias exits with the install outcome, never a swallowed code."""
        from apm_cli.cli import cli

        @click.command(context_settings={"ignore_unknown_options": True})
        @click.argument("argv", nargs=-1, type=click.UNPROCESSED)
        @click.pass_context
        def fake_install(ctx, argv):
            ctx.exit(code)

        runner = make_runner()
        with patch.dict(cli.commands, {"install": fake_install}):
            result = runner.invoke(mcp, ["install", "srv", "--", "npx", "srv"])
        assert result.exit_code == code

    def test_forwards_transport_options(self):
        runner = make_runner()
        with patch("apm_cli.cli.cli.main") as mock_main:
            mock_main.return_value = 0
            result = runner.invoke(
                mcp,
                ["install", "api", "--transport", "http", "--url", "https://example.com/mcp"],
            )
            assert result.exit_code == 0
            forwarded = mock_main.call_args.kwargs.get("args") or mock_main.call_args.args[0]
            assert forwarded[:3] == ["install", "--mcp", "api"]
            assert "--transport" in forwarded
            assert "http" in forwarded
            assert "--url" in forwarded
            assert "https://example.com/mcp" in forwarded

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

    def test_pass_through_value_shaped_like_a_flag_stays_attached(self):
        """``--root --dry-run`` keeps ``--dry-run`` as --root's value, as in apm install."""
        runner = make_runner()
        fake_argv = ["apm", "mcp", "install", "api", "--root", "--dry-run"]
        with (
            patch("apm_cli.commands.install._get_invocation_argv", return_value=fake_argv),
            patch("apm_cli.cli.cli.main", return_value=0) as mock_main,
        ):
            result = runner.invoke(mcp, ["install", "api", "--root", "--dry-run"])
        assert result.exit_code == 0, result.output
        forwarded = mock_main.call_args.kwargs.get("args")
        assert forwarded[:3] == ["install", "--mcp", "api"]
        root_idx = forwarded.index("--root")
        assert forwarded[root_idx + 1] == "--dry-run"
        assert forwarded.count("--dry-run") == 1

    def test_hidden_install_options_forward_but_stay_out_of_help(self):
        """Install options outside the shown set still forward verbatim."""
        runner = make_runner()
        with patch("apm_cli.cli.cli.main", return_value=0) as mock_main:
            result = runner.invoke(
                mcp, ["install", "api", "--runtime", "copilot", "--no-trust-bin"]
            )
        assert result.exit_code == 0, result.output
        forwarded = mock_main.call_args.kwargs.get("args")
        assert forwarded[forwarded.index("--runtime") + 1] == "copilot"
        assert "--no-trust-bin" in forwarded
        help_result = runner.invoke(mcp, ["install", "--help"])
        option_lines = [line.strip() for line in help_result.output.splitlines()]
        assert not any(line.startswith(("--runtime", "--trust-bin")) for line in option_lines)

    def test_forwards_raw_option_values_verbatim(self):
        """Forwarded options reach apm install as typed, before the ``--``."""
        runner = make_runner()
        fake_argv = ["apm", "mcp", "install", "api", "--", "npx", "-y", "srv"]
        with (
            patch("apm_cli.commands.install._get_invocation_argv", return_value=fake_argv),
            patch("apm_cli.cli.cli.main", return_value=0) as mock_main,
        ):
            result = runner.invoke(
                mcp,
                [
                    "install",
                    "api",
                    "-t",
                    "copilot,claude",
                    "-g",
                    "--env",
                    "A=1",
                    "--env",
                    "B=2",
                    "--dry-run",
                    "--runtime",
                    "copilot",
                    "--",
                    "npx",
                    "-y",
                    "srv",
                ],
            )
        assert result.exit_code == 0, result.output
        forwarded = mock_main.call_args.kwargs.get("args")
        dd = forwarded.index("--")
        head = forwarded[:dd]
        assert head[:3] == ["install", "--mcp", "api"]
        assert head[head.index("--target") + 1] == "copilot,claude"
        assert "--global" in head
        assert "--dry-run" in head
        env_values = [head[i + 1] for i, tok in enumerate(head) if tok == "--env"]
        assert env_values == ["A=1", "B=2"]
        # Options not surfaced on mcp install still forward unchanged.
        assert head[head.index("--runtime") + 1] == "copilot"
        assert forwarded[dd + 1 :] == ["npx", "-y", "srv"]
        # Defaults are not materialized into explicit flags.
        assert "--force" not in head
        assert "--url" not in head

    def test_forwarded_option_validation_matches_install(self):
        """Invalid choices fail with install's choice set before forwarding."""
        runner = make_runner()
        with patch("apm_cli.cli.cli.main") as mock_main:
            result = runner.invoke(mcp, ["install", "api", "--transport", "bogus"])
        assert result.exit_code == 2
        assert "Invalid value for '--transport'" in result.output
        mock_main.assert_not_called()

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

    def test_propagates_systemexit_nonzero(self):
        """Failures from the underlying install propagate as non-zero exit codes."""
        runner = make_runner()
        with patch("apm_cli.cli.cli.main", side_effect=SystemExit(2)):
            result = runner.invoke(mcp, ["install", "foo", "--", "npx", "server"])
            assert result.exit_code == 2

    def test_propagates_click_exception(self):
        """ClickException (e.g. conflict errors) propagates with its exit code."""
        runner = make_runner()
        err = click.UsageError("conflicting options")
        with patch("apm_cli.cli.cli.main", side_effect=err):
            result = runner.invoke(mcp, ["install", "foo", "--transport", "stdio"])
            assert result.exit_code == err.exit_code
            assert "conflicting options" in result.output

    def test_success_exit_code_is_zero(self):
        runner = make_runner()
        with patch("apm_cli.cli.cli.main", return_value=0):
            result = runner.invoke(mcp, ["install", "foo", "--", "npx", "server"])
            assert result.exit_code == 0

    def test_double_dash_preserved_in_forwarded_args(self):
        """The ``--`` separator must appear in forwarded args so Click
        does not re-parse post-``--`` tokens (e.g. ``-y``) as options.
        Regression test for PR #810 item 3."""
        runner = make_runner()
        fake_argv = ["apm", "mcp", "install", "fetch", "--", "npx", "-y", "@mcp/server-fetch"]
        with (
            patch("apm_cli.commands.install._get_invocation_argv", return_value=fake_argv),
            patch("apm_cli.cli.cli.main", return_value=0) as mock_main,
        ):
            result = runner.invoke(
                mcp,
                ["install", "fetch", "--", "npx", "-y", "@mcp/server-fetch"],
            )
        assert result.exit_code == 0
        forwarded = mock_main.call_args.kwargs.get("args")
        # The ``--`` must be present between pre- and post-dash tokens.
        assert "--" in forwarded
        dd_idx = forwarded.index("--")
        assert forwarded[:dd_idx] == ["install", "--mcp", "fetch"]
        assert list(forwarded[dd_idx + 1 :]) == ["npx", "-y", "@mcp/server-fetch"]

    def test_dry_run_with_post_dash_args_no_option_error(self):
        """``apm mcp install fetch --dry-run -- npx -y @mcp/server-fetch``
        must not raise ``No such option: -y``.
        Regression test for PR #810 item 3."""
        runner = make_runner()
        fake_argv = [
            "apm",
            "mcp",
            "install",
            "fetch",
            "--dry-run",
            "--",
            "npx",
            "-y",
            "@mcp/server-fetch",
        ]
        with (
            patch("apm_cli.commands.install._get_invocation_argv", return_value=fake_argv),
            patch("apm_cli.cli.cli.main", return_value=0) as mock_main,
        ):
            result = runner.invoke(
                mcp,
                ["install", "fetch", "--dry-run", "--", "npx", "-y", "@mcp/server-fetch"],
            )
        assert result.exit_code == 0
        assert "No such option" not in (result.output or "")
        forwarded = mock_main.call_args.kwargs.get("args")
        assert "--" in forwarded
        dd_idx = forwarded.index("--")
        # --dry-run must be before the separator
        assert "--dry-run" in forwarded[:dd_idx]
        assert forwarded[dd_idx + 1 :] == ["npx", "-y", "@mcp/server-fetch"]

    def test_forwards_registry_flag_to_root_install(self):
        """``apm mcp install fetch --registry https://x.io ...`` must
        propagate ``--registry`` through the alias forwarding so the
        root ``apm install --mcp`` handler validates and persists it.
        Regression for PR #810 follow-up item 4a."""
        runner = make_runner()
        fake_argv = [
            "apm",
            "mcp",
            "install",
            "fetch",
            "--registry",
            "https://r.example.com",
            "--transport",
            "stdio",
            "--",
            "npx",
            "fetch",
        ]
        with (
            patch("apm_cli.commands.install._get_invocation_argv", return_value=fake_argv),
            patch("apm_cli.cli.cli.main", return_value=0) as mock_main,
        ):
            result = runner.invoke(
                mcp,
                [
                    "install",
                    "fetch",
                    "--registry",
                    "https://r.example.com",
                    "--transport",
                    "stdio",
                    "--",
                    "npx",
                    "fetch",
                ],
            )
        assert result.exit_code == 0
        forwarded = mock_main.call_args.kwargs.get("args")
        assert forwarded[:3] == ["install", "--mcp", "fetch"]
        assert "--registry" in forwarded
        idx = forwarded.index("--registry")
        assert forwarded[idx + 1] == "https://r.example.com"
        # --- separator preserved so post-dash tokens are not re-parsed
        assert "--" in forwarded
        dd = forwarded.index("--")
        assert forwarded[dd + 1 :] == ["npx", "fetch"]


# ---------------------------------------------------------------------------
# Registry env-var honouring (regression for #813)
# ---------------------------------------------------------------------------


class TestMcpRegistryEnvVar:
    """Every apm mcp command must let its client resolve the registry URL.

    Issue #813: a hardcoded URL bypassed the ``MCP_REGISTRY_URL`` fallback. The
    fix is still "do not hand the client a URL", and it now matters for a second
    reason: a caller-supplied URL is by definition the ``"explicit"`` layer, so
    pre-resolving would relabel every ambient source and break the
    layer-aware diagnostics added for #2740.
    """

    def _assert_no_url_override(self, mock_cls):
        """Assert RegistryIntegration was constructed without a URL argument."""
        assert mock_cls.called, "RegistryIntegration was not constructed"
        for args, kwargs in mock_cls.call_args_list:
            url = args[0] if args else kwargs.get("registry_url")
            assert url is None, (
                f"RegistryIntegration was built with url={url!r}; the client owns "
                "the precedence chain, and a caller-supplied URL is recorded as "
                "the 'explicit' layer no matter where it really came from"
            )

    def test_search_lets_the_client_resolve(self, monkeypatch):
        monkeypatch.setenv("MCP_REGISTRY_URL", "https://env.internal.example.com")
        runner = make_runner()
        with patch_registry(search_result=FAKE_SERVERS) as mock_cls:
            runner.invoke(mcp, ["search", "cool"])
        self._assert_no_url_override(mock_cls)

    def test_show_lets_the_client_resolve(self, monkeypatch):
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        monkeypatch.setattr(
            "apm_cli.config.get_mcp_registry_url",
            lambda *, create_config=True: "https://config.internal.example.com",
        )
        runner = make_runner()
        with patch_registry(detail_result=FAKE_SERVER_DETAIL) as mock_cls:
            runner.invoke(mcp, ["show", "io.github.acme/cool-server"])
        self._assert_no_url_override(mock_cls)

    def test_list_lets_the_client_resolve(self, monkeypatch):
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        monkeypatch.setattr(
            "apm_cli.config.get_mcp_registry_url", lambda *, create_config=True: None
        )
        runner = make_runner()
        with patch_registry(list_result=FAKE_SERVERS) as mock_cls:
            runner.invoke(mcp, ["list"])
        self._assert_no_url_override(mock_cls)

    def test_diagnostic_reports_the_layer_the_client_recorded(self, monkeypatch):
        """The printed source comes from the client, not from a second lookup."""
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        mock_console = MagicMock()
        with patch_registry(list_result=FAKE_SERVERS) as mock_cls:
            mock_cls.return_value.client.registry_url = "https://config.internal.example.com"
            mock_cls.return_value.client.registry_url_source = "config"
            with patch("apm_cli.commands.mcp._get_console", return_value=mock_console):
                make_runner().invoke(mcp, ["list"])
        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "from apm config" in printed
        assert ("https", "config.internal.example.com") in _printed_urls(printed)

    def test_network_error_hint_names_the_config_layer(self, monkeypatch):
        """A user who never exported the env var is not sent to look at it.

        Regression trap: while the command pre-resolved the URL, the client
        recorded ``source="explicit"`` and this hint fell through to the generic
        "may be temporarily unavailable" line.
        """
        import requests as _requests

        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        mock_console = MagicMock()
        with patch_registry() as mock_cls:
            mock_cls.return_value.client.registry_url = "https://config.internal.example.com"
            mock_cls.return_value.client.registry_url_source = "config"
            mock_cls.return_value.list_available_packages.side_effect = _requests.ConnectionError(
                "boom"
            )
            with patch("apm_cli.commands.mcp._get_console", return_value=mock_console):
                result = make_runner().invoke(mcp, ["list"])
        assert result.exit_code == 1
        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "mcp-registry-url is set in apm config" in printed
        assert "MCP_REGISTRY_URL is set" not in printed

    def test_search_diag_line_when_env_var_set(self, monkeypatch):
        """When MCP_REGISTRY_URL is set, search emits a one-line registry diagnostic."""
        monkeypatch.setenv("MCP_REGISTRY_URL", "https://mcp.internal.example.com")
        runner = make_runner()
        mock_console = MagicMock()
        # Model both attributes the command reads back off its client
        # (RegistryIntegration is mocked out).
        with patch_registry(search_result=FAKE_SERVERS) as mock_cls:
            mock_cls.return_value.client.registry_url = "https://mcp.internal.example.com"
            mock_cls.return_value.client.registry_url_source = "env"
            with patch("apm_cli.commands.mcp._get_console", return_value=mock_console):
                runner.invoke(mcp, ["search", "x"])
        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "Registry:" in printed
        assert ("https", "mcp.internal.example.com") in _printed_urls(printed)

    def test_search_no_diag_when_env_var_unset(self, monkeypatch):
        """When MCP_REGISTRY_URL is unset, search stays quiet about the registry URL."""
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        runner = make_runner()
        mock_console = MagicMock()
        with patch_registry(search_result=FAKE_SERVERS) as mock_cls:
            mock_cls.return_value.client.registry_url = "https://api.mcp.github.com"
            with patch("apm_cli.commands.mcp._get_console", return_value=mock_console):
                runner.invoke(mcp, ["search", "x"])
        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "Registry:" not in printed

    def test_search_request_exception_mentions_env_var_when_set(self, monkeypatch):
        """RequestException error path names the URL and hints at MCP_REGISTRY_URL when set."""
        import requests as _requests

        monkeypatch.setenv("MCP_REGISTRY_URL", "https://busted.internal.example.com")
        runner = make_runner()
        mock_console = MagicMock()
        with patch_registry() as mock_cls:
            mock_cls.return_value.client.registry_url = "https://busted.internal.example.com"
            mock_cls.return_value.client.registry_url_source = "env"
            mock_cls.return_value.search_packages.side_effect = _requests.ConnectionError("boom")
            with patch("apm_cli.commands.mcp._get_console", return_value=mock_console):
                result = runner.invoke(mcp, ["search", "x"])
        assert result.exit_code == 1
        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "Could not reach MCP registry" in printed
        assert ("https", "busted.internal.example.com") in _printed_urls(printed)
        assert "MCP_REGISTRY_URL" in printed
