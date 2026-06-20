"""Base adapter interface for MCP clients."""

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from ...models.dependency.mcp import _RESERVED_EXTRA_KEYS
from ...utils.console import _rich_error, _rich_warning

_INPUT_VAR_RE = re.compile(r"\$\{input:([^}]+)\}")

# Matches ${VAR} and ${env:VAR}, capturing VAR. Intentionally does NOT match
# ${input:VAR} (the optional ``env:`` group cannot also satisfy ``input:``),
# nor GitHub Actions ``${{ ... }}`` templates (the second ``{`` fails the
# identifier class). This keeps env-var handling fully disjoint from input
# variable handling, so existing _INPUT_VAR_RE call sites are unaffected.
_ENV_VAR_RE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")

# Superset of _ENV_VAR_RE that also matches the legacy ``<VAR>`` syntax
# (uppercase identifier only). Used as the single-pass translation target so
# resolved values are NOT re-scanned -- a literal value whose text happens to
# contain ``${...}`` does not get recursively expanded. ``${input:...}`` is
# intentionally not matched here so input-variable handling stays disjoint.
_ENV_PLACEHOLDER_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>|" + _ENV_VAR_RE.pattern)

# Detects the legacy ``<VAR>`` placeholder syntax only. Used to aggregate
# deprecation warnings across all servers in a single install run.
_LEGACY_ANGLE_VAR_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")

# Config keys that ``_extra`` passthrough must NEVER set on a rendered harness
# config. Covers the modeled MCP fields (imported single-source from the model)
# plus harness-specific aliases that mirror a modeled field under a different
# name -- e.g. Codex emits ``http_headers`` for remote auth headers, which must
# not be injectable via passthrough. Enforced unconditionally per adapter path
# (NOT guarded by "key absent from config"), so it also closes paths that do not
# pre-set the key. Security boundary for PR #1765 / issue #1670.
_EXTRA_DENYLIST = _RESERVED_EXTRA_KEYS | frozenset({"http_headers"})


def registry_field_is_required(field: dict[str, Any]) -> bool:
    """Return True unless registry metadata explicitly marks a field optional."""
    return field.get("required", field.get("is_required", True)) is not False


def _translate_env_placeholder(value):
    """Pure-textual translation of env-var placeholders to the canonical
    ``${VAR}`` runtime-substitution syntax.

    Security-critical helper for issue #1152: MUST NOT read ``os.environ``
    and MUST NOT resolve placeholders to literal values. Runtimes that
    support runtime substitution (Copilot CLI) resolve ``${VAR}`` from the
    host environment at server-start, so APM emits placeholders verbatim
    rather than baking secrets to disk.

    Translations:
        ``${env:VAR}``     -> ``${VAR}``     (strip ``env:`` prefix)
        ``${VAR}``         -> ``${VAR}``     (no-op)
        ``<VAR>``          -> ``${VAR}``     (legacy syntax migration)
        ``${VAR:-default}``-> passthrough    (regex doesn't match)
        ``$VAR`` (bare)    -> passthrough    (regex doesn't match)
        ``${input:foo}``   -> passthrough    (regex doesn't match)
        non-string         -> passthrough

    Idempotent: applying twice yields the same result as applying once.
    """
    if not isinstance(value, str):
        return value

    def _to_brace(match):
        # group(1) = legacy <VAR>; group(2) = ${VAR} / ${env:VAR}
        var_name = match.group(1) or match.group(2)
        return "${" + var_name + "}"

    return _ENV_PLACEHOLDER_RE.sub(_to_brace, value)


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
    return bool(_ENV_PLACEHOLDER_RE.search(value))


