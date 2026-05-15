"""Base adapter interface for MCP clients."""

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from ...utils.console import _rich_warning

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
                    translated[name] = _translate_env_placeholder(raw_value)
                    # Record every ${VAR} in the translated value (handles
                    # both ${env:VAR} -> ${VAR} and bare ${VAR} cases).
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
                    translated[name] = "${" + name + "}"
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
                    resolved[name] = "${" + name + "}"
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
            required = env_var.get("required", True)

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
            return _translate_env_placeholder(value)

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
            processed = _translate_env_placeholder(processed)
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
