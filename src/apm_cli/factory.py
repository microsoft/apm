"""Factory classes for creating adapters."""

from pathlib import Path

from apm_cli.harnesses.registry import get_harness_registry

from .adapters.client.base import MCPClientAdapter
from .adapters.package_manager.default_manager import DefaultMCPPackageManager


class ClientFactory:
    """Factory for creating MCP client adapters."""

    @staticmethod
    def create_client(
        client_type,
        project_root: Path | str | None = None,
        user_scope: bool = False,
    ):
        """Create a client adapter based on the specified type.

        Args:
            client_type (str): Type of client adapter to create.
            project_root: Project root used to resolve repo-local config paths.
            user_scope: Whether the adapter should use user-scope paths instead
                of project-local paths when supported.

        Returns:
            MCPClientAdapter: An instance of the specified client adapter.

        Raises:
            ValueError: If the client type is not supported.
        """
        key = client_type.lower()
        registry = get_harness_registry()
        constructor = registry.mcp_clients.get(key)
        if constructor is None:
            raise ValueError(f"Unsupported client type: {client_type}")

        client = constructor(project_root=project_root, user_scope=user_scope)
        if not isinstance(client, MCPClientAdapter):
            raise TypeError(f"Harness MCP constructor '{key}' must return MCPClientAdapter")
        if key in registry.external_mcp_clients and not callable(
            getattr(client, "remove_managed_servers", None)
        ):
            raise TypeError(
                f"Harness MCP client '{key}' must implement remove_managed_servers(names)"
            )
        return client

    @staticmethod
    def supported_clients() -> frozenset[str]:
        """Return the set of MCP client target names supported by APM.

        This is the canonical "what MCP runtimes does APM know about?"
        query.  Use this from any module that iterates over MCP-capable
        targets (e.g. cleanup loops, availability probes) instead of
        hand-maintaining a parallel list.
        """
        return frozenset(get_harness_registry().mcp_clients)


class PackageManagerFactory:
    """Factory for creating MCP package manager adapters."""

    @staticmethod
    def create_package_manager(manager_type="default"):
        """Create a package manager adapter based on the specified type.

        Args:
            manager_type (str, optional): Type of package manager adapter to create.
                Defaults to "default".

        Returns:
            MCPPackageManagerAdapter: An instance of the specified package manager adapter.

        Raises:
            ValueError: If the package manager type is not supported.
        """
        managers = {
            "default": DefaultMCPPackageManager,
            # Add more package managers as they emerge
        }

        if manager_type.lower() not in managers:
            raise ValueError(f"Unsupported package manager type: {manager_type}")

        return managers[manager_type.lower()]()
