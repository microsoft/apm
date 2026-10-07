"""Cursor IDE implementation of MCP client adapter.

Cursor uses the standard ``mcpServers`` JSON format at ``.cursor/mcp.json``
(repo-local) or ``~/.cursor/mcp.json`` (user scope). Unlike the Copilot
adapter, this adapter emits Cursor-native transport discriminators
(``type: stdio`` / ``type: http``) and omits Copilot-only fields (``tools``, ``id``).

At project scope APM only writes to ``.cursor/mcp.json`` when the ``.cursor/`` directory already exists -- Cursor support is opt-in.
"""

import json
import os
from pathlib import Path

from ...core.token_manager import GitHubTokenManager
from ...models.dependency.mcp import TrustedEnvLiteral
from .base import _stringify_env_literal
from .copilot import CopilotClientAdapter


class CursorClientAdapter(CopilotClientAdapter):
    """Cursor IDE MCP client adapter.

    Inherits config-path and read/write logic from
    :class:`CopilotClientAdapter` but overrides ``_format_server_config`` to
    emit Cursor-native transport discriminators instead of Copilot-only fields.
    """

    supports_user_scope: bool = True
    target_name: str = "cursor"
    mcp_servers_key: str = "mcpServers"

    # APM normalizes Cursor env, args, and headers to ${env:NAME}, not command/url.
    # Keep manifest env references native in the project-local config, so
    # those referenced values are not baked into the file. Explicit mcp.env
    # literals remain literal below; shared GitHub token injection is separate.
    _supports_runtime_env_substitution: bool = True

    def _format_runtime_env_placeholder(self, name: str) -> str:
        """Return Cursor's native env-var placeholder syntax."""
        return "${env:" + name + "}"

    def _resolve_environment_variables(
        self,
        env_vars: dict[str, object] | list[dict[str, object]],
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Translate explicit env references while preserving authored literals.

        APM's shared translate-mode dict resolver treats ordinary string
        values as candidates for environment substitution. In Cursor's
        project-local config, an explicit static value must keep its authored
        value; only recognized references are rewritten for Cursor.
        """
        if not isinstance(env_vars, dict):
            return super()._resolve_environment_variables(env_vars, env_overrides)

        resolved: dict[str, str] = {}
        self._last_env_placeholder_keys = set()
        for name, value in env_vars.items():
            if not name or value is None:
                continue
            if isinstance(value, TrustedEnvLiteral):
                resolved[name] = value
            elif isinstance(value, str):
                resolved[name] = self._resolve_env_variable(name, value, env_overrides)
            else:
                resolved[name] = _stringify_env_literal(value)
        return resolved

    # ------------------------------------------------------------------ #
    # Auth-header injection override (for testability)
    # ------------------------------------------------------------------ #

    def _apply_auth_and_headers(
        self, config, remote, server_info, env_overrides, runtime_label="Cursor"
    ):
        """Merge registry headers without persisting a resolved GitHub token.

        Overrides the parent to supply ``GitHubTokenManager`` from *this*
        module's namespace, allowing tests to patch
        ``apm_cli.adapters.client.cursor.GitHubTokenManager`` correctly. Cursor
        reads project-local config, so GitHub credentials must be supplied as
        runtime environment references rather than resolved during install.
        """
        self._apply_auth_and_headers_impl(
            config,
            remote,
            server_info,
            env_overrides,
            runtime_label,
            GitHubTokenManager,
        )

    @staticmethod
    def config_path_for(project_root: Path, user_scope: bool) -> Path:
        """Return the Cursor MCP config path for a scope.

        ``~/.cursor/mcp.json`` at user scope, else ``.cursor/mcp.json`` under
        *project_root*.  Static so stale cleanup can locate the file without
        constructing an adapter.
        """
        root = Path.home() if user_scope else Path(project_root)
        return root / ".cursor" / "mcp.json"

    def get_config_path(self):
        """Return the project or user-scope Cursor MCP config path.

        The project ``.cursor/`` directory is *not* created automatically.
        User-scope installs write to ``~/.cursor/mcp.json`` from any cwd.
        """
        return str(self.config_path_for(self.project_root, self.user_scope))

    # ------------------------------------------------------------------ #
    # Config read / write -- project scope never creates ``.cursor/``
    # ------------------------------------------------------------------ #

    def validate_config_for_install(self) -> bool:
        """Refuse unsafe user configs even when no server write would be needed."""
        return (
            not self.user_scope
            or self._read_json_config_for_update(Path(self.get_config_path())) is not None
        )

    def update_config(self, config_updates):
        """Merge *config_updates* into the ``mcpServers`` section.

        The project ``.cursor/`` directory must already exist. User-scope
        installs create ``~/.cursor/`` when needed.  Returns ``False`` without
        writing when the existing file cannot be rewritten safely.
        """
        config_path = Path(self.get_config_path())

        # Project scope is opt-in; the user-scope directory can be created.
        if not self.user_scope and not config_path.parent.is_dir():
            return None

        current_config = self._read_json_config_for_update(config_path)
        if current_config is None:
            return False
        if "mcpServers" not in current_config:
            current_config["mcpServers"] = {}

        current_config["mcpServers"].update(config_updates)

        self._write_json_config(config_path, current_config)
        return True

    def get_current_config(self):
        """Read the current Cursor MCP config contents."""
        config_path = self.get_config_path()

        if not os.path.exists(config_path):
            return {}

        try:
            with open(config_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    # ------------------------------------------------------------------ #
    # _format_server_config -- Cursor-native schema
    # ------------------------------------------------------------------ #

    def _format_server_config(self, server_info, env_overrides=None, runtime_vars=None):
        """Format server info into Cursor MCP configuration.

        Cursor uses a transport discriminator field ``type`` to determine how
        to launch an MCP server:

        - ``"type": "stdio"`` for local process servers (raw stdio or packages)
        - ``"type": "http"`` for remote HTTP/SSE servers

        Copilot-only fields ``tools`` and ``id`` are never emitted.

        Args:
            server_info: Server information from registry.
            env_overrides: Pre-collected environment variable overrides.
            runtime_vars: Pre-collected runtime variable values.

        Returns:
            dict suitable for writing to ``.cursor/mcp.json``.
        """
        if runtime_vars is None:
            runtime_vars = {}

        config: dict = {}

        # --- raw stdio (self-defined deps) ---
        raw = server_info.get("_raw_stdio")
        if raw:
            config["type"] = "stdio"
            config["command"] = raw["command"]
            if raw.get("cwd") is not None:
                config["cwd"] = raw["cwd"]
            resolved_env_for_args: dict = {}
            if raw.get("env"):
                resolved_env_for_args = self._resolve_environment_variables(
                    raw["env"], env_overrides=env_overrides
                )
                config["env"] = resolved_env_for_args
                self._warn_input_variables(raw["env"], server_info.get("name", ""), "Cursor")
            args = raw.get("args") or []
            config["args"] = [
                self._resolve_variable_placeholders(arg, resolved_env_for_args, runtime_vars)
                if isinstance(arg, str)
                else arg
                for arg in args
            ]
            self._merge_extra(config, server_info)
            return config

        # --- remote endpoints ---
        remotes = server_info.get("remotes", [])
        if remotes:
            remote = self._select_remote_with_url(remotes) or remotes[0]

            transport = (remote.get("transport_type") or "").strip()
            if not transport:
                transport = "http"
            elif transport not in ("sse", "http", "streamable-http"):
                raise ValueError(
                    f"Unsupported remote transport '{transport}' for Cursor. "
                    f"Server: {server_info.get('name', 'unknown')}. "
                    f"Supported transports: http, sse, streamable-http."
                )

            config["type"] = "http"
            config["url"] = (remote.get("url") or "").strip()

            self._apply_auth_and_headers(config, remote, server_info, env_overrides, "Cursor")
            self._merge_extra(config, server_info)
            return config

        # --- local packages ---
        packages = server_info.get("packages", [])

        if not packages and not remotes:
            raise ValueError(
                f"MCP server has incomplete configuration in registry - "
                f"no package information or remote endpoints available. "
                f"This appears to be a temporary registry issue. "
                f"Server: {server_info.get('name', 'unknown')}"
            )

        if packages:
            package = self._select_and_dispatch_best_package(
                config, packages, env_overrides, runtime_vars, set_type_stdio=True
            )
            if not package:
                raise ValueError(
                    f"No supported package type found for Cursor. "
                    f"Server: {server_info.get('name', 'unknown')}. "
                    f"Available packages: "
                    f"{[p.get('registry_name', 'unknown') for p in packages]}."
                )

        self._merge_extra(config, server_info)
        return config

    # ------------------------------------------------------------------ #
    # configure_mcp_server -- thin override for the print label
    # ------------------------------------------------------------------ #

    def configure_mcp_server(
        self,
        server_url,
        server_name=None,
        enabled=True,
        env_overrides=None,
        server_info_cache=None,
        runtime_vars=None,
    ):
        """Configure an MCP server in Cursor's project or user-scope ``mcp.json``.

        Delegates entirely to the parent implementation but prints a
        Cursor-specific success message.
        """
        if not server_url:
            print("Error: server_url cannot be empty")
            return False

        # Project scope is opt-in; global installs work from any directory.
        cursor_dir = self.project_root / ".cursor"
        if not self.user_scope and not cursor_dir.is_dir():
            return True  # nothing to do, not an error

        try:
            server_info = self._fetch_server_info(server_url, server_info_cache)
            if server_info is None:
                return False

            config_key = self._determine_config_key(server_url, server_name)

            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)
            if self.update_config({config_key: server_config}) is False:
                return False

            print(f"Successfully configured MCP server '{config_key}' for Cursor")
            return True

        except Exception as e:
            print(f"Error configuring MCP server: {e}")
            return False
