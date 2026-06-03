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

from ...utils.path_security import PathTraversalError, ensure_path_within
from .copilot import CopilotClientAdapter


def _intellij_config_dir() -> Path:
    """Return the OS-specific JetBrains Copilot config directory.

    Does not guarantee the directory exists; callers that need to write
    to it should call ``mkdir(parents=True, exist_ok=True)`` first.

    Raises
    ------
    PathTraversalError
        If the environment variable the location is derived from
        (``LOCALAPPDATA`` on Windows, ``XDG_DATA_HOME`` on Linux) is unset
        or not absolute.  Without this guard an empty ``LOCALAPPDATA``
        would yield ``Path("")`` -- a *relative* path -- causing APM to
        silently read/write ``./github-copilot/intellij/mcp.json`` in the
        current working directory and to falsely auto-detect the runtime.
    """
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if not local_app_data or not os.path.isabs(local_app_data):
            raise PathTraversalError(
                "LOCALAPPDATA is unset or not an absolute path; cannot locate the "
                "JetBrains Copilot configuration directory."
            )
        return Path(local_app_data) / "github-copilot" / "intellij"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "github-copilot" / "intellij"

    # Linux: honour $XDG_DATA_HOME, fall back to ~/.local/share
    xdg_data = os.environ.get("XDG_DATA_HOME", "")
    if xdg_data:
        if not os.path.isabs(xdg_data):
            raise PathTraversalError(
                "XDG_DATA_HOME is set to a non-absolute path; cannot locate the "
                "JetBrains Copilot configuration directory."
            )
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
        """Return the OS-specific path to ``mcp.json`` for JetBrains Copilot.

        The config directory is derived from an environment variable
        (``LOCALAPPDATA`` / ``XDG_DATA_HOME``).  Validate it resolves
        inside the user's home directory before any read/write so a
        tampered or unexpected environment cannot redirect APM's writes
        outside the user-scope tree (supply-chain hardening).
        """
        config_dir = _intellij_config_dir()
        ensure_path_within(config_dir, Path.home())
        return str(config_dir / "mcp.json")
