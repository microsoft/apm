"""OpenCode implementation of MCP client adapter.

OpenCode uses ``opencode.json`` at the project root with an ``mcp`` key.
The schema differs from VSCode/Cursor:

.. code-block:: json

   {
     "mcp": {
       "server-name": {
         "type": "local",
         "command": ["npx", "-y", "@modelcontextprotocol/server-foo"],
         "environment": { "KEY": "value" },
         "enabled": true
       }
     }
   }

Key differences from Copilot/Cursor:
- Config file: ``opencode.json`` (not ``mcp.json``)
- Wrapper key: ``mcp`` (not ``mcpServers``)
- Command format: single array ``command`` (not ``command`` + ``args``)
- Env key: ``environment`` (not ``env``)

APM only writes to ``opencode.json`` when the ``.opencode/`` directory
already exists — OpenCode support is opt-in.
"""

import json
import os
from pathlib import Path

from .copilot import CopilotClientAdapter


class OpenCodeClientAdapter(CopilotClientAdapter):
    """OpenCode MCP client adapter.

    Converts the standard Copilot config format into OpenCode's schema
    and writes to ``opencode.json`` in the project root.
    """

    supports_user_scope: bool = False

    def get_config_path(self):
        """Return the path to ``opencode.json`` in the repository root."""
        return str(Path(os.getcwd()) / "opencode.json")

    def update_config(self, config_updates, enabled=True):
        """Merge *config_updates* into the ``mcp`` section of ``opencode.json``.

        The ``.opencode/`` directory must already exist; if it does not, this
        method returns silently (opt-in behaviour).

        Translates Copilot-format entries (``command``/``args``/``env``) into
        OpenCode format (``command`` array / ``environment``).
        """
        opencode_dir = Path(os.getcwd()) / ".opencode"
        if not opencode_dir.is_dir():
            return

        config_path = Path(self.get_config_path())
        current_config = self.get_current_config()
        if "mcp" not in current_config:
            current_config["mcp"] = {}

        for name, copilot_entry in config_updates.items():
            current_config["mcp"][name] = self._to_opencode_format(copilot_entry, enabled=enabled)

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(current_config, f, indent=2)

    def get_current_config(self):
        """Read the current ``opencode.json`` contents."""
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
        """Configure an MCP server in ``opencode.json``.

        Delegates to the parent for config formatting, then converts to
        OpenCode schema before writing.
        """
        if not server_url:
            print("Error: server_url cannot be empty")
            return False

        opencode_dir = Path(os.getcwd()) / ".opencode"
        if not opencode_dir.is_dir():
            return False

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
            self.update_config({config_key: server_config}, enabled=enabled)

            print(
                f"Successfully configured MCP server '{config_key}' for OpenCode"
            )
            return True

        except Exception as e:
            print(f"Error configuring MCP server: {e}")
            return False

    @staticmethod
    def _to_opencode_format(copilot_entry: dict, enabled: bool = True) -> dict:
        """Convert a Copilot-format server config to OpenCode format.

        Copilot: ``{"command": "npx", "args": ["-y", "pkg"], "env": {...}}``
        OpenCode: ``{"type": "local", "command": ["npx", "-y", "pkg"],
                     "environment": {...}, "enabled": true}``
        """
        entry: dict = {"type": "local", "enabled": enabled}

        cmd = copilot_entry.get("command", "")
        args = copilot_entry.get("args", [])
        if cmd:
            entry["command"] = [cmd] + list(args)
        elif "url" in copilot_entry:
            entry["type"] = "remote"
            entry["url"] = copilot_entry["url"]
            headers = copilot_entry.get("headers")
            if headers:
                entry["headers"] = dict(headers)

        env = copilot_entry.get("env") or {}
        if env:
            entry["environment"] = dict(env)

        return entry
