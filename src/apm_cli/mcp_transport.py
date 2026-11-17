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
from collections.abc import Mapping

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


__all__ = [
    "CANONICAL_REMOTE_TRANSPORTS",
    "TRANSPORT_SSE",
    "TRANSPORT_STREAMABLE_HTTP",
    "canonical_remote_transport",
]
