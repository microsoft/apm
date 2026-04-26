"""Base adapter interface for MCP clients."""

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path

_INPUT_VAR_RE = re.compile(r"\$\{input:([^}]+)\}")

# Matches ${VAR} and ${env:VAR}, capturing VAR. Intentionally does NOT match
# ${input:VAR} (the optional ``env:`` group cannot also satisfy ``input:``),
# nor GitHub Actions ``${{ ... }}`` templates (the second ``{`` fails the
# identifier class). This keeps env-var handling fully disjoint from input
# variable handling, so existing _INPUT_VAR_RE call sites are unaffected.
_ENV_VAR_RE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")


class MCPClientAdapter(ABC):
    """Base adapter for MCP clients."""

    # Whether this adapter's config path is user/global-scoped (e.g.
    # ``~/.copilot/``) rather than workspace-scoped (e.g. ``.vscode/``).
    # Adapters that target a global path should override this to ``True``
    # so that ``apm install --global`` can install MCP servers to them.
    supports_user_scope: bool = False

    def __init__(
        self,
        project_root: Path | str | None = None,
        user_scope: bool = False,
    ):
        """Initialize the adapter with optional scope-aware path context.

        Args:
            project_root: Project root used to resolve project-local config paths.
                When not provided, adapters fall back to the current working
                directory for project-scoped paths.
            user_scope: Whether the adapter should resolve user-scope config
                paths instead of project-local paths when supported.
        """
        self._project_root = Path(project_root) if project_root is not None else None
        self.user_scope = user_scope

    @property
    def project_root(self) -> Path:
        """Return the explicit project root or the current working directory."""
        if self._project_root is not None:
            return self._project_root
        return Path(os.getcwd())

    @abstractmethod
    def get_config_path(self):
        """Get the path to the MCP configuration file."""
        pass

    @abstractmethod
    def update_config(self, config_updates) -> bool | None:
        """Update the MCP configuration.

        Returns ``False`` or ``None`` when the config write was skipped
        (for example because the existing file could not be parsed safely).
        """
        pass

    @abstractmethod
    def get_current_config(self):
        """Get the current MCP configuration."""
        pass

    @abstractmethod
    def configure_mcp_server(
        self,
        server_url,
        server_name=None,
        enabled=True,
        env_overrides=None,
        server_info_cache=None,
        runtime_vars=None,
    ):
        """Configure an MCP server in the client configuration.

        Args:
            server_url (str): URL of the MCP server.
            server_name (str, optional): Name of the server. Defaults to None.
            enabled (bool, optional): Whether to enable the server. Defaults to True.
            env_overrides (dict, optional): Environment variable overrides. Defaults to None.
            server_info_cache (dict, optional): Pre-fetched server info to avoid duplicate registry calls.
            runtime_vars (dict, optional): Runtime variable values. Defaults to None.

        Returns:
            bool: True if successful, False otherwise.
        """
        pass

    @staticmethod
    def _infer_registry_name(package):
        """Infer the registry type from package metadata.

        The MCP registry API often returns empty ``registry_name``.  This
        method derives the registry from explicit fields first, then falls
        back to heuristics on the package name.

        Args:
            package (dict): A single package entry from the registry.

        Returns:
            str: Inferred registry name (e.g. "npm", "pypi", "docker") or "".
        """
        if not package:
            return ""

        explicit = package.get("registry_name", "")
        if explicit:
            return explicit

        name = package.get("name", "")
        runtime_hint = package.get("runtime_hint", "")

        # Infer from runtime_hint
        if runtime_hint in ("npx", "npm"):
            return "npm"
        if runtime_hint in ("uvx", "pip", "pipx"):
            return "pypi"
        if runtime_hint == "docker":
            return "docker"
        if runtime_hint in ("dotnet", "dnx"):
            return "nuget"

        # Infer from package name patterns
        if name.startswith("@") and "/" in name:
            return "npm"  # scoped npm package, e.g. @azure/mcp
        if name.startswith(("ghcr.io/", "mcr.microsoft.com/", "docker.io/")):
            return "docker"
        if name.startswith("https://") and name.endswith(".mcpb"):
            return "mcpb"
        # PascalCase with dots usually means nuget (e.g. Azure.Mcp)
        if "." in name and not name.startswith("http") and name[0].isupper():
            return "nuget"

        return ""

    @staticmethod
    def _warn_input_variables(mapping, server_name, runtime_label):
        """Emit a warning for each ``${input:...}`` reference found in *mapping*.

        Runtimes that do not support VS Code-style input prompts (Copilot CLI,
        Codex CLI, etc.) should call this so users know their placeholders
        will not be resolved at runtime.

        Args:
            mapping (dict): Header or env dict to scan.
            server_name (str): Server name for the warning message.
            runtime_label (str): Human-readable runtime name (e.g. "Copilot CLI").
        """
        if not mapping:
            return
        seen: set = set()
        for value in mapping.values():
            if not isinstance(value, str):
                continue
            for match in _INPUT_VAR_RE.finditer(value):
                var_id = match.group(1)
                if var_id in seen:
                    continue
                seen.add(var_id)
                print(
                    f"[!]  Warning: ${{input:{var_id}}} in server "
                    f"'{server_name}' will not be resolved -- "
                    f"{runtime_label} does not support input variable prompts"
                )

    def normalize_project_arg(self, value):
        """Normalize workspace placeholders for project-local runtimes."""
        if (
            not self.user_scope
            and isinstance(value, str)
            and value in {"${workspaceFolder}", "${projectRoot}", "${workspaceRoot}"}
        ):
            return "."
        return value
