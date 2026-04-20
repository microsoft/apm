"""Claude Code MCP client adapter.

Project scope: ``.mcp.json`` at the workspace root with top-level ``mcpServers``
(``--scope project`` in Claude Code). Writes are opt-in when ``.claude/`` exists,
matching Cursor-style directory detection.

User scope: top-level ``mcpServers`` in ``~/.claude.json`` (``--scope user``).

See https://code.claude.com/docs/en/mcp
"""

import json
import os
from pathlib import Path

from ...core.scope import InstallScope
from .copilot import CopilotClientAdapter


class ClaudeClientAdapter(CopilotClientAdapter):
    """MCP configuration for Claude Code (``mcpServers`` schema).

    Registry formatting reuses :class:`CopilotClientAdapter`, then entries are
    normalized for Claude Code's on-disk shape (stdio servers omit Copilot-only
    keys like ``type: "local"``, default ``tools``, and empty ``id``).
    """

    @staticmethod
    def _normalize_mcp_entry_for_claude_code(entry: dict) -> dict:
        """Drop Copilot-CLI-only fields that Claude Code does not use for stdio.

        Remote servers keep ``type``/``url`` (and related keys) per Claude Code
        docs.  See https://code.claude.com/docs/en/mcp
        """
        if not isinstance(entry, dict):
            return entry
        out = dict(entry)
        url = out.get("url")
        t = out.get("type")
        is_remote = bool(url) or t in ("http", "sse", "streamable-http")

        if is_remote:
            if out.get("id") in ("", None):
                out.pop("id", None)
            if out.get("tools") == ["*"]:
                out.pop("tools", None)
            return out

        if out.get("type") == "local":
            out.pop("type", None)
        if out.get("tools") == ["*"]:
            out.pop("tools", None)
        if out.get("id") in ("", None):
            out.pop("id", None)
        return out

    @staticmethod
    def _merge_mcp_server_dicts(existing_servers: dict, config_updates: dict) -> None:
        """Merge *config_updates* into *existing_servers* in place.

        Per-server entries are shallow-merged: ``{**old, **new}`` so keys present
        only on plugin- or hand-authored configs (e.g. ``type``, OAuth blocks)
        survive when an update omits them.  Keys in *new* overwrite *old* on
        conflict so APM/registry installs still refresh ``command``/``args``/etc.
        """
        for name, new_cfg in config_updates.items():
            if not isinstance(new_cfg, dict):
                existing_servers[name] = new_cfg
                continue
            prev = existing_servers.get(name)
            if isinstance(prev, dict):
                merged = {**prev, **new_cfg}
                existing_servers[name] = merged
            else:
                existing_servers[name] = dict(new_cfg)

    def _merge_and_normalize_updates(self, data: dict, config_updates: dict) -> None:
        if "mcpServers" not in data:
            data["mcpServers"] = {}
        self._merge_mcp_server_dicts(data["mcpServers"], config_updates)
        for name in config_updates:
            ent = data["mcpServers"].get(name)
            if isinstance(ent, dict):
                data["mcpServers"][name] = self._normalize_mcp_entry_for_claude_code(
                    ent
                )

    def _workspace_root(self) -> Path:
        """Project paths follow the same cwd convention as other repo-local adapters."""
        return Path(os.getcwd())

    def _is_user_scope(self) -> bool:
        return getattr(self, "mcp_install_scope", None) is InstallScope.USER

    def _project_mcp_path(self) -> Path:
        return self._workspace_root() / ".mcp.json"

    def _user_claude_json_path(self) -> Path:
        return Path.home() / ".claude.json"

    def _should_write_project(self) -> bool:
        return (self._workspace_root() / ".claude").is_dir()

    def get_config_path(self):
        if self._is_user_scope():
            return str(self._user_claude_json_path())
        return str(self._project_mcp_path())

    def get_current_config(self):
        if self._is_user_scope():
            path = self._user_claude_json_path()
            if not path.is_file():
                return {"mcpServers": {}}
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return {"mcpServers": {}}
                return {"mcpServers": dict(data.get("mcpServers") or {})}
            except (json.JSONDecodeError, OSError):
                return {"mcpServers": {}}
        path = self._project_mcp_path()
        if not path.is_file():
            return {"mcpServers": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"mcpServers": {}}
            return {"mcpServers": dict(data.get("mcpServers") or {})}
        except (json.JSONDecodeError, OSError):
            return {"mcpServers": {}}

    def update_config(self, config_updates, enabled=True):
        if self._is_user_scope():
            return self._merge_user_mcp(config_updates)
        if not self._should_write_project():
            return True
        path = self._project_mcp_path()
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}
            self._merge_and_normalize_updates(data, config_updates)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return True
        except OSError:
            return False

    def _merge_user_mcp(self, config_updates) -> bool:
        path = self._user_claude_json_path()
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}
            self._merge_and_normalize_updates(data, config_updates)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return True
        except OSError:
            return False

    def configure_mcp_server(
        self,
        server_url,
        server_name=None,
        enabled=True,
        env_overrides=None,
        server_info_cache=None,
        runtime_vars=None,
    ):
        if not server_url:
            print("Error: server_url cannot be empty")
            return False

        if not self._is_user_scope() and not self._should_write_project():
            return True

        try:
            if server_info_cache and server_url in server_info_cache:
                server_info = server_info_cache[server_url]
            else:
                server_info = self.registry_client.find_server_by_reference(server_url)

            if not server_info:
                print(f"Error: MCP server '{server_url}' not found in registry")
                return False

            if server_name:
                config_key = server_name
            elif "/" in server_url:
                config_key = server_url.split("/")[-1]
            else:
                config_key = server_url

            server_config = self._format_server_config(
                server_info, env_overrides, runtime_vars
            )
            self.update_config({config_key: server_config})

            print(f"Successfully configured MCP server '{config_key}' for Claude Code")
            return True

        except Exception as e:
            print(f"Error configuring MCP server: {e}")
            return False
