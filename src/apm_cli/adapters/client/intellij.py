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

import json
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
    inherits all registry-resolution and env-var handling from
    :class:`CopilotClientAdapter` and overrides only the config-path and
    the two methods that reference the hard-coded ``"mcpServers"`` key.
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

    # ------------------------------------------------------------------ #
    # Config read / write
    # ------------------------------------------------------------------ #

    def update_config(self, config_updates: dict) -> None:
        """Merge *config_updates* into the ``"servers"`` section of ``mcp.json``.

        The parent implementation hard-codes ``"mcpServers"``; this override
        uses :attr:`mcp_servers_key` (``"servers"``) instead.
        """
        current_config = self.get_current_config()

        if self.mcp_servers_key not in current_config:
            current_config[self.mcp_servers_key] = {}

        current_config[self.mcp_servers_key].update(config_updates)

        config_path = Path(self.get_config_path())
        config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(current_config, f, indent=2)

    # ------------------------------------------------------------------ #
    # Security: baked-credential detection
    # ------------------------------------------------------------------ #

    def _collect_previously_baked_keys(self, server_url: str, server_name: str):
        """Return ``(env_keys, headers_were_baked)`` from the existing entry.

        The parent reads from ``"mcpServers"``; this override uses
        :attr:`mcp_servers_key` (``"servers"``) instead.
        """
        try:
            current = self.get_current_config()
        except Exception:
            return set(), False

        servers = current.get(self.mcp_servers_key) or {}
        if server_name:
            key = server_name
        elif "/" in server_url:
            key = server_url.split("/")[-1]
        else:
            key = server_url

        existing = servers.get(key)
        if not isinstance(existing, dict):
            return set(), False

        from .copilot import _has_env_placeholder

        baked_env_keys: set = set()
        env_block = existing.get("env") or {}
        if isinstance(env_block, dict):
            for k, v in env_block.items():
                if isinstance(v, str) and v.strip() and not _has_env_placeholder(v):
                    baked_env_keys.add(k)

        headers_were_baked = False
        headers_block = existing.get("headers") or {}
        if isinstance(headers_block, dict):
            for v in headers_block.values():
                if isinstance(v, str) and v.strip() and not _has_env_placeholder(v):
                    headers_were_baked = True
                    break

        return baked_env_keys, headers_were_baked
