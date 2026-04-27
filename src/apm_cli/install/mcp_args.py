"""MCP CLI argument parsing for ``--env`` and ``--header`` repetitions.

Extracted from ``commands/install.py`` per the architecture-invariants
LOC budget (sibling to ``mcp_warnings.py`` / ``mcp_registry.py``).
"""

from __future__ import annotations

import click


def parse_kv_pairs(pairs, *, flag_name):
    """Parse a tuple of ``KEY=VALUE`` strings into a dict.

    Empty input returns ``{}``.  Raises :class:`click.UsageError` (exit
    code 2) on a missing ``=`` separator or empty key.
    """
    result: dict = {}
    for raw in pairs or ():
        if "=" not in raw:
            raise click.UsageError(
                f"Invalid {flag_name} '{raw}': expected KEY=VALUE"
            )
        key, _, value = raw.partition("=")
        if not key:
            raise click.UsageError(
                f"Invalid {flag_name} '{raw}': key cannot be empty"
            )
        result[key] = value
    return result


def parse_env_pairs(pairs):
    """Parse ``--env KEY=VAL`` repetitions into a dict."""
    return parse_kv_pairs(pairs, flag_name="--env")


def parse_header_pairs(pairs):
    """Parse ``--header KEY=VAL`` repetitions into a dict."""
    return parse_kv_pairs(pairs, flag_name="--header")
