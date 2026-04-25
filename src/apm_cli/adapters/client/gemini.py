"""Gemini CLI implementation of MCP client adapter.

Gemini CLI uses ``.gemini/settings.json`` at the project root with an
``mcpServers`` key.  Unlike Copilot, Gemini infers transport from which
key is present (``command`` for stdio, ``url`` for SSE, ``httpUrl`` for
streamable HTTP) and does not use ``type``, ``tools``, or ``id`` fields.

.. code-block:: json

   {
     "mcpServers": {
       "server-name": {
         "command": "npx",
         "args": ["-y", "@modelcontextprotocol/server-foo"],
         "env": { "KEY": "value" }
       }
     }
   }

APM only writes to ``.gemini/settings.json`` when the ``.gemini/``
directory already exists -- Gemini CLI support is opt-in.

Ref: https://geminicli.com/docs/reference/configuration/
"""

import json
import logging
import os
from pathlib import Path

from .copilot import CopilotClientAdapter
from ...core.docker_args import DockerArgsProcessor
from ...utils.console import _rich_error, _rich_success

logger = logging.getLogger(__name__)


class GeminiClientAdapter(CopilotClientAdapter):
    """Gemini CLI MCP client adapter.

    Inherits Copilot's helper methods for package selection, env-var
    resolution, and argument processing but fully reimplements
    ``_format_server_config`` to emit Gemini-valid JSON.
    """

    supports_user_scope: bool = True

    def get_config_path(self):
        """Return the path to ``.gemini/settings.json`` in the repository root."""
        return str(Path(os.getcwd()) / ".gemini" / "settings.json")

    def update_config(self, config_updates):
        """Merge *config_updates* into the ``mcpServers`` section of settings.json.

        The ``.gemini/`` directory must already exist; if it does not, this
        method returns silently (opt-in behaviour).

        Preserves all other top-level keys in settings.json (theme, tools,
        hooks, etc.).
        """
        gemini_dir = Path(os.getcwd()) / ".gemini"
        if not gemini_dir.is_dir():
            return

        config_path = Path(self.get_config_path())
        current_config = self.get_current_config()
        if "mcpServers" not in current_config:
            current_config["mcpServers"] = {}

        for name, entry in config_updates.items():
            current_config["mcpServers"][name] = entry

        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(current_config, f, indent=2)

    def get_current_config(self):
        """Read the current ``.gemini/settings.json`` contents."""
        config_path = self.get_config_path()
        if not os.path.exists(config_path):
            return {}
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _format_server_config(self, server_info, env_overrides=None, runtime_vars=None):
        """Format server info into Gemini CLI MCP configuration.

        Gemini's schema differs from Copilot's:
        - No ``type``, ``tools``, or ``id`` fields.
        - Transport inferred from key: ``command`` (stdio), ``url`` (SSE),
          ``httpUrl`` (streamable HTTP).
        - Tool filtering via ``includeTools``/``excludeTools``.

        Args:
            server_info: Server information from registry.
            env_overrides: Pre-collected environment variable overrides.
            runtime_vars: Pre-collected runtime variable values.

        Returns:
            dict suitable for writing to ``.gemini/settings.json``.
        """
        if runtime_vars is None:
            runtime_vars = {}

        config: dict = {}

        # --- raw stdio (self-defined deps) ---
        raw = server_info.get("_raw_stdio")
        if raw:
            config["command"] = raw["command"]
            config["args"] = raw["args"]
            if raw.get("env"):
                config["env"] = raw["env"]
                self._warn_input_variables(
                    raw["env"], server_info.get("name", ""), "Gemini CLI"
                )
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
                    f"Unsupported remote transport '{transport}' for Gemini. "
                    f"Server: {server_info.get('name', 'unknown')}. "
                    f"Supported transports: http, sse, streamable-http."
                )

            url = (remote.get("url") or "").strip()
            if transport == "sse":
                config["url"] = url
            else:
                config["httpUrl"] = url

            # Registry-supplied headers
            for header in remote.get("headers", []):
                name = header.get("name", "")
                value = header.get("value", "")
                if name and value:
                    config.setdefault("headers", {})[name] = (
                        self._resolve_env_variable(name, value, env_overrides)
                    )

            if config.get("headers"):
                self._warn_input_variables(
                    config["headers"], server_info.get("name", ""), "Gemini CLI"
                )

            return config

        # --- local packages ---
        packages = server_info.get("packages", [])

        if not packages:
            raise ValueError(
                f"MCP server has no package information or remote endpoints. "
                f"Server: {server_info.get('name', 'unknown')}"
            )

        package = self._select_best_package(packages)
        if not package:
            return config

        registry_name = self._infer_registry_name(package)
        package_name = package.get("name", "")
        runtime_hint = package.get("runtime_hint", "")
        runtime_arguments = package.get("runtime_arguments", [])
        package_arguments = package.get("package_arguments", [])
        env_vars = package.get("environment_variables", [])

        resolved_env = self._resolve_environment_variables(
            env_vars, env_overrides
        )
        processed_rt = self._process_arguments(
            runtime_arguments, resolved_env, runtime_vars
        )
        processed_pkg = self._process_arguments(
            package_arguments, resolved_env, runtime_vars
        )

        if registry_name == "npm":
            config["command"] = runtime_hint or "npx"
            config["args"] = ["-y", package_name] + processed_rt + processed_pkg
        elif registry_name == "docker":
            config["command"] = "docker"
            if processed_rt:
                config["args"] = self._inject_env_vars_into_docker_args(
                    processed_rt, resolved_env
                )
            else:
                config["args"] = DockerArgsProcessor.process_docker_args(
                    ["run", "-i", "--rm", package_name], resolved_env
                )
        elif registry_name == "pypi":
            config["command"] = runtime_hint or "uvx"
            config["args"] = [package_name] + processed_rt + processed_pkg
        elif registry_name == "homebrew":
            config["command"] = (
                package_name.split("/")[-1] if "/" in package_name else package_name
            )
            config["args"] = processed_rt + processed_pkg
        else:
            config["command"] = runtime_hint or package_name
            config["args"] = processed_rt + processed_pkg

        if resolved_env:
            config["env"] = resolved_env

        return config

    def configure_mcp_server(
        self,
        server_url,
        server_name=None,
        enabled=True,
        env_overrides=None,
        server_info_cache=None,
        runtime_vars=None,
    ):
        """Configure an MCP server in ``.gemini/settings.json``.

        Delegates to the parent for config formatting, then writes to
        the Gemini CLI settings file.
        """
        if not server_url:
            _rich_error("server_url cannot be empty", symbol="error")
            return False

        gemini_dir = Path(os.getcwd()) / ".gemini"
        if not gemini_dir.is_dir():
            return True

        try:
            if server_info_cache and server_url in server_info_cache:
                server_info = server_info_cache[server_url]
            else:
                server_info = self.registry_client.find_server_by_reference(server_url)

            if not server_info:
                _rich_error(f"MCP server '{server_url}' not found in registry", symbol="error")
                return False

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

            _rich_success(
                f"Configured MCP server '{config_key}' for Gemini CLI", symbol="success"
            )
            return True

        except Exception as e:
            logger.debug("Gemini MCP configuration failed: %s", e)
            _rich_error("Failed to configure MCP server for Gemini CLI", symbol="error")
            return False
