"""The one name rule for MCP and LSP server dependencies.

A server name becomes a key in the client's JSON config, so it must fully match a
conservative character allowlist and must not be a reserved JavaScript
object key (``__proto__``, ``constructor``, ``prototype``, any case).
"""

from __future__ import annotations

import re

_NAME_REGEX = re.compile(r"[a-zA-Z0-9@_][a-zA-Z0-9._@/:=-]{0,127}")
RESERVED_OBJECT_KEYS = frozenset({"__proto__", "constructor", "prototype"})


def validate_server_name(name: str, kind: str, *, example: str = "") -> None:
    """Raise ``ValueError`` unless *name* is a valid *kind* (``MCP``/``LSP``) server name."""
    if not _NAME_REGEX.fullmatch(name):
        hint = f" Example: {example}." if example else ""
        raise ValueError(
            f"Invalid {kind} dependency name '{name}': "
            f"must start with a letter, digit, '@', or '_' and contain "
            f"only [a-zA-Z0-9._@/:=-] (max 128 chars).{hint}"
        )
    if name.lower() in RESERVED_OBJECT_KEYS:
        raise ValueError(
            f"Invalid {kind} dependency name '{name}': reserved object key "
            f"({', '.join(sorted(RESERVED_OBJECT_KEYS))}, any case) cannot be "
            f"used as a server name. Rename the server."
        )
