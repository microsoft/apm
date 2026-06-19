"""OpenAI Codex CLI implementation of MCP client adapter."""

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import toml

from ...registry.client import SimpleRegistryClient
from ...registry.integration import RegistryIntegration
from ...utils.console import _rich_success, _rich_warning
from ._mcp_runtime_args import process_v01_value_hint_arg
from .base import MCPClientAdapter

_log = logging.getLogger(__name__)


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
        os.chmod(config_path, 0o600)
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
        server_name=None,
        enabled=True,
        env_overrides=None,
        server_info_cache=None,
        runtime_vars=None,
    ):
        """Configure an MCP server in Codex CLI configuration.

        This method follows the Codex CLI MCP configuration format with
        mcp_servers sections in the TOML configuration.

        Args:
            server_url (str): URL or identifier of the MCP server.
            server_name (str, optional): Name of the server. Defaults to None.
            enabled (bool, optional): Ignored parameter, kept for API compatibility.
            env_overrides (dict, optional): Pre-collected environment variable overrides.
            server_info_cache (dict, optional): Pre-fetched server info to avoid duplicate registry calls.
            runtime_vars (dict, optional): Runtime variable values. Defaults to None.

        Returns:
            bool: True if successful, False otherwise.
        """
        if not server_url:
            print("Error: server_url cannot be empty")
            return False

        try:
            server_info = self._fetch_server_info(server_url, server_info_cache)
            if server_info is None:
                return False

            config_key = self._determine_config_key(server_url, server_name)

            # Generate server configuration with environment variable resolution
            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)

            # Skip if formatter signaled "unsupported" (e.g. SSE remote on Codex)
            if server_config is None:
                return False

            # Update configuration using the chosen key
            if not self.update_config({config_key: server_config}):
                return False

            _rich_success(
                f"Configured MCP server '{config_key}' for Codex CLI",
                symbol="success",
            )
            return True

        except Exception as e:
            print(f"Error configuring MCP server: {e}")
            return False

    def _format_server_config(self, server_info, env_overrides=None, runtime_vars=None):
        """Format server information into Codex CLI MCP configuration format.

        Args:
            server_info (dict): Server information from registry.
            env_overrides (dict, optional): Pre-collected environment variable overrides.
            runtime_vars (dict, optional): Runtime variable values.

        Returns:
            dict | None: Formatted server configuration for Codex CLI, or None if unsupported (e.g. SSE remote).
        """
        if runtime_vars is None:
            runtime_vars = {}

        # Default configuration structure with registry ID for conflict detection
        config = {
            "command": "unknown",
            "args": [],
            "env": {},
            "id": server_info.get("id", ""),  # Add registry UUID for conflict detection
        }

        # Self-defined stdio deps carry raw command/args. Route ``env`` and
        # ``args`` through the resolver pipeline so all three placeholder
        # syntaxes (``<VAR>``, ``${VAR}``, ``${env:VAR}``) are resolved at
        # install time before being written to ~/.codex/config.toml.
        # See issue #1266.
        raw = server_info.get("_raw_stdio")
        if raw:
            config["command"] = raw["command"]
            resolved_env_for_args: dict = {}
            if raw.get("env"):
                resolved_env_for_args = self._resolve_environment_variables(
                    raw["env"], env_overrides=env_overrides
                )
                config["env"] = resolved_env_for_args
                self._warn_input_variables(raw["env"], server_info.get("name", ""), "Codex CLI")

            def _process_stdio_arg(arg):
                if isinstance(arg, str):
                    arg = self._resolve_variable_placeholders(
                        arg, resolved_env_for_args, runtime_vars
                    )
                return self.normalize_project_arg(arg)

            config["args"] = [_process_stdio_arg(arg) for arg in raw.get("args") or []]
            self._merge_extra(config, server_info)
            return config

        # Remote MCP handling.
        # Precedence on Codex when a server publishes BOTH a remote and a stdio
        # package: prefer the stdio package (falls through to the packages branch
        # below). The remote-only branch here handles the streamable-http path
        # and rejects SSE / non-https / empty-url remotes with explicit warnings.
        remotes = server_info.get("remotes", [])
        packages = server_info.get("packages", [])
        if remotes and not packages:
            remote = self._select_remote_with_url(remotes) or remotes[0]
            server_name = server_info.get("name", "")
            if (remote.get("transport_type") or "").strip() == "sse":
                _rich_warning(
                    f"Skipping MCP server '{server_name}' for Codex CLI: SSE transport "
                    "is deprecated by the MCP spec and not supported by Codex. "
                    "Switch to `transport: streamable-http`.",
                    symbol="warning",
                )
                return None

            remote_url = (remote.get("url") or "").strip()
            if not remote_url:
                _rich_warning(
                    f"Skipping MCP server '{server_name}' for Codex CLI: remote entry "
                    "has an empty url. Set `url:` to the server's streamable-http endpoint.",
                    symbol="warning",
                )
                return None

            scheme = urlparse(remote_url).scheme.lower()
            if scheme != "https":
                _rich_warning(
                    f"Skipping MCP server '{server_name}' for Codex CLI: remote URL "
                    f"must use https:// (got {scheme or 'no scheme'}).",
                    symbol="warning",
                )
                return None

            remote_config = {
                "url": remote_url,
                "id": server_info.get("id", ""),
            }
            http_headers: dict[str, str] = {}
            for header in remote.get("headers", []):
                h_name = header.get("name", "")
                h_value = header.get("value", "")
                if h_name and h_value:
                    http_headers[h_name] = self._resolve_variable_placeholders(
                        h_value, env_overrides or {}, runtime_vars or {}
                    )
            if http_headers:
                remote_config["http_headers"] = http_headers
                self._warn_input_variables(http_headers, server_name, "Codex CLI")
            self._merge_extra(remote_config, server_info)
            return remote_config

        if not packages:
            # If no packages are available, this indicates incomplete server configuration
            # This should fail installation with a clear error message
            raise ValueError(
                f"MCP server has no package information available in registry. "
                f"This appears to be a temporary registry issue or the server is remote-only. "
                f"Server: {server_info.get('name', 'unknown')}"
            )

        if packages:
            if remotes:
                # Hybrid registry server: log that Codex prefers the stdio package
                # over the remote endpoint so the precedence is auditable.
                _log.debug(
                    "Codex hybrid server '%s': preferring stdio package over remote endpoint",
                    server_info.get("name", "unknown"),
                )
            # Use the first package for configuration (prioritize npm, then docker, then others)
            package = self._select_best_package(packages)

            if package:
                registry_name = self._infer_registry_name(package)
                package_name = package.get("name", "")
                runtime_hint = package.get("runtime_hint", "")
                runtime_arguments = package.get("runtime_arguments", [])
                package_arguments = package.get("package_arguments", [])
                env_vars = package.get("environment_variables", [])

                # Resolve environment variables first
                resolved_env = self._process_environment_variables(env_vars, env_overrides)

                # Process arguments to extract simple string values
                processed_runtime_args = self._process_arguments(
                    runtime_arguments, resolved_env, runtime_vars
                )
                processed_package_args = self._process_arguments(
                    package_arguments, resolved_env, runtime_vars
                )

                # Generate command and args based on package type
                if registry_name == "npm":
                    config["command"] = runtime_hint or "npx"
                    all_args = processed_runtime_args + processed_package_args
                    if all_args:
                        # If runtime_arguments already include the package (bare or
                        # versioned), use them as-is — they are authoritative from
                        # the registry and may carry a version pin.
                        has_pkg = any(
                            a == package_name or a.startswith(f"{package_name}@") for a in all_args
                        )
                        if has_pkg:
                            config["args"] = all_args
                        else:
                            # Legacy: runtime_arguments don't mention the package,
                            # prepend -y + bare name ourselves.
                            extra_args = [a for a in all_args if a != "-y"]
                            config["args"] = ["-y", package_name] + extra_args  # noqa: RUF005
                    else:
                        config["args"] = ["-y", package_name]
                    # For NPM packages, also use env block for environment variables
                    if resolved_env:
                        config["env"] = resolved_env
                elif registry_name == "docker":
                    config["command"] = "docker"

                    # For Docker packages in Codex TOML format:
                    # - Ensure all environment variables from resolved_env are represented as -e flags in args
                    # - Put actual environment variable values in separate [env] section
                    config["args"] = self._ensure_docker_env_flags(
                        processed_runtime_args + processed_package_args, resolved_env
                    )

                    # Environment variables go in separate env section for Codex TOML format
                    if resolved_env:
                        config["env"] = resolved_env
                elif registry_name == "pypi":
                    self._apply_pypi_homebrew_generic_config(
                        config,
                        registry_name,
                        package_name,
                        runtime_hint,
                        processed_runtime_args,
                        processed_package_args,
                        resolved_env,
                    )

        self._merge_extra(config, server_info)
        return config

    def _process_arguments(  # pylint: disable=duplicate-code  # structural similarity with copilot adapter is intentional
        self, arguments, resolved_env=None, runtime_vars=None
    ):
        """Process argument objects to extract simple string values with environment resolution.

        Args:
            arguments (list): List of argument objects from registry.
            resolved_env (dict): Resolved environment variables.
            runtime_vars (dict): Runtime variable values.

        Returns:
            list: List of processed argument strings.
        """
        if resolved_env is None:
            resolved_env = {}
        if runtime_vars is None:
            runtime_vars = {}

        processed = []

        for arg in arguments:
            if isinstance(arg, dict):
                # Extract value from argument object
                arg_type = arg.get("type", "")
                if arg_type == "positional":
                    value = arg.get("value", arg.get("default", ""))
                    if value:
                        # Resolve both environment and runtime variable placeholders with actual values
                        processed_value = self._resolve_variable_placeholders(
                            str(value), resolved_env, runtime_vars
                        )
                        processed.append(processed_value)
                elif arg_type == "named":
                    # For named arguments, the flag name is in the "value" field
                    flag_name = arg.get("value", "")
                    if flag_name:
                        processed.append(flag_name)
                        # Some named arguments might have additional values (rare)
                        additional_value = arg.get("name", "")
                        if (
                            additional_value
                            and additional_value != flag_name
                            and not additional_value.startswith("-")
                        ):
                            processed_value = self._resolve_variable_placeholders(
                                str(additional_value), resolved_env, runtime_vars
                            )
                            processed.append(processed_value)
                elif not arg_type and "value_hint" in arg:
                    # v0.1 registry format: shared helper handles is_required
                    # guard and {var_name} placeholder substitution.
                    value = process_v01_value_hint_arg(arg, runtime_vars)
                    if value:
                        processed_value = self._resolve_variable_placeholders(
                            value, resolved_env, runtime_vars
                        )
                        processed.append(processed_value)
            elif isinstance(arg, str):
                # Already a string, use as-is but resolve variable placeholders
                processed_value = self._resolve_variable_placeholders(
                    arg, resolved_env, runtime_vars
                )
                processed.append(processed_value)

        return processed

    def _process_environment_variables(self, env_vars, env_overrides=None):
        """Process environment variable definitions and resolve actual values.

        Args:
            env_vars (list): List of environment variable definitions.
            env_overrides (dict, optional): Pre-collected environment variable overrides.

        Returns:
            dict: Dictionary of resolved environment variable values.
        """
        default_github_env = {"GITHUB_TOOLSETS": "context", "GITHUB_DYNAMIC_TOOLSETS": "1"}
        return self._resolve_env_vars_with_prompting(env_vars, env_overrides, default_github_env)

    def _ensure_docker_env_flags(self, base_args, env_vars):
        """Ensure all environment variables are represented as -e flags in Docker args.

        For Codex TOML format, Docker args should contain -e flags for ALL environment variables
        that will be available to the container, while actual values go in the [env] section.

        Args:
            base_args (list): Base Docker arguments from registry.
            env_vars (dict): All environment variables that should be available.

        Returns:
            list: Docker arguments with -e flags for all environment variables.
        """
        if not env_vars:
            return base_args

        result = []
        existing_env_vars = set()

        # First pass: collect existing -e flags and build result with existing args
        i = 0
        while i < len(base_args):
            arg = base_args[i]
            result.append(arg)

            # Track existing -e flags
            if arg == "-e" and i + 1 < len(base_args):
                env_var_name = base_args[i + 1]
                existing_env_vars.add(env_var_name)
                result.append(env_var_name)
                i += 2
            else:
                i += 1

        # Second pass: add -e flags for any environment variables not already present
        # Insert them after "run" but before the image name (last argument)
        image_name = result[-1] if result else ""
        if image_name and not image_name.startswith("-"):
            # Remove image name temporarily
            result.pop()

            # Add missing environment variable flags
            for env_name in sorted(env_vars.keys()):
                if env_name not in existing_env_vars:
                    result.extend(["-e", env_name])

            # Add image name back
            result.append(image_name)
        else:
            # If we can't identify image name, just append at the end
            for env_name in sorted(env_vars.keys()):
                if env_name not in existing_env_vars:
                    result.extend(["-e", env_name])

        return result

    def _inject_docker_env_vars(self, args, env_vars):
        """Inject environment variables into Docker arguments as -e flags.

        Args:
            args (list): Original Docker arguments.
            env_vars (dict): Environment variables to inject.

        Returns:
            list: Updated arguments with environment variables injected as -e flags.
        """
        if not env_vars:
            return args

        result = []
        existing_env_vars = set()

        # First pass: collect existing -e flags to avoid duplicates
        i = 0
        while i < len(args):
            if args[i] == "-e" and i + 1 < len(args):
                existing_env_vars.add(args[i + 1])
                i += 2
            else:
                i += 1

        # Second pass: build the result with new env vars injected after "run"
        for i, arg in enumerate(args):  # noqa: B007
            result.append(arg)
            # If this is a docker run command, inject new environment variables after "run"
            if arg == "run":
                for env_name in env_vars.keys():  # noqa: SIM118
                    if env_name not in existing_env_vars:
                        result.extend(["-e", env_name])

        return result
