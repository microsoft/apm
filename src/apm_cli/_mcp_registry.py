"""MCP registry URL validation helpers for config.py.

Extracted from ``config.py`` to keep that module under the 800-line guardrail.
The constants and ``_validate_mcp_registry_url`` are imported back into
``config.py`` so they remain accessible as ``apm_cli.config.*``.
"""

from __future__ import annotations

_MCP_REGISTRY_URL_KEY = "mcp_registry_url"
_MCP_REGISTRY_ALLOWED_SCHEMES = frozenset({"http", "https"})
_MCP_REGISTRY_URL_MAX_LENGTH = 2048


def _validate_mcp_registry_url(url: str) -> str:
    """Validate and normalise a registry URL.  Returns the trimmed URL.

    Raises:
        ValueError: If the URL is empty, too long, missing a scheme/host,
            or uses a scheme outside ``http``/``https``.
    """
    from urllib.parse import urlparse

    normalized = url.strip().rstrip("/")
    if not normalized:
        raise ValueError("mcp-registry-url: URL cannot be empty")
    if len(normalized) > _MCP_REGISTRY_URL_MAX_LENGTH:
        raise ValueError(
            f"mcp-registry-url: URL is too long "
            f"({len(normalized)} > {_MCP_REGISTRY_URL_MAX_LENGTH} characters)"
        )
    parsed = urlparse(normalized)
    if not parsed.scheme:
        raise ValueError(
            f"mcp-registry-url: Invalid URL '{normalized}': expected scheme://host "
            f"(e.g. https://mcp.internal.example.com)"
        )
    scheme = parsed.scheme.lower()
    if scheme not in _MCP_REGISTRY_ALLOWED_SCHEMES:
        raise ValueError(
            f"mcp-registry-url: scheme '{scheme}' is not supported; "
            f"use http:// or https://. "
            f"WebSocket URLs (ws/wss) and file:// paths are rejected for security."
        )
    if parsed.username is not None:
        raise ValueError(
            "mcp-registry-url: URL must not contain credentials; "
            "use the MCP_REGISTRY_URL environment variable or a credential helper instead."
        )
    if not parsed.hostname:
        raise ValueError(
            f"mcp-registry-url: Invalid URL '{normalized}': expected scheme://host "
            f"(e.g. https://mcp.internal.example.com)"
        )
    return normalized
