"""Shared helpers for working with primitive ``applyTo`` patterns.

The ``applyTo`` frontmatter on instruction primitives is documented as a
glob OR a comma-separated list of globs.  This module owns the canonical
parse so converters and the placement optimizer behave consistently.
"""

from __future__ import annotations

_APPLY_TO_ESCAPE = "\\"
_APPLY_TO_SEPARATOR = ","
_ESCAPABLE_APPLY_TO_CHARS = frozenset({_APPLY_TO_SEPARATOR, _APPLY_TO_ESCAPE})


class _ApplyToPattern(str):
    """A parsed pattern that remembers an escaped top-level comma."""

    _escaped_top_level_comma: bool

    def __new__(cls, value: str, escaped_top_level_comma: bool):
        instance = super().__new__(cls, value)
        instance._escaped_top_level_comma = escaped_top_level_comma
        return instance

    def strip(self, chars: str | None = None) -> _ApplyToPattern:
        """Preserve comma-boundary metadata while trimming a pattern."""
        return type(self)(super().strip(chars), self._escaped_top_level_comma)


def has_top_level_comma(pattern: str) -> bool:
    """Return True if ``pattern`` contains a comma outside any ``{...}`` group.

    Commas inside brace alternation (e.g. ``**/*.{css,scss}``) are part
    of glob brace expansion and must not be treated as list separators.
    Single source of truth for the comma-vs-brace discrimination; the
    placement optimizer and the integrators both consume this so the
    semantics of ``parse_apply_to`` and its callers stay in lock-step.
    """
    if getattr(pattern, "_escaped_top_level_comma", False):
        return False

    depth = 0
    index = 0
    while index < len(pattern):
        ch = pattern[index]
        if (
            ch == _APPLY_TO_ESCAPE
            and index + 1 < len(pattern)
            and pattern[index + 1] in _ESCAPABLE_APPLY_TO_CHARS
        ):
            index += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
        elif ch == _APPLY_TO_SEPARATOR and depth == 0:
            return True
        index += 1
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


def normalize_apply_to(value: object, default: str = "") -> str:
    """Normalize scalar or YAML-list ``applyTo`` values into one OR expression.

    Compilation stores ``applyTo`` as a string. YAML sequences therefore use
    the documented top-level-comma representation consumed by
    :func:`parse_apply_to`, preserving every non-null list entry.
    """
    if isinstance(value, list):
        patterns = []
        for pattern in value:
            if pattern is None:
                continue
            normalized = str(pattern).strip()
            if normalized:
                patterns.append(_escape_apply_to_segment(normalized))
        return ",".join(patterns) if patterns else default
    if value is None:
        return default
    return str(value)


def _escape_apply_to_segment(pattern: str) -> str:
    """Encode one YAML-list pattern so top-level commas retain their boundary."""
    escaped: list[str] = []
    depth = 0
    for char in pattern:
        if char == _APPLY_TO_ESCAPE:
            escaped.append(_APPLY_TO_ESCAPE * 2)
        elif char == "{":
            depth += 1
            escaped.append(char)
        elif char == "}":
            if depth > 0:
                depth -= 1
            escaped.append(char)
        elif char == _APPLY_TO_SEPARATOR and depth == 0:
            escaped.append(_APPLY_TO_ESCAPE + _APPLY_TO_SEPARATOR)
        else:
            escaped.append(char)
    return "".join(escaped)


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
    segments: list[_ApplyToPattern] = []
    depth = 0
    current: list[str] = []
    escaped_top_level_comma = False

    def append_current() -> None:
        segments.append(_ApplyToPattern("".join(current), escaped_top_level_comma))

    index = 0
    while index < len(value):
        char = value[index]
        if (
            char == _APPLY_TO_ESCAPE
            and index + 1 < len(value)
            and value[index + 1] in _ESCAPABLE_APPLY_TO_CHARS
        ):
            current.append(value[index + 1])
            escaped_top_level_comma = (
                escaped_top_level_comma or value[index + 1] == _APPLY_TO_SEPARATOR
            )
            index += 2
            continue
        if char == "{":
            depth += 1
            current.append(char)
        elif char == "}":
            if depth > 0:
                depth -= 1
            current.append(char)
        elif char == _APPLY_TO_SEPARATOR and depth == 0:
            append_current()
            current = []
            escaped_top_level_comma = False
        else:
            current.append(char)
        index += 1
    append_current()
    return [segment for segment in (s.strip() for s in segments) if segment]
