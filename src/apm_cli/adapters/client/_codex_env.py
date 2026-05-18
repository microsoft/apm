"""Private helpers for Codex CLI env-var and Docker-args processing.

These are pure functions extracted from ``CodexClientAdapter`` to keep
``codex.py`` within the 500-line budget.  They carry **no** dependency on
``self`` and can therefore be unit-tested in isolation.

Internal API — import path may change without notice.
"""

from __future__ import annotations

import logging
import os
import re
import sys

_log = logging.getLogger(__name__)


def _should_skip_prompting(env_overrides: dict | None) -> bool:
    """Return True when interactive environment-variable prompting must be suppressed.

    Three reasons to skip: pre-supplied overrides, CI flag, or non-interactive TTY.
    """
    if env_overrides:
        return True
    if os.getenv("APM_E2E_TESTS") == "1":
        print(" APM_E2E_TESTS detected, will skip environment variable prompts")
        return True
    return not (sys.stdin.isatty() and sys.stdout.isatty())


def _resolve_single_env_var(
    env_var: dict,
    env_overrides: dict,
    skip_prompting: bool,
    empty_value_vars: set[str],
    default_github_env: dict[str, str],
) -> tuple[str, str] | None:
    """Resolve one env-var dict entry to a ``(name, value)`` pair, or None.

    Returns ``None`` when the entry should be skipped (no name, no value
    available, and no applicable default).
    """
    from rich.prompt import Prompt

    name = env_var.get("name", "")
    if not name:
        return None
    description = env_var.get("description", "")
    required = env_var.get("required", True)

    value = env_overrides.get(name) or os.getenv(name)
    if not value and required and not skip_prompting:
        prompt_text = f"Enter value for {name}"
        if description:
            prompt_text += f" ({description})"
        value = Prompt.ask(
            prompt_text,
            password=bool("token" in name.lower() or "key" in name.lower()),
        )

    if value and value.strip():
        return name, value
    if name in default_github_env and (name in empty_value_vars or not required or skip_prompting):
        return name, default_github_env[name]
    return None


def process_environment_variables(
    env_vars: list,
    env_overrides: dict | None = None,
) -> dict:
    """Process environment variable definitions and resolve actual values.

    Args:
        env_vars: List of environment variable definitions from registry.
        env_overrides: Pre-collected environment variable overrides.

    Returns:
        Dictionary of resolved environment variable name -> value.
    """
    resolved: dict[str, str] = {}
    env_overrides = env_overrides or {}

    skip_prompting = _should_skip_prompting(env_overrides)

    # Default GitHub MCP server env vars used when the user supplies no value.
    default_github_env = {"GITHUB_TOOLSETS": "context", "GITHUB_DYNAMIC_TOOLSETS": "1"}

    # Track variables explicitly provided with empty values (user wants defaults).
    empty_value_vars: set[str] = set()
    if env_overrides:
        for key, value in env_overrides.items():
            if not value or not value.strip():
                empty_value_vars.add(key)

    for env_var in env_vars:
        if not isinstance(env_var, dict):
            continue
        pair = _resolve_single_env_var(
            env_var, env_overrides, skip_prompting, empty_value_vars, default_github_env
        )
        if pair is not None:
            resolved[pair[0]] = pair[1]

    return resolved


def resolve_variable_placeholders(
    value: str,
    resolved_env: dict,
    runtime_vars: dict,
) -> str:
    """Resolve ``<ENV_VAR>`` and ``{runtime_var}`` placeholders in *value*.

    Args:
        value: String that may contain ``<TOKEN_NAME>`` or ``{runtime_var}``
            placeholders.
        resolved_env: Resolved environment variable values keyed by name.
        runtime_vars: Resolved runtime variable values keyed by name.

    Returns:
        Processed string with placeholders substituted.
    """
    if not value:
        return value

    processed = str(value)

    # Replace <TOKEN_NAME> with actual values from resolved_env.
    env_pattern = r"<([A-Z_][A-Z0-9_]*)>"

    def _replace_env(match: re.Match) -> str:
        return resolved_env.get(match.group(1), match.group(0))

    processed = re.sub(env_pattern, _replace_env, processed)

    # Replace {runtime_var} with actual values from runtime_vars.
    runtime_pattern = r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}"

    def _replace_runtime(match: re.Match) -> str:
        return runtime_vars.get(match.group(1), match.group(0))

    processed = re.sub(runtime_pattern, _replace_runtime, processed)

    return processed


def ensure_docker_env_flags(base_args: list, env_vars: dict) -> list:
    """Ensure every resolved env var is represented as a ``-e`` flag in Docker args.

    For the Codex TOML format, Docker ``args`` must carry ``-e <VAR_NAME>``
    entries for **all** environment variables whose values live in the
    separate ``[env]`` section.

    Args:
        base_args: Docker arguments already derived from the registry.
        env_vars: All resolved environment variables that the container needs.

    Returns:
        Docker argument list with ``-e <VAR_NAME>`` entries for every key in
        *env_vars* that was not already present in *base_args*.
    """
    if not env_vars:
        return base_args

    result: list[str] = []
    existing_env_vars: set[str] = set()

    # First pass: copy existing args and collect already-present -e flags.
    i = 0
    while i < len(base_args):
        arg = base_args[i]
        result.append(arg)
        if arg == "-e" and i + 1 < len(base_args):
            existing_env_vars.add(base_args[i + 1])
            result.append(base_args[i + 1])
            i += 2
        else:
            i += 1

    # Second pass: add -e flags for variables not yet present.
    # Insert them before the image name (the last positional argument).
    image_name = result[-1] if result else ""
    if image_name and not image_name.startswith("-"):
        result.pop()
        for env_name in sorted(env_vars.keys()):
            if env_name not in existing_env_vars:
                result.extend(["-e", env_name])
        result.append(image_name)
    else:
        for env_name in sorted(env_vars.keys()):
            if env_name not in existing_env_vars:
                result.extend(["-e", env_name])

    return result


def inject_docker_env_vars(args: list, env_vars: dict) -> list:
    """Inject environment variables into Docker args as ``-e`` flags after ``run``.

    Args:
        args: Original Docker arguments.
        env_vars: Environment variables to inject.

    Returns:
        Updated argument list with ``-e <VAR_NAME>`` entries injected
        immediately after the ``run`` subcommand, skipping duplicates.
    """
    if not env_vars:
        return args

    existing_env_vars: set[str] = set()

    # First pass: collect existing -e flags to avoid duplicates.
    i = 0
    while i < len(args):
        if args[i] == "-e" and i + 1 < len(args):
            existing_env_vars.add(args[i + 1])
            i += 2
        else:
            i += 1

    # Second pass: rebuild with new env vars injected after "run".
    result: list[str] = []
    for arg in args:
        result.append(arg)
        if arg == "run":
            for env_name in env_vars:
                if env_name not in existing_env_vars:
                    result.extend(["-e", env_name])

    return result
