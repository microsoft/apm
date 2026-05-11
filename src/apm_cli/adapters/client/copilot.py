"""GitHub Copilot CLI implementation of MCP client adapter.

This adapter implements the Copilot CLI-specific handling of MCP server configuration,
targeting the global ~/.copilot/mcp-config.json file as specified in the MCP installation
architecture specification.
"""

import json
import os
import re
from pathlib import Path
from typing import ClassVar

import click

from ...core.docker_args import DockerArgsProcessor
from ...core.token_manager import GitHubTokenManager
from ...registry.client import SimpleRegistryClient
from ...registry.integration import RegistryIntegration
from ...utils.console import _rich_warning
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

# Detects the legacy ``<VAR>`` placeholder syntax. Used both for translation
# and for emitting an aggregated deprecation warning, mirroring the analogous
# pattern in ``vscode.py``.
_LEGACY_ANGLE_VAR_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")


def _translate_env_placeholder(value):
    """Pure-textual translation of env-var placeholders to Copilot CLI's
    native runtime substitution syntax (``${VAR}``).

    This is the security-critical helper for issue #1152: it MUST NOT read
    ``os.environ`` and MUST NOT resolve placeholders to their literal values.
    Copilot CLI resolves ``${VAR}`` from the host environment at server-start
    time, so APM emits placeholders verbatim rather than baking secrets into
    ``~/.copilot/mcp-config.json``.

    Translations:
        ``${env:VAR}``     -> ``${VAR}``     (strip ``env:`` prefix)
        ``${VAR}``         -> ``${VAR}``     (no-op)
        ``<VAR>``          -> ``${VAR}``     (legacy syntax migration)
        ``${VAR:-default}``-> passthrough    (regex doesn't match)
        ``$VAR`` (bare)    -> passthrough    (regex doesn't match)
        ``${input:foo}``   -> passthrough    (regex doesn't match)
        non-string         -> passthrough

    The translation is idempotent: applying it twice produces the same
    result as applying it once.
    """
    if not isinstance(value, str):
        return value

    def _to_brace(match):
        # group(1) = legacy <VAR>; group(2) = ${VAR} / ${env:VAR}
        var_name = match.group(1) or match.group(2)
        return "${" + var_name + "}"

    return _COPILOT_ENV_RE.sub(_to_brace, value)


def _extract_legacy_angle_vars(value):
    """Return the set of legacy ``<VAR>`` names present in *value*.

    Used to aggregate deprecation warnings across all servers in a single
    install run, so authors see one helpful list instead of one warning per
    occurrence.
    """
    if not isinstance(value, str):
        return set()
    return set(_LEGACY_ANGLE_VAR_RE.findall(value))


def _has_env_placeholder(value):
    """True if *value* is a string containing any recognised env-var
    placeholder syntax (``${VAR}``, ``${env:VAR}``, or legacy ``<VAR>``).
    Used to distinguish placeholder-sourced env values (which translate)
    from hardcoded literal defaults (which stay literal).
    """
    if not isinstance(value, str):
        return False
    return bool(_COPILOT_ENV_RE.search(value))


