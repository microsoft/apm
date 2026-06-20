"""MCP and LSP server-extraction helpers for plugin_parser.

Extracted from :mod:`plugin_parser` to keep that module under the
file-length guardrail. All public names are re-exported from
``plugin_parser`` so import paths are unchanged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


def _read_mcp_file(plugin_path: Path, rel_path: str, logger: logging.Logger) -> dict[str, Any]:
    """Read a JSON file relative to *plugin_path* and return its ``mcpServers`` dict."""
    target = (plugin_path / rel_path).resolve()
    # Security: must stay inside plugin_path and not be a symlink
    try:
        target.relative_to(plugin_path.resolve())
    except ValueError:
        logger.warning("MCP file path escapes plugin root: %s", rel_path)
        return {}
    candidate = plugin_path / rel_path
    if not candidate.exists() or not candidate.is_file():
        logger.warning("MCP file not found: %s", candidate)
        return {}
    if candidate.is_symlink():
        logger.warning("Skipping symlinked MCP file: %s", candidate)
        return {}
    return _read_mcp_json(candidate, logger)


def _read_mcp_json(path: Path, logger: logging.Logger) -> dict[str, Any]:
    """Parse a JSON file and return the ``mcpServers`` mapping."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read MCP config %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    servers = data.get("mcpServers", {})
    return dict(servers) if isinstance(servers, dict) else {}


def _substitute_plugin_root(
    servers: dict[str, Any], abs_root: str, logger: logging.Logger
) -> dict[str, Any]:
    """Replace ``${CLAUDE_PLUGIN_ROOT}`` in server config string values."""
    placeholder = "${CLAUDE_PLUGIN_ROOT}"
    substituted = False

    def _walk(obj: Any) -> Any:
        nonlocal substituted
        if isinstance(obj, str) and placeholder in obj:
            substituted = True
            return obj.replace(placeholder, abs_root)
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(item) for item in obj]
        return obj

    result = {name: _walk(cfg) for name, cfg in servers.items()}
    if substituted:
        logger.info("Substituted ${CLAUDE_PLUGIN_ROOT} with %s", abs_root)
    return result


