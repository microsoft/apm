"""Shared helpers for working with primitive ``applyTo`` patterns.

The ``applyTo`` frontmatter on instruction primitives is documented as a
glob OR a comma-separated list of globs.  This module owns the canonical
parse so converters and the placement optimizer behave consistently.
"""

from __future__ import annotations


def parse_apply_to(value: str | None) -> list[str]:
    """Split a primitive ``applyTo`` value into individual glob patterns.

    The input is either a single glob (``"**/*.py"``) or a
    comma-separated list (``"**/src/**,**/api/**"``).  Each segment is
    stripped of surrounding whitespace; empty segments are discarded so
    leading, trailing, doubled-up, and lone commas are tolerated.

    Commas inside brace alternation (``{a,b}``) are NOT separators -- only
    top-level commas split the list.  So ``"**/*.{css,scss},**/*.py"``
    yields ``["**/*.{css,scss}", "**/*.py"]``.

    Returns an empty list for ``None``, empty, or whitespace-only input.
    """
    if not value:
        return []
    segments: list[str] = []
    depth = 0
    current: list[str] = []
    for char in value:
        if char == "{":
            depth += 1
            current.append(char)
        elif char == "}":
            if depth > 0:
                depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
    segments.append("".join(current))
    return [segment for segment in (s.strip() for s in segments) if segment]
