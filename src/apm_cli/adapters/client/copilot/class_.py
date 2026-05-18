# pylint: disable=duplicate-code
"""GitHub Copilot CLI implementation of MCP client adapter.

This adapter implements the Copilot CLI-specific handling of MCP server configuration,
targeting the global ~/.copilot/mcp-config.json file as specified in the MCP installation
architecture specification.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import ClassVar

from ..base import _ENV_VAR_RE, MCPClientAdapter, McpServerRequest

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


def _stringify_env_literal(value):
    """Return MCP env literal values in the manifest ``map<string, string>`` shape."""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


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
        copilot_package = sys.modules[__package__]
        self.registry_client = copilot_package.SimpleRegistryClient(registry_url)
        self.registry_integration = copilot_package.RegistryIntegration(registry_url)
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
        request: McpServerRequest | None = None,
    ):
        """Configure an MCP server in Copilot CLI configuration.

        Args:
            server_url (str): URL or identifier of the MCP server.
            request: Optional McpServerRequest with server_name, env_overrides,
                server_info_cache, and runtime_vars.

        Returns:
            bool: True if successful, False otherwise.
        """
        if not server_url:
            print("Error: server_url cannot be empty")
            return False

        req = request or McpServerRequest()
        server_name = req.server_name
        env_overrides = req.env_overrides
        server_info_cache = req.server_info_cache
        runtime_vars = req.runtime_vars

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
            # Extract name from server_url (part after last slash)
            # For URLs like "microsoft/azure-devops-mcp" -> "azure-devops-mcp"
            # For URLs like "github/github-mcp-server" -> "github-mcp-server"
            elif "/" in server_url:
                config_key = server_url.split("/")[-1]
            else:
                # Fallback to full server_url if no slash
                config_key = server_url

            # Update configuration using the chosen key
            self.update_config({config_key: server_config})

            # Aggregate diagnostics for the post-install summary.
            _apply_security_upgrade(
                self, config_key, previously_baked_keys, previously_baked_headers
            )

            # Per-server install line with env-var summary parenthetical.
            self._emit_install_summary(config_key, server_config)
            return True

        except Exception as e:
            print(f"Error configuring MCP server: {e}")
            return False

    def _collect_previously_baked_keys(self, server_url, server_name):
        return _summary_emit._collect_previously_baked_keys(self, server_url, server_name)

    def _emit_install_summary(self, config_key, server_config):
        return _summary_emit._emit_install_summary(self, config_key, server_config)

    @classmethod
    def emit_install_run_summary(cls):
        return _summary_emit.emit_install_run_summary(cls)

    @classmethod
    def reset_install_run_state(cls):
        return _summary_emit.reset_install_run_state(cls)

    def _format_server_config(self, server_info, env_overrides=None, runtime_vars=None):
        return _format_config._format_server_config(self, server_info, env_overrides, runtime_vars)

    def _resolve_environment_variables(self, env_vars, env_overrides=None):
        return _env_resolve._resolve_environment_variables(self, env_vars, env_overrides)

    def _resolve_env_variable(self, name, value, env_overrides=None):
        return _env_resolve._resolve_env_variable(self, name, value, env_overrides)

    def _inject_env_vars_into_docker_args(self, docker_args, env_vars):
        return _docker_args._inject_env_vars_into_docker_args(self, docker_args, env_vars)

    def _inject_docker_env_vars(self, args, env_vars):
        return _docker_args._inject_docker_env_vars(self, args, env_vars)

    def _process_arguments(self, arguments, resolved_env=None, runtime_vars=None):
        return _arg_processing._process_arguments(self, arguments, resolved_env, runtime_vars)

    def _resolve_variable_placeholders(self, value, resolved_env, runtime_vars):
        return _arg_processing._resolve_variable_placeholders(
            self, value, resolved_env, runtime_vars
        )

    def _resolve_env_placeholders(self, value, resolved_env):
        return _arg_processing._resolve_env_placeholders(self, value, resolved_env)

    @staticmethod
    def _select_remote_with_url(remotes):
        return _arg_processing._select_remote_with_url(remotes)

    def _select_best_package(self, packages):
        return _arg_processing._select_best_package(self, packages)

    def _is_github_server(self, server_name, url):
        return _arg_processing._is_github_server(self, server_name, url)


def _apply_security_upgrade(self, config_key, previously_baked_keys, previously_baked_headers):
    """Record security-upgrade diagnostics after writing a new server config.

    Must be called after ``update_config`` so that
    ``self._last_env_placeholder_keys`` reflects the newly written config.
    Only does work when ``self._supports_runtime_env_substitution`` is True.
    """
    if not self._supports_runtime_env_substitution:
        return
    if self._last_legacy_angle_vars:
        self._legacy_angle_offenders_by_server[config_key] = set(self._last_legacy_angle_vars)
    upgraded = previously_baked_keys & self._last_env_placeholder_keys
    if previously_baked_headers and self._last_env_placeholder_keys:
        upgraded = upgraded | self._last_env_placeholder_keys
    if upgraded:
        self._security_upgraded_keys.update(upgraded)


from . import arg_processing as _arg_processing
from . import docker_args as _docker_args
from . import env_resolve as _env_resolve
from . import format_config as _format_config
from . import summary_emit as _summary_emit
