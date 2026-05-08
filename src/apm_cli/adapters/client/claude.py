"""Claude Code MCP client adapter.

Project scope: ``.mcp.json`` at the project root with top-level ``mcpServers``
(``--scope project`` in Claude Code). Writes are opt-in when ``.claude/`` exists,
mirroring the Cursor/OpenCode directory-presence convention.

User scope: top-level ``mcpServers`` in ``~/.claude.json`` (``--scope user``).
Writes are atomic and create the file with ``0o600`` permissions on first
write so the shared Claude Code config is not silently truncated by a
concurrent writer and cannot leak any embedded OAuth state to other users
on a multi-user host.

Local scope (Claude Code's third scope -- the default for ``claude mcp add``,
storing per-project private config under ``~/.claude.json -> projects.<abs_path>
.mcpServers``) is intentionally NOT supported: APM packages are designed for
reproducible team installs, which aligns with PROJECT (VCS-shared) and USER
(cross-project), not LOCAL (per-project private to one user).

See https://code.claude.com/docs/en/mcp
"""

import json
from pathlib import Path

from ...utils.atomic_io import atomic_write_text
from ...utils.console import _rich_error, _rich_success, _rich_warning
from .copilot import CopilotClientAdapter


class ClaudeClientAdapter(CopilotClientAdapter):
    """MCP configuration for Claude Code (``mcpServers`` schema).

    Registry formatting reuses :class:`CopilotClientAdapter`, then entries are
    normalized for Claude Code's on-disk shape (stdio servers omit Copilot-only
    keys like ``type: "local"``, default ``tools``, and empty ``id``).

    Scope routing is governed by the ``user_scope`` constructor flag inherited
    from :class:`MCPClientAdapter` -- not by post-construction monkey-patching.
    """

    supports_user_scope: bool = True
    target_name: str = "claude"
    mcp_servers_key: str = "mcpServers"

    # Claude Desktop / Code's mcp config does NOT support runtime env-var
    # substitution -- the value in ``env`` must be a literal string. This
    # adapter MUST keep the legacy install-time resolution behaviour.
    # See #1152 supply-chain analysis.
    _supports_runtime_env_substitution: bool = False

    @staticmethod
    def _normalize_mcp_entry_for_claude_code(entry: dict) -> dict:
        """Normalize a server entry to Claude Code's on-disk shape.

        For remote servers, keep ``type``/``url``/``headers`` per Claude
        Code docs.  For stdio servers, drop Copilot-CLI-only fields
        (``type: "local"``, default ``tools``, empty ``id``) and emit
        an explicit ``type: "stdio"`` so ``claude mcp list`` renders
        the entry the same way it would if installed via
        ``claude mcp add --transport stdio``.

        See https://code.claude.com/docs/en/mcp
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

        if out.get("type") in (None, "local", "stdio"):
            out["type"] = "stdio"
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
                data["mcpServers"][name] = self._normalize_mcp_entry_for_claude_code(ent)

    def _is_user_scope(self) -> bool:
        return bool(self.user_scope)

    def _project_mcp_path(self) -> Path:
        return self.project_root / ".mcp.json"

    def _user_claude_json_path(self) -> Path:
        return Path.home() / ".claude.json"

    def _should_write_project(self) -> bool:
        return (self.project_root / ".claude").is_dir()

    def get_config_path(self):
        if self._is_user_scope():
            return str(self._user_claude_json_path())
        return str(self._project_mcp_path())

    def get_current_config(self):
        path = self._user_claude_json_path() if self._is_user_scope() else self._project_mcp_path()
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
            _rich_warning(
                f"Skipped Claude Code project MCP -- .claude/ not found in {self.project_root}"
            )
            return False
        path = self._project_mcp_path()
        try:
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    _rich_warning(f"Existing {path} is not valid JSON; rewriting from scratch")
                    data = {}
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
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    _rich_warning(f"Existing {path} is not valid JSON; rewriting from scratch")
                    data = {}
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}
            self._merge_and_normalize_updates(data, config_updates)
            payload = json.dumps(data, indent=2) + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, payload, new_file_mode=0o600)
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
            _rich_error("server_url cannot be empty")
            return False

        if not self._is_user_scope() and not self._should_write_project():
            _rich_warning(
                f"Skipped Claude Code project MCP -- .claude/ not found in {self.project_root}"
            )
            return False

        try:
            if server_info_cache and server_url in server_info_cache:
                server_info = server_info_cache[server_url]
            else:
                server_info = self.registry_client.find_server_by_reference(server_url)

            if not server_info:
                _rich_error(f"MCP server '{server_url}' not found in registry")
                return False

            if server_name:
                config_key = server_name
            elif "/" in server_url:
                config_key = server_url.split("/")[-1]
            else:
                config_key = server_url

            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)
            ok = self.update_config({config_key: server_config})
            if not ok:
                _rich_error(f"Failed to write MCP config for '{config_key}' to Claude Code")
                return False

            _rich_success(f"Successfully configured MCP server '{config_key}' for Claude Code")
            return True

        except Exception:
            # Do not interpolate the exception message: registry URLs and
            # other inputs may carry embedded credentials.
            _rich_error("Error configuring MCP server")
            return False
