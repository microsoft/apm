"""OpenAI Codex CLI implementation of MCP client adapter."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import toml

from ...registry.client import SimpleRegistryClient
from ...registry.integration import RegistryIntegration
from ...utils.console import _rich_warning
from . import _codex_env
from . import _codex_server_config as _csc
from .base import MCPClientAdapter, McpServerRequest, _resolve_mcp_request
from .copilot import arg_processing as _arg_processing

_log = logging.getLogger(__name__)


def _process_single_codex_arg(adapter, arg, resolved_env, runtime_vars):
    """Process one argument object and return a list of string tokens."""
    if isinstance(arg, str):
        return [adapter._resolve_variable_placeholders(arg, resolved_env, runtime_vars)]
    if not isinstance(arg, dict):
        return []
    arg_type = arg.get("type", "")
    result = []
    if arg_type == "positional":
        value = arg.get("value", arg.get("default", ""))
        if value:
            result.append(
                adapter._resolve_variable_placeholders(str(value), resolved_env, runtime_vars)
            )
    elif arg_type == "named":
        flag_name = arg.get("value", "")
        if flag_name:
            result.append(flag_name)
            additional_value = arg.get("name", "")
            if (
                additional_value
                and additional_value != flag_name
                and not additional_value.startswith("-")
            ):
                result.append(
                    adapter._resolve_variable_placeholders(
                        str(additional_value), resolved_env, runtime_vars
                    )
                )
    return result


class CodexClientAdapter(MCPClientAdapter):
    """Codex CLI implementation of MCP client adapter.

    This adapter handles Codex CLI-specific configuration for MCP servers using
    a scope-resolved config.toml file, following the TOML format for MCP
    server configuration.
    """

    supports_user_scope: bool = True
    target_name: str = "codex"
    mcp_servers_key: str = "mcp_servers"

    def __init__(
        self,
        registry_url=None,
        project_root: Path | str | None = None,
        user_scope: bool = False,
    ):
        """Initialize the Codex CLI client adapter.

        Args:
            registry_url (str, optional): URL of the MCP registry.
                If not provided, uses the MCP_REGISTRY_URL environment variable
                or falls back to the default GitHub registry.
            project_root: Project root used to resolve project-local Codex
                config paths.
            user_scope: Whether the adapter should resolve user-scope Codex
                config paths instead of project-local paths.
        """
        super().__init__(project_root=project_root, user_scope=user_scope)
        self.registry_client = SimpleRegistryClient(registry_url)
        self.registry_integration = RegistryIntegration(registry_url)

    def _get_codex_dir(self):
        """Return the root directory used for Codex config in the current scope."""
        if self.user_scope:
            return Path.home() / ".codex"
        return self.project_root / ".codex"

    def get_config_path(self):
        """Get the path to the Codex CLI MCP configuration file.

        Returns:
            str: Path to the scope-resolved Codex config.toml
        """
        return str(self._get_codex_dir() / "config.toml")

    def update_config(self, config_updates):
        """Update the Codex CLI MCP configuration.

        Args:
            config_updates (dict): Configuration updates to apply.
        """
        config_path = Path(self.get_config_path())
        current_config = self.get_current_config()
        if current_config is None:
            return False

        # Ensure mcp_servers section exists
        if "mcp_servers" not in current_config:
            current_config["mcp_servers"] = {}

        # Apply updates to mcp_servers section
        current_config["mcp_servers"].update(config_updates)

        # Ensure directory exists
        config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(config_path, "w", encoding="utf-8") as f:
            toml.dump(current_config, f)
        _log.debug("Codex config written to %s", config_path)
        return True

    def get_current_config(self):
        """Get the current Codex CLI MCP configuration.

        Returns:
            dict | None: Current configuration, empty dict if file doesn't
                exist, or None when an existing config cannot be parsed safely.
        """
        config_path = self.get_config_path()

        if not os.path.exists(config_path):
            return {}

        try:
            with open(config_path, encoding="utf-8") as f:
                return toml.load(f)
        except toml.TomlDecodeError as exc:
            _log.debug("Failed to parse Codex config at %s", config_path, exc_info=True)
            _rich_warning(
                f"Could not parse {config_path}: {exc} -- skipping config write to avoid data loss",
                symbol="warning",
            )
            return None
        except OSError:
            _log.debug("Failed to read Codex config at %s", config_path, exc_info=True)
            return None

    def configure_mcp_server(
        self,
        server_url,
        request: McpServerRequest | None = None,
        **legacy_kwargs,
    ):
        """Configure an MCP server in Codex CLI configuration.

        This method follows the Codex CLI MCP configuration format with
        mcp_servers sections in the TOML configuration.

        Args:
            server_url (str): URL or identifier of the MCP server.
            request: Optional McpServerRequest with server_name, env_overrides,
                server_info_cache, and runtime_vars.
            **legacy_kwargs: Deprecated -- pass individual fields through ``McpServerRequest`` instead.

        Returns:
            bool: True if successful, False otherwise.
        """
        request = _resolve_mcp_request(request, legacy_kwargs)
        if not server_url:
            print("Error: server_url cannot be empty")
            return False

        req = request or McpServerRequest()
        server_name = req.server_name
        env_overrides = req.env_overrides
        server_info_cache = req.server_info_cache
        runtime_vars = req.runtime_vars

        try:
            server_info = self._fetch_server_info(server_url, server_info_cache)
            if server_info is None:
                return False

            # Check for remote servers early - Codex doesn't support remote/SSE servers
            remotes = server_info.get("remotes", [])
            packages = server_info.get("packages", [])

            # If server has only remote endpoints and no packages, it's a remote-only server
            if remotes and not packages:
                print(f"[!]  Warning: MCP server '{server_url}' is a remote server (SSE type)")
                print("   Codex CLI only supports local servers with command/args configuration")
                print("   Remote servers are not supported by Codex CLI")
                print("   Skipping installation for Codex CLI")
                return False

            # Determine the server name for configuration key
            if server_name:
                # Use explicitly provided server name
                config_key = server_name
            # Extract name from server_url (part after last slash)
            # For URLs like "microsoft/azure-devops-mcp" -> "azure-devops-mcp"
            # For URLs like "github/github-mcp-server" -> "github-mcp-server"
            elif "/" in server_url:
                config_key = server_url.split("/")[-1]
            else:
                # Fallback to full server_url if no slash
                config_key = server_url

            # Generate server configuration with environment variable resolution
            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)

            # Update configuration using the chosen key
            if not self.update_config({config_key: server_config}):
                return False

            print(f"Successfully configured MCP server '{config_key}' for Codex CLI")
            return True

        except Exception as e:
            print(f"Error configuring MCP server: {e}")
            return False

    def _codex_npm_config(
        self,
        package_name,
        runtime_hint,
        processed_runtime_args,
        processed_package_args,
        resolved_env,
    ):
        """Delegate to _codex_server_config."""
        return _csc._codex_npm_config(
            self,
            package_name,
            runtime_hint,
            processed_runtime_args,
            processed_package_args,
            resolved_env,
        )

    def _codex_docker_config(self, processed_runtime_args, processed_package_args, resolved_env):
        """Delegate to _codex_server_config."""
        return _csc._codex_docker_config(
            self, processed_runtime_args, processed_package_args, resolved_env
        )

    def _codex_pypi_config(self, processed_runtime_args, processed_package_args, resolved_env):
        """Delegate to _codex_server_config."""
        return _csc._codex_pypi_config(
            self, processed_runtime_args, processed_package_args, resolved_env
        )

    def _codex_homebrew_config(self, processed_runtime_args, processed_package_args, resolved_env):
        """Delegate to _codex_server_config."""
        return _csc._codex_homebrew_config(
            self, processed_runtime_args, processed_package_args, resolved_env
        )

    def _codex_generic_config(self, processed_runtime_args, processed_package_args, resolved_env):
        """Delegate to _codex_server_config."""
        return _csc._codex_generic_config(
            self, processed_runtime_args, processed_package_args, resolved_env
        )

    def _format_server_config(self, server_info, env_overrides=None, runtime_vars=None):
        """Delegate to _codex_server_config."""
        return _csc._format_server_config(self, server_info, env_overrides, runtime_vars)

    def _process_environment_variables(self, env_vars, env_overrides=None):
        """Resolve environment-variable definitions for Codex."""
        return _codex_env.process_environment_variables(env_vars, env_overrides)

    def _process_arguments(self, arguments, resolved_env=None, runtime_vars=None):
        """Reuse the shared MCP argument processor."""
        return _arg_processing._process_arguments(self, arguments, resolved_env, runtime_vars)

    def _resolve_variable_placeholders(self, value, resolved_env, runtime_vars):
        """Delegate placeholder expansion to the shared Copilot helper."""
        return _arg_processing._resolve_variable_placeholders(
            self,
            value,
            resolved_env,
            runtime_vars,
        )

    def _select_best_package(self, packages):
        """Delegate package selection to the shared Copilot helper."""
        return _arg_processing._select_best_package(self, packages)
