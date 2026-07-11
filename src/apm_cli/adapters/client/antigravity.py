"""Antigravity CLI (agy) implementation of MCP client adapter.

Antigravity CLI is Google's Gemini-derived agentic CLI.  Unlike Gemini
CLI (which stores MCP servers in ``settings.json``), Antigravity reads a
dedicated ``mcp_config.json`` with an ``mcpServers`` key -- the same JSON
schema, a different file:

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

Scope resolution: project scope writes to
``<project_root>/.agents/mcp_config.json`` (opt-in -- the directory must
already exist).  User scope writes to ``~/.gemini/config/mcp_config.json``
unconditionally, creating the directory if needed.

Ref: https://antigravity.google/docs/mcp
"""

from pathlib import Path

from .gemini import GeminiClientAdapter


class AntigravityClientAdapter(GeminiClientAdapter):
    """Antigravity CLI MCP client adapter.

    Reuses GeminiClientAdapter's ``_format_server_config`` and
    ``configure_mcp_server`` (identical ``mcpServers`` JSON schema) and
    overrides only the config directory, the config filename, and the
    display name.  Antigravity writes MCP servers to a dedicated
    ``mcp_config.json`` rather than ``settings.json``.
    """

    supports_user_scope: bool = True
    target_name: str = "antigravity"

    def _get_config_dir(self) -> Path:
        """Return the ``.agents`` or ``~/.gemini/config`` directory."""
        if self.user_scope:
            return Path.home() / ".gemini" / "config"
        return self.project_root / ".agents"

    def get_config_path(self):
        """Return the path to ``mcp_config.json`` for the active scope."""
        return str(self._get_config_dir() / "mcp_config.json")

    def _format_server_config(self, server_info, env_overrides=None, runtime_vars=None):
        """Format server info into Antigravity CLI MCP configuration.

        Delegates to the parent (GeminiClientAdapter) for initial formatting,
        then replaces any ``url`` or ``httpUrl`` key with ``serverUrl`` for
        remote endpoints to match Antigravity's schema.
        """
        config = super()._format_server_config(
            server_info, env_overrides=env_overrides, runtime_vars=runtime_vars
        )
        if isinstance(config, dict):
            if "url" in config:
                config["serverUrl"] = config.pop("url")
            elif "httpUrl" in config:
                config["serverUrl"] = config.pop("httpUrl")
        return config
