"""OpenCode implementation of MCP client adapter.

At project scope, OpenCode uses ``opencode.json`` at the project root with an
``mcp`` key when the project-root ``.opencode/`` directory exists. Project-scope
writes require ``.opencode/``; user scope creates the resolved user
configuration root as needed.
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

from ...models.dependency.mcp import _EXTRA_DENYLIST
from ...utils.atomic_io import atomic_write_text
from .copilot import CopilotClientAdapter


class OpenCodeClientAdapter(CopilotClientAdapter):
    """OpenCode MCP client adapter.

    Converts the standard Copilot config format into OpenCode's schema.
    Project scope writes ``opencode.json`` at the project root when
    ``.opencode/`` exists; user scope writes to ``opencode.json`` in the
    resolved user configuration root, creating that root when needed.
    """

    supports_user_scope: bool = True
    target_name: str = "opencode"
    mcp_servers_key: str = "mcp"

    # OpenCode's config runtime-substitution support has not yet been
    # individually audited (see #1152). Pin to legacy install-time
    # resolution so this adapter is unchanged by the Copilot security fix;
    # revisit in a follow-up.
    _supports_runtime_env_substitution: bool = False

    def _project_guard_dir(self) -> Path:
        """Return the project opt-in directory."""
        return self.project_root / ".opencode"

    def _user_config_root(self) -> Path:
        """Return the native OpenCode user configuration root."""
        from ...integration.opencode_paths import opencode_user_config_path

        return opencode_user_config_path()

    def _get_config_dir(self) -> Path:
        """Return the scope-appropriate OpenCode directory."""
        return self._user_config_root() if self.user_scope else self._project_guard_dir()

    def get_config_path(self):
        """Return ``opencode.json`` in the scope-appropriate OpenCode root."""
        if self.user_scope:
            return str(self._get_config_dir() / "opencode.json")
        return str(self.project_root / "opencode.json")

    def update_config(self, config_updates, enabled=True):
        """Merge *config_updates* into the ``mcp`` section of ``opencode.json``.

        At project scope, the ``.opencode/`` directory must already exist; if
        it does not, this method returns silently (opt-in behaviour). At user
        scope, the resolved user configuration root is created as needed.

        Translates Copilot-format entries (``command``/``args``/``env``) into
        OpenCode format (``command`` array / ``environment``).
        """
        opencode_dir = self._get_config_dir()
        if not self.user_scope and not opencode_dir.is_dir():
            return
        if self.user_scope:
            opencode_dir.mkdir(parents=True, exist_ok=True)

        config_path = Path(self.get_config_path())
        current_config = self.get_current_config()
        if "mcp" not in current_config:
            current_config["mcp"] = {}

        for name, copilot_entry in config_updates.items():
            current_config["mcp"][name] = self._to_opencode_format(copilot_entry, enabled=enabled)

        atomic_write_text(
            config_path,
            json.dumps(current_config, indent=2),
            new_file_mode=0o600 if self.user_scope else None,
        )
        if self.user_scope and os.name != "nt":
            os.chmod(config_path, 0o600)

    def render_server_config(self, server_info: dict) -> dict:
        """Render the exact OpenCode-native stored server shape."""
        return self._to_opencode_format(
            self._format_server_config(server_info, {}, {}),
        )

    def get_current_config(self):
        """Read the scope-appropriate ``opencode.json`` contents."""
        config_path = self.get_config_path()
        if not os.path.exists(config_path):
            return {}
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)

    def configure_mcp_server(
        self,
        server_url,
        server_name=None,
        enabled=True,
        env_overrides=None,
        server_info_cache=None,
        runtime_vars=None,
    ):
        """Configure an MCP server in the scope-appropriate ``opencode.json``.

        Delegates to the parent for config formatting, then converts to
        OpenCode schema before writing.
        """
        if not server_url:
            print("Error: server_url cannot be empty")
            return False

        if not self.user_scope and not self._get_config_dir().is_dir():
            return False

        try:
            server_info = self._fetch_server_info(server_url, server_info_cache)
            if server_info is None:
                return False

            config_key = self._determine_config_key(server_url, server_name)

            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)
            self.update_config({config_key: server_config}, enabled=enabled)

            print(f"Successfully configured MCP server '{config_key}' for OpenCode")
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
