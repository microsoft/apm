# pylint: disable=duplicate-code
"""GitHub Copilot CLI implementation of MCP client adapter.

This adapter implements the Copilot CLI-specific handling of MCP server configuration,
targeting the global ~/.copilot/mcp-config.json file as specified in the MCP installation
architecture specification.
"""

from __future__ import annotations

import os
import re
import sys

from ....core.docker_args import DockerArgsProcessor
from ..base import _ENV_VAR_RE

_COPILOT_ENV_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>|" + _ENV_VAR_RE.pattern)
_LEGACY_ANGLE_VAR_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")


def _format_raw_stdio_config(self, server_info, raw, env_overrides, runtime_vars):
    """Format configuration for raw stdio servers.

    Args:
        server_info: Server information dict.
        raw: Raw stdio configuration.
        env_overrides: Environment variable overrides.
        runtime_vars: Runtime variable values.

    Returns:
        dict: Formatted server configuration.
    """
    config = {
        "type": "local",
        "tools": ["*"],
        "id": server_info.get("id", ""),
    }
    config["command"] = raw["command"]
    resolved_env_for_args = {}
    if raw.get("env"):
        resolved_env_for_args = self._resolve_environment_variables(
            raw["env"], env_overrides=env_overrides
        )
        config["env"] = resolved_env_for_args
        self._warn_input_variables(raw["env"], server_info.get("name", ""), "Copilot CLI")
    args = raw.get("args") or []
    config["args"] = [
        self._resolve_variable_placeholders(arg, resolved_env_for_args, runtime_vars)
        if isinstance(arg, str)
        else arg
        for arg in args
    ]
    # Apply tools override if present
    tools_override = server_info.get("_apm_tools_override")
    if tools_override:
        config["tools"] = tools_override
    return config


def _inject_remote_headers(config, self_obj, server_info, remote, env_overrides):
    """Inject authentication and registry headers into *config["headers"]*.

    Handles GitHub-token injection and per-header env-var resolution.
    Mutates *config* in place; returns nothing.
    """
    server_name = server_info.get("name", "")
    is_github_server = self_obj._is_github_server(server_name, remote.get("url", ""))

    if is_github_server:
        token_manager = sys.modules[__package__].GitHubTokenManager()
        github_token = token_manager.get_token_for_purpose("copilot") or os.getenv(
            "GITHUB_PERSONAL_ACCESS_TOKEN"
        )
        if github_token:
            config["headers"] = {"Authorization": f"Bearer {github_token}"}

    headers = remote.get("headers", [])
    if headers:
        if "headers" not in config:
            config["headers"] = {}
        for header in headers:
            header_name = header.get("name", "")
            header_value = header.get("value", "")
            if header_name and header_value:
                resolved_value = self_obj._resolve_env_variable(
                    header_name, header_value, env_overrides
                )
                config["headers"][header_name] = resolved_value


def _format_remote_config(self, server_info, remotes, env_overrides):
    """Format configuration for remote servers.

    Args:
        server_info: Server information dict.
        remotes: List of remote endpoints.
        env_overrides: Environment variable overrides.

    Returns:
        dict: Formatted server configuration.
    """
    import os
    import sys

    # Select the first remote with a non-empty URL; fall back to the
    # first entry so downstream code still emits the historical empty
    # URL error path when no remote is usable.
    remote = self._select_remote_with_url(remotes) or remotes[0]

    # Validate transport_type from registry: default to "http" when
    # missing/empty, raise ValueError for unsupported values. Mirrors
    # the VS Code adapter check introduced in PR #656 so registry data
    # with, e.g. transport_type="grpc" fails loudly instead of silently
    # producing a garbage config.
    transport = (remote.get("transport_type") or "").strip()
    if not transport:
        transport = "http"
    elif transport not in ("sse", "http", "streamable-http"):
        raise ValueError(
            f"Unsupported remote transport '{transport}' for Copilot. "
            f"Server: {server_info.get('name', 'unknown')}. "
            f"Supported transports: http, sse, streamable-http."
        )

    # Copilot CLI writes "type": "http" for all remote servers so
    # authentication flows (headers) are consistent regardless of the
    # underlying transport advertised by the registry.
    config = {
        "type": "http",
        "url": (remote.get("url") or "").strip(),
        "tools": ["*"],  # Required by Copilot CLI specification
        "id": server_info.get("id", ""),  # Add registry UUID for conflict detection
    }

    # Add authentication headers and registry headers for this remote
    _inject_remote_headers(config, self, server_info, remote, env_overrides)

    # Warn about unresolvable ${input:...} references in headers
    if config.get("headers"):
        self._warn_input_variables(config["headers"], server_info.get("name", ""), "Copilot CLI")

    # Apply tools override from MCP dependency overlay if present
    tools_override = server_info.get("_apm_tools_override")
    if tools_override:
        config["tools"] = tools_override

    return config


