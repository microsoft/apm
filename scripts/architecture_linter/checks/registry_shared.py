"""Shared helpers and cross-cutting constants for registry/delegation checks.

Every function here is a pure, side-effect-free helper (facts-only; never
reads disk) used by both :mod:`registry_owner_guards` and
:mod:`registry_semantic_rules`. Splitting them out keeps each rule-family
module focused on its own checks while avoiding duplicated helper logic.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts
from scripts.architecture_linter.models import FileFacts, Violation

GROUP = "registry_delegation"


_SRC = "src/apm_cli/"


def _read_required(
    provider: FactsProvider, rule_id: str, paths: Sequence[str]
) -> tuple[dict[str, FileFacts], tuple[Violation, ...]]:
    """Read every required file through the shared cache, failing closed.

    Returns the cached facts keyed by path and a tuple of fail-closed
    violations for any file that could not be read or parsed. A non-empty
    failure tuple means the caller must not proceed: a missing or
    unparseable owner is treated as a guard failure, never a silent pass.
    """
    facts_by_path: dict[str, FileFacts] = {}
    failures: list[Violation] = []
    for path in paths:
        facts, read_failures = checked_facts(provider, path, rule_id, require_python=True)
        facts_by_path[path] = facts
        failures.extend(read_failures)
    return facts_by_path, tuple(failures)


def _python_paths(
    provider: FactsProvider, *, under: str, exclude: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Return inventory ``*.py`` paths under a prefix (AND semantics).

    ``common.inventory_paths`` selects with OR semantics across its prefix
    and suffix criteria, so it cannot express "under this directory *and*
    ending in ``.py``". This helper filters the one canonical inventory
    directly, dropping any path under an `exclude` prefix.
    """
    return tuple(
        path
        for path in provider.inventory
        if path.startswith(under)
        and path.endswith(".py")
        and not (exclude and path.startswith(exclude))
    )


def _count_regex_lines(facts: FileFacts, pattern: str | re.Pattern[str]) -> int:
    """Count lexical lines matching `pattern` (mirrors ``grep -Ec``)."""
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    return sum(1 for line in facts.lines if compiled.search(line) is not None)


def _has_regex(facts: FileFacts, pattern: str | re.Pattern[str]) -> bool:
    """Return whether any lexical line matches `pattern` (``grep -Eq``)."""
    return _count_regex_lines(facts, pattern) > 0


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
