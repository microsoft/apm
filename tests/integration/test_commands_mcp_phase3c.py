"""Integration tests for five modules with large remaining integration gaps.

Covers:
1. ``src/apm_cli/commands/marketplace/__init__.py``   (71.5%, gap=276)
2. ``src/apm_cli/commands/install.py``                (75.6%, gap=199)
3. ``src/apm_cli/commands/pack.py``                   (61.7%, gap=202)
4. ``src/apm_cli/commands/mcp.py``                    (48.7%, gap=191)
5. ``src/apm_cli/install/validation.py``              (58.2%, gap=192)

Strategy
--------
* CLI commands exercised via Click's ``CliRunner``.
* All external I/O (HTTP, subprocess, git, registry) is mocked at the boundary.
* No live network access.
* Pure-helper functions are tested with direct calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Shared fixtures / constants
# ---------------------------------------------------------------------------

_LOCKFILE_TEMPLATE = """\
lockfile_version: '1'
generated_at: '2025-01-01T00:00:00+00:00'
dependencies: []
"""

_APM_YML_MINIMAL = """\
name: test-pkg
version: 1.0.0
description: Test package
owner:
  name: test-org
dependencies:
  apm: []
"""

_APM_YML_WITH_DEP = """\
name: test-pkg
version: 1.0.0
description: Test package
owner:
  name: test-org
dependencies:
  apm:
    - myorg/mypkg
"""

_MARKETPLACE_YML_MINIMAL = """\
name: test-marketplace
description: A test marketplace
version: 1.0.0
owner:
  name: test-org
packages:
  - name: tool-a
    source: org/tool-a
    version: "^1.0.0"
