"""Cursor IDE implementation of MCP client adapter.

Cursor uses the standard ``mcpServers`` JSON format at ``.cursor/mcp.json``
(repo-local).  The config schema is identical to GitHub Copilot CLI, so this
adapter subclasses :class:`CopilotClientAdapter` and only overrides the
config-path logic and the user-facing labels.

APM only writes to ``.cursor/mcp.json`` when the ``.cursor/`` directory
already exists — Cursor support is opt-in.
"""

import json
import os
from pathlib import Path

from .copilot import CopilotClientAdapter


class CursorClientAdapter(CopilotClientAdapter):
    """Cursor IDE MCP client adapter.

    Inherits config path and read/write logic from this class, but
    **must** override :meth:`_format_server_config` because Cursor's JSON
    schema differs from Copilot CLI's in two critical ways:

    - ``type`` must be ``"stdio"`` or ``"http"`` (NOT ``"local"``).
    - ``tools`` and ``id`` fields must **never** be emitted — they are
      Copilot-CLI-specific and cause Cursor's MCP loader to silently
      reject the server.

    .. note::

        This inheritance design is a known fragility.  ``_format_server_config``
        **must** be explicitly overridden in each subclass; silently inheriting
        the Copilot version will produce invalid configs for the target runtime.
    """

    supports_user_scope: bool = False

    # ------------------------------------------------------------------ #
    # Config path
    # ------------------------------------------------------------------ #

    def get_config_path(self):
        """Return the path to ``.cursor/mcp.json`` in the repository root.

        Unlike the Copilot adapter this is a **repo-local** path.  The
        ``.cursor/`` directory is *not* created automatically — APM only
        writes here when the directory already exists.
        """
        cursor_dir = Path(os.getcwd()) / ".cursor"
        return str(cursor_dir / "mcp.json")

    # ------------------------------------------------------------------ #
    # Config read / write — override to avoid auto-creating the directory
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
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    # ------------------------------------------------------------------ #
    # configure_mcp_server — thin override for the print label
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
        cursor_dir = Path(os.getcwd()) / ".cursor"
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

            server_config = self._format_server_config(
                server_info, env_overrides, runtime_vars
            )
            self.update_config({config_key: server_config})

            print(
                f"Successfully configured MCP server '{config_key}' for Cursor"
            )
            return True

        except Exception as e:
            print(f"Error configuring MCP server: {e}")
            return False

    # ------------------------------------------------------------------ #
    # _format_server_config — MUST override; do NOT silently inherit Copilot
    # ------------------------------------------------------------------ #

    def _format_server_config(self, server_info, env_overrides=None, runtime_vars=None):
        """Format server info into Cursor-compatible ``.cursor/mcp.json`` format.

        Cursor uses ``"type": "stdio"`` or ``"type": "http"`` (NOT ``"local"``)
        and does NOT support the ``tools`` or ``id`` fields that Copilot CLI uses.

        Args:
            server_info (dict): Server information from registry.
            env_overrides (dict, optional): Pre-collected environment variable overrides.
            runtime_vars (dict, optional): Pre-collected runtime variable values.

        Returns:
            dict: Cursor-compatible server configuration.
        """
        if runtime_vars is None:
            runtime_vars = {}

        raw = server_info.get("_raw_stdio")
        if raw:
            config = {
                "type": "stdio",
                "command": raw["command"],
                "args": raw["args"],
            }
            if raw.get("env"):
                config["env"] = raw["env"]
                self._warn_input_variables(raw["env"], server_info.get("name", ""), "Cursor")
            return config

        remotes = server_info.get("remotes", [])
        if remotes:
            remote = remotes[0]
            transport = (remote.get("transport_type") or "http").strip()
            if transport in ("sse", "streamable-http"):
                transport = "http"
            config = {
                "type": "http",
                "url": remote.get("url", ""),
            }
            headers = remote.get("headers", [])
            if headers:
                if isinstance(headers, list):
                    config["headers"] = {
                        h["name"]: h["value"] for h in headers if "name" in h and "value" in h
                    }
                else:
                    config["headers"] = headers
            return config

        packages = server_info.get("packages", [])
        if not packages:
            raise ValueError(
                f"MCP server has incomplete configuration in registry - no package "
                f"information or remote endpoints available. "
                f"Server: {server_info.get('name', 'unknown')}"
            )

        package = self._select_best_package(packages)
        if not package:
            raise ValueError(
                f"No suitable package found for MCP server "
                f"'{server_info.get('name', 'unknown')}'"
            )

        registry_name = self._infer_registry_name(package)
        package_name = package.get("name", "")
        runtime_hint = package.get("runtime_hint", "")
        runtime_arguments = package.get("runtime_arguments", [])
        package_arguments = package.get("package_arguments", [])
        env_vars = package.get("environment_variables", [])

        resolved_env = self._resolve_environment_variables(env_vars, env_overrides)
        processed_runtime_args = self._process_arguments(runtime_arguments, resolved_env, runtime_vars)
        processed_package_args = self._process_arguments(package_arguments, resolved_env, runtime_vars)

        config = {"type": "stdio"}

        if registry_name == "npm":
            config["command"] = runtime_hint or "npx"
            config["args"] = ["-y", package_name] + processed_runtime_args + processed_package_args
        elif registry_name == "docker":
            config["command"] = "docker"
            if processed_runtime_args:
                config["args"] = self._inject_env_vars_into_docker_args(
                    processed_runtime_args, resolved_env
                )
            else:
                from ...core.docker_args import DockerArgsProcessor
                config["args"] = DockerArgsProcessor.process_docker_args(
                    ["run", "-i", "--rm", package_name],
                    resolved_env
                )
        elif registry_name == "pypi":
            config["command"] = runtime_hint or "uvx"
            config["args"] = [package_name] + processed_runtime_args + processed_package_args
        elif registry_name == "homebrew":
            config["command"] = package_name.split("/")[-1] if "/" in package_name else package_name
            config["args"] = processed_runtime_args + processed_package_args
        else:
            config["command"] = runtime_hint or package_name
            config["args"] = processed_runtime_args + processed_package_args

        if resolved_env:
            config["env"] = resolved_env

        return config
