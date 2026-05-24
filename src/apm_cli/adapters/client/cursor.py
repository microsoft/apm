"""Cursor IDE implementation of MCP client adapter.

Cursor uses the standard ``mcpServers`` JSON format at ``.cursor/mcp.json``
(repo-local).  Unlike the Copilot adapter, this adapter emits Cursor-native
transport discriminators (``type: stdio`` / ``type: http``) and omits
Copilot-only fields (``tools``, ``id``).

APM only writes to ``.cursor/mcp.json`` when the ``.cursor/`` directory
already exists -- Cursor support is opt-in.
"""

import json
import os
from pathlib import Path

from ...core.token_manager import GitHubTokenManager
from .copilot import CopilotClientAdapter


class CursorClientAdapter(CopilotClientAdapter):
    """Cursor IDE MCP client adapter.

    Inherits config-path and read/write logic from
    :class:`CopilotClientAdapter` but overrides ``_format_server_config`` to
    emit Cursor-native transport discriminators instead of Copilot-only fields.
    """

    supports_user_scope: bool = False
    target_name: str = "cursor"
    mcp_servers_key: str = "mcpServers"

    # Cursor's mcp.json runtime-substitution support has not yet been
    # individually audited (see #1152). Pin to the legacy install-time
    # resolution behaviour so this adapter is unchanged by the Copilot
    # security fix; revisit in a follow-up.
    _supports_runtime_env_substitution: bool = False

    # ------------------------------------------------------------------ #
    # Auth-header injection override (for testability)
    # ------------------------------------------------------------------ #

    def _apply_auth_and_headers(
        self, config, remote, server_info, env_overrides, runtime_label="Cursor"
    ):
        """Inject GitHub token and registry-supplied headers into *config*.

        Overrides the parent to supply ``GitHubTokenManager`` from *this*
        module's namespace, allowing tests to patch
        ``apm_cli.adapters.client.cursor.GitHubTokenManager`` correctly.
        """
        self._apply_auth_and_headers_impl(
            config, remote, server_info, env_overrides, runtime_label, GitHubTokenManager
        )

    def get_config_path(self):
        """Return the path to ``.cursor/mcp.json`` in the repository root.

        Unlike the Copilot adapter this is a **repo-local** path.  The
        ``.cursor/`` directory is *not* created automatically -- APM only
        writes here when the directory already exists.
        """
        cursor_dir = self.project_root / ".cursor"
        return str(cursor_dir / "mcp.json")

    # ------------------------------------------------------------------ #
    # Config read / write -- override to avoid auto-creating the directory
    # ------------------------------------------------------------------ #

    def update_config(self, config_updates):
        """Merge *config_updates* into the ``mcpServers`` section.

        The ``.cursor/`` directory must already exist; if it does not, this
        method returns silently (opt-in behaviour).
        """
        config_path = Path(self.get_config_path())

        # Opt-in: only write when .cursor/ already exists
        if not config_path.parent.exists():
            return

        current_config = self.get_current_config()
        if "mcpServers" not in current_config:
            current_config["mcpServers"] = {}

        current_config["mcpServers"].update(config_updates)

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(current_config, f, indent=2)
        os.chmod(config_path, 0o600)

    def get_current_config(self):
        """Read the current ``.cursor/mcp.json`` contents."""
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
        """Configure an MCP server in Cursor's ``.cursor/mcp.json``.

        Delegates entirely to the parent implementation but prints a
        Cursor-specific success message.
        """
        if not server_url:
            print("Error: server_url cannot be empty")
            return False

        # Opt-in: skip silently when .cursor/ does not exist
        cursor_dir = self.project_root / ".cursor"
        if not cursor_dir.exists():
            return True  # nothing to do, not an error

        try:
            server_info = self._fetch_server_info(server_url, server_info_cache)
            if server_info is None:
                return False

            config_key = self._determine_config_key(server_url, server_name)

            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)
            self.update_config({config_key: server_config})

            print(f"Successfully configured MCP server '{config_key}' for Cursor")
            return True

        except Exception as e:
            print(f"Error configuring MCP server: {e}")
            return False