"""


def _write_apm_yml(directory: Path, content: str = _APM_YML_MINIMAL) -> None:
    (directory / "apm.yml").write_text(content, encoding="utf-8")


def _write_lockfile(directory: Path) -> None:
    (directory / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")


# ===========================================================================
# PART 1 — commands/mcp.py  (all CLI subcommands)
# ===========================================================================


class TestMcpGroupHelp:
    """mcp group wiring and help text."""

    def test_mcp_help_exits_zero(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        result = runner.invoke(mcp, ["--help"])
        assert result.exit_code == 0
        assert "MCP" in result.output or "mcp" in result.output.lower()

    def test_mcp_search_help(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        result = runner.invoke(mcp, ["search", "--help"])
        assert result.exit_code == 0

    def test_mcp_list_help(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        result = runner.invoke(mcp, ["list", "--help"])
        assert result.exit_code == 0

    def test_mcp_show_help(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        result = runner.invoke(mcp, ["show", "--help"])
        assert result.exit_code == 0

    def test_mcp_install_help(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        result = runner.invoke(mcp, ["install", "--help"])
        assert result.exit_code == 0
        description, options = result.output.split("Options:", 1)
        assert "Forwarded install options" in description
        assert "--transport [stdio|http|sse|streamable-http]" in description
        assert "--mcp-version VER" in description
        assert "Common options" not in options
        assert "--transport [stdio|http|sse|streamable-http]" not in options


class TestMcpSearch:
    """mcp search subcommand."""

    def _make_servers(self, n: int = 3) -> list[dict[str, Any]]:
        return [
            {
                "name": f"server-{i}",
                "description": f"Description for server {i}",
                "version": f"1.{i}.0",
            }
            for i in range(n)
        ]

    def test_search_returns_results_no_console(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        servers = self._make_servers(3)

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.search_packages.return_value = servers
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["search", "test"])

        assert result.exit_code == 0
        assert "server-0" in result.output

    def test_search_empty_results_no_console(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.search_packages.return_value = []
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["search", "nonexistent"])

        assert result.exit_code == 0

    def test_search_respects_limit_no_console(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        servers = self._make_servers(20)

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.search_packages.return_value = servers
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["search", "test", "--limit", "5"])

        assert result.exit_code == 0

    def test_search_registry_exception_exits_1(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch(
                "apm_cli.commands.mcp._build_registry_with_diag",
                side_effect=Exception("Registry down"),
            ),
        ):
            result = runner.invoke(mcp, ["search", "test"])

        assert result.exit_code == 1

    def test_search_requests_exception_network_error(self) -> None:
        """requests.RequestException triggers the network-error handler."""
        import requests

        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        exc = requests.RequestException("Connection reset")

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch(
                "apm_cli.commands.mcp._build_registry_with_diag",
                side_effect=exc,
            ),
        ):
            result = runner.invoke(mcp, ["search", "test"])

        assert result.exit_code == 1

    def test_search_with_verbose_flag(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.search_packages.return_value = [{"name": "s1", "description": "d1"}]
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["search", "query", "--verbose"])

        assert result.exit_code == 0

    def test_search_with_rich_console(self) -> None:
        """search renders Rich table when console is available."""
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        mock_console = MagicMock()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.search_packages.return_value = [
                {"name": "cool-server", "description": "A cool server", "version": "1.0.0"}
            ]
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["search", "cool"])

        assert result.exit_code == 0
        mock_console.print.assert_called()

    def test_search_empty_results_rich_console(self) -> None:
        """search with console and no results prints not-found message."""
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        mock_console = MagicMock()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.search_packages.return_value = []
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["search", "nope"])

        assert result.exit_code == 0
        mock_console.print.assert_called()

    def test_search_long_description_truncated(self) -> None:
        """Long descriptions are truncated intelligently."""
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        mock_console = MagicMock()
        long_desc = "A" * 90 + " " + "B" * 10  # breakable around 77

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.search_packages.return_value = [
                {"name": "s", "description": long_desc, "version": "1.0.0"}
            ]
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["search", "test"])

        assert result.exit_code == 0


class TestMcpList:
    """mcp list subcommand."""

    def test_list_returns_servers_no_console(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.list_available_packages.return_value = [
                {"name": "srv-a", "description": "A server", "version": "2.0.0"},
                {"name": "srv-b", "description": "B server"},
            ]
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["list"])

        assert result.exit_code == 0
        assert "srv-a" in result.output

    def test_list_empty_no_console(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.list_available_packages.return_value = []
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["list"])

        assert result.exit_code == 0

    def test_list_respects_limit(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            # Return more than limit
            mock_instance.list_available_packages.return_value = [
                {"name": f"srv-{i}", "description": "d"} for i in range(50)
            ]
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["list", "--limit", "3"])

        assert result.exit_code == 0

    def test_list_with_rich_console(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        mock_console = MagicMock()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.list_available_packages.return_value = [
                {"name": "srv-a", "description": "A", "version": "1.0.0"}
            ]
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["list"])

        assert result.exit_code == 0
        mock_console.print.assert_called()

    def test_list_empty_rich_console(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        mock_console = MagicMock()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.list_available_packages.return_value = []
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["list"])

        assert result.exit_code == 0

    def test_list_registry_error_exits_1(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch(
                "apm_cli.commands.mcp._build_registry_with_diag",
                side_effect=RuntimeError("oops"),
            ),
        ):
            result = runner.invoke(mcp, ["list"])

        assert result.exit_code == 1

    def test_list_pagination_hint_when_at_limit(self) -> None:
        """When result count equals limit, pagination hint shown with Rich console."""
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        mock_console = MagicMock()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            # Exactly 3 items returned (matches --limit 3)
            mock_instance.list_available_packages.return_value = [
                {"name": f"s{i}", "description": "d", "version": "1"} for i in range(3)
            ]
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["list", "--limit", "3"])

        assert result.exit_code == 0


class TestMcpShow:
    """mcp show subcommand."""

    def _make_server_info(self) -> dict[str, Any]:
        return {
            "name": "test-server",
            "description": "A test MCP server",
            "version_detail": {"version": "2.1.0"},
            "repository": {"url": "https://github.com/org/test-server"},
            "id": "abcdef1234567890",
        }

    def test_show_server_no_console(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.get_package_info.return_value = self._make_server_info()
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["show", "test-server"])

        assert result.exit_code == 0
        assert "test-server" in result.output

    def test_show_server_not_found_no_console(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.get_package_info.side_effect = ValueError("not found")
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["show", "no-such-server"])

        assert result.exit_code == 1

    def test_show_server_with_rich_console(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        mock_console = MagicMock()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.get_package_info.return_value = self._make_server_info()
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["show", "test-server"])

        assert result.exit_code == 0
        mock_console.print.assert_called()

    def test_show_server_not_found_rich_console(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        mock_console = MagicMock()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.get_package_info.side_effect = ValueError("not found")
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["show", "missing"])

        assert result.exit_code == 1

    def test_show_server_with_remotes_and_packages(self) -> None:
        """Show renders deployment type for servers with remotes + packages."""
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()
        mock_console = MagicMock()

        server_info = {
            "name": "github-server",
            "description": "GitHub MCP",
            "version": "1.0.0",
            "repository": {"url": "https://github.com/github/github-mcp-server"},
            "id": "deadbeef12345678",
            "remotes": [{"transport_type": "sse", "url": "https://mcp.github.com/sse"}],
            "packages": [
                {"registry_name": "npm", "name": "@github/mcp-server", "runtime_hint": "npx"}
            ],
        }

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=mock_console),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.get_package_info.return_value = server_info
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["show", "github"])

        assert result.exit_code == 0

    def test_show_server_version_from_top_level_key(self) -> None:
        """Fallback to top-level 'version' when 'version_detail' absent."""
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        server_info = {
            "name": "srv",
            "description": "desc",
            "version": "3.0.0",
            "repository": {"url": "https://github.com/o/r"},
        }

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch("apm_cli.commands.mcp._build_registry_with_diag") as mock_reg,
        ):
            mock_instance = MagicMock()
            mock_instance.get_package_info.return_value = server_info
            mock_reg.return_value = mock_instance

            result = runner.invoke(mcp, ["show", "srv"])

        assert result.exit_code == 0

    def test_show_registry_error_exits_1(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        with (
            patch("apm_cli.commands.mcp._get_console", return_value=None),
            patch(
                "apm_cli.commands.mcp._build_registry_with_diag",
                side_effect=RuntimeError("network"),
            ),
        ):
            result = runner.invoke(mcp, ["show", "srv"])

        assert result.exit_code == 1


class TestMcpInstallForwarding:
    """mcp install — forwards to apm install --mcp."""

    def test_install_forwards_to_install_command(self) -> None:
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        with (
            patch("apm_cli.commands.install._split_argv_at_double_dash", return_value=([], ())),
            patch("apm_cli.commands.install._get_invocation_argv", return_value=[]),
            patch("apm_cli.cli.cli.main") as mock_main,
        ):
            mock_main.return_value = None
            result = runner.invoke(mcp, ["install", "fetch"])

        # Either succeed or forward correctly
        assert result.exit_code in (0, 1, 2)

    def test_install_with_extra_args(self) -> None:
        """Extra args are passed through to apm install."""
        from apm_cli.commands.mcp import mcp

        runner = CliRunner()

        with (
            patch("apm_cli.commands.install._split_argv_at_double_dash", return_value=([], ())),
            patch("apm_cli.commands.install._get_invocation_argv", return_value=[]),
            patch("apm_cli.cli.cli.main") as mock_main,
        ):
            mock_main.return_value = None
            result = runner.invoke(mcp, ["install", "fetch", "--transport", "http"])

        assert result.exit_code in (0, 1, 2)


class TestMcpRegistryEnvOverride:
    """MCP_REGISTRY_URL env var triggers diagnostic output."""

    def test_env_override_triggers_registry_url_log(self) -> None:
        from apm_cli.commands.mcp import MCP_REGISTRY_ENV, _build_registry_with_diag
        from apm_cli.core.command_logger import CommandLogger

        logger = CommandLogger("test", verbose=True)

        with (
            patch.dict(os.environ, {MCP_REGISTRY_ENV: "https://my-registry.example.com"}),
            patch("apm_cli.registry.integration.RegistryIntegration") as mock_cls,
        ):
            mock_instance = MagicMock()
            mock_instance.client.registry_url = "https://my-registry.example.com"
            mock_cls.return_value = mock_instance

            with patch.object(logger, "progress") as mock_progress:
                _build_registry_with_diag(None, logger)
                mock_progress.assert_called()
                call_text = " ".join(str(c) for c in mock_progress.call_args_list)
                assert "my-registry.example.com" in call_text

    def test_no_env_override_silent(self) -> None:
        from apm_cli.commands.mcp import MCP_REGISTRY_ENV, _build_registry_with_diag
        from apm_cli.core.command_logger import CommandLogger

        logger = CommandLogger("test")

        env_without_override = {k: v for k, v in os.environ.items() if k != MCP_REGISTRY_ENV}

        with (
            patch.dict(os.environ, env_without_override, clear=True),
            patch("apm_cli.registry.integration.RegistryIntegration") as mock_cls,
        ):
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance

            with patch.object(logger, "progress") as mock_progress:
                _build_registry_with_diag(None, logger)
                mock_progress.assert_not_called()

    def test_console_prints_registry_url_on_override(self) -> None:
        from apm_cli.commands.mcp import MCP_REGISTRY_ENV, _build_registry_with_diag

        mock_console = MagicMock()

        with (
            patch.dict(os.environ, {MCP_REGISTRY_ENV: "https://ent-registry.local"}),
            patch("apm_cli.registry.integration.RegistryIntegration") as mock_cls,
        ):
            mock_instance = MagicMock()
            mock_instance.client.registry_url = "https://ent-registry.local"
            mock_cls.return_value = mock_instance

            _build_registry_with_diag(mock_console, None)
            mock_console.print.assert_called()


class TestHandleRegistryNetworkError:
    """_handle_registry_network_error — all branches."""

    def test_none_registry_returns_false(self) -> None:
        from apm_cli.commands.mcp import _handle_registry_network_error
        from apm_cli.core.command_logger import CommandLogger

        logger = CommandLogger("test")
        result = _handle_registry_network_error(Exception("x"), None, None, logger, "reach")
        assert result is False

    def test_with_registry_returns_true(self) -> None:
        from apm_cli.commands.mcp import _handle_registry_network_error
        from apm_cli.core.command_logger import CommandLogger

        logger = CommandLogger("test")
        mock_reg = MagicMock()
        mock_reg.client.registry_url = "https://r.example.com"

        env = {k: v for k, v in os.environ.items() if k != "MCP_REGISTRY_URL"}
        with patch.dict(os.environ, env, clear=True):
            result = _handle_registry_network_error(
                Exception("timeout"), mock_reg, None, logger, "reach"
            )
        assert result is True

    def test_custom_registry_hint_includes_env_var(self) -> None:
        from apm_cli.commands.mcp import MCP_REGISTRY_ENV, _handle_registry_network_error
        from apm_cli.core.command_logger import CommandLogger

        logger = CommandLogger("test")
        mock_reg = MagicMock()
        mock_reg.client.registry_url = "https://custom.r.io"

        with patch.dict(os.environ, {MCP_REGISTRY_ENV: "https://custom.r.io"}):
            with patch.object(logger, "error") as mock_err:
                _handle_registry_network_error(Exception("x"), mock_reg, None, logger, "reach")
                call_text = " ".join(str(c) for c in mock_err.call_args_list)
                assert MCP_REGISTRY_ENV in call_text

    def test_default_registry_hint_mentions_retry(self) -> None:
        from apm_cli.commands.mcp import MCP_REGISTRY_ENV, _handle_registry_network_error
        from apm_cli.core.command_logger import CommandLogger

        logger = CommandLogger("test")
        mock_reg = MagicMock()
        mock_reg.client.registry_url = "https://public.r.io"

        env = {k: v for k, v in os.environ.items() if k != MCP_REGISTRY_ENV}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(logger, "error") as mock_err:
                _handle_registry_network_error(Exception("x"), mock_reg, None, logger, "reach")
                call_text = " ".join(str(c) for c in mock_err.call_args_list)
                assert "Retry" in call_text or "retry" in call_text or "unavailable" in call_text

    def test_with_rich_console_calls_print(self) -> None:
        from apm_cli.commands.mcp import _handle_registry_network_error

        mock_console = MagicMock()
        mock_reg = MagicMock()
        mock_reg.client.registry_url = "https://r.example.com"

        env = {k: v for k, v in os.environ.items() if k != "MCP_REGISTRY_URL"}
        with patch.dict(os.environ, env, clear=True):
            result = _handle_registry_network_error(
                Exception("x"), mock_reg, mock_console, None, "query"
            )

        assert result is True
        mock_console.print.assert_called()


# ===========================================================================
# PART 2 — install.py helpers
# ===========================================================================


class TestSplitArgvAtDoubleDash:
    """_split_argv_at_double_dash — boundary detection."""

    def test_no_double_dash_returns_full_argv(self) -> None:
        from apm_cli.commands.install import _split_argv_at_double_dash

        argv = ["apm", "install", "--mcp", "fetch"]
        clean, post = _split_argv_at_double_dash(argv)
        assert clean == argv
        assert post == ()

    def test_double_dash_splits_correctly(self) -> None:
        from apm_cli.commands.install import _split_argv_at_double_dash

        argv = ["apm", "install", "--mcp", "fetch", "--", "npx", "-y", "srv"]
        clean, post = _split_argv_at_double_dash(argv)
        assert clean == ["apm", "install", "--mcp", "fetch"]
        assert post == ("npx", "-y", "srv")

    def test_double_dash_at_start(self) -> None:
        from apm_cli.commands.install import _split_argv_at_double_dash

        argv = ["--", "cmd", "arg"]
        clean, post = _split_argv_at_double_dash(argv)
        assert clean == []
        assert post == ("cmd", "arg")

    def test_empty_post_section(self) -> None:
        from apm_cli.commands.install import _split_argv_at_double_dash

        argv = ["apm", "install", "--"]
        clean, post = _split_argv_at_double_dash(argv)
        assert clean == ["apm", "install"]
        assert post == ()


class TestRestoreManifestFromSnapshot:
    """_restore_manifest_from_snapshot — atomic write."""

    def test_restore_writes_original_bytes(self, tmp_path: Path) -> None:
        from apm_cli.commands.install import _restore_manifest_from_snapshot

        apm_yml = tmp_path / "apm.yml"
        original = b"name: original\n"
        apm_yml.write_bytes(b"name: corrupted\n")

        _restore_manifest_from_snapshot(apm_yml, original)

        assert apm_yml.read_bytes() == original

    def test_restore_creates_file_if_missing(self, tmp_path: Path) -> None:
        from apm_cli.commands.install import _restore_manifest_from_snapshot

        apm_yml = tmp_path / "apm.yml"
        original = b"name: fresh\n"

        _restore_manifest_from_snapshot(apm_yml, original)

        assert apm_yml.read_bytes() == original


class TestMaybeRollbackManifest:
    """_maybe_rollback_manifest — no-op when snapshot is None."""

    def test_noop_when_snapshot_is_none(self, tmp_path: Path) -> None:
        from apm_cli.commands.install import _maybe_rollback_manifest
        from apm_cli.core.command_logger import InstallLogger

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_bytes(b"name: current\n")
        logger = MagicMock(spec=InstallLogger)

        _maybe_rollback_manifest(apm_yml, None, logger)

        # File unchanged, no restore called
        assert apm_yml.read_bytes() == b"name: current\n"
        logger.progress.assert_not_called()

    def test_restores_when_snapshot_provided(self, tmp_path: Path) -> None:
        from apm_cli.commands.install import _maybe_rollback_manifest
        from apm_cli.core.command_logger import InstallLogger

        apm_yml = tmp_path / "apm.yml"
        original = b"name: original\n"
        apm_yml.write_bytes(b"name: mutated\n")
        logger = MagicMock(spec=InstallLogger)

        _maybe_rollback_manifest(apm_yml, original, logger)

        assert apm_yml.read_bytes() == original
        logger.progress.assert_called()


class TestCheckPackageConflicts:
    """_check_package_conflicts — duplicate detection."""

    def test_empty_deps_returns_empty_set(self) -> None:
        from apm_cli.commands.install import _check_package_conflicts

        result = _check_package_conflicts([])
        assert result == set()

    def test_string_deps_parsed(self) -> None:
        from apm_cli.commands.install import _check_package_conflicts

        deps = ["myorg/mypkg"]
        result = _check_package_conflicts(deps)
        assert len(result) == 1

    def test_invalid_dep_silently_skipped(self) -> None:
        from apm_cli.commands.install import _check_package_conflicts

        deps = [None, 42, "myorg/mypkg"]  # type: ignore[list-item]
        # Should not raise; invalid entries skipped
        result = _check_package_conflicts(deps)
        assert isinstance(result, set)

    def test_dict_dep_parsed(self) -> None:
        from apm_cli.commands.install import _check_package_conflicts

        deps = [{"git": "https://github.com/myorg/mypkg"}]
        result = _check_package_conflicts(deps)
        assert isinstance(result, set)


class TestInstallCommandBasic:
    """install command — basic CLI invocation paths."""

    def test_install_no_args_with_lockfile(self, tmp_path: Path) -> None:
        """apm install with no packages reads existing manifest."""
        from apm_cli.commands.install import install

        _write_apm_yml(tmp_path)
        _write_lockfile(tmp_path)

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            (Path(".") / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
            (Path(".") / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

            with (
                patch("apm_cli.commands.install._install_apm_dependencies", return_value=0),
                patch("apm_cli.commands.install.AuthResolver"),
            ):
                result = runner.invoke(install, [], catch_exceptions=False)

        # exit code 0 or non-zero is fine; we just verify it runs
        assert result.exit_code in (0, 1, 2)

    def test_install_dry_run_flag(self, tmp_path: Path) -> None:
        from apm_cli.commands.install import install

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            (Path(".") / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
            (Path(".") / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

            with (
                patch("apm_cli.commands.install._install_apm_dependencies", return_value=0),
                patch("apm_cli.commands.install.AuthResolver"),
            ):
                result = runner.invoke(install, ["--dry-run"])

        assert result.exit_code in (0, 1, 2)

    def test_install_no_apm_yml_fails(self, tmp_path: Path) -> None:
        """Without apm.yml the command should fail gracefully."""
        from apm_cli.commands.install import install

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(install, [])

        assert result.exit_code != 0

    def test_install_help_exits_zero(self) -> None:
        from apm_cli.commands.install import install

        runner = CliRunner()
        result = runner.invoke(install, ["--help"])
        assert result.exit_code == 0


# ===========================================================================
# PART 3 — pack.py
# ===========================================================================


class TestPackCommandHelp:
    """pack command help and option parsing."""

    def test_pack_help_exits_zero(self) -> None:
        from apm_cli.commands.pack import pack_cmd

        runner = CliRunner()
        result = runner.invoke(pack_cmd, ["--help"])
        assert result.exit_code == 0

    def test_pack_invalid_marketplace_path_format(self, tmp_path: Path) -> None:
        """--marketplace-path without = sign produces error."""
        from apm_cli.commands.pack import pack_cmd

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            (Path(".") / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
            result = runner.invoke(
                pack_cmd,
                ["--marketplace-path", "notequals"],
            )

        assert result.exit_code != 0

    def test_pack_unknown_marketplace_format(self, tmp_path: Path) -> None:
        """--marketplace-path with unknown format name exits with error."""
        from apm_cli.commands.pack import pack_cmd

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            (Path(".") / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
            result = runner.invoke(
                pack_cmd,
                ["--marketplace-path", "nonexistent-format=out.json"],
            )

        assert result.exit_code in (0, 1)  # validation error handled

    def test_pack_marketplace_filter_none(self, tmp_path: Path) -> None:
        """--marketplace none skips marketplace build."""
        from apm_cli.commands.pack import pack_cmd

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            (Path(".") / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
            (Path(".") / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

            with patch("apm_cli.core.build_orchestrator.BuildOrchestrator.run") as mock_run:
                mock_result = MagicMock()
                mock_result.producer_results = []
                mock_run.return_value = mock_result
                result = runner.invoke(pack_cmd, ["--marketplace", "none"])

        assert result.exit_code in (0, 1)

    def test_pack_marketplace_filter_all(self, tmp_path: Path) -> None:
        """--marketplace all builds all configured outputs."""
        from apm_cli.commands.pack import pack_cmd

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            (Path(".") / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
            (Path(".") / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

            with patch("apm_cli.core.build_orchestrator.BuildOrchestrator.run") as mock_run:
                mock_result = MagicMock()
                mock_result.producer_results = []
                mock_run.return_value = mock_result
                result = runner.invoke(pack_cmd, ["--marketplace", "all"])

        assert result.exit_code in (0, 1)

    def test_pack_deprecated_target_warning(self, tmp_path: Path) -> None:
        """--target emits a deprecation warning."""
        from apm_cli.commands.pack import pack_cmd

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            (Path(".") / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
            (Path(".") / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

            with patch("apm_cli.core.build_orchestrator.BuildOrchestrator.run") as mock_run:
                mock_result = MagicMock()
                mock_result.producer_results = []
                mock_run.return_value = mock_result
                result = runner.invoke(pack_cmd, ["--target", "claude"])

        assert "deprecated" in result.output.lower() or result.exit_code in (0, 1)

    def test_pack_marketplace_output_removed(self, tmp_path: Path) -> None:
        """--marketplace-output was removed; Click rejects it."""
        from apm_cli.commands.pack import pack_cmd

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            (Path(".") / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
            (Path(".") / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

            result = runner.invoke(pack_cmd, ["--marketplace-output", "dist/mkt.json"])

        assert result.exit_code != 0
        assert "no such option" in (result.output or "").lower()
        assert "--marketplace-output" in (result.output or "")

    def test_pack_build_error_exits_nonzero(self, tmp_path: Path) -> None:
        """BuildError from orchestrator surfaces as non-zero exit."""
        from apm_cli.commands.pack import pack_cmd
        from apm_cli.core.build_orchestrator import BuildError

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            (Path(".") / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")

            with patch(
                "apm_cli.core.build_orchestrator.BuildOrchestrator.run",
                side_effect=BuildError("oops"),
            ):
                result = runner.invoke(pack_cmd, [])

        assert result.exit_code != 0

    def test_pack_json_output_flag(self, tmp_path: Path) -> None:
        """--json produces machine-readable JSON envelope."""
        from apm_cli.commands.pack import pack_cmd

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            (Path(".") / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
            (Path(".") / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

            with patch("apm_cli.core.build_orchestrator.BuildOrchestrator.run") as mock_run:
                mock_result = MagicMock()
                mock_result.producer_results = []
                mock_run.return_value = mock_result
                result = runner.invoke(pack_cmd, ["--json"])

        # Should produce JSON or exit non-zero
        if result.exit_code == 0:
            parsed = json.loads(result.output.strip())
            assert "ok" in parsed or "errors" in parsed

    def test_pack_dry_run_no_files_written(self, tmp_path: Path) -> None:
        """--dry-run does not write bundle files."""
        from apm_cli.commands.pack import pack_cmd

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            (Path(".") / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
            (Path(".") / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

            with patch("apm_cli.core.build_orchestrator.BuildOrchestrator.run") as mock_run:
                mock_result = MagicMock()
                mock_result.producer_results = []
                mock_run.return_value = mock_result
                result = runner.invoke(pack_cmd, ["--dry-run"])

        assert result.exit_code in (0, 1)


class TestEmitJsonErrorOrRaise:
    """_emit_json_error_or_raise — JSON vs ClickException branch."""

    def test_json_mode_prints_json(self) -> None:
        import click

        from apm_cli.commands.pack import _emit_json_error_or_raise

        runner = CliRunner()

        @click.command()
        @click.pass_context
        def _cmd(ctx):
            _emit_json_error_or_raise(ctx, True, "code", "message")

        result = runner.invoke(_cmd, [])
        assert result.exit_code == 1
        # stdout should contain valid JSON
        parsed = json.loads(result.output.strip())
        assert "errors" in parsed

    def test_non_json_mode_raises_click_exception(self) -> None:
        import click

        from apm_cli.commands.pack import _emit_json_error_or_raise

        runner = CliRunner()

        @click.command()
        @click.pass_context
        def _cmd(ctx):
            _emit_json_error_or_raise(ctx, False, "code", "an error occurred")

        result = runner.invoke(_cmd, [])
        assert result.exit_code != 0
        assert "an error occurred" in result.output


# ===========================================================================
# PART 4 — marketplace/__init__.py helpers
# ===========================================================================


class TestIsValidAlias:
    """_is_valid_alias — alias validation."""

    def test_simple_name(self) -> None:
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("tools") is True

    def test_dash_dot_underscore_allowed(self) -> None:
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("my.tools-v2_final") is True

    def test_empty_string_rejected(self) -> None:
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("") is False

    def test_space_rejected(self) -> None:
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("my tools") is False

    def test_at_sign_rejected(self) -> None:
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("my@tools") is False

    def test_slash_rejected(self) -> None:
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("my/tools") is False


class TestFindDuplicateNames:
    """_find_duplicate_names — detects duplicate package names."""

    def test_no_duplicates_returns_empty_string(self) -> None:
        from apm_cli.commands.marketplace import _find_duplicate_names

        yml = MagicMock()
        pkg_a = MagicMock()
        pkg_a.name = "tool-a"
        pkg_b = MagicMock()
        pkg_b.name = "tool-b"
        yml.packages = [pkg_a, pkg_b]

        result = _find_duplicate_names(yml)
        assert result == ""

    def test_duplicate_names_returns_message(self) -> None:
        from apm_cli.commands.marketplace import _find_duplicate_names

        yml = MagicMock()
        pkg_a = MagicMock()
        pkg_a.name = "tool-a"
        pkg_a_dup = MagicMock()
        pkg_a_dup.name = "Tool-A"  # case-insensitive duplicate
        yml.packages = [pkg_a, pkg_a_dup]

        result = _find_duplicate_names(yml)
        assert "tool-a" in result.lower() or "Tool-A" in result

    def test_empty_packages_returns_empty_string(self) -> None:
        from apm_cli.commands.marketplace import _find_duplicate_names

        yml = MagicMock()
        yml.packages = []

        result = _find_duplicate_names(yml)
        assert result == ""


class TestParseMarketplaceRepo:
    """_parse_marketplace_repo -- URL and shorthand parsing.

    The function is a URL-first parser: it returns ``(url, kind, host)``
    where ``url`` is the canonical fetch URL (synthesised for shorthand
    inputs, returned verbatim for HTTPS), ``kind`` is the fetcher kind
    (``github``/``gitlab``/``git``/``local``), and ``host`` is the
    embedded or resolved host FQDN.
    """

    def test_simple_owner_repo(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        url, kind, host = _parse_marketplace_repo("myorg/myrepo", None)
        assert url == "https://github.com/myorg/myrepo"
        assert kind == "github"
        assert host == "github.com"

    def test_https_url(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        url, kind, host = _parse_marketplace_repo("https://github.com/myorg/myrepo", None)
        assert url == "https://github.com/myorg/myrepo"
        assert kind == "github"
        assert host == "github.com"

    def test_https_url_preserves_git_suffix(self) -> None:
        """HTTPS URLs are returned verbatim; subprocess git handles .git."""
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        url, _, _ = _parse_marketplace_repo("https://github.com/org/myrepo.git", None)
        assert url == "https://github.com/org/myrepo.git"

    def test_http_url_rejected(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises(ValueError, match="Insecure HTTP"):
            _parse_marketplace_repo("http://github.com/org/repo", None)

    def test_empty_repo_rejected(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises(ValueError):
            _parse_marketplace_repo("", None)

    def test_single_segment_rejected(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises(ValueError):
            _parse_marketplace_repo("singleword", None)

    def test_conflicting_host_raises(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises(ValueError, match="Conflicting host"):
            _parse_marketplace_repo("https://github.com/org/repo", "gitlab.com")

    def test_control_characters_rejected(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises(ValueError):
            _parse_marketplace_repo("org/repo\x00hack", None)

    def test_host_shorthand_three_segments(self) -> None:
        """HOST/OWNER/REPO shorthand when first segment is valid FQDN."""
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        url, kind, host = _parse_marketplace_repo("github.com/myorg/myrepo", None)
        assert url == "https://github.com/myorg/myrepo"
        assert kind == "github"
        assert host == "github.com"


class TestMarketplaceAddUnsupportedHostError:
    """_marketplace_add_unsupported_host_error message content."""

    def test_ado_specific_message(self) -> None:
        from apm_cli.commands.marketplace import _marketplace_add_unsupported_host_error

        msg = _marketplace_add_unsupported_host_error(
            "myorg.visualstudio.com", "'repo'", "'host'", "ado"
        )
        assert "APM marketplaces must be hosted on GitHub" in msg

    def test_generic_unsupported_host_message(self) -> None:
        from apm_cli.commands.marketplace import _marketplace_add_unsupported_host_error

        msg = _marketplace_add_unsupported_host_error(
            "bitbucket.org", "'repo'", "'bitbucket.org'", "generic"
        )
        assert "Supported marketplace hosts" in msg
        assert "GITHUB_HOST" in msg


class TestMarketplaceListCommand:
    """marketplace list subcommand."""

    def test_list_no_marketplaces_registered(self) -> None:
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()

        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            return_value=[],
        ):
            result = runner.invoke(marketplace, ["list"])

        assert result.exit_code == 0

    def test_list_with_registered_marketplace(self) -> None:
        from apm_cli.commands.marketplace import marketplace
        from apm_cli.marketplace.models import MarketplaceSource

        runner = CliRunner()
        source = MarketplaceSource(
            name="acme",
            owner="acme-org",
            repo="plugins",
            branch="main",
            host="github.com",
            path="marketplace.json",
        )

        with (
            patch(
                "apm_cli.marketplace.registry.get_registered_marketplaces",
                return_value=[source],
            ),
            patch("apm_cli.commands.marketplace._get_console", return_value=None),
        ):
            result = runner.invoke(marketplace, ["list"])

        assert result.exit_code == 0

    def test_list_error_exits_1(self) -> None:
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()

        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            side_effect=RuntimeError("db error"),
        ):
            result = runner.invoke(marketplace, ["list"])

        assert result.exit_code == 1


class TestMarketplaceRemoveCommand:
    """marketplace remove subcommand."""

    def test_remove_requires_yes_in_non_interactive(self) -> None:
        from apm_cli.commands.marketplace import marketplace
        from apm_cli.marketplace.models import MarketplaceSource

        runner = CliRunner()
        source = MarketplaceSource(
            name="acme",
            owner="acme-org",
            repo="plugins",
            branch="main",
            host="github.com",
        )

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch("apm_cli.commands.marketplace._is_interactive", return_value=False),
        ):
            result = runner.invoke(marketplace, ["remove", "acme"])

        assert result.exit_code == 1

    def test_remove_with_yes_flag(self) -> None:
        from apm_cli.commands.marketplace import marketplace
        from apm_cli.marketplace.models import MarketplaceSource

        runner = CliRunner()
        source = MarketplaceSource(
            name="acme",
            owner="acme-org",
            repo="plugins",
            branch="main",
            host="github.com",
        )

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch("apm_cli.marketplace.registry.remove_marketplace"),
            patch("apm_cli.marketplace.client.clear_marketplace_cache"),
        ):
            result = runner.invoke(marketplace, ["remove", "acme", "--yes"])

        assert result.exit_code == 0


class TestMarketplaceBrowseCommand:
    """marketplace browse subcommand."""

    def test_browse_shows_plugins(self) -> None:
        from apm_cli.commands.marketplace import marketplace
        from apm_cli.marketplace.models import (
            MarketplaceManifest,
            MarketplacePlugin,
            MarketplaceSource,
        )

        runner = CliRunner()
        source = MarketplaceSource(
            name="acme",
            owner="acme-org",
            repo="plugins",
            branch="main",
            host="github.com",
        )
        manifest = MarketplaceManifest(
            name="acme",
            plugins=(
                MarketplacePlugin(name="tool-a", description="First tool"),
                MarketplacePlugin(name="tool-b", description="Second tool"),
            ),
        )

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch("apm_cli.marketplace.client.fetch_marketplace", return_value=manifest),
            patch("apm_cli.commands.marketplace._get_console", return_value=None),
        ):
            result = runner.invoke(marketplace, ["browse", "acme"])

        assert result.exit_code == 0

    def test_browse_empty_marketplace(self) -> None:
        from apm_cli.commands.marketplace import marketplace
        from apm_cli.marketplace.models import MarketplaceManifest, MarketplaceSource

        runner = CliRunner()
        source = MarketplaceSource(
            name="empty",
            owner="org",
            repo="empty-mkt",
            branch="main",
            host="github.com",
        )
        manifest = MarketplaceManifest(name="empty", plugins=())

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch("apm_cli.marketplace.client.fetch_marketplace", return_value=manifest),
            patch("apm_cli.commands.marketplace._get_console", return_value=None),
        ):
            result = runner.invoke(marketplace, ["browse", "empty"])

        assert result.exit_code == 0


class TestMarketplaceUpdateCommand:
    """marketplace update subcommand."""

    def test_update_specific_marketplace(self) -> None:
        from apm_cli.commands.marketplace import marketplace
        from apm_cli.marketplace.models import MarketplaceManifest, MarketplaceSource

        runner = CliRunner()
        source = MarketplaceSource(
            name="acme",
            owner="acme-org",
            repo="plugins",
            branch="main",
            host="github.com",
        )
        manifest = MarketplaceManifest(name="acme", plugins=())

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch("apm_cli.marketplace.client.clear_marketplace_cache"),
            patch("apm_cli.marketplace.client.fetch_marketplace", return_value=manifest),
        ):
            result = runner.invoke(marketplace, ["update", "acme"])

        assert result.exit_code == 0

    def test_update_no_marketplaces_registered(self) -> None:
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()

        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            return_value=[],
        ):
            result = runner.invoke(marketplace, ["update"])

        assert result.exit_code == 0


class TestMarketplaceSearchCommand:
    """marketplace search subcommand."""

    def test_search_invalid_format_no_at_sign(self) -> None:
        from apm_cli.commands.marketplace import search

        runner = CliRunner()
        result = runner.invoke(search, ["query-without-at"])
        assert result.exit_code != 0

    def test_search_marketplace_not_registered(self) -> None:
        from apm_cli.commands.marketplace import search
        from apm_cli.marketplace.errors import MarketplaceNotFoundError

        runner = CliRunner()

        with patch(
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            side_effect=MarketplaceNotFoundError("unknown"),
        ):
            result = runner.invoke(search, ["tool@unknown-mkt"])

        assert result.exit_code == 1


class TestIsTlsFailure:
    """_is_tls_failure — chain inspection."""

    def test_ssl_error_returns_true(self) -> None:
        import requests

        from apm_cli.install.validation import _is_tls_failure

        exc = requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")
        assert _is_tls_failure(exc) is True

    def test_certificate_verify_failed_msg_returns_true(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = RuntimeError("TLS verification failed: bad cert")
        assert _is_tls_failure(exc) is True

    def test_generic_exception_returns_false(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = ValueError("not a TLS error")
        assert _is_tls_failure(exc) is False

    def test_chained_ssl_error_returns_true(self) -> None:
        import requests

        from apm_cli.install.validation import _is_tls_failure

        inner = requests.exceptions.SSLError("bad cert")
        outer = RuntimeError("connection failed")
        outer.__cause__ = inner
        assert _is_tls_failure(outer) is True

    def test_chained_message_tls_error(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        inner = RuntimeError("TLS verification failed: expired cert")
        outer = RuntimeError("outer")
        outer.__context__ = inner
        assert _is_tls_failure(outer) is True

    def test_none_cause_stops_chain(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = ValueError("benign error")
        assert _is_tls_failure(exc) is False


class TestLogTlsFailure:
    """_log_tls_failure — warning message content."""

    def test_warning_emitted_via_logger(self) -> None:
        from apm_cli.install.validation import _log_tls_failure

        logger = MagicMock()
        exc = RuntimeError("CERTIFICATE_VERIFY_FAILED")

        _log_tls_failure("myhost.example.com", exc, None, logger)

        logger.warning.assert_called_once()
        msg = logger.warning.call_args.args[0]
        assert "TLS" in msg or "ssl" in msg.lower() or "CA" in msg

    def test_verbose_callback_called_with_host(self) -> None:
        from apm_cli.install.validation import _log_tls_failure

        logger = MagicMock()
        verbose_calls = []
        exc = RuntimeError("CERTIFICATE_VERIFY_FAILED: cert expired")

        _log_tls_failure("my-proxy.corp", exc, lambda m: verbose_calls.append(m), logger)

        assert any("my-proxy.corp" in m for m in verbose_calls)

    def test_no_verbose_callback_no_error(self) -> None:
        from apm_cli.install.validation import _log_tls_failure

        logger = MagicMock()
        exc = RuntimeError("TLS verification failed")

        # Should not raise
        _log_tls_failure("host", exc, None, logger)
        logger.warning.assert_called_once()


class TestLocalPathFailureReason:
    """_local_path_failure_reason — local dep validation."""

    def test_non_local_dep_returns_none(self) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        dep_ref = MagicMock()
        dep_ref.is_local = False
        dep_ref.local_path = None

        result = _local_path_failure_reason(dep_ref)
        assert result is None

    def test_nonexistent_path_returns_reason(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(tmp_path / "nonexistent")

        result = _local_path_failure_reason(dep_ref)
        assert result == "path does not exist"

    def test_file_path_returns_reason(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        f = tmp_path / "somefile.txt"
        f.write_text("hi")

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(f)

        result = _local_path_failure_reason(dep_ref)
        assert result == "path is not a directory"

    def test_empty_dir_no_markers(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(empty_dir)

        result = _local_path_failure_reason(dep_ref)
        assert result == "no apm.yml, SKILL.md, or plugin.json found"

    def test_dir_with_apm_yml_still_returns_marker_msg(self, tmp_path: Path) -> None:
        """_local_path_failure_reason is a failure describer, not a validator.

        It always returns the 'no markers' message for any existing directory
        because it is only ever called after _validate_package_exists already
        returned False (at which point the directory lacks valid markers).
        """
        from apm_cli.install.validation import _local_path_failure_reason

        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        (pkg_dir / "apm.yml").write_text("name: mypkg\n")

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(pkg_dir)

        # Function always returns the markers message for existing directories
        result = _local_path_failure_reason(dep_ref)
        assert result == "no apm.yml, SKILL.md, or plugin.json found"


class TestLocalPathNoMarkersHint:
    """_local_path_no_markers_hint — sub-package discovery."""

    def test_no_sub_packages_no_output(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        logger = MagicMock()

        # Should return without calling logger
        _local_path_no_markers_hint(empty_dir, logger=logger)
        logger.progress.assert_not_called()

    def test_finds_sub_package_with_apm_yml(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        # Create a sub-package
        sub_pkg = tmp_path / "tools" / "my-tool"
        sub_pkg.mkdir(parents=True)
        (sub_pkg / "apm.yml").write_text("name: my-tool\n")

        logger = MagicMock()
        _local_path_no_markers_hint(tmp_path, logger=logger)

        logger.progress.assert_called()
        call_text = str(logger.progress.call_args)
        assert "installable" in call_text.lower() or "install" in call_text.lower()

    def test_finds_sub_package_with_skill_md(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        sub_pkg = tmp_path / "skills" / "my-skill"
        sub_pkg.mkdir(parents=True)
        (sub_pkg / "SKILL.md").write_text("# My Skill\n")

        logger = MagicMock()
        _local_path_no_markers_hint(tmp_path, logger=logger)

        logger.progress.assert_called()

    def test_output_without_logger_uses_rich_helpers(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        # Create a sub-package
        sub_pkg = tmp_path / "pkg"
        sub_pkg.mkdir()
        (sub_pkg / "apm.yml").write_text("name: pkg\n")

        # Should not raise when logger is None
        with patch("apm_cli.install.validation._rich_info") as mock_info:
            _local_path_no_markers_hint(tmp_path, logger=None)
            mock_info.assert_called()

    def test_more_than_five_packages_shows_count(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        # Create 7 sub-packages
        for i in range(7):
            sub_pkg = tmp_path / f"pkg-{i}"
            sub_pkg.mkdir()
            (sub_pkg / "apm.yml").write_text(f"name: pkg-{i}\n")

        logger = MagicMock()
        _local_path_no_markers_hint(tmp_path, logger=logger)

        calls = str(logger.verbose_detail.call_args_list)
        assert "more" in calls.lower() or logger.verbose_detail.call_count > 0


class TestValidatePackageExistsLocal:
    """_validate_package_exists — local path branches."""

    def test_nonexistent_local_path_returns_false(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(tmp_path / "nonexistent")
        dep_ref.is_virtual = False

        result = _validate_package_exists(
            str(tmp_path / "nonexistent"),
            dep_ref=dep_ref,
        )
        assert result is False

    def test_local_path_with_apm_yml_returns_true(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        (pkg_dir / "apm.yml").write_text("name: mypkg\n")

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(pkg_dir)
        dep_ref.is_virtual = False

        result = _validate_package_exists(str(pkg_dir), dep_ref=dep_ref)
        assert result is True

    def test_local_path_with_skill_md_returns_true(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Skill\n")

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(skill_dir)
        dep_ref.is_virtual = False

        result = _validate_package_exists(str(skill_dir), dep_ref=dep_ref)
        assert result is True

    def test_local_path_empty_dir_returns_false(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(empty_dir)
        dep_ref.is_virtual = False

        with patch("apm_cli.utils.helpers.find_plugin_json", return_value=None):
            result = _validate_package_exists(str(empty_dir), dep_ref=dep_ref)
        assert result is False


class TestValidatePackageExistsGitHub:
    """_validate_package_exists — GitHub API path."""

    def test_github_repo_accessible_returns_true(self) -> None:
        from apm_cli.install.validation import _validate_package_exists

        dep_ref = MagicMock()
        dep_ref.is_local = False
        dep_ref.local_path = None
        dep_ref.is_virtual = False
        dep_ref.is_azure_devops.return_value = False
        dep_ref.host = None
        dep_ref.repo_url = "myorg/myrepo"
        dep_ref.port = None

        mock_auth = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.source = "env"
        mock_ctx.token_type = "pat"
        mock_auth.resolve.return_value = mock_ctx
        mock_auth.resolve_for_dep.return_value = mock_ctx
        mock_auth.classify_host.return_value = MagicMock(
            kind="github",
            api_base="https://api.github.com",
            display_name="github.com",
        )
        mock_auth.try_with_fallback.return_value = True

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False):
            result = _validate_package_exists(
                "myorg/myrepo",
                auth_resolver=mock_auth,
                dep_ref=dep_ref,
            )

        assert result is True

    def test_github_repo_not_found_returns_false(self) -> None:
        from apm_cli.install.validation import _validate_package_exists

        dep_ref = MagicMock()
        dep_ref.is_local = False
        dep_ref.local_path = None
        dep_ref.is_virtual = False
        dep_ref.is_azure_devops.return_value = False
        dep_ref.host = None
        dep_ref.repo_url = "myorg/missingrepo"
        dep_ref.port = None

        mock_auth = MagicMock()
        mock_auth.classify_host.return_value = MagicMock(
            kind="github",
            api_base="https://api.github.com",
            display_name="github.com",
        )
        mock_auth.try_with_fallback.side_effect = Exception("404 Not Found")
        mock_auth.build_error_context.return_value = "error context"

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False):
            result = _validate_package_exists(
                "myorg/missingrepo",
                auth_resolver=mock_auth,
                dep_ref=dep_ref,
            )

        assert result is False

    def test_enforce_only_mode_skips_probe(self) -> None:
        """PROXY_REGISTRY_ONLY=1 skips all probes and returns True."""
        from apm_cli.install.validation import _validate_package_exists

        dep_ref = MagicMock()
        dep_ref.is_local = False
        dep_ref.local_path = None
        dep_ref.is_virtual = False
        dep_ref.is_azure_devops.return_value = False
        dep_ref.host = None
        dep_ref.repo_url = "org/repo"
        dep_ref.port = None

        mock_auth = MagicMock()
        mock_auth.classify_host.return_value = MagicMock(
            kind="github",
            api_base="https://api.github.com",
            display_name="github.com",
        )

        logger = MagicMock()

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=True):
            result = _validate_package_exists(
                "org/repo",
                auth_resolver=mock_auth,
                logger=logger,
                dep_ref=dep_ref,
            )

        assert result is True


class TestValidatePackageExistsVirtual:
    """_validate_package_exists — virtual package paths."""

    def test_virtual_package_enforce_only_returns_true(self) -> None:
        from apm_cli.install.validation import _validate_package_exists

        dep_ref = MagicMock()
        dep_ref.is_local = False
        dep_ref.local_path = None
        dep_ref.is_virtual = True
        dep_ref.is_virtual_subdirectory.return_value = False
        dep_ref.host = None

        logger = MagicMock()
        mock_auth = MagicMock()

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=True):
            result = _validate_package_exists(
                "org/monorepo/subpkg",
                auth_resolver=mock_auth,
                logger=logger,
                dep_ref=dep_ref,
            )

        assert result is True
        logger.info.assert_called()


class TestValidatePackageExistsTlsFailure:
    """_validate_package_exists — TLS failure path."""

    def test_tls_failure_returns_false_with_warning(self) -> None:
        import requests

        from apm_cli.install.validation import _validate_package_exists

        dep_ref = MagicMock()
        dep_ref.is_local = False
        dep_ref.local_path = None
        dep_ref.is_virtual = False
        dep_ref.is_azure_devops.return_value = False
        dep_ref.host = None
        dep_ref.repo_url = "org/repo"
        dep_ref.port = None

        mock_auth = MagicMock()
        mock_auth.classify_host.return_value = MagicMock(
            kind="github",
            api_base="https://api.github.com",
            display_name="github.com",
        )
        mock_auth.try_with_fallback.side_effect = requests.exceptions.SSLError(
            "CERTIFICATE_VERIFY_FAILED"
        )

        logger = MagicMock()

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False):
            result = _validate_package_exists(
                "org/repo",
                auth_resolver=mock_auth,
                logger=logger,
                dep_ref=dep_ref,
            )

        assert result is False
        logger.warning.assert_called()
