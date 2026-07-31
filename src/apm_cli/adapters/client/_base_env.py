"""Env-resolution mixin and module-level helpers for MCPClientAdapter.

Contains the pure-helper functions and the _BaseEnvMixin class that
MCPClientAdapter composes in.  Kept in a sibling module so base.py stays
under 800 lines while all helpers remain importable from base.py via
re-exports.
"""

import os
import re
from typing import Any, ClassVar

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


def registry_field_is_required(field: dict[str, Any]) -> bool:
    """Return True unless registry metadata explicitly marks a field optional."""
    return field.get("required", field.get("is_required", True)) is not False


class _BaseEnvMixin:
    """Env-resolution logic composed into MCPClientAdapter.

    All methods access instance state (``_last_env_placeholder_keys``,
    ``_last_legacy_angle_vars``) and adapter helpers
    (``_format_runtime_env_placeholder``, ``_translate_env_placeholder_for_runtime``)
    that are defined on ``MCPClientAdapter``.  This is the standard mixin
    pattern: the mixin trusts the final class to provide those attributes.
    """

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

        # Rule B: route through base module so tests patching
        # apm_cli.adapters.client.base._rich_warning are intercepted.
        from apm_cli.adapters.client import base as _b

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
            _b._rich_warning(
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
                _b._rich_warning(
                    f"Required environment variable '{name}' could not be resolved. "
                    f"The MCP server may not function correctly. Set {name} in your "
                    "environment and rerun `apm install`."
                )
                resolved[name] = ""

        return resolved
