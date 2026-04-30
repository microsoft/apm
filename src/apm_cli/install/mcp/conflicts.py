"""MCP CLI flag-conflict matrix (E1-E15).

Extracted from ``commands/install.py`` per the architecture-invariants
LOC budget. ``validate_mcp_conflicts`` is the single chokepoint that
turns invalid ``apm install --mcp`` flag combinations into
``click.UsageError`` (exit 2) before any side-effects fire.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Optional, Tuple  # noqa: F401, UP035

import click

# Mapping for E10: which flags require --mcp.  Keyed by attribute-style
# name so we can read directly from the Click handler locals.
MCP_REQUIRED_FLAGS: tuple[tuple[str, str], ...] = (
    ("transport", "--transport"),
    ("url", "--url"),
    ("env", "--env"),
    ("header", "--header"),
    ("mcp_version", "--mcp-version"),
)


def validate_mcp_conflicts(
    *,
    mcp_name: str | None,
    packages: Sequence[str],
    pre_dash_packages: Sequence[str],
    transport: str | None,
    url: str | None,
    env: Mapping[str, str],
    headers: Mapping[str, str],
    mcp_version: str | None,
    command_argv: Sequence[str] | None,
    global_: bool,
    only: str | None,
    update: bool,
    use_ssh: bool,
    use_https: bool,
    allow_protocol_fallback: bool,
    registry_url: str | None = None,
) -> None:
    """Apply conflict matrix E1-E15.  Raises ``click.UsageError`` on hit."""
    # E10: flags require --mcp -- run first so users get the right hint.
    if mcp_name is None:
        flag_values = {
            "transport": transport,
            "url": url,
            "env": env,
            "header": headers,
            "mcp_version": mcp_version,
            "registry": registry_url,
        }
        for attr, label in (*MCP_REQUIRED_FLAGS, ("registry", "--registry")):
            if flag_values.get(attr):
                raise click.UsageError(f"{label} requires --mcp")
        if command_argv:
            # post-`--` stdio command without --mcp: silently allowed today
            # (legacy install behaviour).  Do not error.
            pass
        return

    # E7/E8: NAME shape.
    if mcp_name == "":
        raise click.UsageError("MCP name cannot be empty")
    if mcp_name.startswith("-"):
        raise click.UsageError(f"MCP name cannot start with '-'; did you forget a value for --mcp?")  # noqa: F541

    # E1: positional packages mixed with --mcp.
    if pre_dash_packages:
        raise click.UsageError("cannot mix --mcp with positional packages")

    # E2: --global not supported for MCP entries.
    if global_:
        raise click.UsageError(
            "MCP servers are project-scoped; --global is not supported for MCP entries"
        )

    # E3: --only apm conflicts with --mcp.
    if only == "apm":
        raise click.UsageError("cannot use --only apm with --mcp")

    # E4: transport selection flags do not apply.
    if use_ssh or use_https or allow_protocol_fallback:
        raise click.UsageError(
            "transport selection flags (--ssh/--https/--allow-protocol-fallback) "
            "don't apply to MCP entries"
        )

    # E5: --update is for refreshing, not adding.
    if update:
        raise click.UsageError("use 'apm update' instead to update MCP entries")

    # E9: --header without --url.
    if headers and not url:
        raise click.UsageError("--header requires --url")

    # E11: --url with stdio command.
    if url and command_argv:
        raise click.UsageError("cannot specify both --url and a stdio command")

    # E12: --transport stdio with --url.
    if transport == "stdio" and url:
        raise click.UsageError("stdio transport doesn't accept --url")

    # E13: remote transports with stdio command.
    if transport in ("http", "sse", "streamable-http") and command_argv:
        raise click.UsageError("remote transports don't accept stdio command")

    # E14: --env with --url and no command.
    if env and url and not command_argv:
        raise click.UsageError("--env applies to stdio MCPs; use --header for remote")

    # E15: --registry only applies to registry-resolved entries.
    if registry_url and (url or command_argv):
        raise click.UsageError(
            "--registry only applies to registry-resolved MCP servers; "
            "remove --url or the post-`--` stdio command, or drop --registry"
        )
