"""Helpers for the ``apm install --mcp ... --registry URL`` flag.

Lives under ``apm_cli/install/`` per the LOC-budget invariant on
``commands/install.py``: new logic for the install path goes into focused
phase modules. This module owns:

- URL validation (scheme allowlist, netloc, length cap) for ``--registry``.
- Precedence resolution between the CLI flag and ``MCP_REGISTRY_URL``.
- A context manager that exports the resolved registry URL as
  ``MCP_REGISTRY_URL`` (and ``MCP_REGISTRY_ALLOW_HTTP=1`` for http) for
  the duration of an ``MCPIntegrator.install`` call, then restores prior
  env values so we never mutate the parent process beyond the call.

It deliberately depends only on stdlib + click (for the typed
``UsageError``) and on the canonical scheme allowlist exported by
``MCPDependency``. Diagnostic emission stays at the CLI layer so that the
``InstallLogger`` instance can be threaded in without circular imports.
"""

from __future__ import annotations

import contextlib
import os
from typing import Iterator, Optional, Tuple
from urllib.parse import urlparse

import click

from ..models.dependency.mcp import _ALLOWED_URL_SCHEMES


# Defensive cap on registry URL length to keep apm.yml diffs reviewable
# and to bound any downstream URL parsing/logging surface.
_MAX_REGISTRY_URL_LENGTH = 2048


def validate_registry_url(value: Optional[str]) -> Optional[str]:
    """Validate a ``--registry`` URL value. Return the normalized URL.

    Reuses the same scheme allowlist as :class:`MCPDependency` (``http``,
    ``https``) so ``file://``, ``ws://``, ``wss://``, ``javascript:``, and
    bare paths are rejected. Both http and https are accepted: explicit
    user intent via a CLI flag is a strong signal, and enterprise/local
    registries on http are common. For env-var-supplied registry URLs the
    stricter https-by-default policy in ``SimpleRegistryClient`` still
    applies (opt-in via ``MCP_REGISTRY_ALLOW_HTTP=1``).

    Raises :class:`click.UsageError` (exit code 2) on any rejected URL.
    Returns ``None`` when ``value`` is ``None`` so callers can pipe the
    flag value through unchanged.
    """
    if value is None:
        return None
    if not isinstance(value, str) or value.strip() == "":
        raise click.UsageError(
            "--registry: URL cannot be empty; expected scheme://host "
            "(e.g. https://mcp.internal.example.com)"
        )
    normalized = value.strip().rstrip("/")
    if len(normalized) > _MAX_REGISTRY_URL_LENGTH:
        raise click.UsageError(
            f"--registry: URL is too long ({len(normalized)} > "
            f"{_MAX_REGISTRY_URL_LENGTH} characters)"
        )
    parsed = urlparse(normalized)
    if not parsed.scheme or not parsed.netloc:
        raise click.UsageError(
            f"--registry: Invalid URL '{value}': expected scheme://host "
            f"(e.g. https://mcp.internal.example.com)"
        )
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise click.UsageError(
            f"--registry: Invalid URL '{value}': scheme '{scheme}' is not "
            f"supported; use http:// or https://. WebSocket URLs (ws/wss) "
            f"and file:// paths are rejected for security."
        )
    return normalized


def resolve_registry_url(
    cli_value: Optional[str],
    *,
    logger=None,
) -> Tuple[Optional[str], str]:
    """Apply precedence chain: CLI flag > ``MCP_REGISTRY_URL`` env > default.

    Returns ``(resolved_url_or_None, source)`` where source is one of
    ``"flag"``, ``"env"``, or ``"default"``. ``None`` is returned for the
    default case so callers can treat default as "no override".

    When the flag is provided AND an env var is also set with a different
    value, emits a one-line ``[i]`` diagnostic naming both so users can
    confirm the flag won. Stays silent otherwise (defaults are quiet,
    overrides are visible).
    """
    env_value = os.environ.get("MCP_REGISTRY_URL")
    if env_value is not None and env_value.strip() == "":
        env_value = None

    if cli_value is not None:
        if env_value and env_value.rstrip("/") != cli_value:
            if logger is not None:
                logger.progress(
                    f"--registry overrides MCP_REGISTRY_URL ({env_value})",
                    symbol="info",
                )
        return cli_value, "flag"
    if env_value is not None:
        return env_value, "env"
    return None, "default"


_REGISTRY_ENV_KEYS = ("MCP_REGISTRY_URL", "MCP_REGISTRY_ALLOW_HTTP")


@contextlib.contextmanager
def registry_env_override(registry_url: Optional[str]) -> Iterator[None]:
    """Temporarily export ``MCP_REGISTRY_URL`` for the duration of a call.

    ``MCPIntegrator.install`` constructs ``MCPServerOperations()`` deep in
    its call graph with no registry argument; that constructor reads
    ``MCP_REGISTRY_URL`` from the process env. Threading a ``registry_url``
    kwarg through the integrator chain is a larger refactor; piggy-backing
    on the existing env contract keeps this change surgical.

    For http URLs we also set ``MCP_REGISTRY_ALLOW_HTTP=1`` so the
    ``SimpleRegistryClient`` https-by-default policy does not reject the
    explicit user choice. CLI-flag intent is treated as a stronger signal
    than ambient env config.

    Prior values are saved and restored on exit (including the absent
    case via ``os.environ.pop``). A ``None`` ``registry_url`` is a no-op,
    so callers can wrap unconditionally.
    """
    if not registry_url:
        yield
        return
    saved = {k: os.environ.get(k) for k in _REGISTRY_ENV_KEYS}
    try:
        os.environ["MCP_REGISTRY_URL"] = registry_url
        if urlparse(registry_url).scheme.lower() == "http":
            os.environ["MCP_REGISTRY_ALLOW_HTTP"] = "1"
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
