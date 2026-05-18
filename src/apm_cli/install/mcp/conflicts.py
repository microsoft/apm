"""MCP CLI flag-conflict matrix (E1-E15).

Extracted from ``commands/install.py`` per the architecture-invariants
LOC budget. ``validate_mcp_conflicts`` is the single chokepoint that
turns invalid ``apm install --mcp`` flag combinations into
``click.UsageError`` (exit 2) before any side-effects fire.
"""

from __future__ import annotations

import click

from .flags import MCPConflictParams

# Mapping for E10: which flags require --mcp.  Keyed by attribute-style
# name so we can read directly from the Click handler locals.
MCP_REQUIRED_FLAGS: tuple[tuple[str, str], ...] = (
    ("transport", "--transport"),
    ("url", "--url"),
    ("env", "--env"),
    ("header", "--header"),
    ("mcp_version", "--mcp-version"),
)


def _validate_requires_flags(params: MCPConflictParams) -> bool:
    if params.mcp_name is not None:
        return False

    flag_values = {
        "transport": params.transport,
        "url": params.url,
        "env": params.env,
        "header": params.headers,
        "mcp_version": params.mcp_version,
        "registry": params.registry_url,
    }
    for attr, label in (*MCP_REQUIRED_FLAGS, ("registry", "--registry")):
        if flag_values.get(attr):
            raise click.UsageError(f"{label} requires --mcp")
    return True


def _validate_name_and_mode_conflicts(params: MCPConflictParams) -> None:
    if params.mcp_name == "":
        raise click.UsageError("MCP name cannot be empty")
    if params.mcp_name.startswith("-"):
        raise click.UsageError("MCP name cannot start with '-'; did you forget a value for --mcp?")
    if params.pre_dash_packages:
        raise click.UsageError("cannot mix --mcp with positional packages")
    if params.global_:
        raise click.UsageError(
            "MCP servers are project-scoped; --global is not supported for MCP entries"
        )
    if params.only == "apm":
        raise click.UsageError("cannot use --only apm with --mcp")
    if params.use_ssh or params.use_https or params.allow_protocol_fallback:
        raise click.UsageError(
            "transport selection flags (--ssh/--https/--allow-protocol-fallback) "
            "don't apply to MCP entries"
        )
    if params.update:
        raise click.UsageError("use 'apm update' instead to update MCP entries")


def _validate_transport_conflicts(params: MCPConflictParams) -> None:
    if params.headers and not params.url:
        raise click.UsageError("--header requires --url")
    if params.url and params.command_argv:
        raise click.UsageError("cannot specify both --url and a stdio command")
    if params.transport == "stdio" and params.url:
        raise click.UsageError("stdio transport doesn't accept --url")
    if params.transport in ("http", "sse", "streamable-http") and params.command_argv:
        raise click.UsageError("remote transports don't accept stdio command")
    if params.env and params.url and not params.command_argv:
        raise click.UsageError("--env applies to stdio MCPs; use --header for remote")


def _validate_registry_conflicts(params: MCPConflictParams) -> None:
    if params.registry_url and (params.url or params.command_argv):
        raise click.UsageError(
            "--registry only applies to registry-resolved MCP servers; "
            "remove --url or the post-`--` stdio command, or drop --registry"
        )


def validate_mcp_conflicts(params: MCPConflictParams | None = None, **kwargs: object) -> None:
    """Apply conflict matrix E1-E15.  Raises ``click.UsageError`` on hit."""
    if params is None:
        params = MCPConflictParams(**kwargs)

    if _validate_requires_flags(params):
        return
    _validate_name_and_mode_conflicts(params)
    _validate_transport_conflicts(params)
    _validate_registry_conflicts(params)
