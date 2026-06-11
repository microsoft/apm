"""VSCode implementation of MCP client adapter.

This adapter implements the VSCode-specific handling of MCP server configuration,
following the official documentation at:
https://code.visualstudio.com/docs/copilot/chat/mcp-servers
"""

import json
import re
from pathlib import Path

from ...registry.client import SimpleRegistryClient
from ...registry.integration import RegistryIntegration
from ...utils.console import _rich_warning
from .base import _ENV_VAR_RE, _INPUT_VAR_RE, MCPClientAdapter, registry_field_is_required

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
            return server_config, input_vars

        # Check for packages information
        if server_info.get("packages"):
            package = self._select_best_package(server_info["packages"])
            runtime_hint = package.get("runtime_hint", "") if package else ""
            registry_name = self._infer_registry_name(package) if package else ""
            pkg_args = (
                self._extract_package_args(package, runtime_vars=runtime_vars) if package else []
            )

            # Handle npm packages
            if runtime_hint == "npx" or registry_name == "npm":
                package_name = package.get("name")
                # Filter out package name from extracted args to avoid duplication
                # (legacy runtime_arguments often include it as the first entry)
                extra_args = [a for a in pkg_args if a != package_name] if pkg_args else []

                server_config = {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", package_name] + extra_args,  # noqa: RUF005
                }

            # Handle docker packages
            elif runtime_hint == "docker" or registry_name == "docker":
                args = pkg_args if pkg_args else ["run", "-i", "--rm", package.get("name")]

                server_config = {"type": "stdio", "command": "docker", "args": args}

            # Handle Python packages
            elif (
                runtime_hint in ["uvx", "pip", "python"]
                or "python" in runtime_hint
                or registry_name == "pypi"
            ):
                # Determine the command based on runtime_hint
                if runtime_hint == "uvx":
                    command = "uvx"
                elif "python" in runtime_hint:
                    command = "python3" if runtime_hint in ["python", "pip"] else runtime_hint
                else:
                    command = "uvx"

                if pkg_args:
                    args = pkg_args
                elif runtime_hint == "uvx" or command == "uvx":
                    args = [package.get("name", "")]
                else:
                    module_name = (
                        package.get("name", "").replace("mcp-server-", "").replace("-", "_")
                    )
                    args = ["-m", f"mcp_server_{module_name}"]

                server_config = {"type": "stdio", "command": command, "args": args}

            # Generic fallback for packages with a runtime_hint (e.g. dotnet, nuget, mcpb)
            elif package and runtime_hint:
                args = pkg_args if pkg_args else [package.get("name", "")]

                server_config = {"type": "stdio", "command": runtime_hint, "args": args}

            # Add environment variables if present. Optional registry env vars
            # are emitted only when the user already has a value in config or
            # install-time collection found one; otherwise they would create an
            # unwanted VS Code prompt on every server start.
            env_vars = (
                package.get("environment_variables") or package.get("environmentVariables") or []
            )
            if env_vars:
                env_config = {}
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
                                "description": env_var.get("description", f"{name} for MCP server"),
                                "password": True,
                            }
                        )
                    else:
                        input_vars.extend(
                            self._extract_input_variables(
                                {name: env_value}, server_info.get("name", "")
                            )
                        )
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

        return server_config, input_vars

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
        """Extract positional arguments from a package entry.

        The MCP registry API uses ``package_arguments`` (with ``type``/``value``
        pairs).  Older or synthetic entries may use ``runtime_arguments``
        (with ``is_required``/``value_hint``).  v0.1 registry format uses
        ``runtime_arguments`` where a ``variables`` dict is a *sibling* of
        ``value_hint``; the ``value_hint`` string contains ``{var_name}``
        placeholders that are substituted at config-generation time.
        This method normalises all formats into a flat list of argument strings.

        Args:
            package (dict): A single package entry.
            runtime_vars (dict, optional): Runtime variable substitutions.
                When a ``{var_name}`` placeholder is encountered in a v0.1
                ``value_hint``, the corresponding value from ``runtime_vars``
                is used.  For VS Code, ``workspaceFolder`` defaults to the
                native ``${workspaceFolder}`` interpolation token when not
                supplied via ``runtime_vars``.

        Returns:
            list[str]: Ordered argument strings, may be empty.
        """
        if not package:
            return []

        # Prefer package_arguments (current API format)
        pkg_args = package.get("package_arguments") or []
        if pkg_args:
            args = []
            for arg in pkg_args:
                if isinstance(arg, dict):
                    value = arg.get("value", "")
                    if value:
                        args.append(value)
            if args:
                return args

        # Fall back to runtime_arguments (legacy / synthetic format and v0.1 variables shape)
        rt_args = package.get("runtime_arguments") or []
        if rt_args:
            args = []
            for arg in rt_args:
                if isinstance(arg, dict):
                    if "variables" in arg and "value_hint" in arg:
                        # v0.1 format: variables is a sibling of value_hint; the
                        # value_hint string contains {var_name} placeholders.
                        value = arg["value_hint"]
                        for var_name in arg["variables"]:
                            if runtime_vars and var_name in runtime_vars:
                                replacement = runtime_vars[var_name]
                            elif var_name == "workspaceFolder":
                                # VS Code native variable substitution token
                                replacement = "${workspaceFolder}"
                            else:
                                replacement = f"${{{var_name}}}"
                            value = value.replace(f"{{{var_name}}}", replacement)
                        if value:
                            args.append(value)
                    elif arg.get("is_required", False) and arg.get("value_hint"):
                        # Legacy format: explicit is_required=True entries
                        args.append(arg["value_hint"])
                    elif "value_hint" in arg and arg["value_hint"] and "is_required" not in arg:
                        # v0.1 format: plain value_hint entries without is_required
                        args.append(arg["value_hint"])
            if args:
                return args

        return []

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
