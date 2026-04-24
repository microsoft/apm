"""Kiro IDE/CLI implementation of MCP client adapter.

Kiro stores MCP configuration at ``.kiro/settings/mcp.json`` (project-local)
or ``~/.kiro/settings/mcp.json`` (global/user scope).  The schema uses the
standard ``mcpServers`` object so the Copilot formatter is reused directly.

APM writes to ``.kiro/settings/mcp.json`` only when the ``.kiro/`` directory
already exists — Kiro support is opt-in at project scope.  At user scope
(``--global``), ``~/.kiro/settings/`` is auto-created.

Ref: https://kiro.dev/docs/mcp/configuration/
"""

import json
import os
from pathlib import Path

from .copilot import CopilotClientAdapter


class KiroClientAdapter(CopilotClientAdapter):
    """Kiro IDE/CLI MCP client adapter.

    Inherits all config formatting from :class:`CopilotClientAdapter`
    (``mcpServers`` JSON with ``command``/``args``/``env``).  Overrides
    the config-file path to target ``.kiro/settings/mcp.json``.

    User-scope (``--global``) writes to ``~/.kiro/settings/mcp.json``.
    Project-scope writes to ``<cwd>/.kiro/settings/mcp.json`` only when
    ``.kiro/`` already exists.
    """

    supports_user_scope: bool = True

    def get_config_path(self, user_scope: bool = False):
        """Return path to the Kiro MCP config file.

        Args:
            user_scope: When True, return the global ``~/.kiro/settings/mcp.json``.
        """
        if user_scope:
            return str(Path.home() / ".kiro" / "settings" / "mcp.json")
        kiro_dir = Path(os.getcwd()) / ".kiro"
        return str(kiro_dir / "settings" / "mcp.json")

    def update_config(self, config_updates, user_scope: bool = False):
        """Merge *config_updates* into the ``mcpServers`` section.

        At project scope the ``.kiro/`` directory must already exist; if it
        does not, this method returns silently (opt-in behaviour).
        At user scope the ``~/.kiro/settings/`` directory is created on demand.
        """
        config_path = Path(self.get_config_path(user_scope=user_scope))

        if user_scope:
            config_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            kiro_root = config_path.parent.parent  # .kiro/
            if not kiro_root.exists():
                return
            config_path.parent.mkdir(parents=True, exist_ok=True)

        current_config = self.get_current_config(user_scope=user_scope)
        if "mcpServers" not in current_config:
            current_config["mcpServers"] = {}

        current_config["mcpServers"].update(config_updates)

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(current_config, f, indent=2)

    def get_current_config(self, user_scope: bool = False):
        """Read the current Kiro MCP config file contents."""
        config_path = self.get_config_path(user_scope=user_scope)

        if not os.path.exists(config_path):
            return {}

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def configure_mcp_server(
        self,
        server_url,
        server_name=None,
        enabled=True,
        env_overrides=None,
        server_info_cache=None,
        runtime_vars=None,
    ):
        """Configure an MCP server in ``.kiro/settings/mcp.json``."""
        if not server_url:
            print("Error: server_url cannot be empty")
            return False

        kiro_dir = Path(os.getcwd()) / ".kiro"
        if not kiro_dir.exists():
            return True  # nothing to do, not an error

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

            print(f"Successfully configured MCP server '{config_key}' for Kiro")
            return True

        except Exception as e:
            print(f"Error configuring MCP server: {e}")
            return False
