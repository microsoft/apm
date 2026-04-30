"""GitHub Copilot CLI implementation of MCP client adapter.

This adapter implements the Copilot CLI-specific handling of MCP server configuration,
targeting the global ~/.copilot/mcp-config.json file as specified in the MCP installation
architecture specification.
"""

import json
import os
import re
from pathlib import Path

from ...core.docker_args import DockerArgsProcessor
from ...core.token_manager import GitHubTokenManager
from ...registry.client import SimpleRegistryClient
from ...registry.integration import RegistryIntegration
from ...utils.github_host import is_github_hostname
from .base import _ENV_VAR_RE, MCPClientAdapter

# Combined env-var placeholder regex covering all three syntaxes Copilot accepts:
#   <VARNAME>          legacy APM (group 1, uppercase only)
#   ${VARNAME}         POSIX shell (group 2)
#   ${env:VARNAME}     VS Code-flavored (group 2)
# A single-pass substitution preserves the original ``<VAR>`` semantics:
# resolved values are NOT re-scanned, so a token whose literal text contains
# ``${...}`` does not get recursively expanded. Module-level compile avoids
# per-call cost. ``${input:...}`` is intentionally not matched here.
_COPILOT_ENV_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>|" + _ENV_VAR_RE.pattern)


class CopilotClientAdapter(MCPClientAdapter):
    """Copilot CLI implementation of MCP client adapter.

    This adapter handles Copilot CLI-specific configuration for MCP servers using
    a global ~/.copilot/mcp-config.json file, following the JSON format for
    MCP server configuration.
    """

    supports_user_scope: bool = True

    def __init__(
        self,
        registry_url=None,
        project_root: Path | str | None = None,
        user_scope: bool = False,
    ):
        """Initialize the Copilot CLI client adapter.

        Args:
            registry_url (str, optional): URL of the MCP registry.
                If not provided, uses the MCP_REGISTRY_URL environment variable
                or falls back to the default GitHub registry.
            project_root: Project root context passed through to the base
                adapter for scope-aware operations.
            user_scope: Whether the adapter should resolve user-scope config
                paths instead of project-local paths when supported.
        """
        super().__init__(project_root=project_root, user_scope=user_scope)
        self.registry_client = SimpleRegistryClient(registry_url)
        self.registry_integration = RegistryIntegration(registry_url)

    def get_config_path(self):
        """Get the path to the Copilot CLI MCP configuration file.

        Returns:
            str: Path to ~/.copilot/mcp-config.json
        """
        copilot_dir = Path.home() / ".copilot"
        return str(copilot_dir / "mcp-config.json")

    def update_config(self, config_updates):
        """Update the Copilot CLI MCP configuration.

        Args:
            config_updates (dict): Configuration updates to apply.
        """
        current_config = self.get_current_config()

        # Ensure mcpServers section exists
        if "mcpServers" not in current_config:
            current_config["mcpServers"] = {}

        # Apply updates
        current_config["mcpServers"].update(config_updates)

        # Write back to file
        config_path = Path(self.get_config_path())

        # Ensure directory exists
        config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(config_path, "w") as f:
            json.dump(current_config, f, indent=2)

    def get_current_config(self):
        """Get the current Copilot CLI MCP configuration.

        Returns:
            dict: Current configuration, or empty dict if file doesn't exist.
        """
        config_path = self.get_config_path()

        if not os.path.exists(config_path):
            return {}

        try:
            with open(config_path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def configure_mcp_server(
        self,
        server_url,
        server_name=None,
        enabled=True,
        env_overrides=None,
        server_info_cache=None,
        runtime_vars=None,
    ):
        """Configure an MCP server in Copilot CLI configuration.

        This method follows the Copilot CLI MCP configuration format with
        mcpServers object containing server configurations.

        Args:
            server_url (str): URL or identifier of the MCP server.
            server_name (str, optional): Name of the server. Defaults to None.
            enabled (bool, optional): Ignored parameter, kept for API compatibility.
            env_overrides (dict, optional): Pre-collected environment variable overrides.
            server_info_cache (dict, optional): Pre-fetched server info to avoid duplicate registry calls.
            runtime_vars (dict, optional): Pre-collected runtime variable values.

        Returns:
            bool: True if successful, False otherwise.
        """
        if not server_url:
            print("Error: server_url cannot be empty")
            return False

        try:
            # Use cached server info if available, otherwise fetch from registry
            if server_info_cache and server_url in server_info_cache:
                server_info = server_info_cache[server_url]
            else:
                # Fallback to registry lookup if not cached
                server_info = self.registry_client.find_server_by_reference(server_url)

            # Fail if server is not found in registry - security requirement
            if not server_info:
                print(f"Error: MCP server '{server_url}' not found in registry")
                return False

            # Determine the server name for configuration key
            if server_name:
                # Use explicitly provided server name
                config_key = server_name
            else:  # noqa: PLR5501
                # Extract name from server_url (part after last slash)
                # For URLs like "microsoft/azure-devops-mcp" -> "azure-devops-mcp"
                # For URLs like "github/github-mcp-server" -> "github-mcp-server"
                if "/" in server_url:  # noqa: SIM108
                    config_key = server_url.split("/")[-1]
                else:
                    # Fallback to full server_url if no slash
                    config_key = server_url

            # Generate server configuration with environment and runtime variable resolution
            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)

            # Update configuration using the chosen key
            self.update_config({config_key: server_config})

            print(f"Successfully configured MCP server '{config_key}' for Copilot CLI")
            return True

        except Exception as e:
            print(f"Error configuring MCP server: {e}")
            return False

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

        # Self-defined stdio deps carry raw command/args  -- use directly
        raw = server_info.get("_raw_stdio")
        if raw:
            config["command"] = raw["command"]
            config["args"] = raw["args"]
            if raw.get("env"):
                config["env"] = raw["env"]
                self._warn_input_variables(raw["env"], server_info.get("name", ""), "Copilot CLI")
            # Apply tools override if present
            tools_override = server_info.get("_apm_tools_override")
            if tools_override:
                config["tools"] = tools_override
            return config

        # Check for remote endpoints first (registry-defined priority)
        remotes = server_info.get("remotes", [])
        if remotes:
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

            # Add authentication headers for GitHub MCP server
            server_name = server_info.get("name", "")
            is_github_server = self._is_github_server(server_name, remote.get("url", ""))

            if is_github_server:
                # Use centralized token manager (copilot chain: GITHUB_COPILOT_PAT → GITHUB_TOKEN → GITHUB_APM_PAT),
                # falling back to GITHUB_PERSONAL_ACCESS_TOKEN for Copilot CLI compat.
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
                        # Resolve environment variable value
                        resolved_value = self._resolve_env_variable(
                            header_name, header_value, env_overrides
                        )
                        config["headers"][header_name] = resolved_value

            # Warn about unresolvable ${input:...} references in headers
            if config.get("headers"):
                self._warn_input_variables(
                    config["headers"], server_info.get("name", ""), "Copilot CLI"
                )

            # Apply tools override from MCP dependency overlay if present
            tools_override = server_info.get("_apm_tools_override")
            if tools_override:
                config["tools"] = tools_override

            return config

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
                registry_name = self._infer_registry_name(package)
                package_name = package.get("name", "")
                runtime_hint = package.get("runtime_hint", "")
                runtime_arguments = package.get("runtime_arguments", [])
                package_arguments = package.get("package_arguments", [])

                # Process arguments to extract simple string values
                env_vars = package.get("environment_variables", [])

                # Resolve environment variables first
                resolved_env = self._resolve_environment_variables(env_vars, env_overrides)

                processed_runtime_args = self._process_arguments(
                    runtime_arguments, resolved_env, runtime_vars
                )
                processed_package_args = self._process_arguments(
                    package_arguments, resolved_env, runtime_vars
                )

                # Generate command and args based on package type
                if registry_name == "npm":
                    config["command"] = runtime_hint or "npx"
                    config["args"] = (
                        ["-y", package_name] + processed_runtime_args + processed_package_args  # noqa: RUF005
                    )
                    # For NPM packages, use env block for environment variables
                    if resolved_env:
                        config["env"] = resolved_env
                elif registry_name == "docker":
                    config["command"] = "docker"

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
                elif registry_name == "pypi":
                    config["command"] = runtime_hint or "uvx"
                    config["args"] = (
                        [package_name] + processed_runtime_args + processed_package_args  # noqa: RUF005
                    )
                    # For PyPI packages, use env block
                    if resolved_env:
                        config["env"] = resolved_env
                elif registry_name == "homebrew":
                    # For homebrew packages, assume the binary name is the command
                    config["command"] = (
                        package_name.split("/")[-1] if "/" in package_name else package_name
                    )
                    config["args"] = processed_runtime_args + processed_package_args
                    # For Homebrew packages, use env block
                    if resolved_env:
                        config["env"] = resolved_env
                else:
                    # Generic package handling
                    config["command"] = runtime_hint or package_name
                    config["args"] = processed_runtime_args + processed_package_args
                    # Use env block for generic packages
                    if resolved_env:
                        config["env"] = resolved_env

        # Apply tools override from MCP dependency overlay if present
        tools_override = server_info.get("_apm_tools_override")
        if tools_override:
            config["tools"] = tools_override

        return config

    def _resolve_environment_variables(self, env_vars, env_overrides=None):
        """Resolve environment variables to actual values.

        Args:
            env_vars (list): List of environment variable definitions from server info.
            env_overrides (dict, optional): Pre-collected environment variable overrides.

        Returns:
            dict: Dictionary of resolved environment variables.
        """
        import os
        import sys

        from rich.prompt import Prompt

        resolved = {}
        env_overrides = env_overrides or {}

        # If env_overrides is provided, it means the CLI has already handled environment variable collection
        # In this case, we should NEVER prompt for additional variables
        skip_prompting = bool(env_overrides)

        # Check for CI/automated environment via APM_E2E_TESTS flag (more reliable than TTY detection)
        if os.getenv("APM_E2E_TESTS") == "1":
            skip_prompting = True
            print(f" APM_E2E_TESTS detected, will skip environment variable prompts")  # noqa: F541

        # Also skip prompting if we're in a non-interactive environment (fallback)
        is_interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if not is_interactive:
            skip_prompting = True

        # Add default GitHub MCP server environment variables for essential functionality first
        # This ensures variables have defaults when user provides empty values or they're optional
        default_github_env = {"GITHUB_TOOLSETS": "context", "GITHUB_DYNAMIC_TOOLSETS": "1"}

        # Track which variables were explicitly provided with empty values (user wants defaults)
        empty_value_vars = set()
        if env_overrides:
            for key, value in env_overrides.items():
                if key in env_overrides and (not value or not value.strip()):
                    empty_value_vars.add(key)

        for env_var in env_vars:
            if isinstance(env_var, dict):
                name = env_var.get("name", "")
                description = env_var.get("description", "")
                required = env_var.get("required", True)

                if name:
                    # First check overrides, then environment
                    value = env_overrides.get(name) or os.getenv(name)

                    # Only prompt if not provided in overrides or environment AND it's required AND we're not in managed override mode
                    if not value and required and not skip_prompting:
                        prompt_text = f"Enter value for {name}"
                        if description:
                            prompt_text += f" ({description})"
                        value = Prompt.ask(
                            prompt_text,
                            password=True  # noqa: SIM210
                            if "token" in name.lower() or "key" in name.lower()
                            else False,
                        )

                    # Add variable if it has a value OR if user explicitly provided empty and we have a default
                    if value and value.strip():
                        resolved[name] = value
                    elif name in empty_value_vars and name in default_github_env:
                        # User provided empty value and we have a default - use default
                        resolved[name] = default_github_env[name]
                    elif not required and name in default_github_env:
                        # Variable is optional and we have a default - use default
                        resolved[name] = default_github_env[name]
                    elif skip_prompting and name in default_github_env:
                        # Non-interactive environment and we have a default - use default
                        resolved[name] = default_github_env[name]

        return resolved

    def _resolve_env_variable(self, name, value, env_overrides=None):
        """Resolve a single environment variable value.

        Args:
            name (str): Environment variable name.
            value (str): Environment variable value or placeholder.
            env_overrides (dict, optional): Pre-collected environment variable overrides.

        Returns:
            str: Resolved environment variable value.
        """
        import sys

        from rich.prompt import Prompt

        env_overrides = env_overrides or {}
        # If env_overrides is provided, it means we're in managed environment collection mode
        skip_prompting = bool(env_overrides)

        # Check for CI/automated environment via APM_E2E_TESTS flag (more reliable than TTY detection)
        if os.getenv("APM_E2E_TESTS") == "1":
            skip_prompting = True

        # Also skip prompting if we're in a non-interactive environment (fallback)
        is_interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if not is_interactive:
            skip_prompting = True

        # Three accepted placeholder syntaxes (see _COPILOT_ENV_RE at module
        # top), all resolved against env_overrides -> os.environ -> optional
        # interactive prompt. Single-pass substitution preserves the legacy
        # ``<VAR>`` semantics: resolved values are not re-scanned for further
        # placeholder expansion.
        def _replace(match):
            # Group 1 = legacy <VAR>; group 2 = ${VAR} / ${env:VAR}.
            env_name = match.group(1) or match.group(2)
            env_value = env_overrides.get(env_name) or os.getenv(env_name)
            if not env_value and not skip_prompting:
                prompt_text = f"Enter value for {env_name}"
                env_value = Prompt.ask(
                    prompt_text,
                    password=True  # noqa: SIM210
                    if "token" in env_name.lower() or "key" in env_name.lower()
                    else False,
                )
            return env_value if env_value else match.group(0)

        return _COPILOT_ENV_RE.sub(_replace, value)

    def _inject_env_vars_into_docker_args(self, docker_args, env_vars):
        """Inject environment variables into Docker arguments following registry template.

        The registry provides a complete Docker command template in runtime_arguments.
        We need to inject actual environment variable values while respecting the template structure.
        Also ensures required Docker flags (-i, --rm) are present.

        Args:
            docker_args (list): Docker arguments from registry runtime_arguments.
            env_vars (dict): Resolved environment variables.

        Returns:
            list: Docker arguments with environment variables properly injected and required flags.
        """
        if not env_vars:
            env_vars = {}

        result = []
        i = 0
        has_interactive = False
        has_rm = False

        # Check for existing -i and --rm flags
        for arg in docker_args:
            if arg == "-i" or arg == "--interactive":  # noqa: PLR1714
                has_interactive = True
            elif arg == "--rm":
                has_rm = True

        while i < len(docker_args):
            arg = docker_args[i]
            result.append(arg)

            # When we encounter "run", inject required flags first
            if arg == "run":
                # Add -i flag if not present
                if not has_interactive:
                    result.append("-i")

                # Add --rm flag if not present
                if not has_rm:
                    result.append("--rm")

            # If this is an environment variable name placeholder, replace with actual env var
            if arg in env_vars:
                # This is an environment variable name that should be replaced with -e VAR=value
                result.pop()  # Remove the env var name
                result.extend(["-e", f"{arg}={env_vars[arg]}"])
            elif arg == "-e" and i + 1 < len(docker_args):
                # Handle -e flag followed by env var name
                next_arg = docker_args[i + 1]
                if next_arg in env_vars:
                    result.append(f"{next_arg}={env_vars[next_arg]}")
                    i += 1  # Skip the next argument as we've processed it
                else:
                    # Keep the original argument structure
                    result.append(next_arg)
                    i += 1

            i += 1

        # Add any remaining environment variables that weren't in the template
        template_env_vars = set()
        for arg in docker_args:
            if arg in env_vars:
                template_env_vars.add(arg)

        for env_name, env_value in env_vars.items():
            if env_name not in template_env_vars:
                # Find a good place to insert additional env vars (after "run" but before image name)
                insert_pos = len(result)
                for idx, arg in enumerate(result):
                    if arg == "run":
                        # Insert after run command but before image name (usually last arg)
                        insert_pos = min(len(result) - 1, idx + 1)
                        break

                result.insert(insert_pos, "-e")
                result.insert(insert_pos + 1, f"{env_name}={env_value}")

        # Add default GitHub MCP server environment variables if not already present
        # Only add defaults for variables that were NOT explicitly provided (even if empty)
        default_github_env = {"GITHUB_TOOLSETS": "context", "GITHUB_DYNAMIC_TOOLSETS": "1"}  # noqa: F841

        existing_env_vars = set()
        for i, arg in enumerate(result):
            if arg == "-e" and i + 1 < len(result):
                env_spec = result[i + 1]
                if "=" in env_spec:
                    env_name = env_spec.split("=", 1)[0]
                    existing_env_vars.add(env_name)

        # For Copilot, defaults are already added during environment resolution
        # This section is kept for compatibility but shouldn't add duplicates

        return result

    def _inject_docker_env_vars(self, args, env_vars):
        """Inject environment variables into Docker arguments.

        Args:
            args (list): Original Docker arguments.
            env_vars (dict): Environment variables to inject.

        Returns:
            list: Updated arguments with environment variables injected.
        """
        result = []

        for arg in args:
            result.append(arg)
            # If this is a docker run command, inject environment variables after "run"
            if arg == "run" and env_vars:
                for env_name, env_value in env_vars.items():
                    result.extend(["-e", f"{env_name}={env_value}"])

        return result

    def _process_arguments(self, arguments, resolved_env=None, runtime_vars=None):
        """Process argument objects to extract simple string values with environment and runtime variable resolution.

        Args:
            arguments (list): List of argument objects from registry.
            resolved_env (dict): Resolved environment variables.
            runtime_vars (dict): Resolved runtime variables.

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
                    name = arg.get("name", "")
                    value = arg.get("value", arg.get("default", ""))
                    if name:
                        processed.append(name)
                        # For named arguments, only add value if it's different from the flag name
                        # and not empty
                        if value and value != name and not value.startswith("-"):
                            processed_value = self._resolve_variable_placeholders(
                                str(value), resolved_env, runtime_vars
                            )
                            processed.append(processed_value)
            elif isinstance(arg, str):
                # Already a string, use as-is but resolve variable placeholders
                processed_value = self._resolve_variable_placeholders(
                    arg, resolved_env, runtime_vars
                )
                processed.append(processed_value)

        return processed

    def _resolve_variable_placeholders(self, value, resolved_env, runtime_vars):
        """Resolve both environment and runtime variable placeholders in values.

        Args:
            value (str): Value that may contain placeholders like <TOKEN_NAME> or {ado_org}
            resolved_env (dict): Dictionary of resolved environment variables.
            runtime_vars (dict): Dictionary of resolved runtime variables.

        Returns:
            str: Processed value with actual variable values.
        """
        import re

        if not value:
            return value

        processed = str(value)

        # Replace <TOKEN_NAME> with actual values from resolved_env (for Docker env vars)
        env_pattern = r"<([A-Z_][A-Z0-9_]*)>"

        def replace_env_var(match):
            env_name = match.group(1)
            return resolved_env.get(env_name, match.group(0))  # Return original if not found

        processed = re.sub(env_pattern, replace_env_var, processed)

        # Replace {runtime_var} with actual values from runtime_vars (for NPM args)
        runtime_pattern = r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}"

        def replace_runtime_var(match):
            var_name = match.group(1)
            return runtime_vars.get(var_name, match.group(0))  # Return original if not found

        processed = re.sub(runtime_pattern, replace_runtime_var, processed)

        return processed

    def _resolve_env_placeholders(self, value, resolved_env):
        """Legacy method for backward compatibility. Use _resolve_variable_placeholders instead."""
        return self._resolve_variable_placeholders(value, resolved_env, {})

    @staticmethod
    def _select_remote_with_url(remotes):
        """Return the first remote entry that has a non-empty URL.

        Args:
            remotes (list): Candidate remote entries from the registry.

        Returns:
            dict or None: The first usable remote, or None if none qualify.
        """
        for remote in remotes:
            url = (remote.get("url") or "").strip()
            if url:
                return remote
        return None

    def _select_best_package(self, packages):
        """Select the best package for installation from available packages.

        Prioritizes packages in order: npm, docker, pypi, homebrew, others.
        Uses ``_infer_registry_name`` so selection works even when the
        registry API returns empty ``registry_name``.

        Args:
            packages (list): List of package dictionaries.

        Returns:
            dict: Best package to use, or None if no suitable package found.
        """
        priority_order = ["npm", "docker", "pypi", "homebrew"]

        for target in priority_order:
            for package in packages:
                if self._infer_registry_name(package) == target:
                    return package

        # If no priority package found, return the first one
        return packages[0] if packages else None

    def _is_github_server(self, server_name, url):
        """Securely determine if a server is a GitHub MCP server.

        This method uses proper URL parsing and hostname validation to prevent
        security vulnerabilities from substring-based checks.

        Args:
            server_name (str): Name of the MCP server.
            url (str): URL of the remote endpoint.

        Returns:
            bool: True if this is a legitimate GitHub MCP server, False otherwise.
        """
        from urllib.parse import urlparse

        # Check server name against an allowlist of known GitHub MCP servers
        github_server_names = [
            "github-mcp-server",
            "github",
            "github-mcp",
            "github-copilot-mcp-server",
        ]

        # Exact match check for server names (case-insensitive)
        if server_name and server_name.lower() in [name.lower() for name in github_server_names]:
            return True

        # If URL is provided, validate the hostname
        if url:
            try:
                parsed_url = urlparse(url)
                hostname = parsed_url.hostname

                if hostname:
                    # Use helper to determine whether hostname is a GitHub host (cloud or enterprise)
                    if is_github_hostname(hostname):
                        return True

            except Exception:
                # If URL parsing fails, assume it's not a GitHub server
                return False

        return False
