"""Shared helpers and cross-cutting constants for registry/delegation checks.

Every function here is a pure, side-effect-free helper (facts-only; never
reads disk) used by both :mod:`registry_owner_guards` and
:mod:`registry_semantic_rules`. Splitting them out keeps each rule-family
module focused on its own checks while avoiding duplicated helper logic.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.lexical_shared import (
    count_regex,
    has_regex,
    python_paths,
    read_required_python,
)
from scripts.architecture_linter.models import FileFacts

GROUP = "registry_delegation"

_count_regex_lines = count_regex
_has_regex = has_regex
_python_paths = python_paths
_read_required = read_required_python


_SRC = "src/apm_cli/"


def _definition_span(
    facts: FileFacts, name: str, *, kinds: tuple[str, ...] = ("function", "async_function")
) -> tuple[int, int] | None:
    """Return the ``(start, end)`` 1-based line span of the first matching def."""
    for definition in facts.definitions:
        if definition.name == name and definition.kind in kinds:
            return definition.line, definition.end_line
    return None


def _scope_lines(facts: FileFacts, span: tuple[int, int]) -> tuple[tuple[int, str], ...]:
    """Return ``(line_number, text)`` pairs for a 1-based inclusive span."""
    start, end = span
    numbered: list[tuple[int, str]] = []
    for number, text in enumerate(facts.lines, start=1):
        if start <= number <= end:
            numbered.append((number, text))
    return tuple(numbered)


def _literal_string_value(value_repr: str) -> str:
    """Return the string value behind a cached Python literal repr.

    The shared traversal stores each string as its quoted ``repr``. For the
    short, quote-free status glyphs
    this strips one matching pair of surrounding quotes; anything else is
    returned unchanged so it simply fails the raw-symbol membership test.
    """
    if len(value_repr) >= 2 and value_repr[0] in "'\"" and value_repr[-1] == value_repr[0]:
        return value_repr[1:-1]
    return value_repr
