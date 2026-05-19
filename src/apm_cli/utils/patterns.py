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

    Returns an empty list for ``None``, empty, or whitespace-only input.
    """
    if not value:
        return []
    return [segment for segment in (part.strip() for part in value.split(",")) if segment]
