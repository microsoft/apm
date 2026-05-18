"""Base adapter interface for MCP clients."""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ...utils.console import _rich_error, _rich_warning

_INPUT_VAR_RE = re.compile(r"\$\{input:([^}]+)\}")

# Matches ${VAR} and ${env:VAR}, capturing VAR. Intentionally does NOT match
# ${input:VAR} (the optional ``env:`` group cannot also satisfy ``input:``),
# nor GitHub Actions ``${{ ... }}`` templates (the second ``{`` fails the
# identifier class). This keeps env-var handling fully disjoint from input
# variable handling, so existing _INPUT_VAR_RE call sites are unaffected.
_ENV_VAR_RE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class McpServerRequest:
    """Keyword arguments for :meth:`MCPClientAdapter.configure_mcp_server`.

    Grouping these into a single object keeps the method signature at two
    positional parameters (``self``, ``server_url``) and avoids PLR0913.
    """

    server_name: str | None = None
    enabled: bool = True
    env_overrides: dict | None = None
    server_info_cache: dict | None = None
    runtime_vars: dict | None = None
    logger: object | None = None


def _resolve_mcp_request(
    request: McpServerRequest | str | None,
    legacy_kwargs: dict,
) -> McpServerRequest | None:
    """Normalise the ``request`` arg from legacy positional/keyword callers.

    Extracted so subclass ``configure_mcp_server`` implementations don't pay
    the C901 complexity penalty for the compat shim.
    """
    if isinstance(request, str):
        legacy_kwargs.setdefault("server_name", request)
        request = None
    if request is None and legacy_kwargs:
        _valid = McpServerRequest.__dataclass_fields__
        request = McpServerRequest(**{k: v for k, v in legacy_kwargs.items() if k in _valid})
    return request


def _infer_from_runtime_hint(runtime_hint: str) -> str:
    """Map a runtime_hint value to a registry name, or return ``""``."""
    if runtime_hint in ("npx", "npm"):
        return "npm"
    if runtime_hint in ("uvx", "pip", "pipx"):
        return "pypi"
    if runtime_hint == "docker":
        return "docker"
    if runtime_hint in ("dotnet", "dnx"):
        return "nuget"
    return ""


def _infer_from_name(name: str) -> str:
    """Map a package name to a registry name via heuristics, or return ``""``."""
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


