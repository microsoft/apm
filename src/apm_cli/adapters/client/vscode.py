"""VSCode implementation of MCP client adapter.

This adapter implements the VSCode-specific handling of MCP server configuration,
following the official documentation at:
https://code.visualstudio.com/docs/copilot/chat/mcp-servers
"""

import hashlib
import json
import re
from pathlib import Path

from ...core.docker_args import DockerArgsProcessor
from ...registry.client import SimpleRegistryClient
from ...registry.integration import RegistryIntegration
from ...utils.console import _rich_warning
from .base import (
    _ENV_VAR_RE,
    _INPUT_VAR_RE,
    _RUNTIME_TEMPLATE_VARIABLE_RE,
    MCPClientAdapter,
    registry_field_is_required,
)

# Legacy ``<VAR>`` placeholder (Copilot CLI / Codex only). VS Code does not
# resolve angle-bracket placeholders, so emitting them produces literal
# ``<VAR>`` text in headers / env values -- silently breaking auth at runtime.
_LEGACY_ANGLE_VAR_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")


class VSCodeClientAdapter(MCPClientAdapter):
    """VSCode implementation of MCP client adapter.

    This adapter handles VSCode-specific configuration for MCP servers using
    a repository-level .vscode/mcp.json file, following the format specified
    in the VSCode documentation.
    """

    target_name: str = "vscode"
    mcp_servers_key: str = "servers"

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
        server_name=None,
        enabled=True,
        env_overrides=None,
        server_info_cache=None,
        runtime_vars=None,
        logger=None,
    ):
        """Configure an MCP server in VS Code mcp.json file.

        This method updates the .vscode/mcp.json file to add or update
        an MCP server configuration.

        Args:
            server_url (str): URL or identifier of the MCP server.
            server_name (str, optional): Name of the server. Defaults to None.
            enabled (bool, optional): Whether to enable the server. Defaults to True.
            env_overrides (dict, optional): Environment variable overrides. Defaults to None.
            server_info_cache (dict, optional): Pre-fetched server info to avoid duplicate registry calls.
            logger: Optional CommandLogger for structured output.

        Returns:
            bool: True if successful, False otherwise.

        Raises:
            ValueError: If server is not found in registry.
        """
        if not server_url:
            if logger:
                logger.error("server_url cannot be empty")
            else:
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
            # This raises ValueError as expected by tests
            if not server_info:
                raise ValueError(
                    f"Failed to retrieve server details for '{server_url}'. Server not found in registry."
                )

            # Use provided server name or fallback to server_url
            config_key = server_name or server_url

            # Get current config before formatting so optional registry fields
            # that the user already edited can be preserved on reinstall.
            current_config = self.get_current_config(logger=logger)
            existing_servers = current_config.get("servers")
            if not isinstance(existing_servers, dict):
                existing_servers = {}
            existing_server_config = existing_servers.get(config_key)

            # Generate server configuration
            server_config, input_vars = self._format_server_config(
                server_info,
                runtime_vars=runtime_vars,
                env_overrides=env_overrides,
                existing_server_config=existing_server_config,
            )

            if not server_config:
                if logger:
                    logger.error(f"Unable to configure server: {server_url}")
                else:
                    print(f"Unable to configure server: {server_url}")
                return False

            # Ensure servers and inputs sections exist
            if "servers" not in current_config or not isinstance(current_config["servers"], dict):
                current_config["servers"] = {}
            if "inputs" not in current_config or not isinstance(current_config["inputs"], list):
                current_config["inputs"] = []

            # Add the server configuration
            current_config["servers"][config_key] = server_config

            # Add input variables (avoiding duplicates)
            existing_input_ids = {
                var.get("id") for var in current_config["inputs"] if isinstance(var, dict)
            }
            for var in input_vars:
                if var.get("id") not in existing_input_ids:
                    current_config["inputs"].append(var)
                    existing_input_ids.add(var.get("id"))

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
            if logger:
                logger.error(f"Error configuring MCP server: {e}")
            else:
                print(f"Error configuring MCP server: {e}")
            return False

    def _format_server_config(
        self,
        server_info,
        runtime_vars=None,
        env_overrides=None,
        existing_server_config=None,
    ):
        """Format server details into VSCode mcp.json compatible format.

        Args:
            server_info (dict): Server information from registry.
            runtime_vars (dict, optional): Runtime variable substitutions.
            env_overrides (dict, optional): Environment values collected at install time.
            existing_server_config (dict, optional): Current config for this server.

        Returns:
            tuple: (server_config, input_vars) where:
                - server_config is the formatted server configuration for mcp.json
                - input_vars is a list of input variable definitions
        """
        # Initialize the base config structure
        server_config = {}
        input_vars = []

        # Self-defined stdio deps carry raw command/args  -- use directly
        raw = server_info.get("_raw_stdio")
        if raw:
            server_config = {
                "type": "stdio",
                "command": raw["command"],
                "args": raw["args"],
            }
            if raw.get("env"):
                # Translate bare ${VAR} -> ${env:VAR} so VS Code's runtime env
                # interpolation resolves them at server-start. ${input:...}
                # references are preserved for input-variable extraction below.
                self._warn_on_legacy_angle_vars(
                    raw["env"], server_info.get("name", "unknown"), "env"
                )
                env_translated = self._translate_env_vars_for_vscode(raw["env"])
                server_config["env"] = env_translated
                input_vars.extend(
                    self._extract_input_variables(env_translated, server_info.get("name", ""))
                )
            self._merge_extra(server_config, server_info)
            return server_config, input_vars

        # Check for packages information
        if server_info.get("packages"):
            package = self._select_best_package(server_info["packages"])
            runtime_hint = package.get("runtime_hint", "") if package else ""
            registry_name = self._infer_registry_name(package) if package else ""
            is_docker = runtime_hint == "docker" or registry_name == "docker"
            env_config, declared_inputs = self._declared_vscode_environment(
                package,
                server_name=server_info.get("name", ""),
                env_overrides=env_overrides,
                existing_server_config=existing_server_config,
            )
            secret_fallbacks, argument_inputs = self._declared_vscode_argument_secret_inputs(
                package,
                server_name=server_info.get("name", ""),
            )
            seen_input_ids = {item.get("id") for item in input_vars}
            for input_definition in (*declared_inputs, *argument_inputs):
                if input_definition.get("id") in seen_input_ids:
                    continue
                input_vars.append(input_definition)
                seen_input_ids.add(input_definition.get("id"))

            runtime_groups = []
            package_groups = []
            if package and not is_docker:
                parser_kwargs = {
                    "resolved_env": env_config,
                    "runtime_vars": runtime_vars,
                    "runtime_variable_fallbacks": {"workspaceFolder": "${workspaceFolder}"},
                    "secret_variable_fallbacks": secret_fallbacks,
                    "package_name": package.get("name", ""),
                }
                runtime_groups = self._parse_non_container_argument_groups(
                    package.get("runtime_arguments"),
                    **parser_kwargs,
                )
                package_groups = self._parse_non_container_argument_groups(
                    package.get("package_arguments"),
                    **parser_kwargs,
                )

            # Handle npm packages
            if runtime_hint == "npx" or registry_name == "npm":
                package_name = package.get("name")
                server_config = {
                    "type": "stdio",
                    "command": runtime_hint or "npx",
                    "args": self._build_non_container_launcher_argv(
                        package_name,
                        runtime_groups,
                        package_groups,
                        launcher_prefix=("-y",),
                    ),
                }

            # Handle docker packages
            elif is_docker:
                docker_kwargs = (
                    {"secret_variable_fallbacks": secret_fallbacks} if secret_fallbacks else {}
                )
                unresolved_variables: list[str] = []
                args = self._docker_run_args(
                    package,
                    runtime_vars,
                    unresolved_variables=unresolved_variables,
                    **docker_kwargs,
                )
                if args is None:
                    if package.get("runtime_arguments") or package.get("package_arguments"):
                        variable_name = (
                            unresolved_variables[0] if unresolved_variables else "unknown"
                        )
                        _rich_warning(
                            "Could not resolve required container run option "
                            f"'{variable_name}' for '{package.get('name', '')}'; "
                            "target configuration was not changed. Set the required "
                            "registry runtime variables and rerun 'apm install'."
                        )
                        return {}, []
                    args = self._ensure_docker_image_arg(["run", "-i", "--rm"], package.get("name"))

                server_config = {"type": "stdio", "command": "docker", "args": args}

            # Handle Python packages
            elif (
                runtime_hint in ["uvx", "pip", "python"]
                or "python" in runtime_hint
                or registry_name == "pypi"
            ):
                if runtime_hint == "uvx":
                    command = "uvx"
                elif "python" in runtime_hint:
                    command = "python3" if runtime_hint in ["python", "pip"] else runtime_hint
                else:
                    command = "uvx"
                if command != "uvx":
                    module_name = (
                        package.get("name", "").replace("mcp-server-", "").replace("-", "_")
                    )
                    module_operand = f"mcp_server_{module_name}"
                    authored_args = self._flatten_non_container_argument_groups(
                        [*runtime_groups, *package_groups]
                    )
                    runtime_values = self._flatten_non_container_argument_groups(runtime_groups)
                    has_authored_entrypoint = (
                        any(group.kind == "legacy" for group in runtime_groups)
                        or any(
                            value == "-"
                            or value in ("-c", "-m")
                            or (value.startswith(("-c", "-m")) and len(value) > 2)
                            for value in runtime_values
                        )
                        or any(
                            group.kind == "positional"
                            and len(group.values) == 1
                            and not group.values[0].startswith("-")
                            for group in runtime_groups
                        )
                    )
                    if has_authored_entrypoint:
                        args = authored_args
                    else:
                        args = self._build_non_container_launcher_argv(
                            module_operand,
                            runtime_groups,
                            package_groups,
                            launcher_prefix=("-m",),
                        )
                else:
                    args = self._build_non_container_launcher_argv(
                        package.get("name", ""),
                        runtime_groups,
                        package_groups,
                    )
                server_config = {
                    "type": "stdio",
                    "command": command,
                    "args": args,
                }

            # Generic fallback for packages with a runtime_hint (e.g. dotnet, nuget, mcpb)
            elif package and runtime_hint:
                server_config = {
                    "type": "stdio",
                    "command": runtime_hint,
                    "args": self._build_non_container_launcher_argv(
                        package.get("name", ""),
                        runtime_groups,
                        package_groups,
                    ),
                }

            if env_config:
                server_config["env"] = env_config

        # If no server config was created from packages, check for other server types
        if not server_config:
            # Check for SSE endpoints
            if "sse_endpoint" in server_info:
                server_config = {
                    "type": "sse",
                    "url": server_info["sse_endpoint"],
                    "headers": server_info.get("sse_headers", {}),
                }
            # Check for remotes (similar to Copilot adapter)
            elif server_info.get("remotes"):
                remote = self._select_remote_with_url(server_info["remotes"])
                if remote:
                    transport = (remote.get("transport_type") or "").strip()
                    # Default to "http" when transport_type is missing/empty,
                    # matching the Copilot adapter behavior (copilot.py:190-192).
                    if not transport:
                        transport = "http"
                    elif transport not in ("sse", "http", "streamable-http"):
                        raise ValueError(
                            f"Unsupported remote transport '{transport}' for VS Code. "
                            f"Server: {server_info.get('name', 'unknown')}. "
                            f"Supported transports: http, sse, streamable-http."
                        )
                    headers = remote.get("headers", {})
                    # Normalize header list format to dict
                    if isinstance(headers, list):
                        headers = {
                            h["name"]: h["value"] for h in headers if "name" in h and "value" in h
                        }
                    # Translate bare ${VAR} -> ${env:VAR} so VS Code resolves
                    # them from the host environment at runtime, instead of
                    # sending the literal placeholder as the header value.
                    self._warn_on_legacy_angle_vars(
                        headers, server_info.get("name", "unknown"), "headers"
                    )
                    headers = self._translate_env_vars_for_vscode(headers)
                    server_config = {
                        "type": transport,
                        "url": remote["url"].strip(),
                        "headers": headers,
                    }
                    input_vars.extend(
                        self._extract_input_variables(headers, server_info.get("name", ""))
                    )
            # If no packages AND no endpoints/remotes, fail with clear error
            else:
                packages = server_info.get("packages", [])
                if packages:
                    inferred = [
                        self._infer_registry_name(p) or p.get("name", "unknown") for p in packages
                    ]
                    raise ValueError(
                        f"No supported transport for VS Code runtime. "
                        f"Server '{server_info.get('name', 'unknown')}' provides stdio packages "
                        f"({', '.join(inferred)}) but none could be mapped to a VS Code configuration. "
                        f"Supported package types: npm, pypi, docker."
                    )
                raise ValueError(
                    f"MCP server has incomplete configuration in registry - no package information or remote endpoints available. "
                    f"Server: {server_info.get('name', 'unknown')}"
                )

        self._merge_extra(server_config, server_info)
        return server_config, input_vars

    def _declared_vscode_environment(
        self,
        package,
        *,
        server_name,
        env_overrides=None,
        existing_server_config=None,
    ):
        """Return target-native env values and input definitions for a package."""
        if not package:
            return {}, []
        env_vars = package.get("environment_variables") or package.get("environmentVariables") or []
        env_config = {}
        input_vars = []
        for env_var in env_vars:
            if "name" not in env_var:
                continue
            name = env_var["name"]
            input_var_name = name.lower().replace("_", "-")
            input_ref = f"${{input:{input_var_name}}}"
            env_value = self._value_for_declared_vscode_env(
                env_var,
                env_overrides=env_overrides,
                existing_server_config=existing_server_config,
                input_ref=input_ref,
            )
            if env_value is None:
                continue
            env_config[name] = env_value
            if env_value == input_ref:
                input_vars.append(
                    {
                        "type": "promptString",
                        "id": input_var_name,
                        "description": env_var.get(
                            "description",
                            f"{name} for MCP server",
                        ),
                        "password": True,
                    }
                )
            else:
                input_vars.extend(
                    self._extract_input_variables(
                        {name: env_value},
                        server_name,
                    )
                )
        return env_config, input_vars

    @staticmethod
    def _vscode_argument_secret_input_id(server_name: str, variable_name: str) -> str:
        """Return a collision-safe VS Code input ID for one server variable."""
        server_digest = hashlib.sha256(server_name.encode("utf-8")).hexdigest()[:12]
        return f"mcp-{server_digest}-{variable_name.encode('utf-8').hex()}"

    @classmethod
    def _declared_vscode_argument_secret_inputs(cls, package, *, server_name: str):
        """Return VS Code input indirections for secret argument variables."""
        if not package:
            return {}, []
        package_variables = cls._package_runtime_variable_metadata(package)
        if package_variables is None:
            raise ValueError("MCP package argument variable metadata must be valid")
        fallbacks = {}
        input_vars = []
        templates = []
        for field_name in ("runtime_arguments", "package_arguments"):
            for argument in package.get(field_name) or []:
                if not isinstance(argument, dict):
                    continue
                template = argument.get(
                    "value",
                    argument.get("default", argument.get("value_hint", "")),
                )
                if isinstance(template, str):
                    templates.append(template)
        for variable_name, variable_info in package_variables.items():
            if not any(f"{{{variable_name}}}" in template for template in templates):
                continue
            if not variable_info.get("isSecret", variable_info.get("is_secret", False)):
                continue
            input_id = cls._vscode_argument_secret_input_id(server_name, variable_name)
            fallbacks[variable_name] = f"${{input:{input_id}}}"
            input_vars.append(
                {
                    "type": "promptString",
                    "id": input_id,
                    "description": variable_info.get(
                        "description",
                        f"{variable_name} for MCP server",
                    ),
                    "password": True,
                }
            )
        return fallbacks, input_vars

    @staticmethod
    def _has_value(value):
        """Return True when a user/config value is present and non-empty."""
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        return True

    @staticmethod
    def _existing_mapping_value(existing_server_config, section, name):
        """Return an existing server mapping value for *section[name]*, if any."""
        if not isinstance(existing_server_config, dict):
            return None
        existing_mapping = existing_server_config.get(section)
        if not isinstance(existing_mapping, dict):
            return None
        return existing_mapping.get(name)

    @staticmethod
    def _value_for_declared_vscode_env(
        env_var,
        *,
        env_overrides=None,
        existing_server_config=None,
        input_ref,
    ):
        """Return the VS Code env value for a registry-declared env var.

        Required variables keep the existing promptString behavior. Optional
        variables are omitted unless a value was collected for this install or
        the user already has an edited value in the existing config. Collected
        optional values are emitted as ``${env:NAME}``, which VS Code resolves
        from the VS Code process environment when the server starts.
        """
        name = env_var["name"]
        if registry_field_is_required(env_var):
            return input_ref

        existing_value = VSCodeClientAdapter._existing_mapping_value(
            existing_server_config, "env", name
        )
        if VSCodeClientAdapter._has_value(existing_value):
            return existing_value

        env_overrides = env_overrides or {}
        override_value = env_overrides.get(name)
        if VSCodeClientAdapter._has_value(override_value):
            return f"${{env:{name}}}"

        return None

    @staticmethod
    def _translate_env_vars_for_vscode(mapping):
        """Normalize ``${VAR}`` and ``${env:VAR}`` references to ``${env:VAR}``.

        VS Code's mcp.json natively resolves ``${env:VAR}`` from the host
        environment at server-start time. Bare ``${VAR}`` is *not* part of the
        mcp.json grammar, so VS Code would otherwise pass the literal text
        through (silently breaking auth headers, env vars, etc.).

        This translation is purely textual and idempotent:
        - ``${VAR}``      -> ``${env:VAR}``
        - ``${env:VAR}``  -> ``${env:VAR}`` (no change)
        - ``${input:X}``  -> ``${input:X}`` (no change; handled separately)
        - non-string values pass through

        A new dict is returned so callers may continue to use the original
        for input-variable extraction without ordering concerns.
        """
        if not mapping:
            return mapping
        return {
            k: (_ENV_VAR_RE.sub(r"${env:\1}", v) if isinstance(v, str) else v)
            for k, v in mapping.items()
        }

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

    def _extract_input_variables(self, mapping, server_name):
        """Scan dict values for ${input:...} references and return input variable definitions.

        Args:
            mapping (dict): Header or env dict whose values may contain
                ``${input:<id>}`` placeholders.
            server_name (str): Server name used in the description field.

        Returns:
            list[dict]: Input variable definitions (``promptString``, ``password: true``).
                Duplicates within *mapping* are already deduplicated.
        """
        seen: set = set()
        result: list = []
        for value in (mapping or {}).values():
            if not isinstance(value, str):
                continue
            for match in _INPUT_VAR_RE.finditer(value):
                var_id = match.group(1)
                if var_id in seen:
                    continue
                seen.add(var_id)
                result.append(
                    {
                        "type": "promptString",
                        "id": var_id,
                        "description": f"{var_id} for MCP server {server_name}",
                        "password": True,
                    }
                )
        return result

    @staticmethod
    def _extract_package_args(package, runtime_vars=None):
        """Keep the retired flat extractor available to private compatibility callers.

        Launcher generation must not call this method: flattening erases named
        group boundaries and cannot merge package identity safely. The canonical
        production path is ``MCPClientAdapter._build_non_container_launcher_argv``.
        Existing private callers retain the historical package-arguments-first
        and legacy ``value_hint`` behavior during the transition.
        """
        if not package:
            return []

        package_args = package.get("package_arguments") or []
        extracted = [
            argument.get("value")
            for argument in package_args
            if isinstance(argument, dict) and argument.get("value")
        ]
        if extracted:
            return extracted

        extracted = []
        for argument in package.get("runtime_arguments") or []:
            if not isinstance(argument, dict):
                continue
            if "variables" in argument and "value_hint" in argument:
                value = argument["value_hint"]
                for variable_name in argument["variables"]:
                    replacement = (runtime_vars or {}).get(variable_name)
                    if replacement is None:
                        replacement = (
                            "${workspaceFolder}"
                            if variable_name == "workspaceFolder"
                            else f"${{{variable_name}}}"
                        )
                    value = value.replace(
                        f"{{{variable_name}}}",
                        str(replacement),
                    )
                if value:
                    extracted.append(value)
            elif argument.get("value_hint") and (
                argument.get("is_required", False) or "is_required" not in argument
            ):
                extracted.append(argument["value_hint"])
        return extracted

    @classmethod
    def _docker_run_args(
        cls,
        package: dict,
        runtime_vars: dict | None = None,
        *,
        secret_variable_fallbacks: dict[str, str] | None = None,
        unresolved_variables: list[str] | None = None,
    ) -> list[str] | None:
        """Build ``docker run`` arguments from a container package's metadata.

        Container packages retain a dedicated assembler because Docker requires
        options before the image and package arguments after it. Non-container
        package identity and typed argument grouping are owned separately by
        ``MCPClientAdapter._build_non_container_launcher_argv``.

        Handles both argument spellings -- v0.1 ``value`` (beside a ``type``)
        and the legacy ``value_hint`` -- because reading only the latter
        dropped every option a v0.1 registry supplied, including mounts whose
        values APM had just collected from the user (#2377). ``named`` entries
        contribute their flag as well as its value, matching
        ``CopilotClientAdapter._process_arguments``.

        ``package_arguments`` are the container's own argv and follow the image
        per ``docker run [OPTIONS] IMAGE [ARG...]``.

        Returns ``None`` -- leaving the caller on its previous synthesized
        launcher -- when the registry arguments cannot form a usable command:
        no ``run`` verb, or a placeholder in a required entry that this
        install could not resolve. Emitting a half-substituted mount path
        would be worse than the image-only command that shipped before. An
        *optional* entry whose placeholder is unresolved is dropped on its
        own instead -- declining a whole working command over a mount the
        registry itself marked optional would throw away every required
        argument with it.

        Args:
            package (dict): A single package entry from the registry.
            runtime_vars (dict, optional): Collected runtime variable values.

        Returns:
            list[str] | None: Ordered ``docker`` arguments, or None to decline.
        """
        if not package:
            return None
        package_variables = cls._package_runtime_variable_metadata(package)
        if package_variables is None:
            if unresolved_variables is not None:
                unresolved_variables.append("invalid metadata")
            return None
        run_options = cls._docker_arg_values(
            package.get("runtime_arguments"),
            runtime_vars,
            secret_variable_fallbacks,
            package_variables,
            unresolved_variables,
        )
        if run_options is None:
            return None
        package_args = cls._docker_arg_values(
            package.get("package_arguments"),
            runtime_vars,
            secret_variable_fallbacks,
            package_variables,
            unresolved_variables,
        )
        if package_args is None:
            return None
        if not run_options and package_args and package_args[0] == "run":
            image_index = cls._docker_image_arg_index(package_args, package.get("name"))
            if image_index is None:
                run_options = package_args
                package_args = []
            else:
                run_options = package_args[: image_index + 1]
                package_args = package_args[image_index + 1 :]
        elif not run_options and package_args:
            run_options = ["run", "-i", "--rm"]
        # The subcommand has to lead: `docker -v … run` is not a run invocation,
        # and there would be nothing for the flag normalizer to anchor on.
        if not run_options or run_options[0] != "run":
            return None

        image = package.get("name")
        # Some registry data repeats the whole docker argv in package_arguments
        # instead of describing the container's own arguments. Appending that
        # after the image would hand docker a second `run …` as the container
        # command, so treat it as a duplicate of what runtime_arguments said.
        if (package_args and package_args[0] == "run") or any(
            cls._docker_image_references_match(argument, image) for argument in package_args
        ):
            package_args = []

        # Reuse the shared normalizer so a registry template that omits -i/--rm
        # still yields an interactive, self-cleaning container.
        args: list[str] = DockerArgsProcessor.process_docker_args(run_options, {})
        try:
            with_image: list[str] = cls._ensure_docker_image_arg(args, image)
        except ValueError:
            return None
        return with_image + package_args

    @classmethod
    def _docker_arg_values(
        cls,
        entries: list | None,
        runtime_vars: dict | None,
        secret_variable_fallbacks: dict[str, str] | None,
        package_variables: dict[str, dict],
        unresolved_variables: list[str] | None,
    ) -> list[str] | None:
        """Resolve one registry argument list into CLI argument strings.

        Returns ``None`` when a *required* entry carries a placeholder this
        install has no value for, so the caller can decline the whole command.
        An optional entry in the same situation is skipped by itself.

        An optional entry whose placeholder DID resolve is kept: its value was
        explicitly collected from the user, and discarding user-supplied
        configuration is the failure mode #2377 exists to prevent.
        """
        args: list[str] = []
        for arg in entries or []:
            if not isinstance(arg, dict):
                continue
            arg_type = arg.get("type")
            # v0.1 marks optional entries with ``isRequired``; only package-level
            # keys get camelCase-normalized, so accept either spelling here. An
            # explicit null means "unspecified", not "optional".
            required = arg.get("is_required", arg.get("isRequired"))
            required = True if required is None else bool(required)
            if not required and "variables" not in arg:
                continue

            # A typed entry carries its payload in ``value`` -- ``value_hint`` is
            # the schema's *display* hint for it, and emitting that renders
            # placeholder words like "env_var_name" as CLI arguments. Untyped
            # legacy entries have only the hint. This mirrors
            # ``CopilotClientAdapter._process_arguments``.
            if arg_type in ("positional", "named"):
                raw = arg.get("value", arg.get("default"))
            else:
                raw = arg.get("value_hint", arg.get("value", arg.get("default")))

            if raw is None or raw == "":
                # A named entry contributes its flag even with no value:
                # dropping ``--read-only`` silently weakens the container.
                if arg_type == "named" and arg.get("name"):
                    name = str(arg["name"])
                    if cls._docker_option_requires_value(name):
                        if required:
                            return None
                        continue
                    args.append(name)
                continue

            template = str(raw)
            substituted = cls._substitute_runtime_variables(
                template,
                package_variables,
                runtime_vars,
                {"workspaceFolder": "${workspaceFolder}"},
                secret_variable_fallbacks,
            )
            if substituted is None:
                if required:
                    if unresolved_variables is not None:
                        match = _RUNTIME_TEMPLATE_VARIABLE_RE.search(template)
                        unresolved_variables.append(match.group(1) if match else "unknown")
                    return None
                continue
            template = substituted
            if arg_type == "named" and arg.get("name"):
                args.append(str(arg["name"]))
            args.append(template)
        return args

    @staticmethod
    def _select_remote_with_url(remotes):
        """Return the first remote entry that has a non-empty URL.

        Returns:
            dict or None: The first usable remote, or None if none found.
        """
        for remote in remotes:
            url = (remote.get("url") or "").strip()
            if url:
                return remote
        return None

    def _select_best_package(self, packages):
        """Select the best package for VS Code installation from available packages.

        Prioritizes packages in order: npm, pypi, docker, then others.
        Uses ``_infer_registry_name`` so selection works even when the
        API returns an empty ``registry_name``.

        Args:
            packages (list): List of package dictionaries.

        Returns:
            dict: Best package to use, or None if no suitable package found.
        """
        priority_order = ["npm", "pypi", "docker"]

        for target in priority_order:
            for package in packages:
                if self._infer_registry_name(package) == target:
                    return package

        # Fall back to any package that has a runtime_hint
        for package in packages:
            if package.get("runtime_hint"):
                return package

        return packages[0] if packages else None
