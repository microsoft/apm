"""Shared, side-effect-free helpers for hook/MCP mutation-write analyzers.

Every helper here is used by two or more of the cohesive check-family
modules (:mod:`mutation_hook_contract`, :mod:`mutation_mcp_target_and_scope`,
:mod:`mutation_hook_membership`); splitting them out avoids duplicating the
same span/grep/scan primitives in every family module.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from scripts.architecture_linter.checks.lexical_shared import (
    count_regex,
    has_regex,
    python_paths,
    read_required_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import (
    EXEMPT_MARKER,
    checked_facts,
    source_text,
    violation,
)
from scripts.architecture_linter.models import FileFacts, Violation

GROUP = "mutation_writes"

_count_regex_lines = count_regex
_has_regex = has_regex
_python_paths = python_paths
_read_required = read_required_python


_SRC = "src/apm_cli/"


_MCP_OWNERSHIP = "src/apm_cli/install/mcp/ownership.py"


def _has_fixed(facts: FileFacts, needle: str) -> bool:
    """Return whether any lexical line contains `needle` (``grep -q``)."""
    return needle in source_text(facts)


def _function_span(
    facts: FileFacts, name: str, *, top_level: bool = True
) -> tuple[int, int] | None:
    """Return the ``(start, end)`` line span of the first matching ``def``."""
    for definition in facts.definitions:
        if definition.name != name or definition.kind != "function":
            continue
        is_top = definition.scope == "<module>"
        if top_level and is_top:
            return definition.line, definition.end_line
        if not top_level and not is_top:
            return definition.line, definition.end_line
    return None


def _span_lines(facts: FileFacts, span: tuple[int, int]) -> tuple[tuple[int, str], ...]:
    """Return ``(line_number, text)`` pairs for a 1-based inclusive span."""
    start, end = span
    return tuple(
        (number, text) for number, text in enumerate(facts.lines, start=1) if start <= number <= end
    )


def _first_span_line(facts: FileFacts, span: tuple[int, int], needle: str) -> int | None:
    """Return the first 1-based line in `span` containing fixed `needle`."""
    for number, text in _span_lines(facts, span):
        if needle in text:
            return number
    return None


def _span_has_fixed(facts: FileFacts, span: tuple[int, int], needle: str) -> bool:
    """Return whether any line in `span` contains fixed `needle`."""
    return _first_span_line(facts, span, needle) is not None


def _span_has_regex(facts: FileFacts, span: tuple[int, int], pattern: str) -> bool:
    """Return whether any line in `span` matches `pattern`."""
    compiled = re.compile(pattern)
    return any(compiled.search(text) is not None for _, text in _span_lines(facts, span))


def _duplicate_scan(
    provider: FactsProvider,
    *,
    rule_id: str,
    paths: Sequence[str],
    pattern: str,
    message: str,
    exempt: bool,
    exclude_line_pattern: str | None = None,
) -> tuple[Violation, ...]:
    """Report every non-owner line matching `pattern` (mirrors ``grep -rEn``)."""
    compiled = re.compile(pattern)
    excluder = re.compile(exclude_line_pattern) if exclude_line_pattern else None
    findings: list[Violation] = []
    for path in paths:
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        findings.extend(failures)
        if failures:
            continue
        for number, text in enumerate(facts.lines, start=1):
            if exempt and EXEMPT_MARKER in text:
                continue
            match = compiled.search(text)
            if match is None:
                continue
            if excluder is not None and excluder.search(text) is not None:
                continue
            findings.append(
                violation(rule_id, path, message, line=number, column=match.start() + 1)
            )
    return findings


def _require(condition: bool, rule_id: str, path: str, message: str) -> tuple[Violation, ...]:
    """Return one violation when a required `condition` is false."""
    return () if condition else (violation(rule_id, path, message),)
