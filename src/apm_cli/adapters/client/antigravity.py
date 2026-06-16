"""Antigravity CLI (agy) implementation of MCP client adapter.

Antigravity CLI is Google's successor to Gemini CLI.  It reads
``.agent/settings.json`` with an ``mcpServers`` key -- the same JSON
schema as Gemini CLI's ``settings.json``.

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
``<project_root>/.agent/settings.json`` (opt-in -- the directory must
already exist).  User scope writes to ``~/.antigravity/settings.json``
unconditionally, creating the directory if needed.

Ref: https://antigravity.google/docs/mcp
"""

from pathlib import Path

from .gemini import GeminiClientAdapter


class AntigravityClientAdapter(GeminiClientAdapter):
    """Antigravity CLI MCP client adapter.

    Reuses GeminiClientAdapter's ``_format_server_config`` and
    ``configure_mcp_server`` (identical ``mcpServers`` JSON schema)
    and overrides only the directory and display-name logic.
    """

    supports_user_scope: bool = True
    target_name: str = "antigravity"

    def _get_config_dir(self) -> Path:
        """Return the ``.agent`` or ``~/.antigravity`` directory."""
        if self.user_scope:
            return Path.home() / ".antigravity"
        return self.project_root / ".agent"

    def get_config_path(self):
        """Return the path to ``settings.json`` for the active scope."""
        return str(self._get_config_dir() / "settings.json")
