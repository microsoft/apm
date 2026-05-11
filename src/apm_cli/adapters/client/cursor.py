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

from ...core.docker_args import DockerArgsProcessor
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
    # Config path
    # ------------------------------------------------------------------ #

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

            # Add authentication headers for GitHub MCP server
            server_name = server_info.get("name", "")
            is_github_server = self._is_github_server(server_name, remote.get("url", ""))

            if is_github_server:
                _tm = GitHubTokenManager()
                github_token = _tm.get_token_for_purpose("copilot") or os.getenv(
                    "GITHUB_PERSONAL_ACCESS_TOKEN"
                )
                if github_token:
                    config["headers"] = {"Authorization": f"Bearer {github_token}"}

            # Add any additional headers from registry if present
            headers = remote.get("headers", [])
            if headers:
                if "headers" not in config:
                    config["headers"] = {}
                for header in headers:
                    header_name = header.get("name", "")
                    header_value = header.get("value", "")
                    if header_name and header_value:
                        # Prevent registry-supplied headers from overriding
                        # the injected GitHub token
                        if header_name == "Authorization" and is_github_server:
                            continue
                        resolved_value = self._resolve_env_variable(
                            header_name, header_value, env_overrides
                        )
                        config["headers"][header_name] = resolved_value

            # Warn about unresolvable ${input:...} references in headers
            if config.get("headers"):
                self._warn_input_variables(config["headers"], server_info.get("name", ""), "Cursor")

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
            package = self._select_best_package(packages)

            if package:
                registry_name = self._infer_registry_name(package)
                package_name = package.get("name", "")
                runtime_hint = package.get("runtime_hint", "")
                runtime_arguments = package.get("runtime_arguments", [])
                package_arguments = package.get("package_arguments", [])
                env_vars = package.get("environment_variables", [])

                resolved_env = self._resolve_environment_variables(env_vars, env_overrides)
                processed_runtime_args = self._process_arguments(
                    runtime_arguments, resolved_env, runtime_vars
                )
                processed_package_args = self._process_arguments(
                    package_arguments, resolved_env, runtime_vars
                )

                config["type"] = "stdio"

                if registry_name == "npm":
                    config["command"] = runtime_hint or "npx"
                    config["args"] = (
                        ["-y", package_name] + processed_runtime_args + processed_package_args  # noqa: RUF005
                    )
                    if resolved_env:
                        config["env"] = resolved_env
                elif registry_name == "docker":
                    config["command"] = "docker"
                    if processed_runtime_args:
                        config["args"] = self._inject_env_vars_into_docker_args(
                            processed_runtime_args, resolved_env
                        )
                    else:
                        config["args"] = DockerArgsProcessor.process_docker_args(
                            ["run", "-i", "--rm", package_name], resolved_env
                        )
                elif registry_name == "pypi":
                    config["command"] = runtime_hint or "uvx"
                    config["args"] = (
                        [package_name] + processed_runtime_args + processed_package_args  # noqa: RUF005
                    )
                    if resolved_env:
                        config["env"] = resolved_env
                elif registry_name == "homebrew":
                    config["command"] = (
                        package_name.split("/")[-1] if "/" in package_name else package_name
                    )
                    config["args"] = processed_runtime_args + processed_package_args
                    if resolved_env:
                        config["env"] = resolved_env
                else:
                    config["command"] = runtime_hint or package_name
                    config["args"] = processed_runtime_args + processed_package_args
                    if resolved_env:
                        config["env"] = resolved_env
            else:
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
            # Use cached server info if available, otherwise fetch from registry
            if server_info_cache and server_url in server_info_cache:
                server_info = server_info_cache[server_url]
            else:
                server_info = self.registry_client.find_server_by_reference(server_url)

            if not server_info:
                print(f"Error: MCP server '{server_url}' not found in registry")
                return False

            # Determine config key
            if server_name:
                config_key = server_name
            elif "/" in server_url:
                config_key = server_url.split("/")[-1]
            else:
                config_key = server_url

            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)
            self.update_config({config_key: server_config})

            print(f"Successfully configured MCP server '{config_key}' for Cursor")
            return True

        except Exception as e:
            print(f"Error configuring MCP server: {e}")
            return False
