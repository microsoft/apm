"""MCP server operations and installation logic."""

import logging
from pathlib import Path

import requests

from ._env_vars import (
    _collect_env_vars_from_servers,
    _collect_runtime_vars_from_servers,
    _do_prompt_for_environment_variables,
    _MCPServerOperations_extract_ids_from_codex_config,
    _MCPServerOperations_extract_ids_from_mcp_servers,
    _MCPServerOperations_extract_ids_from_vscode_config,
)
from .client import SimpleRegistryClient

logger = logging.getLogger(__name__)


class MCPServerOperations:
    """Handles MCP server operations like conflict detection and installation status."""

    def __init__(self, registry_url: str | None = None):
        """Initialize MCP server operations.

        Args:
            registry_url: Optional registry URL override
        """
        self.registry_client = SimpleRegistryClient(registry_url)

    def check_servers_needing_installation(
        self,
        target_runtimes: list[str],
        server_references: list[str],
        project_root: Path | str | None = None,
        user_scope: bool = False,
        max_workers: int = 4,
    ) -> list[str]:
        """Check which MCP servers actually need installation across target runtimes.

        This method checks the actual MCP configuration files to see which servers
        are already installed by comparing server IDs (UUIDs), not names.

        WS2b (#1116): per-server registry lookups run in parallel via a bounded
        ThreadPoolExecutor (uv-inspired, cap 4).

        Args:
            target_runtimes: List of target runtimes to check
            server_references: List of MCP server references (names or IDs)
            project_root: Project root used to resolve project-local client config
                paths when checking install status.
            user_scope: Whether to inspect user-scope config instead of
                project-local config for runtimes that support it.
            max_workers: Max parallel lookups (default 4).

        Returns:
            List of server references that need installation in at least one runtime
        """
        from concurrent.futures import ThreadPoolExecutor

        # Pre-load installed IDs per runtime (O(R) reads instead of O(S*R))
        installed_by_runtime: dict[str, set[str]] = {
            runtime: self._get_installed_server_ids(
                [runtime],
                project_root=project_root,
                user_scope=user_scope,
            )
            for runtime in target_runtimes
        }

        def _check_one(server_ref: str) -> tuple[str, bool]:
            """Return (server_ref, needs_install)."""
            try:
                server_info = self.registry_client.find_server_by_reference(server_ref)
                if not server_info:
                    return (server_ref, True)
                server_id = server_info.get("id")
                if not server_id:
                    return (server_ref, True)
                for runtime in target_runtimes:
                    if server_id not in installed_by_runtime[runtime]:
                        return (server_ref, True)
                return (server_ref, False)
            except Exception:
                return (server_ref, True)

        servers_needing_installation: list[str] = []
        workers = min(max_workers, len(server_references)) if server_references else 1
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mcp-check") as executor:
            for ref, needs_install in executor.map(_check_one, server_references):
                if needs_install:
                    servers_needing_installation.append(ref)

        return servers_needing_installation

    @staticmethod
    def _extract_ids_from_runtime_config(runtime: str, config: dict) -> set[str]:
        """Extract installed server IDs from a runtime's config dict.

        Each runtime uses a different config shape; this method normalises them.
        """
        if runtime in ("copilot", "claude"):
            return _MCPServerOperations_extract_ids_from_mcp_servers(config)
        if runtime == "codex":
            return _MCPServerOperations_extract_ids_from_codex_config(config)
        if runtime == "vscode":
            return _MCPServerOperations_extract_ids_from_vscode_config(config)
        return set()

    def _get_installed_server_ids(
        self,
        target_runtimes: list[str],
        project_root: Path | str | None = None,
        user_scope: bool = False,
    ) -> set[str]:
        """Get all installed server IDs across target runtimes.

        Args:
            target_runtimes: List of runtimes to check
            project_root: Project root used to resolve project-local client config
                paths while inspecting installed server IDs.
            user_scope: Whether to inspect user-scope config instead of
                project-local config for runtimes that support it.

        Returns:
            Set of server IDs that are currently installed
        """
        installed_ids: set[str] = set()

        # Import here to avoid circular imports
        try:
            from ..factory import ClientFactory
        except ImportError:
            return installed_ids

        for runtime in target_runtimes:
            try:
                client = ClientFactory.create_client(
                    runtime,
                    project_root=project_root,
                    user_scope=user_scope,
                )
                config = client.get_current_config()
                if isinstance(config, dict):
                    installed_ids.update(self._extract_ids_from_runtime_config(runtime, config))
            except Exception:  # noqa: S112
                # If we can't read a runtime's config, skip it
                continue

        return installed_ids

    def validate_servers_exist(
        self, server_references: list[str], max_workers: int = 4
    ) -> tuple[list[str], list[str]]:
        """Validate that all servers exist in the registry before attempting installation.

        This implements fail-fast validation similar to npm's behavior.
        Network errors are treated as transient -- the server is assumed valid
        so a flaky registry API does not block installation.

        WS2b (#1116): lookups run in parallel via a bounded ThreadPoolExecutor
        (uv-inspired).  Each registry HTTP call is independent; results are
        collected in submission order via ``executor.map``.

        Args:
            server_references: List of MCP server references to validate
            max_workers: Max parallel HTTP lookups (default 4).

        Returns:
            Tuple of (valid_servers, invalid_servers)
        """
        from concurrent.futures import ThreadPoolExecutor

        valid_servers: list[str] = []
        invalid_servers: list[str] = []

        def _validate_one(server_ref: str) -> tuple[str, bool]:
            """Return (server_ref, is_valid)."""
            try:
                server_info = self.registry_client.find_server_by_reference(server_ref)
                return (server_ref, server_info is not None)
            except requests.RequestException:
                if getattr(self.registry_client, "_is_custom_url", False):
                    raise RuntimeError(  # noqa: B904
                        f"Could not reach MCP registry at "
                        f"{self.registry_client.registry_url} while validating "
                        f"server '{server_ref}'. MCP_REGISTRY_URL is set -- "
                        f"verify the URL is correct and reachable."
                    )
                logger.debug(
                    "Registry lookup failed for %s, assuming valid (transient error)",
                    server_ref,
                    exc_info=True,
                )
                return (server_ref, True)

        workers = min(max_workers, len(server_references)) if server_references else 1
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mcp-validate") as executor:
            for ref, is_valid in executor.map(_validate_one, server_references):
                if is_valid:
                    valid_servers.append(ref)
                else:
                    invalid_servers.append(ref)

        return valid_servers, invalid_servers

    def batch_fetch_server_info(self, server_references: list[str]) -> dict[str, dict | None]:
        """Batch fetch server info for all servers to avoid duplicate registry calls.

        Args:
            server_references: List of MCP server references

        Returns:
            Dictionary mapping server reference to server info (or None if not found)
        """
        server_info_cache = {}

        for server_ref in server_references:
            try:
                server_info = self.registry_client.find_server_by_reference(server_ref)
                server_info_cache[server_ref] = server_info
            except Exception:
                server_info_cache[server_ref] = None

        return server_info_cache

    def collect_runtime_variables(
        self,
        server_references: list[str],
        server_info_cache: dict[str, dict | None] | None = None,
    ) -> dict[str, str]:
        """Collect runtime variables from runtime_arguments.variables fields.

        These are NOT environment variables but CLI argument placeholders that need
        to be substituted directly into the command arguments (e.g., {ado_org}).

        Args:
            server_references: List of MCP server references
            server_info_cache: Pre-fetched server info to avoid duplicate registry calls

        Returns:
            Dictionary mapping runtime variable names to their values
        """
        all_required_vars = {}

        if server_info_cache is None:
            server_info_cache = self.batch_fetch_server_info(server_references)

        _collect_runtime_vars_from_servers(server_references, server_info_cache, all_required_vars)

        if all_required_vars:
            return self._prompt_for_environment_variables(all_required_vars)

        return {}

    def collect_environment_variables(
        self,
        server_references: list[str],
        server_info_cache: dict[str, dict | None] | None = None,
    ) -> dict[str, str]:
        """Collect environment variables needed by the specified servers.

        Args:
            server_references: List of MCP server references
            server_info_cache: Pre-fetched server info to avoid duplicate registry calls

        Returns:
            Dictionary mapping environment variable names to their values
        """
        all_required_vars: dict[str, dict] = {}

        if server_info_cache is None:
            server_info_cache = self.batch_fetch_server_info(server_references)

        _collect_env_vars_from_servers(server_references, server_info_cache, all_required_vars)

        if all_required_vars:
            return self._prompt_for_environment_variables(all_required_vars)

        return {}

    def _prompt_for_environment_variables(self, required_vars: dict[str, dict]) -> dict[str, str]:
        """Prompt user for environment variables."""
        return _do_prompt_for_environment_variables(required_vars)