def _format_npm_config(package, processed_runtime_args, processed_package_args, resolved_env):
    """Format configuration for npm packages.

    Args:
        package: Package information dict.
        processed_runtime_args: Processed runtime arguments.
        processed_package_args: Processed package arguments.
        resolved_env: Resolved environment variables.

    Returns:
        dict: Formatted configuration segment.
    """
    package_name = package.get("name", "")
    runtime_hint = package.get("runtime_hint", "")
    config = {
        "command": runtime_hint or "npx",
        "args": ["-y", package_name] + processed_runtime_args + processed_package_args,  # noqa: RUF005
    }
    # For NPM packages, use env block for environment variables
    if resolved_env:
        config["env"] = resolved_env
    return config


def _format_docker_config(self, package, processed_runtime_args, resolved_env):
    """Format configuration for docker packages.

    Args:
        package: Package information dict.
        processed_runtime_args: Processed runtime arguments.
        resolved_env: Resolved environment variables.

    Returns:
        dict: Formatted configuration segment.
    """
    from ....core.docker_args import DockerArgsProcessor

    package_name = package.get("name", "")
    config = {"command": "docker"}

    # For Docker packages, the registry provides the complete command template
    # We should respect the runtime_arguments as the authoritative Docker command structure
    if processed_runtime_args:
        # Registry provides complete Docker command arguments
        # Just inject environment variables where appropriate
        config["args"] = self._inject_env_vars_into_docker_args(
            processed_runtime_args, resolved_env
        )
    else:
        # Fallback to basic docker run command if no runtime args
        config["args"] = DockerArgsProcessor.process_docker_args(
            ["run", "-i", "--rm", package_name], resolved_env
        )
    return config


def _format_pypi_config(package, processed_runtime_args, processed_package_args, resolved_env):
    """Format configuration for pypi packages.

    Args:
        package: Package information dict.
        processed_runtime_args: Processed runtime arguments.
        processed_package_args: Processed package arguments.
        resolved_env: Resolved environment variables.

    Returns:
        dict: Formatted configuration segment.
    """
    package_name = package.get("name", "")
    runtime_hint = package.get("runtime_hint", "")
    config = {
        "command": runtime_hint or "uvx",
        "args": [package_name] + processed_runtime_args + processed_package_args,  # noqa: RUF005
    }
    # For PyPI packages, use env block
    if resolved_env:
        config["env"] = resolved_env
    return config


def _format_homebrew_config(package, processed_runtime_args, processed_package_args, resolved_env):
    """Format configuration for homebrew packages.

    Args:
        package: Package information dict.
        processed_runtime_args: Processed runtime arguments.
        processed_package_args: Processed package arguments.
        resolved_env: Resolved environment variables.

    Returns:
        dict: Formatted configuration segment.
    """
    package_name = package.get("name", "")
    config = {
        "command": package_name.split("/")[-1] if "/" in package_name else package_name,
        "args": processed_runtime_args + processed_package_args,
    }
    # For Homebrew packages, use env block
    if resolved_env:
        config["env"] = resolved_env
    return config


