"""Canonical MCP remote transport vocabulary (single owner).

Remote MCP endpoints describe their transport in three historical places:

* the MCP Registry v0.1 ``type`` field (``streamable-http`` or ``sse``),
  required by every published server schema;
* the legacy ``transport_type`` key some older registry payloads and tests
  still carry;
* the ``transport`` an ``apm.yml`` declaration (or APM's own ingestion)
  records.

:func:`canonical_remote_transport` is the only place that turns those into
the canonical vocabulary. Ingestion (registry client, self-defined servers,
manifest overlays) stores the result under ``remote["transport"]`` and every
adapter reads the transport back through this function, never from the raw
keys.

Ref: https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

TRANSPORT_STREAMABLE_HTTP = "streamable-http"
TRANSPORT_SSE = "sse"
CANONICAL_REMOTE_TRANSPORTS: frozenset[str] = frozenset({TRANSPORT_STREAMABLE_HTTP, TRANSPORT_SSE})

# ``http`` is the manifest/legacy alias of Streamable HTTP.
_ALIASES: Mapping[str, str] = {
    "http": TRANSPORT_STREAMABLE_HTTP,
    TRANSPORT_STREAMABLE_HTTP: TRANSPORT_STREAMABLE_HTTP,
    TRANSPORT_SSE: TRANSPORT_SSE,
}

_SUPPORTED_LABEL = "http, sse, streamable-http"

# Precedence: registry v0.1 ``type``, then legacy ``transport_type``, then the
# manifest/canonical ``transport``.
_TRANSPORT_KEYS = ("type", "transport_type", "transport")


def _declared(remote: Mapping, key: str) -> str:
    value = remote.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        return repr(value)
    return value.strip()


def canonical_remote_transport(
    remote: Mapping,
    *,
    runtime_label: str | None = None,
    server_name: str | None = None,
) -> str:
    """Return the canonical transport (``streamable-http`` or ``sse``) of *remote*.

    A missing transport means Streamable HTTP. An unknown value raises
    ``ValueError``; *runtime_label* and *server_name* only shape that message.
    When the v0.1 ``type`` and a legacy ``transport_type`` disagree, ``type``
    wins (it is the published schema field).
    """
    declared = {key: _declared(remote, key) for key in _TRANSPORT_KEYS}
    raw = next((declared[key] for key in _TRANSPORT_KEYS if declared[key]), "")
    if not raw:
        return TRANSPORT_STREAMABLE_HTTP
    canonical = _ALIASES.get(raw)
    if canonical is None:
        target = f" for {runtime_label}" if runtime_label else ""
        raise ValueError(
            f"Unsupported remote transport '{raw}'{target}. "
            f"Server: {server_name or 'unknown'}. "
            f"Supported transports: {_SUPPORTED_LABEL}."
        )
    legacy = declared["transport_type"]
    if declared["type"] and legacy and _ALIASES.get(legacy) != canonical:
        logger.debug(
            "Remote for %s declares type %r and transport_type %r; using type",
            server_name or "unknown",
            declared["type"],
            legacy,
        )
    return canonical


# -- Policy projection (org policy ``mcp.transport.allow``) -----------------

TRANSPORT_STDIO = "stdio"
# Ordered for stable user-facing messages.
POLICY_TRANSPORTS: tuple[str, ...] = (TRANSPORT_STDIO, TRANSPORT_STREAMABLE_HTTP, TRANSPORT_SSE)


def canonical_transport_name(value: str) -> str:
    """Return the canonical spelling of one transport name from policy or apm.yml.

    ``http`` is an alias of ``streamable-http``. Unknown names come back
    trimmed and lower-cased, so they compare stably but match nothing.
    """
    token = value.strip().lower()
    if token == TRANSPORT_STDIO:
        return TRANSPORT_STDIO
    return _ALIASES.get(token, token)


def canonical_transport_names(values: Iterable[str]) -> tuple[str, ...]:
    """Canonicalize and de-duplicate transport names, keeping first-seen order."""
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(canonical_transport_name(value), None)
    return tuple(seen)


@dataclass(frozen=True)
class UnresolvedTransport:
    """An effective transport that could not be resolved, with a user-facing reason."""

    reason: str


def server_write_transports(
    server_info: Mapping[str, Any],
    *,
    selected_remote: Callable[[dict], Mapping | None],
) -> frozenset[str]:
    """Every canonical transport APM may write for one resolved server.

    *server_info* is the (overlaid) server info handed to the client adapters;
    *selected_remote* is the adapters' remote choice
    (``MCPClientAdapter.selected_remote``), whose result covers every adapter
    that renders a remote. Adapters differ in whether a package or a remote
    wins, so a server offering both reports both. Raises ``ValueError`` for an
    unsupported remote transport, as the adapters do.
    """
    if server_info.get("_raw_stdio"):
        return frozenset({TRANSPORT_STDIO})
    transports: set[str] = set()
    remote = selected_remote(dict(server_info))
    if remote is not None:
        transports.add(
            canonical_remote_transport(remote, server_name=str(server_info.get("name") or ""))
        )
    if server_info.get("packages"):
        transports.add(TRANSPORT_STDIO)
    if server_info.get("sse_endpoint"):
        transports.add(TRANSPORT_SSE)
    return frozenset(transports)


__all__ = [
    "CANONICAL_REMOTE_TRANSPORTS",
    "POLICY_TRANSPORTS",
    "TRANSPORT_SSE",
    "TRANSPORT_STDIO",
    "TRANSPORT_STREAMABLE_HTTP",
    "UnresolvedTransport",
    "canonical_remote_transport",
    "canonical_transport_name",
    "canonical_transport_names",
    "server_write_transports",
]