def _extract_mcp_servers(plugin_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Extract MCP server definitions from a plugin manifest.

    Resolves ``mcpServers`` by type (per Claude Code spec):
    - ``str``  -> read that file path relative to plugin root, parse JSON,
      extract ``mcpServers`` key.
    - ``list`` -> read each file path, merge (last-wins on name conflict).
    - ``dict`` -> use directly as inline server definitions.

    When ``mcpServers`` is absent and ``.mcp.json`` (or ``.github/.mcp.json``)
    exists at plugin root, read it as the default (matches Claude Code
    auto-discovery).

    Security: symlinks are skipped, JSON parse errors are logged as warnings.

    ``${CLAUDE_PLUGIN_ROOT}`` in string values is replaced with the absolute
    plugin path.

    Args:
        plugin_path: Root of the plugin directory.
        manifest: Parsed plugin.json dict.

    Returns:
        dict mapping server name -> server config.  Empty on failure.
    """
    logger = logging.getLogger("apm")
    mcp_value = manifest.get("mcpServers")

    if mcp_value is not None:
        # Manifest explicitly defines mcpServers
        if isinstance(mcp_value, dict):
            servers = dict(mcp_value)
        elif isinstance(mcp_value, str):
            servers = _read_mcp_file(plugin_path, mcp_value, logger)
        elif isinstance(mcp_value, list):
            servers = {}
            for entry in mcp_value:
                if isinstance(entry, str):
                    servers.update(_read_mcp_file(plugin_path, entry, logger))
                else:
                    logger.warning("Ignoring non-string entry in mcpServers array: %s", entry)
        else:
            logger.warning("Unsupported mcpServers type %s; ignoring", type(mcp_value).__name__)
            return {}
    else:
        # Fall back to auto-discovery: .mcp.json then .github/.mcp.json
        servers = {}
        for fallback in (".mcp.json", ".github/.mcp.json"):
            candidate = plugin_path / fallback
            if candidate.exists() and candidate.is_file() and not candidate.is_symlink():
                servers = _read_mcp_json(candidate, logger)
                if servers:
                    break

    # Substitute ${CLAUDE_PLUGIN_ROOT} in all string values
    if servers:
        abs_root = str(plugin_path.resolve())
        servers = _substitute_plugin_root(servers, abs_root, logger)

    return servers


def _mcp_servers_to_apm_deps(servers: dict[str, Any], plugin_path: Path) -> list[dict[str, Any]]:
    """Convert raw MCP server configs to ``dependencies.mcp`` dicts.

    Transport inference:
    - ``command`` present -> stdio
    - ``url`` present -> http (or ``type`` if it's a valid transport)
    - Neither -> skipped with warning

    Every entry gets ``registry: false`` (self-defined, not registry lookups).

    All resulting entries are routed through ``MCPDependency.from_dict()``
    so plugin-synthesised servers must clear the same security validation
    chokepoint as CLI-authored or manually edited entries (name shape, URL
    scheme allowlist, header CRLF, command path-traversal). Entries that
    fail validation are skipped with a warning rather than crashing the
    plugin install -- a single malformed server should not block the
    whole plugin.

    Args:
        servers: Mapping of server name -> server config dict.
        plugin_path: Plugin root (used for log context only).

    Returns:
        List of dicts consumable by ``MCPDependency.from_dict()``.
    """
    from ..models.dependency.mcp import MCPDependency
    from .plugin_parser import _surface_warning

    logger = logging.getLogger("apm")
    deps: list[dict[str, Any]] = []

    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            logger.warning("Skipping non-dict MCP server config '%s'", name)
            continue

        dep: dict[str, Any] = {"name": name, "registry": False}

        if "command" in cfg:
            dep["transport"] = "stdio"
            dep["command"] = cfg["command"]
            if "args" in cfg:
                dep["args"] = cfg["args"]
        elif "url" in cfg:
            raw_type = cfg.get("type", "http")
            valid_transports = {"http", "sse", "streamable-http"}
            dep["transport"] = raw_type if raw_type in valid_transports else "http"
            dep["url"] = cfg["url"]
            if "headers" in cfg:
                dep["headers"] = cfg["headers"]
        else:
            _surface_warning(
                f"Skipping MCP server '{name}' from plugin "
                f"'{plugin_path.name}': no 'command' or 'url'",
                logger,
            )
            continue

        if "env" in cfg:
            dep["env"] = cfg["env"]
        if "tools" in cfg:
            dep["tools"] = cfg["tools"]

        # Route through the validation chokepoint. Plugins are an ingress
        # path: a malicious plugin could otherwise smuggle path traversal,
        # CRLF, or unsafe URL schemes that bypass MCPDependency.validate().
        # PR #809 follow-up: surface validation errors to the user via the
        # rich console (stdlib logger has no handlers configured).
        try:
            MCPDependency.from_dict(dep)
        except (ValueError, Exception) as exc:
            _surface_warning(
                f"Skipping invalid MCP server '{name}' from plugin '{plugin_path.name}': {exc}",
                logger,
            )
            continue

        deps.append(dep)

    return deps


def _read_lsp_file(plugin_path: Path, rel_path: str, logger: logging.Logger) -> dict[str, Any]:
    """Read a JSON file relative to *plugin_path* and return its LSP server dict."""
    target = (plugin_path / rel_path).resolve()
    try:
        target.relative_to(plugin_path.resolve())
    except ValueError:
        logger.warning("LSP file path escapes plugin root: %s", rel_path)
        return {}
    candidate = plugin_path / rel_path
    if not candidate.exists() or not candidate.is_file():
        logger.warning("LSP file not found: %s", candidate)
        return {}
    if candidate.is_symlink():
        logger.warning("Skipping symlinked LSP file: %s", candidate)
        return {}
    return _read_lsp_json(candidate, logger)


def _read_lsp_json(path: Path, logger: logging.Logger) -> dict[str, Any]:
    """Parse a JSON file and return the LSP servers mapping.

    Accepts two formats:
    - Flat: top-level keys are server names (e.g. ``{"pyright": {...}}``).
    - Wrapped: a ``"lspServers"`` envelope wraps the servers
      (e.g. ``{"lspServers": {"pyright": {...}}}``).

    The wrapped format is standard in Copilot ``.github/lsp.json`` and
    Claude ``~/.claude.json``.  Plugins may ship either variant.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read LSP config %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    # Unwrap the { "lspServers": { ... } } envelope when present.
    # Only unwrap when the inner value looks like a server *map* (all values
    # are dicts).  A flat-format server literally named "lspServers" would
    # have scalar values like "command", so the all-dicts check avoids
    # mis-detecting it as an envelope.
    lsp_inner = data.get("lspServers")
    if (
        isinstance(lsp_inner, dict)
        and lsp_inner
        and all(isinstance(v, dict) for v in lsp_inner.values())
    ):
        logger.debug("Unwrapped lspServers envelope in %s", path)
        return dict(lsp_inner)
    return dict(data)


def _extract_lsp_servers(plugin_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Extract LSP server definitions from a plugin manifest.

    Resolves ``lspServers`` by type (per Claude Code spec):
    - ``str``  -> read that file path relative to plugin root, parse JSON.
    - ``dict`` -> use directly as inline server definitions.

    When ``lspServers`` is absent and ``.lsp.json`` exists at plugin root,
    read it as the default (matches Claude Code auto-discovery).

    Security: symlinks are skipped, JSON parse errors are logged as warnings.

    ``${CLAUDE_PLUGIN_ROOT}`` in string values is replaced with the absolute
    plugin path.

    Args:
        plugin_path: Root of the plugin directory.
        manifest: Parsed plugin.json dict.

    Returns:
        dict mapping server name -> server config.  Empty on failure.
    """
    logger = logging.getLogger("apm")
    lsp_value = manifest.get("lspServers")

    if lsp_value is not None:
        if isinstance(lsp_value, dict):
            servers = dict(lsp_value)
        elif isinstance(lsp_value, str):
            servers = _read_lsp_file(plugin_path, lsp_value, logger)
        else:
            logger.warning("Unsupported lspServers type %s; ignoring", type(lsp_value).__name__)
            return {}
    else:
        # Fall back to auto-discovery: .lsp.json
        servers = {}
        candidate = plugin_path / ".lsp.json"
        if candidate.exists() and candidate.is_file() and not candidate.is_symlink():
            servers = _read_lsp_json(candidate, logger)

    # Substitute ${CLAUDE_PLUGIN_ROOT} in all string values
    if servers:
        abs_root = str(plugin_path.resolve())
        servers = _substitute_plugin_root(servers, abs_root, logger)

    return servers


def _lsp_servers_to_apm_deps(servers: dict[str, Any], plugin_path: Path) -> list[dict[str, Any]]:
    """Convert raw LSP server configs to ``dependencies.lsp`` dicts.

    Required fields per Claude Code spec:
    - ``command``: binary to run
    - ``extensionToLanguage``: mapping of file extensions to language IDs

    All resulting entries are routed through ``LSPDependency.from_dict()``
    for validation. Entries that fail validation are skipped with a warning.

    Args:
        servers: Mapping of server name -> server config dict.
        plugin_path: Plugin root (used for log context only).

    Returns:
        List of dicts consumable by ``LSPDependency.from_dict()``.
    """
    from ..models.dependency.lsp import LSPDependency
    from .plugin_parser import _surface_warning

    logger = logging.getLogger("apm")
    deps: list[dict[str, Any]] = []

    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            logger.warning("Skipping non-dict LSP server config '%s'", name)
            continue

        dep: dict[str, Any] = {"name": name}

        # Copy all recognised fields
        for key in (
            "command",
            "args",
            "extensionToLanguage",
            "transport",
            "env",
            "initializationOptions",
            "settings",
            "workspaceFolder",
            "startupTimeout",
            "shutdownTimeout",
            "restartOnCrash",
            "maxRestarts",
        ):
            if key in cfg:
                dep[key] = cfg[key]

        # Route through the validation chokepoint
        try:
            LSPDependency.from_dict(dep)
        except Exception as exc:
            _surface_warning(
                f"Skipping invalid LSP server '{name}' from plugin '{plugin_path.name}': {exc}",
                logger,
            )
            continue

        deps.append(dep)

    return deps
