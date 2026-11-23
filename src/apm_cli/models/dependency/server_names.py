"""The one name rule for MCP and LSP server dependencies.

A server name becomes a key in the client's JSON config, so it must match a
conservative character allowlist and must not be a reserved JavaScript object
key (``__proto__``, ``constructor``, ``prototype``, any case). Callers whose
config key differs from the name (a registry MCP reference) check that key too.
"""

from __future__ import annotations

import re

_NAME_REGEX = re.compile(r"^[a-zA-Z0-9@_][a-zA-Z0-9._@/:=-]{0,127}$")
RESERVED_OBJECT_KEYS = frozenset({"__proto__", "constructor", "prototype"})


def is_reserved_server_name(name: str) -> bool:
    """Return whether *name*, used as a config key, is a reserved object key."""
    return name.rstrip().lower() in RESERVED_OBJECT_KEYS


def validate_server_name(
    name: str, kind: str, *, example: str = "", config_key: str | None = None
) -> None:
    """Raise ``ValueError`` unless *name* is a valid *kind* (``MCP``/``LSP``) server name.

    *config_key* is the key a config writer derives from *name* when it differs.
    """
    if not _NAME_REGEX.match(name):
        hint = f" Example: {example}." if example else ""
        raise ValueError(
            f"Invalid {kind} dependency name '{name}': "
            f"must start with a letter, digit, '@', or '_' and contain "
            f"only [a-zA-Z0-9._@/:=-] (max 128 chars).{hint}"
        )
    if is_reserved_server_name(name) or (
        config_key is not None and is_reserved_server_name(config_key)
    ):
        raise ValueError(
            f"Invalid {kind} dependency name '{name}': reserved object key "
            f"({', '.join(sorted(RESERVED_OBJECT_KEYS))}, any case) cannot be "
            f"used as a server name or config key. Rename the server."
        )
