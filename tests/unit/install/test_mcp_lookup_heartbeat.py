"""Unit tests for ``mcp_lookup_heartbeat`` (F4, microsoft/apm#1116).

The MCP registry round-trip in ``apm install`` historically gave no
user-visible signal during the (sometimes multi-second) lookup. This
heartbeat is a single static line emitted before
``operations.validate_servers_exist`` so users see the install moving
forward instead of suspecting a stall.
"""

from unittest.mock import patch

from apm_cli.core.command_logger import InstallLogger
from apm_cli.core.null_logger import NullCommandLogger


@patch("apm_cli.core.command_logger._rich_info")
def test_mcp_lookup_heartbeat_singular(mock_info):
    InstallLogger().mcp_lookup_heartbeat(1)
    msg = mock_info.call_args.args[0]
    assert "1 MCP server in registry" in msg
    assert mock_info.call_args.kwargs.get("symbol") == "running"


@patch("apm_cli.core.command_logger._rich_info")
def test_mcp_lookup_heartbeat_plural(mock_info):
    InstallLogger().mcp_lookup_heartbeat(4)
    msg = mock_info.call_args.args[0]
    assert "4 MCP servers in registry" in msg


@patch("apm_cli.core.command_logger._rich_info")
def test_mcp_lookup_heartbeat_zero_is_silent(mock_info):
    """Zero-count batches must NOT emit a misleading lookup line."""
    InstallLogger().mcp_lookup_heartbeat(0)
    InstallLogger().mcp_lookup_heartbeat(-1)
    assert mock_info.call_count == 0


@patch("apm_cli.core.null_logger._rich_info")
def test_null_logger_mirrors_heartbeat(mock_info):
    """``NullCommandLogger`` ships the same heartbeat so ``MCPIntegrator``
    can call it unconditionally without hasattr/isinstance checks."""
    NullCommandLogger().mcp_lookup_heartbeat(2)
    msg = mock_info.call_args.args[0]
    assert "2 MCP servers in registry" in msg
