"""VSCode implementation of MCP client adapter.

This adapter implements the VSCode-specific handling of MCP server configuration,
following the official documentation at:
https://code.visualstudio.com/docs/copilot/chat/mcp-servers
"""

from __future__ import annotations

import json
from pathlib import Path

from ...registry.client import SimpleRegistryClient
from ...registry.integration import RegistryIntegration
from ...utils.console import _rich_warning
from . import _vscode_server_config as _vsc
from ._vscode_format import (
    _LEGACY_ANGLE_VAR_RE,
    _build_package_input_vars,
    _build_python_command_args,
    _extract_package_args,
    _select_remote_with_url,
    _translate_env_vars_for_vscode,
)
from .base import _INPUT_VAR_RE, MCPClientAdapter, McpServerRequest, _resolve_mcp_request


def _emit_log(logger, level: str, msg: str) -> None:
    """Emit a log message via *logger* or fall back to ``print``."""
    if logger:
        getattr(logger, level, logger.error)(msg)
    else:
        print(msg)


def _merge_input_vars(current_inputs: list, input_vars: list) -> None:
    """Append *input_vars* to *current_inputs*, skipping duplicate ids."""
    existing_ids = {var.get("id") for var in current_inputs if isinstance(var, dict)}
    for var in input_vars:
        if var.get("id") not in existing_ids:
            current_inputs.append(var)
            existing_ids.add(var.get("id"))


def _ensure_mcp_sections(config: dict) -> None:
    """Ensure *config* has ``servers`` and ``inputs`` top-level keys."""
    if "servers" not in config:
        config["servers"] = {}
    if "inputs" not in config:
        config["inputs"] = []


