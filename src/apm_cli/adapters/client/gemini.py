"""Gemini CLI implementation of MCP client adapter.

Gemini CLI uses ``.gemini/settings.json`` at the project root with an
``mcpServers`` key.  The schema is nearly identical to Copilot's:

.. code-block:: json

   {
     "mcpServers": {
       "server-name": {
         "command": "npx",
         "args": ["-y", "@modelcontextprotocol/server-foo"],
         "env": { "KEY": "value" }
       }
     }
   }

APM only writes to ``.gemini/settings.json`` when the ``.gemini/``
directory already exists -- Gemini CLI support is opt-in.

Ref: https://geminicli.com/docs/reference/configuration/
"""

import json
import logging
import os
from pathlib import Path

from .copilot import CopilotClientAdapter
from ...utils.console import _rich_error, _rich_info, _rich_success

logger = logging.getLogger(__name__)


class GeminiClientAdapter(CopilotClientAdapter):
    """Gemini CLI MCP client adapter.

    Reuses Copilot's config formatting (``mcpServers`` schema is
    compatible) and writes to ``.gemini/settings.json`` in the
    project root.
    """

    supports_user_scope: bool = True

    def get_config_path(self):
        """Return the path to ``.gemini/settings.json`` in the repository root."""
        return str(Path(os.getcwd()) / ".gemini" / "settings.json")

    def update_config(self, config_updates):
        """Merge *config_updates* into the ``mcpServers`` section of settings.json.

        The ``.gemini/`` directory must already exist; if it does not, this
        method returns silently (opt-in behaviour).

        Preserves all other top-level keys in settings.json (theme, tools,
        hooks, etc.).
        """
        gemini_dir = Path(os.getcwd()) / ".gemini"
        if not gemini_dir.is_dir():
            return

        config_path = Path(self.get_config_path())
        current_config = self.get_current_config()
        if "mcpServers" not in current_config:
            current_config["mcpServers"] = {}

        for name, entry in config_updates.items():
            current_config["mcpServers"][name] = entry

        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(current_config, f, indent=2)

    def get_current_config(self):
        """Read the current ``.gemini/settings.json`` contents."""
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
        """Configure an MCP server in ``.gemini/settings.json``.

        Delegates to the parent for config formatting, then writes to
        the Gemini CLI settings file.
        """
        if not server_url:
            _rich_error("server_url cannot be empty", symbol="error")
            return False

        gemini_dir = Path(os.getcwd()) / ".gemini"
        if not gemini_dir.is_dir():
            return True

        try:
            if server_info_cache and server_url in server_info_cache:
                server_info = server_info_cache[server_url]
            else:
                server_info = self.registry_client.find_server_by_reference(server_url)

            if not server_info:
                _rich_error(f"MCP server '{server_url}' not found in registry", symbol="error")
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

            _rich_success(
                f"Configured MCP server '{config_key}' for Gemini CLI", symbol="success"
            )
            return True

        except Exception as e:
            logger.debug("Gemini MCP configuration failed: %s", e)
            _rich_error("Failed to configure MCP server for Gemini CLI", symbol="error")
            return False
