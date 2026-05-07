"""Stale MCP server cleanup helpers."""

from __future__ import annotations

import builtins
import logging
from pathlib import Path

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.utils.console import _rich_success

_log = logging.getLogger(__name__)


def remove_stale(  # noqa: C901, PLR0912
    stale_names: builtins.set,
    runtime: str = None,  # noqa: RUF013
    exclude: str = None,  # noqa: RUF013
    project_root=None,
    user_scope: bool = False,
    logger=None,
    scope=None,
) -> None:
    """Remove MCP server entries that are no longer required by any dependency.

    Cleans up runtime configuration files only for the runtimes that were
    actually targeted during installation.  *stale_names* contains MCP
    dependency references (e.g. ``"io.github.github/github-mcp-server"``).
    For Copilot CLI and Codex, config keys are derived from the last path
    segment, so we match against both the full reference and the short name.

    Args:
        scope: InstallScope (PROJECT or USER).  When USER, only
            global-capable runtimes are cleaned.
    """
    if logger is None:
        logger = NullCommandLogger()
    if not stale_names:
        return

    # Determine which runtimes to clean, mirroring install-time logic.
    # Derived from ClientFactory so adding a new MCP-capable target
    # extends cleanup automatically (no parallel list to maintain).
    from apm_cli.factory import ClientFactory

    all_runtimes = ClientFactory.supported_clients()
    if runtime:  # noqa: SIM108
        target_runtimes = {runtime}
    else:
        target_runtimes = builtins.set(all_runtimes)
    if exclude:
        target_runtimes.discard(exclude)

    # Scope filtering: at USER scope, only clean global-capable runtimes.
    from apm_cli.core.scope import InstallScope

    if scope is InstallScope.USER:
        from apm_cli.factory import ClientFactory as _CF

        supported = builtins.set()
        for rt in target_runtimes:
            try:
                if _CF.create_client(rt).supports_user_scope:
                    supported.add(rt)
            except ValueError:
                pass
        target_runtimes = supported

    # Claude Code: when scope is unspecified, fail safely toward the project
    # config only -- never touch ~/.claude.json on the user's behalf without
    # an explicit USER scope, since that file is shared across all Claude
    # Code projects on the host.
    clean_claude_project = "claude" in target_runtimes and scope is not InstallScope.USER
    clean_claude_user = "claude" in target_runtimes and scope is InstallScope.USER
    if "claude" in target_runtimes and scope is None:
        logger.progress(
            "Claude Code stale cleanup: scope unspecified -- defaulting to "
            "project .mcp.json only; pass -g/--global to also clean ~/.claude.json"
        )

    # Build an expanded set that includes both the full reference and the
    # last-segment short name so we match config keys in every runtime.
    expanded_stale: builtins.set = builtins.set()
    for n in stale_names:
        expanded_stale.add(n)
        if "/" in n:
            expanded_stale.add(n.rsplit("/", 1)[-1])

    project_root_path = Path(project_root) if project_root is not None else Path.cwd()

    # Clean .vscode/mcp.json
    if "vscode" in target_runtimes:
        vscode_mcp = project_root_path / ".vscode" / "mcp.json"
        if vscode_mcp.exists():
            try:
                import json as _json

                config = _json.loads(vscode_mcp.read_text(encoding="utf-8"))
                servers = config.get("servers", {})
                removed = [n for n in expanded_stale if n in servers]
                for name in removed:
                    del servers[name]
                if removed:
                    vscode_mcp.write_text(_json.dumps(config, indent=2), encoding="utf-8")
                    for name in removed:
                        logger.progress(f"Removed stale MCP server '{name}' from .vscode/mcp.json")
            except Exception:
                _log.debug(
                    "Failed to clean stale MCP servers from .vscode/mcp.json",
                    exc_info=True,
                )

    # Clean ~/.copilot/mcp-config.json
    if "copilot" in target_runtimes:
        copilot_mcp = Path.home() / ".copilot" / "mcp-config.json"
        if copilot_mcp.exists():
            try:
                import json as _json

                config = _json.loads(copilot_mcp.read_text(encoding="utf-8"))
                servers = config.get("mcpServers", {})
                removed = [n for n in expanded_stale if n in servers]
                for name in removed:
                    del servers[name]
                if removed:
                    copilot_mcp.write_text(_json.dumps(config, indent=2), encoding="utf-8")
                    for name in removed:
                        _rich_success(
                            f"Removed stale MCP server '{name}' from Copilot CLI config",
                            symbol="check",
                        )
            except Exception:
                _log.debug(
                    "Failed to clean stale MCP servers from Copilot CLI config",
                    exc_info=True,
                )

    # Clean the scope-resolved Codex config.toml (mcp_servers section)
    if "codex" in target_runtimes:
        from apm_cli.factory import ClientFactory

        codex_cfg = Path(
            ClientFactory.create_client(
                "codex",
                project_root=project_root,
                user_scope=user_scope,
            ).get_config_path()
        )
        if codex_cfg.exists():
            try:
                import toml as _toml

                config = _toml.loads(codex_cfg.read_text(encoding="utf-8"))
                servers = config.get("mcp_servers", {})
                removed = [n for n in expanded_stale if n in servers]
                for name in removed:
                    del servers[name]
                if removed:
                    codex_cfg.write_text(_toml.dumps(config), encoding="utf-8")
                    for name in removed:
                        _rich_success(
                            f"Removed stale MCP server '{name}' from Codex CLI config",
                            symbol="check",
                        )
            except Exception:
                _log.debug(
                    "Failed to clean stale MCP servers from Codex CLI config",
                    exc_info=True,
                )

    # Clean .cursor/mcp.json (only if .cursor/ directory exists)
    if "cursor" in target_runtimes:
        cursor_mcp = project_root_path / ".cursor" / "mcp.json"
        if cursor_mcp.exists():
            try:
                import json as _json

                config = _json.loads(cursor_mcp.read_text(encoding="utf-8"))
                servers = config.get("mcpServers", {})
                removed = [n for n in expanded_stale if n in servers]
                for name in removed:
                    del servers[name]
                if removed:
                    cursor_mcp.write_text(_json.dumps(config, indent=2), encoding="utf-8")
                    for name in removed:
                        _rich_success(
                            f"Removed stale MCP server '{name}' from .cursor/mcp.json",
                            symbol="check",
                        )
            except Exception:
                _log.debug(
                    "Failed to clean stale MCP servers from .cursor/mcp.json",
                    exc_info=True,
                )

    # Clean opencode.json (only if .opencode/ directory exists)
    if "opencode" in target_runtimes:
        opencode_cfg = project_root_path / "opencode.json"
        if opencode_cfg.exists() and (project_root_path / ".opencode").is_dir():
            try:
                import json as _json

                config = _json.loads(opencode_cfg.read_text(encoding="utf-8"))
                servers = config.get("mcp", {})
                removed = [n for n in expanded_stale if n in servers]
                for name in removed:
                    del servers[name]
                if removed:
                    opencode_cfg.write_text(_json.dumps(config, indent=2), encoding="utf-8")
                    for name in removed:
                        logger.progress(f"Removed stale MCP server '{name}' from opencode.json")
            except Exception:
                _log.debug(
                    "Failed to clean stale MCP servers from opencode.json",
                    exc_info=True,
                )

    # Clean ~/.codeium/windsurf/mcp_config.json
    if "windsurf" in target_runtimes:
        windsurf_mcp = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
        if windsurf_mcp.exists():
            try:
                import json as _json

                config = _json.loads(windsurf_mcp.read_text(encoding="utf-8"))
                servers = config.get("mcpServers", {})
                removed = [n for n in expanded_stale if n in servers]
                for name in removed:
                    del servers[name]
                if removed:
                    windsurf_mcp.write_text(_json.dumps(config, indent=2), encoding="utf-8")
                    for name in removed:
                        _rich_success(
                            f"Removed stale MCP server '{name}' from Windsurf config",
                            symbol="check",
                        )
            except Exception:
                _log.debug(
                    "Failed to clean stale MCP servers from Windsurf config",
                    exc_info=True,
                )

    # Clean .gemini/settings.json (only if .gemini/ directory exists)
    if "gemini" in target_runtimes:
        gemini_cfg = Path.cwd() / ".gemini" / "settings.json"
        if gemini_cfg.exists():
            try:
                import json as _json

                config = _json.loads(gemini_cfg.read_text(encoding="utf-8"))
                servers = config.get("mcpServers", {})
                removed = [n for n in expanded_stale if n in servers]
                for name in removed:
                    del servers[name]
                if removed:
                    gemini_cfg.write_text(_json.dumps(config, indent=2), encoding="utf-8")
                    for name in removed:
                        if logger:
                            logger.progress(
                                f"Removed stale MCP server '{name}' from .gemini/settings.json"
                            )
                        else:
                            _rich_success(
                                f"Removed stale MCP server '{name}' from .gemini/settings.json",
                                symbol="check",
                            )
            except Exception:
                _log.debug(
                    "Failed to clean stale MCP servers from .gemini/settings.json",
                    exc_info=True,
                )

    # Clean Claude Code project .mcp.json (only if .claude/ directory exists)
    if clean_claude_project:
        claude_mcp = project_root_path / ".mcp.json"
        if claude_mcp.exists() and (project_root_path / ".claude").is_dir():
            try:
                import json as _json

                config = _json.loads(claude_mcp.read_text(encoding="utf-8"))
                servers = config.get("mcpServers", {})
                if not isinstance(servers, dict):
                    servers = {}
                removed = [n for n in expanded_stale if n in servers]
                for name in removed:
                    del servers[name]
                if removed:
                    claude_mcp.write_text(_json.dumps(config, indent=2) + "\n", encoding="utf-8")
                    for name in removed:
                        logger.progress(f"Removed stale MCP server '{name}' from .mcp.json")
            except Exception:
                _log.debug(
                    "Failed to clean stale MCP servers from .mcp.json",
                    exc_info=True,
                )

    # Clean Claude Code user ~/.claude.json (USER scope only)
    if clean_claude_user:
        claude_user = Path.home() / ".claude.json"
        if claude_user.exists():
            try:
                import json as _json

                config = _json.loads(claude_user.read_text(encoding="utf-8"))
                if isinstance(config, dict):
                    servers = config.get("mcpServers", {})
                    if not isinstance(servers, dict):
                        servers = {}
                    removed = [n for n in expanded_stale if n in servers]
                    for name in removed:
                        del servers[name]
                    if removed:
                        claude_user.write_text(
                            _json.dumps(config, indent=2) + "\n", encoding="utf-8"
                        )
                        for name in removed:
                            logger.progress(
                                f"Removed stale MCP server '{name}' from ~/.claude.json"
                            )
            except Exception:
                _log.debug(
                    "Failed to clean stale MCP servers from ~/.claude.json",
                    exc_info=True,
                )


# ------------------------------------------------------------------
# Lockfile persistence
# ------------------------------------------------------------------
