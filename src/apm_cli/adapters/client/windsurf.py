"""Windsurf/Cascade implementation of MCP client adapter.

Windsurf uses the standard ``mcpServers`` JSON format at
``~/.codeium/windsurf/mcp_config.json`` (global).  The config schema is
identical to GitHub Copilot CLI, so this adapter subclasses
:class:`CopilotClientAdapter` and only overrides the config-path logic
and the ``_client_label`` used in log messages.

Ref: https://docs.windsurf.com/windsurf/cascade/mcp
"""

from pathlib import Path

from .copilot import CopilotClientAdapter


class WindsurfClientAdapter(CopilotClientAdapter):
    """Windsurf/Cascade MCP client adapter.

    Inherits all config formatting and MCP server configuration logic
    from :class:`CopilotClientAdapter` (``mcpServers`` JSON with
    ``command``/``args``/``env``).  Only the config-file location and
    the user-facing label differ.
    """

    supports_user_scope: bool = True
    _client_label: str = "Windsurf"
    target_name: str = "windsurf"
    mcp_servers_key: str = "mcpServers"

    # ------------------------------------------------------------------ #
    # Config path
    # ------------------------------------------------------------------ #

    def get_config_path(self) -> str:
        """Return the path to ``~/.codeium/windsurf/mcp_config.json``.

        This is a **global** config path -- Windsurf reads MCP server
        definitions from the user-level directory, not the workspace.
        """
        windsurf_dir = Path.home() / ".codeium" / "windsurf"
        return str(windsurf_dir / "mcp_config.json")
