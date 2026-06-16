"""Gemini CLI implementation of MCP client adapter.

Gemini CLI reads ``.gemini/settings.json`` with an ``mcpServers`` key.
Unlike Copilot, Gemini infers transport from which key is present
(``command`` for stdio, ``url`` for SSE, ``httpUrl`` for streamable
HTTP) and does not use ``type``, ``tools``, or ``id`` fields.

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

Scope resolution follows the shared adapter contract: project scope
writes to ``<project_root>/.gemini/settings.json`` and is opt-in --
the directory must already exist or the write is skipped silently.
User scope writes to ``~/.gemini/settings.json`` unconditionally and
creates the directory if needed.

Ref: https://geminicli.com/docs/reference/configuration/
"""

import json
import logging
import os
from pathlib import Path

from ...core.docker_args import DockerArgsProcessor
from ...utils.console import _rich_error, _rich_success
from .copilot import CopilotClientAdapter

logger = logging.getLogger(__name__)


class GeminiClientAdapter(CopilotClientAdapter):
    """Gemini CLI MCP client adapter.

    Inherits Copilot's helper methods for package selection, env-var
    resolution, and argument processing but fully reimplements
    ``_format_server_config`` to emit Gemini-valid JSON.

    Scope routing is governed by ``user_scope``/``project_root`` inherited
    from :class:`MCPClientAdapter`: project scope reads/writes
    ``<project_root>/.gemini/settings.json`` (opt-in -- the directory must
    already exist), and user scope reads/writes ``~/.gemini/settings.json``.
    """

    supports_user_scope: bool = True
    target_name: str = "gemini"
    mcp_servers_key: str = "mcpServers"

    # Gemini CLI's settings.json runtime-substitution support has not yet
    # been individually audited (see #1152). Pin to legacy install-time
    # resolution so this adapter is unchanged by the Copilot security fix;
    # revisit in a follow-up.
    _supports_runtime_env_substitution: bool = False

    def _get_config_dir(self) -> Path:
        """Return the ``.gemini`` directory for the active scope."""
        if self.user_scope:
            return Path.home() / ".gemini"
        return self.project_root / ".gemini"

    def get_config_path(self):
        """Return the path to ``settings.json`` for the active scope."""
        return str(self._get_config_dir() / "settings.json")

    def update_config(self, config_updates):
        """Merge *config_updates* into the ``mcpServers`` section of settings.json.

        Project scope is opt-in: if the target config directory does not
        exist, this method returns silently. User scope always writes,
        creating the directory if needed.

        Preserves all other top-level keys in settings.json (theme, tools,
        hooks, etc.).
        """
        config_dir = self._get_config_dir()
        if not self.user_scope and not config_dir.is_dir():
            logger.debug(
                "Skipping %s project-scope write -- %s does not exist (opt-in)",
                self.target_name,
                config_dir,
            )
            return

        config_path = Path(self.get_config_path())
        current_config = self.get_current_config()
        if "mcpServers" not in current_config:
            current_config["mcpServers"] = {}

        for name, entry in config_updates.items():
            current_config["mcpServers"][name] = entry

        if not config_path.parent.is_dir():
            logger.debug(
                "Creating %s for %s user configuration", config_path.parent, self.target_name
            )
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(current_config, f, indent=2)
        os.chmod(config_path, 0o600)

    def get_current_config(self):
        """Read the current ``settings.json`` contents for the active scope."""
        config_path = Path(self.get_config_path())
        if not config_path.exists():
            return {}
        try:
            with open(config_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s: %s", config_path, exc)
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
        # Route ``env`` and ``args`` through the resolver pipeline so all
        # three placeholder syntaxes (``<VAR>``, ``${VAR}``, ``${env:VAR}``)
        # are resolved at install time before being written to
        # ``.gemini/settings.json``. See issue #1266.
        raw = server_info.get("_raw_stdio")
        if raw:
            config["command"] = raw["command"]
            resolved_env_for_args: dict = {}
            if raw.get("env"):
                resolved_env_for_args = self._resolve_environment_variables(
                    raw["env"], env_overrides=env_overrides
                )
                config["env"] = resolved_env_for_args
                self._warn_input_variables(
                    raw["env"], server_info.get("name", ""), self.target_name
                )
            config["args"] = [
                self._resolve_variable_placeholders(arg, resolved_env_for_args, runtime_vars)
                if isinstance(arg, str)
                else arg
                for arg in raw.get("args") or []
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
                    f"Unsupported remote transport '{transport}' for {self.target_name}. "
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
                    config.setdefault("headers", {})[name] = self._resolve_env_variable(
                        name, value, env_overrides
                    )

            if config.get("headers"):
                self._warn_input_variables(
                    config["headers"], server_info.get("name", ""), self.target_name
                )

            self._merge_extra(config, server_info)
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
            self._merge_extra(config, server_info)
            return config

        registry_name = self._infer_registry_name(package)
        package_name = package.get("name", "")
        runtime_hint = package.get("runtime_hint", "")
        runtime_arguments = package.get("runtime_arguments", [])
        package_arguments = package.get("package_arguments", [])
        env_vars = package.get("environment_variables", [])

        resolved_env = self._resolve_environment_variables(env_vars, env_overrides)
        processed_rt = self._process_arguments(runtime_arguments, resolved_env, runtime_vars)
        processed_pkg = self._process_arguments(package_arguments, resolved_env, runtime_vars)

        if registry_name == "npm":
            config["command"] = runtime_hint or "npx"
            config["args"] = ["-y", package_name] + processed_rt + processed_pkg  # noqa: RUF005
        elif registry_name == "docker":
            config["command"] = "docker"
            if processed_rt:
                config["args"] = self._inject_env_vars_into_docker_args(processed_rt, resolved_env)
            else:
                config["args"] = DockerArgsProcessor.process_docker_args(
                    ["run", "-i", "--rm", package_name], resolved_env
                )
        elif registry_name == "pypi":
            config["command"] = runtime_hint or "uvx"
            config["args"] = [package_name] + processed_rt + processed_pkg  # noqa: RUF005
        elif registry_name == "homebrew":
            config["command"] = package_name.split("/")[-1] if "/" in package_name else package_name
            config["args"] = processed_rt + processed_pkg
        else:
            config["command"] = runtime_hint or package_name
            config["args"] = processed_rt + processed_pkg

        if resolved_env:
            config["env"] = resolved_env

        self._merge_extra(config, server_info)
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
        """Configure an MCP server in the target's ``settings.json``.

        Delegates to the parent for config formatting, then writes to
        the target CLI settings file.
        """
        if not server_url:
            _rich_error("server_url cannot be empty", symbol="error")
            return False

        if not self.user_scope and not self._get_config_dir().is_dir():
            logger.debug(
                "%s opt-in gate: %s absent, skipping configure_mcp_server",
                self.target_name,
                self._get_config_dir(),
            )
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

            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)
            self.update_config({config_key: server_config})

            _rich_success(
                f"Configured MCP server '{config_key}' for {self.target_name}",
                symbol="success",
            )
            return True

        except Exception as e:
            logger.debug("%s MCP configuration failed: %s", self.target_name, e)
            _rich_error(
                f"Failed to configure MCP server for {self.target_name}",
                symbol="error",
            )
            return False
