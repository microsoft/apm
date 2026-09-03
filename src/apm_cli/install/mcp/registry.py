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
from collections.abc import Iterator, Mapping, Sequence
from urllib.parse import urlparse

import click

from ...models.dependency.mcp import _ALLOWED_URL_SCHEMES
from ...utils.net import is_loopback_host, parse_host_address

# Defensive cap on registry URL length to keep apm.yml diffs reviewable
# and to bound any downstream URL parsing/logging surface.
_MAX_REGISTRY_URL_LENGTH = 2048


def _redact_url_credentials(url: str) -> str:
    """Strip credentials and query data from a URL before logging it."""
    from ...registry.client import redact_mcp_registry_url

    redacted = redact_mcp_registry_url(url)
    if redacted == "<invalid registry URL>":
        return "<redacted-invalid-registry-url>"
    return redacted


def _is_local_or_metadata_host(host: str | None) -> bool:
    """Return True for loopback, link-local, RFC1918, or cloud-metadata IPs.

    Used to surface a soft warning when ``--registry`` points at the local
    machine or a cloud metadata endpoint -- both common SSRF sinks. The
    warning is informational only; we do not block, because local registries
    are a legitimate dev/CI workflow.
    """
    if not host:
        return False
    lowered = host.lower()
    if is_loopback_host(lowered):
        return True
    addr = parse_host_address(lowered)
    if addr is None:
        return False
    return addr.is_link_local or addr.is_private or addr.is_multicast or addr.is_unspecified


def validate_registry_url(value: str | None) -> str | None:
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
    # Redact credentials before echoing the URL back to the user: any
    # ``user:password@`` segment in `value` would otherwise land in
    # `UsageError` text and be captured by CI logs / shell history.
    safe_value = _redact_url_credentials(value)
    if not parsed.scheme or not parsed.netloc:
        raise click.UsageError(
            f"--registry: Invalid URL '{safe_value}': expected scheme://host "
            f"(e.g. https://mcp.internal.example.com)"
        )
    scheme = parsed.scheme.lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise click.UsageError(f"--registry: Invalid URL '{safe_value}': invalid port") from exc
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise click.UsageError(
            f"--registry: Invalid URL '{safe_value}': scheme '{scheme}' is not "
            f"supported; use http:// or https://. WebSocket URLs (ws/wss) "
            f"and file:// paths are rejected for security."
        )
    if parsed.username is not None:
        raise click.UsageError("--registry: embedded credentials are not supported")
    if parsed.query or parsed.fragment:
        raise click.UsageError(
            "--registry: base URL must not contain a query or fragment; "
            "query strings and fragments are not supported"
        )
    return normalized


