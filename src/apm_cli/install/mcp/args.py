"""MCP CLI argument parsing for ``--env`` and ``--header`` repetitions.

Extracted from ``commands/install.py`` per the architecture-invariants
LOC budget (sibling to ``warnings.py`` / ``registry.py``).
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional

import click


def parse_kv_pairs(
    pairs: Optional[Iterable[str]],
    *,
    flag_name: str,
) -> Dict[str, str]:
    """Parse a tuple of ``KEY=VALUE`` strings into a dict.

    Empty input returns ``{}``.  Raises :class:`click.UsageError` (exit
    code 2) on a missing ``=`` separator or empty key.
    """
    result: Dict[str, str] = {}
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


def parse_env_pairs(pairs: Optional[Iterable[str]]) -> Dict[str, str]:
    """Parse ``--env KEY=VAL`` repetitions into a dict."""
    return parse_kv_pairs(pairs, flag_name="--env")


def parse_header_pairs(pairs: Optional[Iterable[str]]) -> Dict[str, str]:
    """Parse ``--header KEY=VAL`` repetitions into a dict."""
    return parse_kv_pairs(pairs, flag_name="--header")
