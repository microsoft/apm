"""Base adapter interface for MCP clients."""

import os
from abc import ABC, abstractmethod
from pathlib import Path

from ...models.dependency.mcp import _RESERVED_EXTRA_KEYS
from ...utils.console import _rich_error, _rich_warning
from ._base_env import (
    _ENV_PLACEHOLDER_RE,
    _ENV_VAR_RE,
    _INPUT_VAR_RE,
    _LEGACY_ANGLE_VAR_RE,
    _BaseEnvMixin,
    _extract_legacy_angle_vars,
    _has_env_placeholder,
    _stringify_env_literal,
    _translate_env_placeholder,
    registry_field_is_required,
)

# Re-export so existing ``from .base import _translate_env_placeholder`` etc.
# in sibling modules keep working unchanged.
__all__ = [
    "_ENV_PLACEHOLDER_RE",
    "_ENV_VAR_RE",
    "_INPUT_VAR_RE",
    "_LEGACY_ANGLE_VAR_RE",
    "_extract_legacy_angle_vars",
    "_has_env_placeholder",
    "_stringify_env_literal",
    "_translate_env_placeholder",
    "registry_field_is_required",
]

# Config keys that ``_extra`` passthrough must NEVER set on a rendered harness
# config. Covers the modeled MCP fields (imported single-source from the model)
# plus harness-specific aliases that mirror a modeled field under a different
# name -- e.g. Codex emits ``http_headers`` for remote auth headers, which must
# not be injectable via passthrough. Enforced unconditionally per adapter path
# (NOT guarded by "key absent from config"), so it also closes paths that do not
# pre-set the key. Security boundary for PR #1765 / issue #1670.
_EXTRA_DENYLIST = _RESERVED_EXTRA_KEYS | frozenset({"http_headers"})


class MCPClientAdapter(_BaseEnvMixin, ABC):
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

    @staticmethod
    def _merge_extra(config: dict, server_info: dict) -> dict:
        """Merge harness-specific ``_extra`` keys from server_info into config.

        Two guards apply:

        * Denylist (unconditional): a key naming a modeled MCP field -- or a
          harness alias of one (see ``_EXTRA_DENYLIST``) -- is dropped on EVERY
          path, even when the config does not already carry it. This stops a
          passthrough value from shadowing/redirecting a modeled field on
          adapter paths that start empty or set only a subset of keys.
        * Shadow guard: a non-reserved key is appended only when absent, so it
          never overwrites a value the adapter set itself.
        """
        extra = server_info.get("_extra")
        if extra and isinstance(extra, dict):
            for k, v in extra.items():
                if k in _EXTRA_DENYLIST:
                    continue
                if k not in config:
                    config[k] = v
        return config

    # Whether this adapter's config path is user/global-scoped (e.g.
    # ``~/.copilot/``) rather than workspace-scoped (e.g. ``.vscode/``).
    # Adapters that target a global path should override this to ``True``
    # so that ``apm install --global`` can install MCP servers to them.
    supports_user_scope: bool = False

    # Whether the target runtime resolves ``${VAR}`` placeholders from the
    # host environment at server-start time. Adapters that opt in (Copilot
    # CLI) emit placeholders verbatim so secrets never touch disk; legacy
    # adapters resolve to literal values at install time via env_overrides
    # -> os.environ -> optional interactive prompt. See issue #1152.
    _supports_runtime_env_substitution: bool = False

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
        # Per-server tracking populated by the env-resolution helpers and
        # consumed by ``configure_mcp_server`` for the post-install summary
        # and the aggregated legacy-syntax deprecation warning. Defined on
        # the base so every adapter has the attributes regardless of which
        # subclass path constructed it.
        self._last_env_placeholder_keys: set[str] = set()
        self._last_legacy_angle_vars: set[str] = set()

    def _format_runtime_env_placeholder(self, name: str) -> str:
        """Return the target runtime's env-var placeholder syntax for *name*."""
        return "${" + name + "}"

    def _translate_env_placeholder_for_runtime(self, value):
        """Translate env-var placeholders to this adapter's runtime syntax."""
        if not isinstance(value, str):
            return value

        def _to_runtime(match):
            var_name = match.group(1) or match.group(2)
            return self._format_runtime_env_placeholder(var_name)

        return _ENV_PLACEHOLDER_RE.sub(_to_runtime, value)

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

        # Lookup tables replace per-value if/return chains.
        _hint_map = {
            "npx": "npm",
            "npm": "npm",
            "uvx": "pypi",
            "pip": "pypi",
            "pipx": "pypi",
            "docker": "docker",
            "dotnet": "nuget",
            "dnx": "nuget",
        }
        if runtime_hint in _hint_map:
            return _hint_map[runtime_hint]

        # Infer from package name patterns
        if (name.startswith("@") and "/" in name) or name.startswith(
            ("ghcr.io/", "mcr.microsoft.com/", "docker.io/")
        ):
            return "npm" if name.startswith("@") else "docker"
        if name.startswith("https://") and name.endswith(".mcpb"):
            return "mcpb"
        # PascalCase with dots usually means nuget (e.g. Azure.Mcp)
        if "." in name and not name.startswith("http") and name[0].isupper():
            return "nuget"

        return ""

    @classmethod
    def _select_best_package(cls, packages):
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
                if cls._infer_registry_name(package) == target:
                    return package

        # If no priority package found, return the first one
        return packages[0] if packages else None

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
    def _determine_config_key(server_url: str, server_name: str | None) -> str:
        """Return the configuration key to use for *server_url*/*server_name*.

        The caller-supplied *server_name* takes precedence. If it is absent,
        preserve npm-style scoped names such as ``@scope/name`` (one slash by
        npm convention) while keeping the historical ``owner/repo -> repo``
        fallback for registry paths.

        Args:
            server_url: Registry reference used as fallback source.
            server_name: Explicit caller-supplied name, if any.

        Returns:
            Non-empty configuration key string.
        """
        if server_name:
            return server_name
        if server_url.startswith("@") and server_url.count("/") == 1:
            return server_url
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
