"""Factory classes for creating adapters."""

from pathlib import Path

from .adapters.client.antigravity import AntigravityClientAdapter
from .adapters.client.claude import ClaudeClientAdapter
from .adapters.client.codex import CodexClientAdapter
from .adapters.client.copilot import CopilotClientAdapter
from .adapters.client.copilot_project import CopilotProjectClientAdapter
from .adapters.client.cursor import CursorClientAdapter
from .adapters.client.gemini import GeminiClientAdapter
from .adapters.client.hermes import HermesClientAdapter
from .adapters.client.intellij import IntelliJClientAdapter
from .adapters.client.kiro import KiroClientAdapter
from .adapters.client.opencode import OpenCodeClientAdapter
from .adapters.client.vscode import VSCodeClientAdapter
from .adapters.client.windsurf import WindsurfClientAdapter
from .adapters.package_manager.default_manager import DefaultMCPPackageManager

# Single source of truth for MCP client adapter registration.  Adding a
# new MCP-capable target means a single dict entry here -- callers that
# need "every MCP runtime APM supports" should iterate
# ``ClientFactory.supported_clients()`` rather than maintain parallel
# lists (see ``mcp_integrator`` runtime loops for the canonical
# consumers).
# Runtimes whose project MCP destination is workspace-root ``.mcp.json``
# (ClaudeClientAdapter._project_mcp_path, CopilotProjectClientAdapter).
_ROOT_MCP_JSON_CLIENTS = frozenset({"claude", "copilot"})

_MCP_CLIENT_REGISTRY: dict[str, type] = {
    "antigravity": AntigravityClientAdapter,
    "copilot": CopilotClientAdapter,
    "vscode": VSCodeClientAdapter,
    "codex": CodexClientAdapter,
    "cursor": CursorClientAdapter,
    "gemini": GeminiClientAdapter,
    "intellij": IntelliJClientAdapter,
    "kiro": KiroClientAdapter,
    "opencode": OpenCodeClientAdapter,
    "windsurf": WindsurfClientAdapter,
    "claude": ClaudeClientAdapter,
    "hermes": HermesClientAdapter,
}


class ClientFactory:
    """Factory for creating MCP client adapters."""

    @staticmethod
    def adapter_class(client_type: str) -> type | None:
        """Return the adapter class registered for *client_type*, or None if unsupported."""
        return _MCP_CLIENT_REGISTRY.get(client_type.lower())

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
        if key not in _MCP_CLIENT_REGISTRY:
            raise ValueError(f"Unsupported client type: {client_type}")
        if key == "copilot" and not user_scope:
            # Project MCP destination owner: workspace-root .mcp.json (#3866).
            return CopilotProjectClientAdapter(project_root=project_root)

        return _MCP_CLIENT_REGISTRY[key](
            project_root=project_root,
            user_scope=user_scope,
        )

    @staticmethod
    def project_destination_is_root_mcp_json(client_type: str) -> bool | None:
        """Whether *client_type* writes project MCP servers to workspace-root ``.mcp.json``.

        Read-only: no adapter is built and nothing is created on disk. ``None``
        for a runtime APM does not know, so callers can choose a conservative
        answer. Claude Code and the project ``copilot`` adapter share the root
        file; every other runtime keeps its own native config.
        """
        key = client_type.lower()
        if key not in _MCP_CLIENT_REGISTRY:
            return None
        return key in _ROOT_MCP_JSON_CLIENTS

    @staticmethod
    def user_wide_config_display(client_type: str) -> str | None:
        """User-wide config a project install of *client_type* writes, if any.

        Read-only: no adapter is built. ``None`` when the runtime keeps a
        workspace config or APM does not know it.
        """
        adapter = _MCP_CLIENT_REGISTRY.get(client_type.lower())
        return getattr(adapter, "user_wide_config_display", None)

    @staticmethod
    def supported_clients() -> frozenset[str]:
        """Return the set of MCP client target names supported by APM.

        This is the canonical "what MCP runtimes does APM know about?"
        query.  Use this from any module that iterates over MCP-capable
        targets (e.g. cleanup loops, availability probes) instead of
        hand-maintaining a parallel list.
        """
        return frozenset(_MCP_CLIENT_REGISTRY.keys())


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
