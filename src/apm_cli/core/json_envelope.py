"""Versioned JSON envelope for marketplace and view command results."""

from __future__ import annotations

SCHEMA_VERSION = 1


def success_envelope(data: object) -> dict[str, object]:
    """Wrap a JSON-compatible result without changing its data."""
    return {"schema_version": SCHEMA_VERSION, "data": data}


def error_envelope(code: str) -> dict[str, object]:
    """Wrap a stable machine-readable error code, not exception text."""
    return {"schema_version": SCHEMA_VERSION, "error": code}
