"""Env-resolution and docker-args mixin for CopilotClientAdapter.

Extracted from copilot.py to keep that module under 800 lines while
preserving full MRO override semantics: ``CopilotClientAdapter`` lists
``_CopilotEnvMixin`` before ``MCPClientAdapter`` so these methods shadow
the base implementations for every ``CopilotClientAdapter`` instance.

Rule B: none of these methods reference patched module-level names from
copilot.py (``_rich_warning``, ``SimpleRegistryClient``, etc.), so no
function-level late imports are required here.
"""

import os

from ._mcp_runtime_args import process_v01_value_hint_arg
from .base import (
    _ENV_PLACEHOLDER_RE,
    _ENV_VAR_RE,
    _extract_legacy_angle_vars,
    _has_env_placeholder,
    _stringify_env_literal,
    registry_field_is_required,
)


class _CopilotEnvMixin:
    """Env-resolution and docker-args helpers composed into CopilotClientAdapter.

    Overrides the corresponding base-class methods so that Copilot CLI's
    translate-mode behaviour (emit ``${VAR}`` placeholders, never read secrets
    at install time) takes effect for every ``CopilotClientAdapter`` instance
    while sibling adapters (Cursor, Claude, etc.) keep the legacy-resolve path.
    """

    def _resolve_environment_variables(self, env_vars, env_overrides=None):
        """Resolve (or translate) declared environment variables.

        Behaviour depends on ``self._supports_runtime_env_substitution``:

        - True (Copilot CLI default): each declared env var ``NAME`` gets a
          ``${NAME}`` placeholder that Copilot CLI resolves at server-start
          from the host environment. Hardcoded literal defaults
          (``GITHUB_TOOLSETS``, ``GITHUB_DYNAMIC_TOOLSETS``) stay literal
          because they are not secrets and provide essential server
          configuration. The host environment is NOT read; secrets never
          touch disk. See issue #1152 for context.

        - False (legacy / sibling-adapter behaviour): resolve each variable
          to its literal value via ``env_overrides`` -> ``os.environ`` ->
          optional interactive prompt, baking the result into the config.

        Args:
            env_vars (list): List of environment variable definitions from
                server info (each item is ``{name, description, required}``).
            env_overrides (dict, optional): Pre-collected environment
                variable overrides. Ignored in translate mode.

        Returns:
            dict: ``{name: value}`` -- placeholder string in translate mode,
            literal value in legacy mode.
        """
        # Hardcoded literal defaults that supply essential server behaviour
        # rather than secrets. These stay literal in translate mode so that
        # tool-selection still works without a user export step.
        default_github_env = {"GITHUB_TOOLSETS": "context", "GITHUB_DYNAMIC_TOOLSETS": "1"}

        # Self-defined stdio deps pass ``env`` as a plain dict
        # ({NAME: value-or-placeholder}); registry-sourced deps pass a list
        # of {name, description, required} dicts. Translate-mode handling
        # for the dict shape: each value is either already a placeholder
        # (translate it to the adapter's runtime form) or a literal
        # (record the key as a placeholder reference and emit a runtime
        # placeholder so the value never lands on disk). See issue #1152.
        if isinstance(env_vars, dict) and self._supports_runtime_env_substitution:
            translated = {}
            placeholder_keys = []
            for name, raw_value in env_vars.items():
                if not name:
                    continue
                if raw_value is None:
                    continue
                if not isinstance(raw_value, str):
                    translated[name] = _stringify_env_literal(raw_value)
                    continue
                if _has_env_placeholder(raw_value):
                    self._last_legacy_angle_vars.update(_extract_legacy_angle_vars(raw_value))
                    translated[name] = self._translate_env_placeholder_for_runtime(raw_value)
                    for match in _ENV_VAR_RE.finditer(translated[name]):
                        placeholder_keys.append(match.group(1))
                elif name in default_github_env and raw_value == default_github_env[name]:
                    translated[name] = raw_value
                else:
                    # Literal value present in apm.yml -- replace with a
                    # runtime placeholder so the secret never touches disk.
                    translated[name] = self._format_runtime_env_placeholder(name)
                    placeholder_keys.append(name)
            self._last_env_placeholder_keys = set(placeholder_keys)
            return translated

        if self._supports_runtime_env_substitution:
            env_overrides = env_overrides or {}
            resolved = {}
            placeholder_keys = []
            for env_var in env_vars:
                if not isinstance(env_var, dict):
                    continue
                name = env_var.get("name", "")
                if not name:
                    continue
                required = registry_field_is_required(env_var)
                override_value = env_overrides.get(name)
                has_override = bool(
                    override_value.strip() if isinstance(override_value, str) else override_value
                )
                if name in default_github_env:
                    # Non-secret literal default -- preserve as-is.
                    resolved[name] = default_github_env[name]
                elif required or has_override:
                    # Emit a runtime-substitution placeholder; APM never reads
                    # or stores the value. Optional variables are included only
                    # when install-time collection observed a value.
                    resolved[name] = self._format_runtime_env_placeholder(name)
                    placeholder_keys.append(name)
            # Record for the post-install summary line and the
            # security-improvement notice.
            self._last_env_placeholder_keys = set(placeholder_keys)
            return resolved

        if isinstance(env_vars, dict):
            # Mirror the base-class dict-shape branch but coerce non-string
            # scalars through Copilot's hardened ``_stringify_env_literal``
            # helper so booleans/ints land as the strings Copilot CLI expects.
            return {
                name: (
                    self._resolve_env_variable(name, value, env_overrides=env_overrides)
                    if isinstance(value, str)
                    else _stringify_env_literal(value)
                )
                for name, value in env_vars.items()
                if name and value is not None
            }

        return self._resolve_env_vars_with_prompting(env_vars, env_overrides, default_github_env)

    def _resolve_env_variable(self, name, value, env_overrides=None):
        """Resolve (or translate) a single environment variable value.

        Behaviour depends on ``self._supports_runtime_env_substitution``:

        - True (Copilot CLI default): translate placeholders to Copilot CLI's
          native runtime substitution syntax (``${VAR}``). The host
          environment is NOT read; the secret never touches disk. See issue
          #1152 for context. Legacy ``<VAR>`` offenders are tracked for the
          aggregated deprecation warning emitted by
          ``configure_mcp_server``.

        - False (legacy / sibling-adapter behaviour): resolve placeholders
          to literal values via ``env_overrides`` -> ``os.environ`` ->
          optional interactive prompt, baking the result into the config.

        Args:
            name (str): Environment variable name.
            value (str): Environment variable value or placeholder.
            env_overrides (dict, optional): Pre-collected environment
                variable overrides. Ignored in translate mode.

        Returns:
            str: Translated placeholder (translate mode) or resolved
            literal value (legacy mode).
        """
        if self._supports_runtime_env_substitution:
            # Track legacy <VAR> offenders for the aggregated deprecation
            # warning. Translation itself is a pure-textual rewrite.
            self._last_legacy_angle_vars.update(_extract_legacy_angle_vars(value))
            # Track env-var names referenced via this header/value so the
            # security-upgrade detector and per-server summary can see
            # them (the env-block path tracks via _resolve_environment_variables).
            for match in _ENV_VAR_RE.finditer(value):
                self._last_env_placeholder_keys.add(match.group(1))
            return self._translate_env_placeholder_for_runtime(value)

        import sys

        from rich.prompt import Prompt

        env_overrides = env_overrides or {}
        # If env_overrides is provided, it means we're in managed environment collection mode
        skip_prompting = bool(env_overrides)

        # Check for CI/automated environment via APM_E2E_TESTS flag (more reliable than TTY detection)
        if os.getenv("APM_E2E_TESTS") == "1":
            skip_prompting = True

        # Also skip prompting if we're in a non-interactive environment (fallback)
        is_interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if not is_interactive:
            skip_prompting = True

        # Three accepted placeholder syntaxes (see _COPILOT_ENV_RE at module
        # top), all resolved against env_overrides -> os.environ -> optional
        # interactive prompt. Single-pass substitution preserves the legacy
        # ``<VAR>`` semantics: resolved values are not re-scanned for further
        # placeholder expansion.
        def _replace(match):
            # Group 1 = legacy <VAR>; group 2 = ${VAR} / ${env:VAR}.
            env_name = match.group(1) or match.group(2)
            env_value = env_overrides.get(env_name) or os.getenv(env_name)
            if not env_value and not skip_prompting:
                prompt_text = f"Enter value for {env_name}"
                env_value = Prompt.ask(
                    prompt_text,
                    password=True  # noqa: SIM210
                    if "token" in env_name.lower() or "key" in env_name.lower()
                    else False,
                )
            return env_value if env_value else match.group(0)

        return _ENV_PLACEHOLDER_RE.sub(_replace, value)

    def _inject_env_vars_into_docker_args(self, docker_args, env_vars):
        """Inject environment variables into Docker arguments following registry template.

        The registry provides a complete Docker command template in runtime_arguments.
        We need to inject actual environment variable values while respecting the template structure.
        Also ensures required Docker flags (-i, --rm) are present.

        Args:
            docker_args (list): Docker arguments from registry runtime_arguments.
            env_vars (dict): Resolved environment variables.

        Returns:
            list: Docker arguments with environment variables properly injected and required flags.
        """
        if not env_vars:
            env_vars = {}

        result = []
        i = 0
        has_interactive = False
        has_rm = False

        # Check for existing -i and --rm flags
        for arg in docker_args:
            if arg == "-i" or arg == "--interactive":  # noqa: PLR1714
                has_interactive = True
            elif arg == "--rm":
                has_rm = True

        while i < len(docker_args):
            arg = docker_args[i]
            result.append(arg)

            # When we encounter "run", inject required flags first
            if arg == "run":
                # Add -i flag if not present
                if not has_interactive:
                    result.append("-i")

                # Add --rm flag if not present
                if not has_rm:
                    result.append("--rm")

            # If this is an environment variable name placeholder, replace with actual env var
            if arg in env_vars:
                # This is an environment variable name that should be replaced with -e VAR=value
                result.pop()  # Remove the env var name
                result.extend(["-e", f"{arg}={env_vars[arg]}"])
            elif arg == "-e" and i + 1 < len(docker_args):
                # Handle -e flag followed by env var name
                next_arg = docker_args[i + 1]
                if next_arg in env_vars:
                    result.append(f"{next_arg}={env_vars[next_arg]}")
                    i += 1  # Skip the next argument as we've processed it
                else:
                    # Keep the original argument structure
                    result.append(next_arg)
                    i += 1

            i += 1

        # Add any remaining environment variables that weren't in the template
        template_env_vars = set()
        for arg in docker_args:
            if arg in env_vars:
                template_env_vars.add(arg)

        for env_name, env_value in env_vars.items():
            if env_name not in template_env_vars:
                # Find a good place to insert additional env vars (after "run" but before image name)
                insert_pos = len(result)
                for idx, arg in enumerate(result):
                    if arg == "run":
                        # Insert after run command but before image name (usually last arg)
                        insert_pos = min(len(result) - 1, idx + 1)
                        break

                result.insert(insert_pos, "-e")
                result.insert(insert_pos + 1, f"{env_name}={env_value}")

        # Add default GitHub MCP server environment variables if not already present
        # Only add defaults for variables that were NOT explicitly provided (even if empty)
        default_github_env = {"GITHUB_TOOLSETS": "context", "GITHUB_DYNAMIC_TOOLSETS": "1"}  # noqa: F841

        existing_env_vars = set()
        for i, arg in enumerate(result):
            if arg == "-e" and i + 1 < len(result):
                env_spec = result[i + 1]
                if "=" in env_spec:
                    env_name = env_spec.split("=", 1)[0]
                    existing_env_vars.add(env_name)

        # For Copilot, defaults are already added during environment resolution
        # This section is kept for compatibility but shouldn't add duplicates

        return result

    def _inject_docker_env_vars(self, args, env_vars):
        """Inject environment variables into Docker arguments.

        Args:
            args (list): Original Docker arguments.
            env_vars (dict): Environment variables to inject.

        Returns:
            list: Updated arguments with environment variables injected.
        """
        result = []

        for arg in args:
            result.append(arg)
            # If this is a docker run command, inject environment variables after "run"
            if arg == "run" and env_vars:
                for env_name, env_value in env_vars.items():
                    result.extend(["-e", f"{env_name}={env_value}"])

        return result

    def _process_arguments(self, arguments, resolved_env=None, runtime_vars=None):
        """Process argument objects to extract simple string values with environment and runtime variable resolution.

        Args:
            arguments (list): List of argument objects from registry.
            resolved_env (dict): Resolved environment variables.
            runtime_vars (dict): Resolved runtime variables.

        Returns:
            list: List of processed argument strings.
        """
        if resolved_env is None:
            resolved_env = {}
        if runtime_vars is None:
            runtime_vars = {}

        processed = []

        for arg in arguments:
            if isinstance(arg, dict):
                # Extract value from argument object
                arg_type = arg.get("type", "")
                if arg_type == "positional":
                    value = arg.get("value", arg.get("default", ""))
                    if value:
                        # Resolve both environment and runtime variable placeholders with actual values
                        processed_value = self._resolve_variable_placeholders(
                            str(value), resolved_env, runtime_vars
                        )
                        processed.append(processed_value)
                elif arg_type == "named":
                    name = arg.get("name", "")
                    value = arg.get("value", arg.get("default", ""))
                    if name:
                        processed.append(name)
                        # For named arguments, only add value if it's different from the flag name
                        # and not empty
                        if value and value != name and not value.startswith("-"):
                            processed_value = self._resolve_variable_placeholders(
                                str(value), resolved_env, runtime_vars
                            )
                            processed.append(processed_value)
                elif not arg_type and "value_hint" in arg:
                    # v0.1 registry format: shared helper handles is_required
                    # guard and {var_name} placeholder substitution.
                    value = process_v01_value_hint_arg(arg, runtime_vars)
                    if value:
                        processed_value = self._resolve_variable_placeholders(
                            value, resolved_env, runtime_vars
                        )
                        processed.append(processed_value)
            elif isinstance(arg, str):
                # Already a string, use as-is but resolve variable placeholders
                processed_value = self._resolve_variable_placeholders(
                    arg, resolved_env, runtime_vars
                )
                processed.append(processed_value)

        return processed