def _format_generic_config(package, processed_runtime_args, processed_package_args, resolved_env):
    """Format configuration for generic packages.

    Args:
        package: Package information dict.
        processed_runtime_args: Processed runtime arguments.
        processed_package_args: Processed package arguments.
        resolved_env: Resolved environment variables.

    Returns:
        dict: Formatted configuration segment.
    """
    package_name = package.get("name", "")
    runtime_hint = package.get("runtime_hint", "")
    config = {
        "command": runtime_hint or package_name,
        "args": processed_runtime_args + processed_package_args,
    }
    # Use env block for generic packages
    if resolved_env:
        config["env"] = resolved_env
    return config


def _format_package_config(self, package, env_vars, env_overrides, runtime_vars):
    """Format configuration for a local package.

    Args:
        package: Package information dict.
        env_vars: Environment variable definitions.
        env_overrides: Environment variable overrides.
        runtime_vars: Runtime variable values.

    Returns:
        dict: Formatted configuration segment.
    """
    registry_name = self._infer_registry_name(package)
    runtime_arguments = package.get("runtime_arguments", [])
    package_arguments = package.get("package_arguments", [])

    # Resolve environment variables first
    resolved_env = self._resolve_environment_variables(env_vars, env_overrides)

    processed_runtime_args = self._process_arguments(runtime_arguments, resolved_env, runtime_vars)
    processed_package_args = self._process_arguments(package_arguments, resolved_env, runtime_vars)

    # Generate command and args based on package type
    if registry_name == "npm":
        return _format_npm_config(
            package, processed_runtime_args, processed_package_args, resolved_env
        )
    elif registry_name == "docker":
        return _format_docker_config(self, package, processed_runtime_args, resolved_env)
    elif registry_name == "pypi":
        return _format_pypi_config(
            package, processed_runtime_args, processed_package_args, resolved_env
        )
    elif registry_name == "homebrew":
        return _format_homebrew_config(
            package, processed_runtime_args, processed_package_args, resolved_env
        )
    else:
        return _format_generic_config(
            package, processed_runtime_args, processed_package_args, resolved_env
        )


def _format_server_config(self, server_info, env_overrides=None, runtime_vars=None):
    """Format server information into Copilot CLI MCP configuration format.

    Args:
        server_info (dict): Server information from registry.
        env_overrides (dict, optional): Pre-collected environment variable overrides.
        runtime_vars (dict, optional): Pre-collected runtime variable values.

    Returns:
        dict: Formatted server configuration for Copilot CLI.
    """
    if runtime_vars is None:
        runtime_vars = {}

    # Default configuration structure with registry ID for conflict detection
    config = {
        "type": "local",
        "tools": ["*"],  # Required by Copilot CLI specification - default to all tools
        "id": server_info.get("id", ""),  # Add registry UUID for conflict detection
    }

    # Self-defined stdio deps carry raw command/args  -- use directly,
    # but route values through the env-var translation/resolution pipeline
    # so secrets are not baked into the persisted config when the harness
    # supports runtime substitution (Copilot CLI).
    raw = server_info.get("_raw_stdio")
    if raw:
        return _format_raw_stdio_config(self, server_info, raw, env_overrides, runtime_vars)

    # Check for remote endpoints first (registry-defined priority)
    remotes = server_info.get("remotes", [])
    if remotes:
        return _format_remote_config(self, server_info, remotes, env_overrides)

    # Get packages from server info
    packages = server_info.get("packages", [])

    if not packages and not remotes:
        # If no packages AND no remotes are available, this indicates incomplete server configuration
        # This should fail installation with a clear error message
        raise ValueError(
            f"MCP server has incomplete configuration in registry - no package information or remote endpoints available. "
            f"This appears to be a temporary registry issue. "
            f"Server: {server_info.get('name', 'unknown')}"
        )

    if packages:
        # Use the first package for configuration (prioritize npm, then docker, then others)
        package = self._select_best_package(packages)

        if package:
            env_vars = package.get("environment_variables", [])
            package_config = _format_package_config(
                self, package, env_vars, env_overrides, runtime_vars
            )
            config.update(package_config)

    # Apply tools override from MCP dependency overlay if present
    tools_override = server_info.get("_apm_tools_override")
    if tools_override:
        config["tools"] = tools_override

    return config
