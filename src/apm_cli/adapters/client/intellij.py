"""JetBrains Copilot implementation of the MCP client adapter.

GitHub Copilot for JetBrains IDEs stores MCP server configuration under
a user-scope directory whose location is OS-dependent:

  Windows  : %LOCALAPPDATA%\\github-copilot\\intellij\\mcp.json
  macOS    : ~/Library/Application Support/github-copilot/intellij/mcp.json
  Linux    : $XDG_DATA_HOME/github-copilot/intellij/mcp.json
               (defaults to ~/.local/share/github-copilot/intellij/mcp.json)

The configuration file uses a top-level ``"servers"`` key (unlike most
other Copilot-family adapters which use ``"mcpServers"``).

Ref: https://github.com/orgs/community/discussions/139762
"""

import os
import sys
from pathlib import Path

from .copilot import CopilotClientAdapter


def _intellij_config_dir() -> Path:
    """Return the OS-specific JetBrains Copilot config directory.

    Does not guarantee the directory exists; callers that need to write
    to it should call ``mkdir(parents=True, exist_ok=True)`` first.
    """
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        return Path(local_app_data) / "github-copilot" / "intellij"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "github-copilot" / "intellij"

    # Linux: honour $XDG_DATA_HOME, fall back to ~/.local/share
    xdg_data = os.environ.get("XDG_DATA_HOME", "")
    if xdg_data:
        return Path(xdg_data) / "github-copilot" / "intellij"
    return Path.home() / ".local" / "share" / "github-copilot" / "intellij"


class IntelliJClientAdapter(CopilotClientAdapter):
    """MCP client adapter for GitHub Copilot inside JetBrains IDEs.

    JetBrains Copilot stores server definitions in a user-scope JSON file
    with a ``"servers"`` top-level key (not ``"mcpServers"``).  This adapter
    inherits all registry-resolution, env-var handling, and config read/write
    from :class:`CopilotClientAdapter` and overrides only the config path and
    ``mcp_servers_key`` so the parent methods operate on the correct key.
    """

    supports_user_scope: bool = True
    _client_label: str = "JetBrains Copilot"
    target_name: str = "intellij"
    mcp_servers_key: str = "servers"

    # JetBrains runtime-substitution support has not yet been audited.
    # Pin to install-time resolution for safety; revisit in a follow-up.
    _supports_runtime_env_substitution: bool = False

    # ------------------------------------------------------------------ #
    # Config path
    # ------------------------------------------------------------------ #

    def get_config_path(self) -> str:
        """Return the OS-specific path to ``mcp.json`` for JetBrains Copilot."""
        return str(_intellij_config_dir() / "mcp.json")
