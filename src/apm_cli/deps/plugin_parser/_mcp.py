"""MCP server extraction helpers for Claude plugin manifests.

Private module – imported only via :mod:`apm_cli.deps.plugin_parser`.
"""

import json
import logging
from pathlib import Path
from typing import Any

from ...utils.console import _rich_warning


def _surface_warning(message: str, logger: logging.Logger) -> None:
    """Emit a warning to both the stdlib logger and the rich console.

    The ``apm`` stdlib logger has no handlers configured by default, so
    ``logger.warning`` calls are silently dropped in non-debug runs. For
    user-visible plugin-parse issues (skipped MCP servers, validation
    failures), also route through ``_rich_warning`` so the user sees them
    even without ``--verbose``. Falls back gracefully if Rich is unavailable.
    """
    logger.warning(message)
    try:  # noqa: SIM105
        _rich_warning(message, symbol="warning")
    except Exception:
        # Console output is best-effort; never mask the underlying warning.
        pass


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


def _dispatch_explicit_mcp_value(
    mcp_value: Any,
    plugin_path: Path,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Resolve an explicit ``mcpServers`` manifest value to a server dict.

    Handles all three value types allowed by the Claude Code spec:
    - ``dict``  -> used directly as inline server definitions.
    - ``str``   -> read that file path relative to plugin root.
    - ``list``  -> read each file path and merge (last-wins on name conflict).
    - anything else -> logged as warning, empty dict returned.

    Extracted from :func:`_extract_mcp_servers` to reduce its McCabe
    complexity within the configured Ruff thresholds.
    """
    if isinstance(mcp_value, dict):
        return dict(mcp_value)
    if isinstance(mcp_value, str):
        return _read_mcp_file(plugin_path, mcp_value, logger)
    if isinstance(mcp_value, list):
        servers: dict[str, Any] = {}
        for entry in mcp_value:
            if isinstance(entry, str):
                servers.update(_read_mcp_file(plugin_path, entry, logger))
            else:
                logger.warning("Ignoring non-string entry in mcpServers array: %s", entry)
        return servers
    logger.warning("Unsupported mcpServers type %s; ignoring", type(mcp_value).__name__)
    return {}


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
        servers = _dispatch_explicit_mcp_value(mcp_value, plugin_path, logger)
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
    so plugin-synthesized servers must clear the same security validation
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
    from ...models.dependency.mcp import MCPDependency

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