class CopilotClientAdapter(MCPClientAdapter):
    """Copilot CLI implementation of MCP client adapter.

    This adapter handles Copilot CLI-specific configuration for MCP servers using
    a global ~/.copilot/mcp-config.json file, following the JSON format for
    MCP server configuration.
    """

    supports_user_scope: bool = True
    _client_label: str = "Copilot CLI"
    target_name: str = "copilot"
    mcp_servers_key: str = "mcpServers"

    # When True, env-var placeholders (``${VAR}``, ``${env:VAR}``, legacy
    # ``<VAR>``) are translated to Copilot CLI's native runtime-substitution
    # syntax (``${VAR}``) and emitted into mcp-config.json verbatim. The
    # secret never touches disk.
    #
    # When False, placeholders are resolved at install time against the host
    # environment and the literal value is baked into the config file
    # (legacy pre-#1152 behaviour).
    #
    # Subclasses (Cursor / Windsurf / OpenCode / Claude / Gemini) override
    # this to ``False`` until their respective config formats are individually
    # audited for runtime-substitution support. Critically, Claude Desktop's
    # config format does NOT support runtime substitution -- it MUST keep
    # resolving at install time.
    _supports_runtime_env_substitution: bool = True

    # Process-wide aggregation of legacy ``<VAR>`` offenders, keyed by
    # adapter class so subclasses (Cursor, etc.) maintain their own
    # buckets. Populated by ``configure_mcp_server`` and drained by the
    # post-install summary helper. Class-level so cross-server warnings
    # work even when a fresh adapter instance is created per dep.
    _legacy_angle_offenders_by_server: ClassVar[dict] = {}
    # Process-wide aggregation of env-var keys whose values were previously
    # baked as plaintext literals on disk and have just been rewritten to
    # ``${KEY}`` placeholders. Drives the security-improvement notice.
    _security_upgraded_keys: ClassVar[set] = set()
    # Process-wide aggregation of env-var names referenced by configs that
    # are NOT exported in the current shell. Drives the post-install
    # actionable warning that lists vars the user must export before
    # launching ``gh copilot``.
    _unset_env_keys_by_server: ClassVar[dict] = {}
    # Guard so the post-install summary is emitted at most once per CLI
    # invocation, regardless of how many ``configure_mcp_server`` calls
    # contributed to the aggregation buckets.
    _install_run_summary_emitted: ClassVar[bool] = False

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
        # Per-server tracking of placeholder-sourced env-var keys, populated
        # during ``_format_server_config`` and consumed by the post-install
        # summary line. Keys: env-var names; never holds resolved values.
        self._last_env_placeholder_keys = set()
        # Per-server collection of legacy ``<VAR>`` offenders, populated by
        # the resolution helpers and consumed by ``configure_mcp_server`` to
        # feed the aggregated deprecation warning.
        self._last_legacy_angle_vars = set()

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

            # Reset per-server tracking before formatting (so the per-server
            # summary line and aggregated diagnostics reflect this server only).
            self._last_env_placeholder_keys = set()
            self._last_legacy_angle_vars = set()

            # Detect security upgrade: was the previous on-disk config for
            # this server holding literal (resolved) values for env keys
            # we are about to replace with ${KEY} placeholders? If so,
            # remember the affected keys for the post-install notice. We
            # snapshot BEFORE writing the new config.
            previously_baked_keys = set()
            previously_baked_headers = False
            if self._supports_runtime_env_substitution:
                previously_baked_keys, previously_baked_headers = (
                    self._collect_previously_baked_keys(server_url, server_name)
                )

            # Generate server configuration with environment and runtime variable resolution
            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)

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

            # Update configuration using the chosen key
            self.update_config({config_key: server_config})

            # Aggregate diagnostics for the post-install summary.
            if self._supports_runtime_env_substitution:
                if self._last_legacy_angle_vars:
                    self._legacy_angle_offenders_by_server[config_key] = set(
                        self._last_legacy_angle_vars
                    )
                # Only flag a security upgrade when the previously baked keys
                # actually overlap with what we are now placeholderizing -- OR
                # when the previous on-disk state had baked HTTP header
                # literals (which don't expose env-var names directly, so we
                # surface every newly-placeholderised key for this server).
                upgraded = previously_baked_keys & self._last_env_placeholder_keys
                if previously_baked_headers and self._last_env_placeholder_keys:
                    upgraded = upgraded | self._last_env_placeholder_keys
                if upgraded:
                    self._security_upgraded_keys.update(upgraded)

            # Per-server install line with env-var summary parenthetical.
            self._emit_install_summary(config_key, server_config)
            return True

        except Exception as e:
            print(f"Error configuring MCP server: {e}")
            return False

    def _collect_previously_baked_keys(self, server_url, server_name):
        """Return ``(env_keys, headers_were_baked)`` for the existing on-disk
        entry: the set of env-block keys whose values are literal
        (non-placeholder) strings, and a flag indicating whether the headers
        block contained any literal values. Together these drive the
        security-improvement notice. Headers don't expose env-var names
        directly, so the caller unions current-write placeholder keys when
        ``headers_were_baked`` is True.
        """
        try:
            current = self.get_current_config()
        except Exception:
            return set(), False
        servers = current.get("mcpServers") or {}
        # Match the same key resolution rule used below.
        if server_name:
            key = server_name
        elif "/" in server_url:
            key = server_url.split("/")[-1]
        else:
            key = server_url
        existing = servers.get(key)
        if not isinstance(existing, dict):
            return set(), False
        baked_env_keys = set()
        env_block = existing.get("env") or {}
        if isinstance(env_block, dict):
            for k, v in env_block.items():
                if isinstance(v, str) and v.strip() and not _has_env_placeholder(v):
                    baked_env_keys.add(k)
        headers_were_baked = False
        headers_block = existing.get("headers") or {}
        if isinstance(headers_block, dict):
            for v in headers_block.values():
                if isinstance(v, str) and v.strip() and not _has_env_placeholder(v):
                    headers_were_baked = True
                    break
        return baked_env_keys, headers_were_baked

    def _emit_install_summary(self, config_key, server_config):
        """Record env-var references for the post-install aggregated
        summary. No per-server line is emitted here; the integrator's
        tree (``|  +  {name} -> Copilot (configured)``) is the success
        signal. The summary references env-var names only -- never their
        values.
        """
        if not self._supports_runtime_env_substitution:
            return
        keys = set(self._last_env_placeholder_keys)
        if isinstance(server_config, dict):
            for block_key in ("env", "headers"):
                block = server_config.get(block_key)
                if not isinstance(block, dict):
                    continue
                for value in block.values():
                    if isinstance(value, str):
                        for match in _ENV_VAR_RE.finditer(value):
                            keys.add(match.group(1))
        unset = sorted(name for name in keys if not os.environ.get(name))
        if unset:
            self.__class__._unset_env_keys_by_server.setdefault(config_key, []).extend(
                u
                for u in unset
                if u not in self.__class__._unset_env_keys_by_server.get(config_key, [])
            )

    @classmethod
    def emit_install_run_summary(cls):
        """Emit aggregated cross-server diagnostics at the end of an install
        run. Idempotent: subsequent calls within the same process are no-ops.

        Three diagnostics are emitted (when applicable):

        1. Security improvement notice -- when the install rewrote
           previously baked literal env values to runtime placeholders.
           Emitted as a warning because it is an action item (the user
           must export the affected vars).
        2. Aggregated unset-env warning -- when one or more configured
           servers reference env vars that are not currently exported.
           Includes a copy-pasteable ``export`` hint.
        3. Aggregated legacy ``<VAR>`` deprecation warning -- one line
           naming all affected servers, mirroring the established VS Code
           adapter pattern.

        State is drained after emission so a subsequent install run in
        the same process (e.g. tests) starts clean.
        """
        if cls._install_run_summary_emitted:
            return

        # Visual separator from the install tree's closing line so the
        # post-tree summary block reads as a distinct section.
        emitted_any = False

        def _emit_separator_once():
            nonlocal emitted_any
            if not emitted_any:
                click.echo("")
                emitted_any = True

        if cls._security_upgraded_keys:
            visible = sorted(cls._security_upgraded_keys)
            count = len(visible)
            noun = "variable" if count == 1 else "variables"
            affected = ", ".join(visible)
            _emit_separator_once()
            _rich_warning(
                f"Security improvement: {count} environment {noun} previously stored as "
                f"plaintext in the Copilot config are now resolved at runtime.\n"
                f"    Affected: {affected}\n"
                f"    Ensure these are exported in your shell before running 'gh copilot'",
                symbol="warning",
            )
        if cls._unset_env_keys_by_server:
            all_unset: set[str] = set()
            for names in cls._unset_env_keys_by_server.values():
                all_unset.update(names)
            sorted_unset = sorted(all_unset)
            export_hint = " ".join(f"{name}=..." for name in sorted_unset)
            count = len(sorted_unset)
            noun = "variable" if count == 1 else "variables"
            _emit_separator_once()
            _rich_warning(
                f"Copilot CLI will resolve {count} environment {noun} at runtime "
                f"that {'is' if count == 1 else 'are'} not currently set: "
                f"{', '.join(sorted_unset)}.\n"
                f"    Export {'it' if count == 1 else 'them'} in your shell before "
                f"running 'gh copilot', e.g.:\n"
                f"      export {export_hint}",
                symbol="warning",
            )
        # Deprecation notice is informational housekeeping (not a runtime
        # blocker), but it ships unguarded for now so legacy <VAR> usage
        # remains visible until the v1.0 removal. If --quiet gating is
        # added in future, the unset-env and security warnings above must
        # remain unsuppressible because they describe action-required state.
        if cls._legacy_angle_offenders_by_server:
            servers = sorted(cls._legacy_angle_offenders_by_server.keys())
            count = len(servers)
            noun = "server" if count == 1 else "servers"
            _emit_separator_once()
            _rich_warning(
                f"Deprecated: <VAR> placeholder syntax used in {count} {noun} "
                f"({', '.join(servers)}). Migrate to ${{VAR}} in apm.yml. "
                f"<VAR> support will be removed in v1.0.",
                symbol="warning",
            )
        cls._install_run_summary_emitted = True

    @classmethod
    def reset_install_run_state(cls):
        """Reset the process-wide aggregation buckets. Intended for tests
        and for explicitly starting a new install run within the same
        process."""
        cls._legacy_angle_offenders_by_server = {}
        cls._security_upgraded_keys = set()
        cls._unset_env_keys_by_server = {}
        cls._install_run_summary_emitted = False

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
        """Resolve (or translate) declared environment variables.

        Behaviour depends on ``self._supports_runtime_env_substitution``:

        - True (Copilot CLI default): each declared env var ``NAME`` gets a
          ``${NAME}`` placeholder that Copilot CLI resolves at server-start
          from the host environment. Hardcoded literal defaults
          (``GITHUB_TOOLSETS``, ``GITHUB_DYNAMIC_TOOLSETS``) stay literal
          because they are not secrets and provide essential server
          configuration. The host environment is NOT read; secrets never
          touch disk. See issue #1152 for context.

        - False (legacy / sibling-adapter behaviour): resolve each variable
          to its literal value via ``env_overrides`` -> ``os.environ`` ->
          optional interactive prompt, baking the result into the config.

        Args:
            env_vars (list): List of environment variable definitions from
                server info (each item is ``{name, description, required}``).
            env_overrides (dict, optional): Pre-collected environment
                variable overrides. Ignored in translate mode.

        Returns:
            dict: ``{name: value}`` -- placeholder string in translate mode,
            literal value in legacy mode.
        """
        # Hardcoded literal defaults that supply essential server behaviour
        # rather than secrets. These stay literal in translate mode so that
        # tool-selection still works without a user export step.
        default_github_env = {"GITHUB_TOOLSETS": "context", "GITHUB_DYNAMIC_TOOLSETS": "1"}

        # Self-defined stdio deps pass ``env`` as a plain dict
        # ({NAME: value-or-placeholder}); registry-sourced deps pass a list
        # of {name, description, required} dicts. Translate-mode handling
        # for the dict shape: each value is either already a placeholder
        # (translate it to the canonical ${VAR} form) or a literal (record
        # the key as a placeholder reference and emit ${NAME} so the
        # value never lands on disk). See issue #1152.
        if isinstance(env_vars, dict) and self._supports_runtime_env_substitution:
            translated = {}
            placeholder_keys = []
            for name, raw_value in env_vars.items():
                if not name:
                    continue
                if not isinstance(raw_value, str):
                    translated[name] = raw_value
                    continue
                if _has_env_placeholder(raw_value):
                    self._last_legacy_angle_vars.update(_extract_legacy_angle_vars(raw_value))
                    translated[name] = _translate_env_placeholder(raw_value)
                    # Record every ${VAR} in the translated value (handles
                    # both ${env:VAR} -> ${VAR} and bare ${VAR} cases).
                    for match in _ENV_VAR_RE.finditer(translated[name]):
                        placeholder_keys.append(match.group(1))
                elif name in default_github_env and raw_value == default_github_env[name]:
                    translated[name] = raw_value
                else:
                    # Literal value present in apm.yml -- replace with a
                    # runtime placeholder so the secret never touches disk.
                    translated[name] = "${" + name + "}"
                    placeholder_keys.append(name)
            self._last_env_placeholder_keys = set(placeholder_keys)
            return translated

        if self._supports_runtime_env_substitution:
            resolved = {}
            placeholder_keys = []
            for env_var in env_vars:
                if not isinstance(env_var, dict):
                    continue
                name = env_var.get("name", "")
                if not name:
                    continue
                if name in default_github_env:
                    # Non-secret literal default -- preserve as-is.
                    resolved[name] = default_github_env[name]
                else:
                    # Emit a runtime-substitution placeholder; Copilot CLI
                    # resolves ``${NAME}`` from the host environment at
                    # server-start. APM never reads or stores the value.
                    resolved[name] = "${" + name + "}"
                    placeholder_keys.append(name)
            # Record for the post-install summary line and the
            # security-improvement notice.
            self._last_env_placeholder_keys = set(placeholder_keys)
            return resolved

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
        """Resolve (or translate) a single environment variable value.

        Behaviour depends on ``self._supports_runtime_env_substitution``:

        - True (Copilot CLI default): translate placeholders to Copilot CLI's
          native runtime substitution syntax (``${VAR}``). The host
          environment is NOT read; the secret never touches disk. See issue
          #1152 for context. Legacy ``<VAR>`` offenders are tracked for the
          aggregated deprecation warning emitted by
          ``configure_mcp_server``.

        - False (legacy / sibling-adapter behaviour): resolve placeholders
          to literal values via ``env_overrides`` -> ``os.environ`` ->
          optional interactive prompt, baking the result into the config.

        Args:
            name (str): Environment variable name.
            value (str): Environment variable value or placeholder.
            env_overrides (dict, optional): Pre-collected environment
                variable overrides. Ignored in translate mode.

        Returns:
            str: Translated placeholder (translate mode) or resolved
            literal value (legacy mode).
        """
        if self._supports_runtime_env_substitution:
            # Track legacy <VAR> offenders for the aggregated deprecation
            # warning. Translation itself is a pure-textual rewrite.
            self._last_legacy_angle_vars.update(_extract_legacy_angle_vars(value))
            # Track env-var names referenced via this header/value so the
            # security-upgrade detector and per-server summary can see
            # them (the env-block path tracks via _resolve_environment_variables).
            for match in _ENV_VAR_RE.finditer(value):
                self._last_env_placeholder_keys.add(match.group(1))
            return _translate_env_placeholder(value)

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
        """Resolve runtime template variables and translate or resolve env-var
        placeholders in argument strings.

        Behaviour depends on ``self._supports_runtime_env_substitution``:

        - True (Copilot CLI default): env-var placeholders (``<VAR>``,
          ``${VAR}``, ``${env:VAR}``) are translated to ``${VAR}`` for
          runtime substitution by Copilot CLI. APM template variables
          (``{runtime_var}``) are still resolved at install time because
          they are an APM-internal concept Copilot cannot interpret.

        - False (legacy / sibling-adapter behaviour): legacy ``<VAR>``
          placeholders are resolved against ``resolved_env`` (the dict of
          literal env-var values), and ``{runtime_var}`` against
          ``runtime_vars``. Newer ``${VAR}`` / ``${env:VAR}`` syntaxes are
          left as-is for backward compatibility.

        Args:
            value (str): Value that may contain placeholders.
            resolved_env (dict): Dictionary of resolved env vars (legacy
                mode) or placeholder strings (translate mode).
            runtime_vars (dict): Dictionary of resolved runtime variables.

        Returns:
            str: Processed value with placeholders translated or resolved.
        """
        import re

        if not value:
            return value

        processed = str(value)

        if self._supports_runtime_env_substitution:
            # Track legacy <VAR> offenders before translating them away.
            self._last_legacy_angle_vars.update(_extract_legacy_angle_vars(processed))
            # Translate all three env-var placeholder syntaxes to ${VAR}.
            processed = _translate_env_placeholder(processed)
        else:
            # Replace <TOKEN_NAME> with actual values from resolved_env (for Docker env vars)
            env_pattern = r"<([A-Z_][A-Z0-9_]*)>"

            def replace_env_var(match):
                env_name = match.group(1)
                return resolved_env.get(env_name, match.group(0))  # Return original if not found

            processed = re.sub(env_pattern, replace_env_var, processed)

        # Replace {runtime_var} with actual values from runtime_vars (for NPM args).
        # Negative lookbehind on `$` so we never re-substitute inside an already-translated
        # ${VAR} env placeholder (the brace is part of a Copilot CLI runtime substitution,
        # not an APM template variable).
        if runtime_vars:
            runtime_pattern = r"(?<!\$)\{([a-zA-Z_][a-zA-Z0-9_]*)\}"

            def replace_runtime_var(match):
                var_name = match.group(1)
                return runtime_vars.get(var_name, match.group(0))

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

        Uses proper URL parsing and hostname validation to prevent token
        injection via poisoned registry entries. Both the server name and
        the URL hostname must match the GitHub allowlists before a GitHub
        token is injected.

        Args:
            server_name (str): Name of the MCP server.
            url (str): URL of the remote endpoint.

        Returns:
            bool: True if this is a legitimate GitHub MCP server, False otherwise.
        """
        from urllib.parse import urlparse

        github_server_names = [
            "github-mcp-server",
            "github",
            "github-mcp",
            "github-copilot-mcp-server",
        ]

        def _is_github_mcp_hostname(hostname: str) -> bool:
            """Check if *hostname* belongs to GitHub (cloud, enterprise, or Copilot API)."""
            if is_github_hostname(hostname):
                return True
            h = hostname.lower()
            # Subdomains of github.com (e.g. api.github.com)
            if h.endswith(".github.com"):
                return True
            # Copilot API hosts (e.g. api.githubcopilot.com, api.business.githubcopilot.com)
            return h == "githubcopilot.com" or h.endswith(".githubcopilot.com")

        name_matches = bool(
            server_name and server_name.lower() in [n.lower() for n in github_server_names]
        )

        # Parse and validate hostname from URL
        hostname = None
        if url:
            try:
                parsed_url = urlparse(url)
                # Reject non-HTTPS URLs to prevent cleartext token leakage
                if parsed_url.scheme and parsed_url.scheme.lower() != "https":
                    return False
                hostname = parsed_url.hostname
            except Exception:
                return False

        host_matches = bool(hostname and _is_github_mcp_hostname(hostname))

        return name_matches and host_matches