def resolve_registry_url(
    cli_value: str | None,
    *,
    logger=None,
    announce: bool = True,
) -> tuple[str | None, str]:
    """Apply precedence chain: flag > env > apm config > default.

    The chain itself lives in
    :func:`~apm_cli.registry.client.resolve_mcp_registry_url`, which every
    registry consumer shares; this wrapper adds the ``apm install --mcp``
    diagnostics on top of it. Returns ``(resolved_url_or_None, source)`` where
    source is one of ``"flag"``, ``"env"``, ``"config"``, or ``"default"``.
    ``None`` is returned for the default case so callers can treat default as
    "no override".

    When the flag is provided AND an env var is also set with a different
    value, emits a one-line diagnostic naming both so users can confirm the
    flag won. Stays silent for the default (defaults are quiet, overrides are
    visible).

    Pass ``announce=False`` when a later phase will name the endpoint it
    actually queries -- the real ``--mcp`` install reaches
    ``_install_registry_group``, which announces there, so emitting here too
    would print the same line twice. The local-host warning and the
    flag-versus-env notice are unaffected: both say something no later phase
    can.
    """
    from ...registry.client import REGISTRY_SOURCE_LABELS, resolve_mcp_registry_url

    url, source = resolve_mcp_registry_url(cli_value)
    if source == "default":
        return None, "default"

    try:
        validated_url = validate_registry_url(url)
    except click.UsageError as exc:
        detail = exc.message.removeprefix("--registry: ")
        if source == "env":
            raise click.UsageError(f"MCP_REGISTRY_URL is invalid: {detail}") from exc
        if source == "config":
            raise click.UsageError(
                "Configured mcp-registry-url is invalid; run "
                f"'apm config unset mcp-registry-url' and retry: {detail}"
            ) from exc
        raise
    if validated_url is not None:
        url = validated_url

    if source == "explicit":
        source = "flag"
        env_value = os.environ.get("MCP_REGISTRY_URL")
        if logger is not None and announce:
            logger.progress(
                f"Using MCP registry: {_redact_url_credentials(url)} (from --registry)",
                symbol="info",
            )
        if logger is not None and env_value and env_value.rstrip("/") != url:
            logger.progress(
                f"--registry overrides MCP_REGISTRY_URL ({_redact_url_credentials(env_value)})",
                symbol="info",
            )
    elif logger is not None and announce:
        # Defaults are quiet, overrides are visible: surface the redirect so a
        # poisoned MCP_REGISTRY_URL or config entry cannot silently change
        # package resolution. Always emitted (not verbose-gated).
        logger.progress(
            f"Using MCP registry: {_redact_url_credentials(url)} "
            f"({REGISTRY_SOURCE_LABELS[source]})",
            symbol="info",
        )
    _maybe_warn_local_host(url, logger)
    return url, source


def _maybe_warn_local_host(url: str, logger) -> None:
    """Emit a soft warning when a registry URL targets localhost / RFC1918 /
    link-local (incl. cloud metadata 169.254.169.254) hosts. Informational
    only -- local registries are a legitimate workflow."""
    if logger is None:
        return
    try:
        host = urlparse(url).hostname
    except (ValueError, TypeError):
        return
    if _is_local_or_metadata_host(host):
        logger.warning(
            f"--registry host '{host}' is loopback/private/link-local; "
            f"only registry-resolved installs will reach it. "
            f"Confirm this is intentional (local dev / private mirror).",
            symbol="warning",
        )


_REGISTRY_SOURCE_ENV_KEY = "APM_MCP_REGISTRY_SOURCE"
_REGISTRY_ENV_KEYS = ("MCP_REGISTRY_URL", "MCP_REGISTRY_ALLOW_HTTP", _REGISTRY_SOURCE_ENV_KEY)


@contextlib.contextmanager
def registry_env_override(
    registry_url: str | None,
    *,
    allow_http: bool = True,
    source: str | None = None,
) -> Iterator[None]:
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
        if source in {"flag", "env", "config"}:
            os.environ[_REGISTRY_SOURCE_ENV_KEY] = source
        if allow_http and urlparse(registry_url).scheme.lower() == "http":
            os.environ["MCP_REGISTRY_ALLOW_HTTP"] = "1"
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def validate_mcp_dry_run_entry(
    name: str,
    *,
    transport: str | None = None,
    url: str | None = None,
    env: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    version: str | None = None,
    command_argv: Sequence[str] | None = None,
    registry_url: str | None = None,
) -> None:
    """C1: validate the MCP entry that ``apm install --mcp ... --dry-run``
    would persist, raising :class:`click.UsageError` on rejection.

    Mirrors the validation that real install runs via ``build_mcp_entry``,
    so dry-run never previews "success" for an entry the real install
    would reject. Lives here (not in commands/install.py) per the LOC-budget
    invariant on that module. The keyword-only signature matches
    :func:`build_mcp_entry` exactly so unknown kwargs surface as
    ``TypeError`` at the boundary instead of being silently swallowed.
    """
    from .entry import build_mcp_entry

    try:
        build_mcp_entry(
            name,
            transport=transport,
            url=url,
            env=env,
            headers=headers,
            version=version,
            command_argv=command_argv,
            registry_url=registry_url,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc))  # noqa: B904
