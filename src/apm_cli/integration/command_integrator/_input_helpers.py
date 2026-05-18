"""Input argument validation helpers for command frontmatter.

Provides constants and utilities for validating and extracting argument
names from the APM ``input:`` front-matter key used in ``.prompt.md`` files.
"""

from __future__ import annotations

import re
from typing import Any

# Allowlist for argument names extracted from package-supplied 'input:' front-matter.
# Restricts to identifiers that are safe to embed in YAML frontmatter and in
# Claude command bodies as $name placeholders.  Rejects YAML-significant
# characters (newline, colon, quote, etc.) to prevent frontmatter injection.
_INPUT_NAME_RE = re.compile(r"^[A-Za-z][\w-]{0,63}$")


# Frontmatter keys preserved (or consumed) by the shared claude_command
# transformer.  Any key in source frontmatter not in this set is dropped
# during transformation and surfaced as a diagnostic warning so package
# authors can act on it.  See CommandIntegrator.integrate_command().
_PRESERVED_COMMAND_KEYS = frozenset(
    {
        "description",
        "allowed-tools",
        "allowedTools",
        "model",
        "argument-hint",
        "argumentHint",
        "input",
    }
)

# User-facing display names for preserved keys.  Excludes camelCase
# aliases (allowedTools, argumentHint) -- those are accepted on input
# for compat but the canonical kebab-case form is what we surface to
# package authors in diagnostic messages.
_PRESERVED_COMMAND_KEYS_DISPLAY = frozenset(
    {
        "description",
        "allowed-tools",
        "model",
        "argument-hint",
        "input",
    }
)


def _is_valid_input_name(name: str) -> bool:
    """Return True if *name* is a safe argument identifier."""
    return bool(_INPUT_NAME_RE.match(name))


def _accept_input_name(candidate: Any, valid: list[str], rejected: list[str]) -> None:
    """Classify one raw input name candidate."""
    if not isinstance(candidate, str):
        rejected.append(repr(candidate))
        return
    stripped = candidate.strip()
    if not stripped:
        return
    if _is_valid_input_name(stripped):
        valid.append(stripped)
    else:
        rejected.append(stripped)


def _extract_input_names(
    input_spec: Any,
) -> tuple[list[str], list[str]]:
    """Extract argument names from an APM 'input' front-matter value.

    Handles both formats:
      - Simple list:  input: [name, category]
      - Object list:  input:
                        - feature_name: "desc"
                        - feature_description: "desc"

    Args:
        input_spec: The raw value of the 'input' front-matter key.

    Returns:
        Tuple[List[str], List[str]]: (valid names in order, rejected raw entries).
        Names are accepted only if they match ``^[A-Za-z][\\w-]{0,63}$``;
        anything else (empty/whitespace, YAML-significant chars, oversize) is
        rejected and reported back so the caller can surface a warning.
    """
    valid: list[str] = []
    rejected: list[str] = []

    if input_spec is None:
        return valid, rejected

    if isinstance(input_spec, list):
        for item in input_spec:
            if isinstance(item, str):
                _accept_input_name(item, valid, rejected)
            elif isinstance(item, dict):
                for k in item:
                    _accept_input_name(k, valid, rejected)
            else:
                rejected.append(repr(item))
        return valid, rejected

    if isinstance(input_spec, str):
        _accept_input_name(input_spec, valid, rejected)
        return valid, rejected

    if isinstance(input_spec, dict):
        for k in input_spec:
            _accept_input_name(k, valid, rejected)
        return valid, rejected

    return valid, rejected
