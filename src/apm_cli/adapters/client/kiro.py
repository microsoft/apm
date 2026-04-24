"""Kiro IDE implementation of MCP client adapter.

Kiro uses the standard ``mcpServers`` JSON format at ``.kiro/mcp.json``
(repo-local).  The config schema is identical to GitHub Copilot CLI, so this
adapter subclasses :class:`CopilotClientAdapter` and only overrides the
config-path logic and the user-facing labels.

APM only writes to ``.kiro/mcp.json`` when the ``.kiro/`` directory
already exists — Kiro support is opt-in.
"""

import json
import os
from pathlib import Path

from .copilot import CopilotClientAdapter


class KiroClientAdapter(CopilotClientAdapter):
    """Kiro IDE MCP client adapter.

    Inherits all config formatting from :class:`CopilotClientAdapter`
    (``mcpServers`` JSON with ``command``/``args``/``env``).  Only the
    config-file location differs: repo-local ``.kiro/mcp.json`` instead
    of global ``~/.copilot/mcp-config.json``.
    """

    supports_user_scope: bool = False

    def get_config_path(self):
        """Return the path to ``.kiro/mcp.json`` in the repository root."""
        kiro_dir = Path(os.getcwd()) / ".kiro"
        return str(kiro_dir / "mcp.json")

    def update_config(self, config_updates):
        """Merge *config_updates* into the ``mcpServers`` section.

        The ``.kiro/`` directory must already exist; if it does not, this
        method returns silently (opt-in behaviour).
        """
        config_path = Path(self.get_config_path())

        if not config_path.parent.exists():
            return

        current_config = self.get_current_config()
        if "mcpServers" not in current_config:
            current_config["mcpServers"] = {}

        current_config["mcpServers"].update(config_updates)

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(current_config, f, indent=2)

    def get_current_config(self):
        """Read the current ``.kiro/mcp.json`` contents."""
        config_path = self.get_config_path()

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
        """Configure an MCP server in Kiro's ``.kiro/mcp.json``."""
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
