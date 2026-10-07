"""OpenCode implementation of MCP client adapter.

OpenCode uses ``opencode.json`` at the project root or
``~/.config/opencode/opencode.json`` at user scope, with an ``mcp`` key.
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

At project scope APM only writes to ``opencode.json`` when the
``.opencode/`` directory already exists -- OpenCode support is opt-in.
"""

import json
import os
from pathlib import Path
from typing import Any

from ...models.dependency.mcp import _EXTRA_DENYLIST, opencode_enabled_value
from .copilot import CopilotClientAdapter


class OpenCodeClientAdapter(CopilotClientAdapter):
    """OpenCode MCP client adapter.

    Converts the standard Copilot config format into OpenCode's schema
    and writes to the project or user-scope ``opencode.json``.
    """

    supports_user_scope: bool = True
    target_name: str = "opencode"
    mcp_servers_key: str = "mcp"

    # OpenCode's config runtime-substitution support has not yet been
    # individually audited (see #1152). Pin to legacy install-time
    # resolution so this adapter is unchanged by the Copilot security fix;
    # revisit in a follow-up.
    _supports_runtime_env_substitution: bool = False

    @staticmethod
    def config_path_for(project_root: Path, user_scope: bool) -> Path:
        """Return the OpenCode config path for a scope.

        ``~/.config/opencode/opencode.json`` at user scope, else
        ``opencode.json`` under *project_root*.  Static so stale cleanup can
        locate the file without constructing an adapter.
        """
        if user_scope:
            return Path.home() / ".config" / "opencode" / "opencode.json"
        return Path(project_root) / "opencode.json"

    def get_config_path(self):
        """Return the project or user-scope OpenCode config path."""
        return str(self.config_path_for(self.project_root, self.user_scope))

    def update_config(self, config_updates, enabled: Any = True):
        """Merge *config_updates* into the ``mcp`` section of ``opencode.json``.

        The project ``.opencode/`` directory must already exist. User-scope
        installs create ``~/.config/opencode/`` when needed.  Returns
        ``False`` without writing when the existing file cannot be rewritten
        safely.

        Translates Copilot-format entries (``command``/``args``/``env``) into
        OpenCode format (``command`` array / ``environment``).
        """
        opencode_dir = self.project_root / ".opencode"
        if not self.user_scope and not opencode_dir.is_dir():
            return None

        config_path = Path(self.get_config_path())
        current_config = self._read_json_config_for_update(config_path)
        if current_config is None:
            return False
        if "mcp" not in current_config:
            current_config["mcp"] = {}

        for name, copilot_entry in config_updates.items():
            current_config["mcp"][name] = self._to_opencode_format(copilot_entry, enabled=enabled)

        self._write_json_config(config_path, current_config)
        return True

    def validate_config_for_install(self) -> bool:
        """Refuse unsafe user configs even when no server write would be needed."""
        return (
            not self.user_scope
            or self._read_json_config_for_update(Path(self.get_config_path())) is not None
        )

    def get_current_config(self):
        """Read the current ``opencode.json`` contents."""
        config_path = self.get_config_path()
        if not os.path.exists(config_path):
            return {}
        try:
            with open(config_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def render_server_config(self, server_info: dict) -> dict:
        """Render the ``mcp`` entry APM writes, for exact baseline comparisons."""
        return self._to_opencode_format(
            super().render_server_config(server_info),
            enabled=opencode_enabled_value(server_info),
        )

    def configure_mcp_server(
        self,
        server_url,
        server_name=None,
        enabled: Any = True,
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

        opencode_dir = self.project_root / ".opencode"
        if not self.user_scope and not opencode_dir.is_dir():
            return False

        try:
            server_info = self._fetch_server_info(server_url, server_info_cache)
            if server_info is None:
                return False

            config_key = self._determine_config_key(server_url, server_name)

            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)
            written = self.update_config(
                {config_key: server_config},
                enabled=opencode_enabled_value(server_info, enabled),
            )
            if written is False:
                return False

            print(f"Successfully configured MCP server '{config_key}' for OpenCode")
            return True

        except Exception as e:
            print(f"Error configuring MCP server: {e}")
            return False

    @staticmethod
    def _to_opencode_format(copilot_entry: dict, enabled: Any = True) -> dict:
        """Convert a Copilot-format server config to OpenCode format.

        Copilot: ``{"command": "npx", "args": ["-y", "pkg"], "env": {...}}``
        OpenCode: ``{"type": "local", "command": ["npx", "-y", "pkg"],
                     "environment": {...}, "enabled": true}``
        """
        entry: dict = {"type": "local", "enabled": enabled}

        cmd = copilot_entry.get("command", "")
        args = copilot_entry.get("args", [])
        if cmd:
            entry["command"] = [cmd] + list(args)  # noqa: RUF005
        elif "url" in copilot_entry:
            entry["type"] = "remote"
            entry["url"] = copilot_entry["url"]
            headers = copilot_entry.get("headers")
            if headers:
                entry["headers"] = dict(headers)

        env = copilot_entry.get("env") or {}
        if env:
            entry["environment"] = dict(env)

        translated_keys = _EXTRA_DENYLIST
        for key, value in copilot_entry.items():
            if key not in translated_keys and key not in entry:
                entry[key] = value

        return entry
