"""Input-name parsing helpers for command_integrator.

Contains the regex and two module-level functions that validate and extract
argument names from APM 'input:' front-matter.  Extracted here so
command_integrator.py stays within the 800-line hard gate.

Re-exported from ``command_integrator`` so existing import paths and
test monkeypatches are byte-identical.
"""

from __future__ import annotations

import re
from typing import Any

# Allowlist for argument names extracted from package-supplied 'input:' front-matter.
# Restricts to identifiers that are safe to embed in YAML frontmatter and in
# Claude command bodies as $name placeholders. Rejects YAML-significant
# characters (newline, colon, quote, etc.) to prevent frontmatter injection.
_INPUT_NAME_RE = re.compile(r"^[A-Za-z][\w-]{0,63}$")


def _is_valid_input_name(name: str) -> bool:
    """Return True if *name* is a safe argument identifier."""
    return bool(_INPUT_NAME_RE.match(name))


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

    def _accept(candidate: Any) -> None:
        if not isinstance(candidate, str):
            rejected.append(repr(candidate))
            return
        stripped = candidate.strip()
        if not stripped:
            return  # silently drop pure-whitespace entries
        if _is_valid_input_name(stripped):
            valid.append(stripped)
        else:
            rejected.append(stripped)

    if input_spec is None:
        return valid, rejected

    if isinstance(input_spec, list):
        for item in input_spec:
            if isinstance(item, str):
                _accept(item)
            elif isinstance(item, dict):
                for k in item.keys():  # noqa: SIM118
                    _accept(k)
            else:
                rejected.append(repr(item))
        return valid, rejected

    if isinstance(input_spec, str):
        _accept(input_spec)
        return valid, rejected

    if isinstance(input_spec, dict):
        for k in input_spec.keys():  # noqa: SIM118
            _accept(k)
        return valid, rejected

    return valid, rejected