class MCPClientAdapter(ABC):
    """Base adapter for MCP clients."""

    # Identifier matching the corresponding ``KNOWN_TARGETS`` entry name.
    # Subclasses MUST override this so target-aware code can look up
    # per-target metadata via ``KNOWN_TARGETS[adapter.target_name]``
    # instead of sniffing class names.  The ``vscode`` adapter is the
    # only MCP-only pseudo-target (no entry in ``KNOWN_TARGETS``), so
    # downstream code that joins on this field must tolerate misses.
    target_name: str = ""

    # Top-level config key under which this adapter's MCP server entries
    # live (``"mcpServers"``, ``"mcp_servers"``, ``"servers"``, ...).
    # Subclasses MUST override this; ``MCPConflictDetector`` reads it to
    # extract existing server configs without classname dispatch.
    # The adapter is the canonical owner of its config schema, so this
    # field lives here rather than on ``TargetProfile`` (which is
    # primitive-focused) and applies uniformly to MCP-only adapters
    # (e.g. ``VSCodeClientAdapter``) that have no ``KNOWN_TARGETS`` entry.
    mcp_servers_key: str = ""

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
        request: McpServerRequest | None = None,
        **legacy_kwargs,
    ):
        """Configure an MCP server in the client configuration.

        Args:
            server_url (str): URL of the MCP server.
            request (McpServerRequest, optional): Additional configuration
                options bundled as a single object.
            **legacy_kwargs: Deprecated -- pass individual fields through ``McpServerRequest`` instead.

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

        result = _infer_from_runtime_hint(runtime_hint)
        if result:
            return result

        return _infer_from_name(name)

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
                _rich_warning(
                    f"${{input:{var_id}}} in server "
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

    # ------------------------------------------------------------------
    # Shared server-info helpers (used by all adapter subclasses)
    # ------------------------------------------------------------------

    def _fetch_server_info(self, server_url: str, server_info_cache: dict | None) -> dict | None:
        """Look up *server_url* in *server_info_cache* or fetch from registry.

        Prints a user-visible error and returns ``None`` when the server is
        not found, so callers can do a simple ``if server_info is None: return False``
        guard and the error message stays consistent across adapters.

        Args:
            server_url: Registry reference (``owner/repo`` or full URL).
            server_info_cache: Optional pre-fetched cache; ``None`` skips
                the cache lookup.

        Returns:
            Server-info dict on success; ``None`` when not found.
        """
        if server_info_cache and server_url in server_info_cache:
            return server_info_cache[server_url]
        server_info = self.registry_client.find_server_by_reference(server_url)
        if not server_info:
            _rich_error(f"Error: MCP server '{server_url}' not found in registry")
            return None
        return server_info

    @staticmethod
    def _determine_config_key(server_url: str, server_name: str) -> str:
        """Return the configuration key to use for *server_url*/*server_name*.

        The caller-supplied *server_name* takes precedence; if empty the last
        path segment of *server_url* is used as a fallback, which mirrors the
        convention ``owner/repo -> repo``.

        Args:
            server_url: Registry reference used as fallback source.
            server_name: Explicit caller-supplied name (may be empty string).

        Returns:
            Non-empty configuration key string.
        """
        if server_name:
            return server_name
        if "/" in server_url:
            return server_url.split("/")[-1]
        return server_url

    @staticmethod
    def _apply_pypi_homebrew_generic_config(
        config: dict,
        registry_name: str,
        package_name: str,
        runtime_hint: str,
        processed_runtime_args: list,
        processed_package_args: list,
        resolved_env: dict,
    ) -> None:
        """Apply pypi / homebrew / generic (uvx / brew / npx) run config to *config*.

        Mutates *config* in-place with ``command``, ``args``, and optionally
        ``env`` keys appropriate for the detected registry type.

        Args:
            config: Mutable server-config dict to populate.
            registry_name: Registry identifier (``"pypi"``, ``"homebrew"``,
                ``"npm"``, or any other string treated as generic).
            package_name: Base package / formula / module name.
            runtime_hint: Caller-specified runtime hint (e.g. ``"uvx"``).
            processed_runtime_args: Fully resolved positional args for the
                runtime launcher.
            processed_package_args: Fully resolved positional args appended
                after the package name.
            resolved_env: Pre-resolved environment variables dict; an empty
                dict is omitted.
        """
        if registry_name == "pypi":
            launcher = runtime_hint or "uvx"
            config["command"] = launcher
            config["args"] = [package_name] + processed_runtime_args + processed_package_args  # noqa: RUF005
        elif registry_name == "homebrew":
            formula_name = package_name.split("/")[-1] if "/" in package_name else package_name
            config["command"] = formula_name
            config["args"] = processed_runtime_args + processed_package_args
        else:
            # Generic / npm-compatible fallback
            config["command"] = "npx"
            config["args"] = processed_runtime_args + ["-y", package_name] + processed_package_args  # noqa: RUF005
        if resolved_env:
            config["env"] = resolved_env

    def _apply_auth_and_headers_impl(
        self,
        config: dict,
        remote: dict,
        server_info: dict,
        env_overrides: dict,
        runtime_label: str,
        token_manager_class,
    ) -> None:
        """Core implementation of GitHub-token injection and header merging.

        Factored out so that each concrete adapter subclass can supply its own
        *token_manager_class* (looked up from the subclass module's namespace),
        allowing :func:`unittest.mock.patch` to intercept the class at the
        right module scope in tests.

        Args:
            config: Mutable config dict updated in place.
            remote: Registry remote entry (may contain a ``"headers"`` list).
            server_info: Registry server metadata used for name / URL lookup.
            env_overrides: Caller-supplied env-var override mapping.
            runtime_label: Label for diagnostic messages.
            token_manager_class: The ``GitHubTokenManager`` class (or mock) to
                instantiate.  Passed by the caller so tests can patch the right
                module-level name.
        """
        server_name = server_info.get("name", "")
        is_github_server = self._is_github_server(server_name, remote.get("url", ""))
        local_token_injected = False
        if is_github_server:
            _tm = token_manager_class()
            github_token = _tm.get_token_for_purpose("copilot") or os.getenv(
                "GITHUB_PERSONAL_ACCESS_TOKEN"
            )
            if github_token:
                config["headers"] = {"Authorization": f"Bearer {github_token}"}
                local_token_injected = True
        headers = remote.get("headers", [])
        if headers:
            if "headers" not in config:
                config["headers"] = {}
            for header in headers:
                header_name = header.get("name", "")
                header_value = header.get("value", "")
                if header_name and header_value:
                    if header_name == "Authorization" and local_token_injected:
                        continue
                    resolved_value = self._resolve_env_variable(
                        header_name, header_value, env_overrides
                    )
                    config["headers"][header_name] = resolved_value
        if config.get("headers"):
            self._warn_input_variables(
                config["headers"], server_info.get("name", ""), runtime_label
            )

    @staticmethod
    def _resolve_single_env_var(
        env_var: dict,
        env_overrides: dict,
        default_github_env: dict,
        skip_prompting: bool,
    ) -> tuple[str, str] | None:
        """Resolve a single env-var descriptor to a ``(name, value)`` pair.

        Returns ``None`` when *env_var* has no usable ``name`` field.
        Resolution priority:

        1. Caller-supplied override in *env_overrides*.
        2. GitHub-specific defaults from *default_github_env*.
        3. OS environment variable with the same name.
        4. Interactive :mod:`rich.prompt` (skipped when *skip_prompting*).

        Args:
            env_var: Env-var descriptor dict from the registry.
            env_overrides: Pre-collected ``{name: value}`` overrides.
            default_github_env: Mapping of well-known GitHub variable names
                to their preferred default values.
            skip_prompting: When ``True``, interactive prompts are suppressed.

        Returns:
            A ``(name, value)`` tuple, or ``None`` when *env_var* carries no
            usable name.
        """
        name = env_var.get("name", "")
        if not name:
            return None

        # Priority 1: caller-supplied override
        if name in env_overrides:
            return name, env_overrides[name]

        # Priority 2: GitHub-specific defaults
        if name in default_github_env:
            return name, os.getenv(name) or default_github_env[name]

        # Priority 3: environment variable with the same name
        env_val = os.getenv(name, "")
        if env_val:
            return name, env_val

        # Priority 4: interactive prompt or fallback
        default_value = env_var.get("value", "")
        required = env_var.get("required", False)

        if not skip_prompting:
            from rich.prompt import Prompt

            description = env_var.get("description", "")
            prompt_text = f"Enter value for {name}"
            if description:
                prompt_text += f" ({description})"
            is_secret = "token" in name.lower() or "key" in name.lower()
            user_input = Prompt.ask(
                prompt_text,
                default=default_value,
                password=True  # noqa: SIM210
                if is_secret
                else False,
            )
            return name, user_input

        if default_value:
            return name, default_value
        if required:
            _rich_warning(
                f"Warning: Required environment variable '{name}' could not be resolved. "
                f"The MCP server may not function correctly."
            )
        return name, default_value

    @staticmethod
    def _resolve_env_vars_with_prompting(
        env_vars: list,
        env_overrides: dict,
        default_github_env: dict,
    ) -> dict:
        """Resolve *env_vars* from overrides, environment, or interactive prompts.

        Identical logic shared between
        :meth:`CopilotClientAdapter._process_environment_variables` and
        :meth:`CodexClientAdapter._process_environment_variables`.

        All imports are deferred so that ``rich.prompt`` (an optional
        dependency) is never imported at module load time.

        Args:
            env_vars: List of env-var descriptor dicts from the registry.
            env_overrides: Pre-collected ``{name: value}`` overrides (empty
                dict when none).
            default_github_env: Mapping of well-known GitHub variable names
                to their preferred environment-variable lookup names.

        Returns:
            ``resolved`` dict mapping each env-var name to its resolved value
            (empty string when unresolvable).
        """
        import sys

        env_overrides = env_overrides or {}
        resolved: dict = {}

        # Determine whether interactive prompting is available.
        # If env_overrides is provided the CLI has already collected variables -- never prompt again.
        skip_prompting = (
            bool(env_overrides)
            or bool(os.getenv("CI"))
            or bool(os.getenv("APM_E2E_TESTS"))
            or not sys.stdout.isatty()
            or not sys.stdin.isatty()
        )

        # First pass: identify variables with empty values to warn the user.
        empty_value_vars = [ev for ev in env_vars if ev.get("required") and not ev.get("value")]
        if empty_value_vars and skip_prompting:
            var_names = [ev.get("name") for ev in empty_value_vars]
            _rich_warning(
                f"Warning: The following required environment variables have no default "
                f"value and cannot be prompted in non-interactive mode: {var_names}"
            )

        for env_var in env_vars:
            result = MCPClientAdapter._resolve_single_env_var(
                env_var, env_overrides, default_github_env, skip_prompting
            )
            if result is not None:
                name, value = result
                resolved[name] = value

        return resolved