def _stringify_env_literal(value):
    """Return MCP env literal values in the manifest ``map<string, string>`` shape."""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


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

    # -- Env-var placeholder resolution -------------------------------------
    # GitHub MCP server defaults: not secrets, preserved literal in translate
    # mode and used as fallbacks in legacy mode. The defaults apply regardless
    # of which client CLI runs the server, so they live on the base.
    _DEFAULT_GITHUB_ENV: ClassVar[dict[str, str]] = {
        "GITHUB_TOOLSETS": "context",
        "GITHUB_DYNAMIC_TOOLSETS": "1",
    }

    @staticmethod
    def _should_skip_env_prompts(env_overrides):
        """True when the caller has already collected env vars (managed mode),
        when APM_E2E_TESTS is set, or when stdin/stdout is not a TTY.

        Centralising this policy keeps the resolver paths consistent and
        avoids subtle drift between ``_resolve_environment_variables`` and
        ``_resolve_env_variable``.
        """
        import sys

        if env_overrides:
            return True
        if os.getenv("APM_E2E_TESTS") == "1":
            return True
        return not (sys.stdin.isatty() and sys.stdout.isatty())

    def _resolve_environment_variables(self, env_vars, env_overrides=None):
        """Resolve (or translate) declared environment variables.

        Behaviour follows ``self._supports_runtime_env_substitution``:
        translate-mode (Copilot CLI) emits ``${VAR}`` placeholders verbatim
        so the runtime resolves them at server-start (see issue #1152);
        legacy-mode resolves placeholders to literal values via env_overrides
        -> os.environ -> optional interactive prompt.

        Args:
            env_vars: Either a ``dict[name, value-or-placeholder]`` from a
                self-defined stdio dep (``_raw_stdio["env"]``), or a
                ``list[{name, description, required}]`` from the registry.
            env_overrides: Pre-collected env-var overrides (ignored in
                translate mode).

        Returns:
            dict: ``{name: value}`` -- placeholder string in translate
            mode, literal value in legacy mode.
        """
        # ---- translate mode, dict shape (self-defined stdio in apm.yml) ----
        if isinstance(env_vars, dict) and self._supports_runtime_env_substitution:
            # Value type is intentionally untyped: most entries are translated
            # placeholder strings, but non-string values (e.g. an int/bool
            # YAML scalar) are passed through verbatim and serialised by the
            # adapter's config writer (JSON/TOML).
            translated: dict = {}
            placeholder_keys: list[str] = []
            for name, raw_value in env_vars.items():
                if not name:
                    continue
                if not isinstance(raw_value, str):
                    translated[name] = raw_value
                    continue
                if _has_env_placeholder(raw_value):
                    self._last_legacy_angle_vars.update(_extract_legacy_angle_vars(raw_value))
                    translated[name] = self._translate_env_placeholder_for_runtime(raw_value)
                    placeholder_keys.extend(
                        m.group(1) for m in _ENV_VAR_RE.finditer(translated[name])
                    )
                elif (
                    name in self._DEFAULT_GITHUB_ENV and raw_value == self._DEFAULT_GITHUB_ENV[name]
                ):
                    translated[name] = raw_value
                else:
                    # Literal value present in apm.yml -- replace with a
                    # runtime placeholder so the secret never touches disk.
                    translated[name] = self._format_runtime_env_placeholder(name)
                    placeholder_keys.append(name)
            self._last_env_placeholder_keys = set(placeholder_keys)
            return translated

        # ---- translate mode, registry list shape ----
        if self._supports_runtime_env_substitution:
            resolved: dict[str, str] = {}
            placeholder_keys: list[str] = []
            for env_var in env_vars:
                if not isinstance(env_var, dict):
                    continue
                name = env_var.get("name", "")
                if not name:
                    continue
                if name in self._DEFAULT_GITHUB_ENV:
                    resolved[name] = self._DEFAULT_GITHUB_ENV[name]
                else:
                    resolved[name] = self._format_runtime_env_placeholder(name)
                    placeholder_keys.append(name)
            self._last_env_placeholder_keys = set(placeholder_keys)
            return resolved

        # ---- legacy mode, dict shape (self-defined stdio in apm.yml) ----
        # Issue #1266 / #1222: ``_raw_stdio["env"]`` is a plain dict. Each
        # value is resolved via the same single-value pipeline used for
        # header values so all three placeholder syntaxes (``<VAR>``,
        # ``${VAR}``, ``${env:VAR}``) behave consistently across adapters.
        #
        # Note the deliberate semantic divergence from the legacy-list branch
        # below: empty strings authored in apm.yml are preserved as-is and
        # ``_DEFAULT_GITHUB_ENV`` fallbacks are NOT applied, because a value
        # explicitly written by the user expresses intent, whereas an empty
        # value coming from ``env_overrides`` / ``os.environ`` for a
        # registry-declared schema entry means "no value supplied, use the
        # default if one exists".
        if isinstance(env_vars, dict):
            resolved = {}
            for name, value in env_vars.items():
                if not name:
                    continue
                if isinstance(value, str):
                    resolved[name] = self._resolve_env_variable(
                        name, value, env_overrides=env_overrides
                    )
                elif value is not None:
                    resolved[name] = str(value)
            return resolved

        # ---- legacy mode, registry list shape ----
        from rich.prompt import Prompt

        env_overrides = env_overrides or {}
        skip_prompting = self._should_skip_env_prompts(env_overrides)

        # Variables explicitly provided with empty values mean "use the default".
        empty_value_vars = {k for k, v in env_overrides.items() if not v or not v.strip()}

        resolved = {}
        for env_var in env_vars:
            if not isinstance(env_var, dict):
                continue
            name = env_var.get("name", "")
            if not name:
                continue
            required = registry_field_is_required(env_var)

            value = env_overrides.get(name) or os.getenv(name)
            if not value and required and not skip_prompting:
                prompt_text = f"Enter value for {name}"
                if description := env_var.get("description", ""):
                    prompt_text += f" ({description})"
                value = Prompt.ask(
                    prompt_text,
                    password="token" in name.lower() or "key" in name.lower(),
                )

            if value and value.strip():
                resolved[name] = value
            elif name in self._DEFAULT_GITHUB_ENV and (
                name in empty_value_vars or not required or skip_prompting
            ):
                resolved[name] = self._DEFAULT_GITHUB_ENV[name]

        return resolved

    def _resolve_env_variable(self, name, value, env_overrides=None):
        """Resolve (or translate) a single env-var value.

        Used for header values and for individual entries in dict-shape
        env blocks. The ``name`` parameter is currently unused by the
        method body but kept in the signature because every call site
        (headers, dict iteration) already has the name in hand, and
        passing it preserves call-site symmetry with future hooks that
        may want to dispatch on it.

        Args:
            name: Env-var name (currently unused, see above).
            value: Env-var value possibly containing placeholders.
            env_overrides: Pre-collected overrides (ignored in translate mode).
        """
        if self._supports_runtime_env_substitution:
            legacy_keys = _extract_legacy_angle_vars(value)
            self._last_legacy_angle_vars.update(legacy_keys)
            self._last_env_placeholder_keys.update(legacy_keys)
            for match in _ENV_VAR_RE.finditer(value):
                self._last_env_placeholder_keys.add(match.group(1))
            return self._translate_env_placeholder_for_runtime(value)

        from rich.prompt import Prompt

        env_overrides = env_overrides or {}
        skip_prompting = self._should_skip_env_prompts(env_overrides)

        # Three accepted placeholder syntaxes resolved against
        # env_overrides -> os.environ -> optional interactive prompt.
        # Single-pass substitution preserves the legacy ``<VAR>`` semantics:
        # resolved values are NOT re-scanned for further expansion.
        def _replace(match):
            env_name = match.group(1) or match.group(2)
            env_value = env_overrides.get(env_name) or os.getenv(env_name)
            if not env_value and not skip_prompting:
                env_value = Prompt.ask(
                    f"Enter value for {env_name}",
                    password="token" in env_name.lower() or "key" in env_name.lower(),
                )
            return env_value if env_value else match.group(0)

        return _ENV_PLACEHOLDER_RE.sub(_replace, value)

    def _resolve_variable_placeholders(self, value, resolved_env, runtime_vars):
        """Resolve env-var and APM template placeholders in argument strings.

        Translate mode rewrites all three env-var placeholder syntaxes to
        ``${VAR}`` (so the runtime can resolve them at server-start); legacy
        mode resolves only the legacy ``<VAR>`` form against ``resolved_env``
        and leaves the newer ``${VAR}`` / ``${env:VAR}`` syntaxes untouched
        for backward compatibility. APM template variables (``{runtime_var}``)
        are always resolved at install time because they are an APM-internal
        concept the target runtime cannot interpret.

        Args:
            value: String possibly containing placeholders.
            resolved_env: Resolved env-var literals (legacy mode) or
                placeholder strings (translate mode).
            runtime_vars: Resolved APM template variables.

        Returns:
            str: ``value`` with placeholders translated or resolved.
        """
        if not value:
            return value

        processed = str(value)

        if self._supports_runtime_env_substitution:
            self._last_legacy_angle_vars.update(_extract_legacy_angle_vars(processed))
            processed = self._translate_env_placeholder_for_runtime(processed)
        else:
            # Resolve only the legacy ``<VAR>`` form; newer syntaxes are
            # preserved verbatim for backward compatibility.
            def _replace_legacy_angle(match):
                return resolved_env.get(match.group(1), match.group(0))

            processed = _LEGACY_ANGLE_VAR_RE.sub(_replace_legacy_angle, processed)

        # Resolve APM ``{runtime_var}`` template variables. The negative
        # lookbehind on ``$`` ensures we never accidentally match the brace
        # of an already-translated ``${VAR}`` env placeholder.
        if runtime_vars:
            runtime_pattern = re.compile(r"(?<!\$)\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

            def _replace_runtime(match):
                return runtime_vars.get(match.group(1), match.group(0))

            processed = runtime_pattern.sub(_replace_runtime, processed)

        return processed

    def _resolve_env_placeholders(self, value, resolved_env):
        """Legacy thin wrapper for backward compatibility.

        Kept because external callers and the phase-3 test suite invoke
        the pre-#1277 name. Delegates to ``_resolve_variable_placeholders``
        with an empty ``runtime_vars`` map. New code should call
        ``_resolve_variable_placeholders`` directly.
        """
        return self._resolve_variable_placeholders(value, resolved_env, {})

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
        empty_value_vars = [
            ev for ev in env_vars if registry_field_is_required(ev) and not ev.get("value")
        ]
        if empty_value_vars and skip_prompting:
            var_names = [ev.get("name") for ev in empty_value_vars]
            _rich_warning(
                f"Required environment variables have no default value and cannot be "
                f"prompted in non-interactive mode: {var_names}. Set them in your "
                "environment and rerun `apm install`."
            )

        for env_var in env_vars:
            name = env_var.get("name", "")
            if not name:
                continue

            # Priority 1: caller-supplied override.
            # An explicit empty (or whitespace-only) value is treated as
            # "user cleared this". For names with a GitHub-style default the
            # logic falls through so the literal default wins; for names
            # without a default the entry is dropped from the resolved map.
            if name in env_overrides:
                override_value = env_overrides[name]
                if isinstance(override_value, str) and not override_value.strip():
                    if name not in default_github_env:
                        continue
                else:
                    resolved[name] = override_value
                    continue

            # Priority 2: check GitHub-specific defaults (values are literal defaults, not env-var names)
            if name in default_github_env:
                resolved[name] = os.getenv(name) or default_github_env[name]
                continue

            # Priority 3: environment variable with the same name
            env_val = os.getenv(name, "")
            if env_val:
                resolved[name] = env_val
                continue

            # Priority 4: interactive prompt
            default_value = env_var.get("value", "")
            required = registry_field_is_required(env_var)

            if not skip_prompting and required:
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
                resolved[name] = user_input
            elif default_value:
                resolved[name] = default_value
            elif required:
                _rich_warning(
                    f"Required environment variable '{name}' could not be resolved. "
                    f"The MCP server may not function correctly. Set {name} in your "
                    "environment and rerun `apm install`."
                )
                resolved[name] = ""

        return resolved
