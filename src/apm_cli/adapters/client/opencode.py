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

OpenCode v2 nests servers under ``mcp.servers`` and replaces ``enabled``
with its inverse, ``disabled``.  Both versions read the same files, but
v1 cannot read the v2 layout, so APM writes v2 entries only when the
target file already declares an ``mcp.servers`` map.  Otherwise it keeps
the v1 layout above, which v2 also accepts.

At project scope APM only writes to ``opencode.json`` when the
``.opencode/`` directory already exists -- OpenCode support is opt-in.
"""

import json
import os
import re
from pathlib import Path
from typing import Any

from ...models.dependency.mcp import (
    _EXTRA_DENYLIST,
    opencode_enabled_matches,
    opencode_enabled_value,
)
from ...utils.console import _rich_warning
from .copilot import CopilotClientAdapter

_ENV_REF_TEMPLATE = "{env:%s}"
_ENV_REF_RE = re.compile(re.escape(_ENV_REF_TEMPLATE) % r"([A-Za-z_][A-Za-z0-9_]*)")

_V2_SERVERS_KEY = "servers"
# Settings that share the ``mcp`` object with v1 server entries in v2 files.
V2_MCP_SETTINGS = frozenset({_V2_SERVERS_KEY, "timeout"})


class OpenCodeClientAdapter(CopilotClientAdapter):
    """OpenCode MCP client adapter.

    Converts the standard Copilot config format into OpenCode's schema
    and writes to the project or user-scope ``opencode.json``.
    """

    supports_user_scope: bool = True
    target_name: str = "opencode"
    mcp_servers_key: str = "mcp"

    # OpenCode resolves {env:NAME} throughout config strings, including MCP
    # environment, headers and command args (https://opencode.ai/docs/config/#env-vars).
    # Keep referenced secrets out of generated configuration; see #3051.
    _supports_runtime_env_substitution: bool = True

    def _format_runtime_env_placeholder(self, name: str) -> str:
        """Return OpenCode's native environment reference."""
        return _ENV_REF_TEMPLATE % name

    def _resolve_environment_variables(
        self,
        env_vars: dict[str, object] | list[dict[str, object]],
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Preserve explicit literals; translate references for runtime lookup."""
        if not isinstance(env_vars, dict):
            return super()._resolve_environment_variables(env_vars, env_overrides)
        return self._resolve_explicit_environment_values(env_vars, env_overrides)

    def _format_server_config(self, server_info, env_overrides=None, runtime_vars=None):
        """Preserve per-server authored registry env values after normal rendering."""
        config = super()._format_server_config(server_info, env_overrides, runtime_vars)
        overlay = server_info.get("_apm_env_overlay")
        env = config.get("env")
        if server_info.get("_raw_stdio") or not overlay or not isinstance(env, dict):
            return config

        # Only declared/emitted keys participate, as on the legacy registry path.
        # Never recover authored values from the shared collected overrides.
        authored = {name: value for name, value in overlay.items() if name in env}
        previous_keys = self._last_env_placeholder_keys - set(env)
        # Deliberately omit collected overrides: only authored values belong here.
        env.update(self._resolve_explicit_environment_values(authored))
        self._last_env_placeholder_keys = previous_keys | {
            name for value in env.values() for name in _ENV_REF_RE.findall(value)
        }
        return config

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

        if self.uses_native_v2(current_config):
            return self._update_v2_config(config_path, current_config, config_updates, enabled)

        for name, copilot_entry in config_updates.items():
            current_config["mcp"][name] = self._to_opencode_format(copilot_entry, enabled=enabled)

        self._write_json_config(config_path, current_config)
        return True

    def _update_v2_config(
        self,
        config_path: Path,
        current_config: dict,
        config_updates: dict,
        enabled: Any,
    ) -> bool:
        """Write *config_updates* under ``mcp.servers`` in a v2 config.

        A v1-layout entry with the same name is removed only when it is
        exactly what APM would have written there, so user edits survive.
        Nothing is written if any entry cannot be expressed in v2.
        """
        mcp = current_config["mcp"]
        servers = mcp[_V2_SERVERS_KEY]
        try:
            rendered = {
                name: self._to_opencode_v2_format(entry, enabled=enabled)
                for name, entry in config_updates.items()
            }
        except ValueError as exc:
            _rich_warning(f"{exc} -- skipping {config_path}", symbol="warning")
            return False

        for name, entry in rendered.items():
            legacy = mcp.get(name) if name not in V2_MCP_SETTINGS else None
            if legacy is not None:
                written = self._to_opencode_format(config_updates[name], enabled=enabled)
                if legacy == written and opencode_enabled_matches(legacy, written):
                    del mcp[name]
                else:
                    _rich_warning(
                        f"Kept the edited v1 entry 'mcp.{name}' in {config_path}; "
                        f"OpenCode v2 uses 'mcp.servers.{name}' instead",
                        symbol="warning",
                    )
            servers[name] = entry

        self._write_json_config(config_path, current_config)
        return True

    @staticmethod
    def uses_native_v2(config: Any) -> bool:
        """Return whether *config* declares the OpenCode v2 server layout.

        A v1 server that happens to be named ``servers`` carries a string
        ``type`` and is not treated as a v2 server map.
        """
        mcp = config.get("mcp") if isinstance(config, dict) else None
        servers = mcp.get(_V2_SERVERS_KEY) if isinstance(mcp, dict) else None
        return isinstance(servers, dict) and not isinstance(servers.get("type"), str)

    @classmethod
    def native_v2_servers(cls, config: dict) -> dict | None:
        """Return the mutable ``mcp.servers`` map of a v2 config, else ``None``."""
        return config["mcp"][_V2_SERVERS_KEY] if cls.uses_native_v2(config) else None

    def validate_config_for_install(self) -> bool:
        """Refuse unsafe user configs even when no server write would be needed."""
        return (
            not self.user_scope
            or self._read_json_config_for_update(Path(self.get_config_path())) is not None
        )

    def get_current_config(self):
        """Read the current ``opencode.json`` contents.

        For a v2 config, ``mcp`` lists only the ``mcp.servers`` entries.
        A server still in the v1 layout then counts as not installed, so a
        reinstall writes its native entry and retires the v1 one.
        """
        config = self._read_config_file()
        if not self.uses_native_v2(config):
            return config
        return {**config, "mcp": dict(config["mcp"][_V2_SERVERS_KEY])}

    def _read_config_file(self) -> dict:
        """Return the parsed config file, or ``{}`` when it is absent or unreadable."""
        config_path = self.get_config_path()
        if not os.path.exists(config_path):
            return {}
        try:
            with open(config_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def render_server_config(self, server_info: dict) -> dict:
        """Render the entry APM writes, for exact baseline comparisons."""
        copilot_entry = super().render_server_config(server_info)
        enabled = opencode_enabled_value(server_info)
        if self.uses_native_v2(self._read_config_file()):
            return self._to_opencode_v2_format(copilot_entry, enabled=enabled)
        return self._to_opencode_format(copilot_entry, enabled=enabled)

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

    @classmethod
    def _to_opencode_v2_format(cls, copilot_entry: dict, enabled: Any = True) -> dict:
        """Convert a Copilot-format server config to the OpenCode v2 layout.

        Identical to the v1 entry except that ``enabled`` becomes
        ``disabled: true`` when false and is omitted when true, the v2
        default.  ``disabled`` always follows ``enabled``, even when an
        extra field supplies it; other passthrough fields such as
        ``codemode`` are kept as is.

        Raises:
            ValueError: *enabled* is not a boolean, so it has no v2 equivalent.
        """
        if not isinstance(enabled, bool):
            raise ValueError(
                f"OpenCode v2 needs a boolean 'enabled' value, got {json.dumps(enabled)}"
            )
        entry = cls._to_opencode_format(copilot_entry, enabled=enabled)
        del entry["enabled"]
        entry.pop("disabled", None)
        if not enabled:
            entry["disabled"] = True
        return entry