class VSCodeClientAdapter(MCPClientAdapter):
    """VSCode implementation of MCP client adapter.

    This adapter handles VSCode-specific configuration for MCP servers using
    a repository-level .vscode/mcp.json file, following the format specified
    in the VSCode documentation.
    """

    target_name: str = "vscode"
    mcp_servers_key: str = "servers"

    # Re-expose pure formatting helpers as staticmethods so that both
    # ``self.method(...)`` and ``VSCodeClientAdapter.method(...)`` work.
    _translate_env_vars_for_vscode = staticmethod(_translate_env_vars_for_vscode)
    _extract_package_args = staticmethod(_extract_package_args)
    _select_remote_with_url = staticmethod(_select_remote_with_url)
    _build_python_command_args = staticmethod(_build_python_command_args)
    _build_package_input_vars = staticmethod(_build_package_input_vars)

    @staticmethod
    def _warn_on_legacy_angle_vars(mapping, server_name, field):
        """Emit a warning when legacy ``<VAR>`` placeholders appear in *mapping*.

        VS Code does not resolve ``<VAR>`` placeholders, so they would render
        as literal ``<VAR>`` text in the generated mcp.json -- silently
        breaking auth headers / env values at server-start. Surface this as
        an explicit warning so authors can switch to the cross-harness
        ``${VAR}`` / ``${env:VAR}`` syntax (see manifest-schema reference).
        """
        if not mapping:
            return
        offenders = []
        for value in mapping.values():
            if isinstance(value, str):
                offenders.extend(_LEGACY_ANGLE_VAR_RE.findall(value))
        if offenders:
            unique = sorted(set(offenders))
            _rich_warning(
                f"Server '{server_name}' {field} use legacy <VAR> placeholder(s) "
                f"({', '.join('<' + n + '>' for n in unique)}) which VS Code "
                f"cannot resolve. Use ${{VAR}} or ${{env:VAR}} instead so the "
                f"value resolves at runtime."
            )

    def __init__(
        self,
        registry_url=None,
        project_root: Path | str | None = None,
        user_scope: bool = False,
    ):
        """Initialize the VSCode client adapter.

        Args:
            registry_url (str, optional): URL of the MCP registry.
                If not provided, uses the MCP_REGISTRY_URL environment variable
                or falls back to the default demo registry.
            project_root: Project root used to resolve the repository-local
                `.vscode/mcp.json` path.
            user_scope: Whether to resolve user-scope config paths instead of
                project-local paths when supported.
        """
        super().__init__(project_root=project_root, user_scope=user_scope)
        self.registry_client = SimpleRegistryClient(registry_url)
        self.registry_integration = RegistryIntegration(registry_url)

    def get_config_path(self, logger=None):
        """Get the path to the VSCode MCP configuration file in the repository.

        Returns:
            str: Path to the .vscode/mcp.json file.
        """
        # Use the resolved project root, which may be explicitly provided
        repo_root = self.project_root

        # Path to .vscode/mcp.json in the repository
        vscode_dir = repo_root / ".vscode"
        mcp_config_path = vscode_dir / "mcp.json"

        # Create the .vscode directory if it doesn't exist
        try:
            if not vscode_dir.exists():
                vscode_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            if logger:
                logger.warning(f"Could not create .vscode directory: {e}")
            else:
                print(f"Warning: Could not create .vscode directory: {e}")

        return str(mcp_config_path)

    def update_config(self, new_config, logger=None):
        """Update the VSCode MCP configuration with new values.

        Args:
            new_config (dict): Complete configuration object to write.

        Returns:
            bool: True if successful, False otherwise.
        """
        config_path = self.get_config_path(logger=logger)

        try:
            # Write the updated config
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(new_config, f, indent=2)

            return True
        except Exception as e:
            if logger:
                logger.error(f"Error updating VSCode MCP configuration: {e}")
            else:
                print(f"Error updating VSCode MCP configuration: {e}")
            return False

    def get_current_config(self, logger=None):
        """Get the current VSCode MCP configuration.

        Returns:
            dict: Current VSCode MCP configuration from the local .vscode/mcp.json file.
        """
        config_path = self.get_config_path(logger=logger)

        try:
            try:
                with open(config_path, encoding="utf-8") as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return {}
        except Exception as e:
            if logger:
                logger.error(f"Error reading VSCode MCP configuration: {e}")
            else:
                print(f"Error reading VSCode MCP configuration: {e}")
            return {}

    def configure_mcp_server(
        self,
        server_url,
        request: McpServerRequest | None = None,
        **legacy_kwargs,
    ):
        """Configure an MCP server in VS Code mcp.json file.

        This method updates the .vscode/mcp.json file to add or update
        an MCP server configuration.

        Args:
            server_url (str): URL or identifier of the MCP server.
            request: Optional McpServerRequest with server_name, server_info_cache,
                and logger.
            **legacy_kwargs: Deprecated -- pass individual fields through ``McpServerRequest`` instead.

        Returns:
            bool: True if successful, False otherwise.

        Raises:
            ValueError: If server is not found in registry.
        """
        request = _resolve_mcp_request(request, legacy_kwargs)
        if not server_url:
            if request and request.logger:
                request.logger.error("server_url cannot be empty")
            else:
                print("Error: server_url cannot be empty")
            return False

        req = request or McpServerRequest()
        server_name = req.server_name
        server_info_cache = req.server_info_cache
        logger = req.logger

        try:
            # Use cached server info if available, otherwise fetch from registry
            if server_info_cache and server_url in server_info_cache:
                server_info = server_info_cache[server_url]
            else:
                # Fallback to registry lookup if not cached
                server_info = self.registry_client.find_server_by_reference(server_url)

            # Fail if server is not found in registry - security requirement
            # This raises ValueError as expected by tests
            if not server_info:
                raise ValueError(
                    f"Failed to retrieve server details for '{server_url}'. Server not found in registry."
                )

            # Generate server configuration
            server_config, input_vars = self._format_server_config(server_info)

            if not server_config:
                _emit_log(logger, "error", f"Unable to configure server: {server_url}")
                return False

            # Use provided server name or fallback to server_url
            config_key = server_name or server_url

            # Get current config
            current_config = self.get_current_config(logger=logger)

            # Ensure servers and inputs sections exist
            _ensure_mcp_sections(current_config)

            # Add the server configuration
            current_config["servers"][config_key] = server_config

            # Add input variables (avoiding duplicates)
            _merge_input_vars(current_config["inputs"], input_vars)

            # Update the configuration
            result = self.update_config(current_config, logger=logger)

            if result:
                if logger:
                    logger.verbose_detail(f"Configured MCP server '{config_key}' for VS Code")
                else:
                    print(f"Successfully configured MCP server '{config_key}' for VS Code")
            return result

        except ValueError:
            # Re-raise ValueError for registry errors
            raise
        except Exception as e:
            _emit_log(logger, "error", f"Error configuring MCP server: {e}")
            return False

    def _format_server_config(self, server_info):
        """Delegate to _vscode_server_config."""
        return _vsc._format_server_config(self, server_info)

    def _format_raw_stdio_config(self, server_info, raw):
        """Delegate to _vscode_server_config."""
        return _vsc._format_raw_stdio_config(self, server_info, raw)

    def _format_package_config(self, server_info):
        """Delegate to _vscode_server_config."""
        return _vsc._format_package_config(self, server_info)

    def _build_package_server_config(self, package, runtime_hint, registry_name, pkg_args):
        """Delegate to _vscode_server_config."""
        return _vsc._build_package_server_config(
            self, package, runtime_hint, registry_name, pkg_args
        )

    def _format_remote_config(self, server_info):
        """Delegate to _vscode_server_config."""
        return _vsc._format_remote_config(self, server_info)

    def _format_remote_endpoint_config(self, server_info):
        """Delegate to _vscode_server_config."""
        return _vsc._format_remote_endpoint_config(self, server_info)

    def _handle_incomplete_config(self, server_info):
        """Delegate to _vscode_server_config."""
        return _vsc._handle_incomplete_config(self, server_info)

    def _extract_input_variables(self, mapping, server_name):
        """Delegate to _vscode_server_config."""
        return _vsc._extract_input_variables(self, mapping, server_name)

    def _select_best_package(self, packages):
        """Delegate to _vscode_server_config."""
        return _vsc._select_best_package(self, packages)
