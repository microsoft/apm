"""GitHub Copilot CLI implementation of MCP client adapter.

This adapter implements the Copilot CLI-specific handling of MCP server configuration,
targeting the global ~/.copilot/mcp-config.json file as specified in the MCP installation
architecture specification.
"""

import json
import os
from pathlib import Path
from typing import ClassVar

import click

from ...core.docker_args import DockerArgsProcessor
from ...core.token_manager import GitHubTokenManager
from ...registry.client import SimpleRegistryClient
from ...registry.integration import RegistryIntegration
from ...utils.console import _rich_warning
from ...utils.github_host import is_github_hostname
from ._copilot_env import _CopilotEnvMixin
from .base import (
    _ENV_VAR_RE,
    MCPClientAdapter,
    _has_env_placeholder,
)
from .base import (
    _extract_legacy_angle_vars as _extract_legacy_angle_vars,
)
from .base import (
    _stringify_env_literal as _stringify_env_literal,
)
from .base import (
    _translate_env_placeholder as _translate_env_placeholder,
)


class CopilotClientAdapter(_CopilotEnvMixin, MCPClientAdapter):
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

        # Ensure servers section exists and is a dict (guard against malformed config).
        if not isinstance(current_config.get(self.mcp_servers_key), dict):
            current_config[self.mcp_servers_key] = {}

        # Apply updates
        current_config[self.mcp_servers_key].update(config_updates)

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
            server_info = self._fetch_server_info(server_url, server_info_cache)
            if server_info is None:
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

            config_key = self._determine_config_key(server_url, server_name)

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
        servers = current.get(self.mcp_servers_key) or {}
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

            self._apply_auth_and_headers(config, remote, server_info, env_overrides)

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
            self._select_and_dispatch_best_package(config, packages, env_overrides, runtime_vars)

        # Apply tools override from MCP dependency overlay if present
        tools_override = server_info.get("_apm_tools_override")
        if tools_override:
            config["tools"] = tools_override

        return config

    def _apply_auth_and_headers(
        self, config, remote, server_info, env_overrides, runtime_label="Copilot CLI"
    ):
        """Inject GitHub token and registry-supplied headers into *config*.

        Delegates to :meth:`MCPClientAdapter._apply_auth_and_headers_impl`,
        supplying ``GitHubTokenManager`` from this module's namespace so that
        ``unittest.mock.patch("apm_cli.adapters.client.copilot.GitHubTokenManager")``
        correctly intercepts the instantiation in tests.
        """
        self._apply_auth_and_headers_impl(
            config, remote, server_info, env_overrides, runtime_label, GitHubTokenManager
        )

    def _dispatch_package_to_config(
        self,
        config,
        package_name,
        registry_name,
        runtime_hint,
        processed_runtime_args,
        processed_package_args,
        resolved_env,
    ):
        """Populate *config* with command/args/env for a single package.

        Handles npm and docker natively; delegates pypi, homebrew, and
        generic registries to :meth:`MCPClientAdapter._apply_pypi_homebrew_generic_config`.

        Args:
            config: Mutable config dict; updated in place.
            package_name: Registry package identifier.
            registry_name: Registry type (``"npm"``, ``"docker"``, ``"pypi"``, etc.).
            runtime_hint: Optional runtime override from the package entry.
            processed_runtime_args: Pre-processed runtime argument list.
            processed_package_args: Pre-processed package argument list.
            resolved_env: Resolved environment variable mapping.
        """
        if registry_name == "npm":
            config["command"] = runtime_hint or "npx"
            config["args"] = (
                ["-y", package_name] + processed_runtime_args + processed_package_args  # noqa: RUF005
            )
            if resolved_env:
                config["env"] = resolved_env
        elif registry_name == "docker":
            config["command"] = "docker"
            if processed_runtime_args:
                config["args"] = self._inject_env_vars_into_docker_args(
                    processed_runtime_args, resolved_env
                )
            else:
                config["args"] = DockerArgsProcessor.process_docker_args(
                    ["run", "-i", "--rm", package_name], resolved_env
                )
        else:
            self._apply_pypi_homebrew_generic_config(
                config,
                registry_name,
                package_name,
                runtime_hint,
                processed_runtime_args,
                processed_package_args,
                resolved_env,
            )

    def _select_and_dispatch_best_package(
        self,
        config,
        packages,
        env_overrides,
        runtime_vars,
        set_type_stdio: bool = False,
    ):
        """Select the best package from *packages*, resolve env, and populate *config*.

        Shared dispatch path used by both
        :meth:`CopilotClientAdapter._build_server_config` and
        :meth:`CursorClientAdapter._build_server_config`.

        Args:
            config: Mutable config dict; updated in place.
            packages: List of package dicts from the registry server info.
            env_overrides: Pre-collected env-var overrides (may be empty).
            runtime_vars: Runtime variable substitutions.
            set_type_stdio: When ``True``, sets ``config["type"] = "stdio"``
                before dispatching (required by the Cursor format).

        Returns:
            The selected package dict, or ``None`` if no package matched.
        """
        package = self._select_best_package(packages)
        if not package:
            return None

        registry_name = self._infer_registry_name(package)
        package_name = package.get("name", "")
        runtime_hint = package.get("runtime_hint", "")
        runtime_arguments = package.get("runtime_arguments", [])
        package_arguments = package.get("package_arguments", [])
        env_vars = package.get("environment_variables", [])

        resolved_env = self._resolve_environment_variables(env_vars, env_overrides)
        processed_runtime_args = self._process_arguments(
            runtime_arguments, resolved_env, runtime_vars
        )
        processed_package_args = self._process_arguments(
            package_arguments, resolved_env, runtime_vars
        )

        if set_type_stdio:
            config["type"] = "stdio"

        self._dispatch_package_to_config(
            config,
            package_name,
            registry_name,
            runtime_hint,
            processed_runtime_args,
            processed_package_args,
            resolved_env,
        )
        return package

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
