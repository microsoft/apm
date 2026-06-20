"""JSON / TOML / Claude MCP config cleanup helpers.

Extracted from :mod:`apm_cli.integration.mcp_integrator` to keep that module
under the file-length budget.  Removal notices route ``_rich_success`` back
through ``mcp_integrator`` so the module-level patch target
``apm_cli.integration.mcp_integrator._rich_success`` stays honored.
"""

import builtins
import json
import logging
from pathlib import Path

_log = logging.getLogger(__name__)


def _emit_rich_success(msg: str) -> None:
    """Emit a rich success notice via the (patchable) mcp_integrator helper."""
    from apm_cli.integration import mcp_integrator as _mi

    _mi._rich_success(msg, symbol="check")


def _clean_json_mcp_config(
    config_path: Path,
    stale_names: builtins.set,
    logger,
    label: str,
    servers_key: str = "mcpServers",
    trailing_newline: bool = False,
    use_rich: bool = False,
) -> int:
    """Remove stale entries from a JSON-based MCP config file.

    Args:
        config_path: Path to the JSON config file.
        stale_names: Set of server names to remove (expanded form).
        logger: Command logger for progress messages.
        label: Human-readable config label used in log messages.
        servers_key: Key under which MCP servers are stored (default: ``"mcpServers"``).
        trailing_newline: When True, append a trailing newline after JSON serialisation.
        use_rich: When True, emit removal notices via ``_rich_success``; otherwise use
            ``logger.progress``.

    Returns:
        Number of entries removed.
    """
    if not config_path.exists():
        return 0
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        servers = config.get(servers_key, {})
        removed = [n for n in stale_names if n in servers]
        for name in removed:
            del servers[name]
        if removed:
            text = json.dumps(config, indent=2)
            if trailing_newline:
                text += "\n"
            config_path.write_text(text, encoding="utf-8")
            for name in removed:
                msg = f"Removed stale MCP server '{name}' from {label}"
                if use_rich:
                    _emit_rich_success(msg)
                else:
                    logger.progress(msg)
        return len(removed)
    except Exception:
        _log.debug("Failed to clean stale MCP servers from %s", label, exc_info=True)
        return 0


def _clean_toml_mcp_config(
    config_path: Path,
    stale_names: builtins.set,
    label: str,
    logger=None,
    use_rich: bool = True,
) -> int:
    """Remove stale entries from a TOML-based MCP config file.

    Args:
        config_path: Path to the TOML config file.
        stale_names: Set of server names to remove (expanded form).
        label: Human-readable config label used in log messages.
        logger: Optional command logger for progress messages. When provided
            and *use_rich* is False, removal notices use ``logger.progress``.
        use_rich: When True (default), emit removal notices via ``_rich_success``;
            otherwise use ``logger.progress``.

    Returns:
        Number of entries removed.
    """
    if not config_path.exists():
        return 0
    try:
        import toml as _toml

        config = _toml.loads(config_path.read_text(encoding="utf-8"))
        servers = config.get("mcp_servers", {})
        removed = [n for n in stale_names if n in servers]
        for name in removed:
            del servers[name]
        if removed:
            config_path.write_text(_toml.dumps(config), encoding="utf-8")
            for name in removed:
                msg = f"Removed stale MCP server '{name}' from {label}"
                if use_rich:
                    _emit_rich_success(msg)
                elif logger is not None:
                    logger.progress(msg)
        return len(removed)
    except Exception:
        _log.debug("Failed to clean stale MCP servers from %s", label, exc_info=True)
        return 0


def _clean_claude_config(
    config_path: Path,
    stale_names: builtins.set,
    logger,
    is_user_scope: bool = False,
) -> int:
    """Remove stale entries from a Claude Code JSON config file.

    Handles both the project-level ``.mcp.json`` and the user-level
    ``~/.claude.json``, which share the same JSON structure but differ in
    scope-validation requirements and log labels.

    Args:
        config_path: Path to the Claude JSON config file.
        stale_names: Set of server names to remove (expanded form).
        logger: Command logger for progress messages.
        is_user_scope: When True, validates that the top-level config is a dict
            (``~/.claude.json`` guard) and uses the user-scope log label.

    Returns:
        Number of entries removed.
    """
    label = "~/.claude.json" if is_user_scope else ".mcp.json"
    if not config_path.exists():
        return 0
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if is_user_scope and not isinstance(config, dict):
            return 0
        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict):
            servers = {}
        removed = [n for n in stale_names if n in servers]
        for name in removed:
            del servers[name]
        if removed:
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            for name in removed:
                logger.progress(f"Removed stale MCP server '{name}' from {label}")
        return len(removed)
    except Exception:
        _log.debug("Failed to clean stale MCP servers from %s", label, exc_info=True)
        return 0
