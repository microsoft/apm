"""Shared MCP request-identity dataclass."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class MCPRequestSpec:
    """Shared MCP request-identity fields common to install + conflict validation."""

    mcp_name: str | None
    transport: str | None
    url: str | None
    mcp_version: str | None
    command_argv: Sequence[str] | None
    registry_url: str | None = None
