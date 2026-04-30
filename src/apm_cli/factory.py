"""Factory classes for creating adapters."""

from pathlib import Path

from .adapters.client.codex import CodexClientAdapter
from .adapters.client.copilot import CopilotClientAdapter
from .adapters.client.cursor import CursorClientAdapter
from .adapters.client.gemini import GeminiClientAdapter
from .adapters.client.opencode import OpenCodeClientAdapter
from .adapters.client.vscode import VSCodeClientAdapter
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
        clients = {
            "copilot": CopilotClientAdapter,
            "vscode": VSCodeClientAdapter,
            "codex": CodexClientAdapter,
            "cursor": CursorClientAdapter,
            "gemini": GeminiClientAdapter,
            "opencode": OpenCodeClientAdapter,
            # Add more clients as needed
        }

        if client_type.lower() not in clients:
            raise ValueError(f"Unsupported client type: {client_type}")

        return clients[client_type.lower()](
            project_root=project_root,
            user_scope=user_scope,
        )


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
