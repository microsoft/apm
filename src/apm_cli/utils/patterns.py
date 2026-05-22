"""Shared helpers for working with primitive ``applyTo`` patterns.

The ``applyTo`` frontmatter on instruction primitives is documented as a
glob OR a comma-separated list of globs.  This module owns the canonical
parse so converters and the placement optimizer behave consistently.
"""

from __future__ import annotations


def has_top_level_comma(pattern: str) -> bool:
    """Return True if ``pattern`` contains a comma outside any ``{...}`` group.

    Commas inside brace alternation (e.g. ``**/*.{css,scss}``) are part
    of glob brace expansion and must not be treated as list separators.
    Single source of truth for the comma-vs-brace discrimination; the
    placement optimizer and the integrators both consume this so the
    semantics of ``parse_apply_to`` and its callers stay in lock-step.
    """
    depth = 0
    for ch in pattern:
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
        elif ch == "," and depth == 0:
            return True
    return False


def yaml_double_quote(value: str) -> str:
    """Escape ``value`` for embedding inside a YAML double-quoted scalar.

    Defence-in-depth for the instruction integrators that emit YAML
    frontmatter via f-strings (``f'  - "{g}"'``). A glob containing a
    literal backslash, double-quote, or control character would break
    the surrounding YAML if inlined verbatim; this helper escapes the
    minimal set needed for the YAML 1.2 double-quoted form. Returns the
    value already wrapped in the surrounding double quotes.

    Note: ``parse_apply_to`` already strips leading/trailing whitespace
    per segment, and the Windsurf integrator strips newlines from the
    raw frontmatter value before splitting, so the practical risk today
    is near-zero -- this exists so emitted YAML stays well-formed even
    on adversarial or copy-paste-mangled inputs.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


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
