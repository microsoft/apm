"""Shared helper for MCP v0.1 runtimeArguments.variables handling.

Extracted to avoid R0801 duplicate-code violations across adapter modules
that each implement a ``_process_arguments`` method but share identical v0.1
``value_hint`` processing logic (copilot and codex).

Usage in an adapter's ``_process_arguments`` loop::

    from ._mcp_runtime_args import process_v01_value_hint_arg

    elif not arg_type and "value_hint" in arg:
        value = process_v01_value_hint_arg(arg, runtime_vars)
        if value:
            processed.append(self._resolve_variable_placeholders(
                value, resolved_env, runtime_vars
            ))
"""

from __future__ import annotations


def process_v01_value_hint_arg(arg: dict, runtime_vars: dict | None) -> str | None:
    """Process a single v0.1-format runtimeArguments entry.

    A v0.1 entry has a ``value_hint`` key but no ``type`` key.  An optional
    ``variables`` dict maps placeholder names to their metadata.

    The ``is_required`` field on the *arg itself* controls whether the entry
    participates in argument building.  Entries with ``is_required: False``
    are optional hints that must be skipped -- VS Code's extractor only
    includes entries with ``is_required: True``, so including them would
    produce extra, unintended CLI args.  When ``is_required`` is absent the
    entry is treated as required (default True).

    Args:
        arg: The argument dict to process.  Must contain ``value_hint``.
        runtime_vars: Resolved APM template variables (may be ``None`` or
            empty).

    Returns:
        The processed value string after ``{var_name}`` placeholder
        substitution, or ``None`` if the entry should be skipped (optional
        hint or empty value).
    """
    # Skip optional legacy hints that are not required.
    if not arg.get("is_required", True):
        return None

    value: str = arg.get("value_hint", "")
    if not value:
        return None

    if "variables" in arg:
        for var_name in arg["variables"]:
            if runtime_vars and var_name in runtime_vars:
                replacement = runtime_vars[var_name]
            else:
                replacement = f"${{{var_name}}}"
            value = value.replace(f"{{{var_name}}}", replacement)

    return value or None
