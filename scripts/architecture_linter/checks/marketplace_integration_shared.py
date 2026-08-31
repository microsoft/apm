"""Shared, side-effect-free helpers for marketplace/integration analyzers.

Used by two or more of the cohesive check-family modules under this
catalog; splitting them out avoids duplicating the same lexical/AST
primitives in every family module.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Sequence

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import (
    EXEMPT_MARKER,
    checked_facts,
    inventory_paths,
    line_pattern_violations,
    source_text,
    violation,
)
from scripts.architecture_linter.models import DefinitionFact, FileFacts, Violation

GROUP = "marketplace_integrations"


_SENTINEL = "pyproject.toml"


_SRC_PREFIX = "src/apm_cli/"


_PY: tuple[str, ...] = (".py",)


def _load(
    provider: FactsProvider,
    inv: frozenset[str],
    rule_id: str,
    path: str,
    *,
    parse: bool = False,
) -> tuple[FileFacts | None, tuple[Violation, ...]]:
    """Read one required source, failing closed on missing/unreadable/unparseable."""
    if path not in inv:
        return None, (
            violation(rule_id, _SENTINEL, f"required source missing from inventory: {path}"),
        )
    facts, failures = checked_facts(provider, path, rule_id, require_python=parse)
    if failures:
        return None, failures
    return facts, ()


def _has_re(facts: FileFacts, pattern: re.Pattern[str]) -> bool:
    return any(pattern.search(line) for line in facts.lines)


def _count_re(facts: FileFacts, pattern: re.Pattern[str]) -> int:
    return sum(1 for line in facts.lines if pattern.search(line))


def _count_sub(facts: FileFacts, needle: str) -> int:
    return sum(1 for line in facts.lines if needle in line)


def _definition(facts: FileFacts, name: str) -> DefinitionFact | None:
    for definition in reversed(facts.definitions):
        if definition.name == name:
            return definition
    return None


def _def_body_text(facts: FileFacts, name: str) -> str:
    """Return the joined source of the last recorded ``name`` definition."""
    definition = _definition(facts, name)
    if definition is None:
        return ""
    return "\n".join(facts.lines[definition.line - 1 : definition.end_line])


def _ast_definition_body(facts: FileFacts, definition: ast.AST) -> str:
    """Return source text for one AST definition."""
    line = _definition_line(definition)
    end_line = max(getattr(definition, "end_lineno", line) or line, line)
    return "\n".join(facts.lines[line - 1 : end_line])


def _count_calls_named(facts: FileFacts, name: str) -> int:
    """Count call expressions whose terminal callee name is ``name``."""
    total = 0
    for call in facts.calls:
        terminal = call.qualname.rsplit(".", 1)[-1]
        if terminal == name:
            total += 1
    return total


def _count_calls_qualified(facts: FileFacts, qualname: str) -> int:
    """Count calls to one exact AST-derived dotted callable name."""
    return sum(1 for call in facts.calls if call.qualname == qualname)


def _require_subs(
    provider: FactsProvider,
    inv: frozenset[str],
    rule_id: str,
    path: str,
    needles: Sequence[str],
    message: str,
    *,
    parse: bool = False,
) -> tuple[Violation, ...]:
    facts, failures = _load(provider, inv, rule_id, path, parse=parse)
    if failures:
        return failures
    text = source_text(facts)
    missing = [needle for needle in needles if needle not in text]
    if missing:
        rendered = ", ".join(repr(item) for item in missing)
        return (violation(rule_id, path, f"{message}; missing: {rendered}"),)
    return ()


def _require_res(
    provider: FactsProvider,
    inv: frozenset[str],
    rule_id: str,
    path: str,
    patterns: Sequence[re.Pattern[str]],
    message: str,
    *,
    parse: bool = False,
) -> tuple[Violation, ...]:
    facts, failures = _load(provider, inv, rule_id, path, parse=parse)
    if failures:
        return failures
    missing = [pattern.pattern for pattern in patterns if not _has_re(facts, pattern)]
    if missing:
        return (violation(rule_id, path, f"{message}; missing pattern(s): {', '.join(missing)}"),)
    return ()


def _forbid_scan(
    provider: FactsProvider,
    inv: frozenset[str],
    rule_id: str,
    paths: Iterable[str],
    pattern: str | re.Pattern[str],
    message: str,
    *,
    exempt: bool,
) -> tuple[Violation, ...]:
    present = tuple(path for path in paths if path in inv)
    if not present:
        return ()
    return line_pattern_violations(
        provider,
        rule_id=rule_id,
        paths=present,
        pattern=pattern,
        message=message,
        exempt_marker=EXEMPT_MARKER if exempt else None,
    )


def _count_checks(
    provider: FactsProvider,
    inv: frozenset[str],
    rule_id: str,
    path: str,
    checks: Sequence[tuple[str, str, int, str]],
    message: str,
    *,
    parse: bool = False,
) -> tuple[Violation, ...]:
    facts, failures = _load(provider, inv, rule_id, path, parse=parse)
    if failures:
        return failures
    problems: list[str] = []
    for kind, target, expected, comparison in checks:
        found = _count_sub(facts, target) if kind == "sub" else _count_re(facts, re.compile(target))
        satisfied = found == expected if comparison == "eq" else found >= expected
        if not satisfied:
            bound = ">=" if comparison == "ge" else "=="
            problems.append(f"{target!r} matched {found} line(s), expected {bound} {expected}")
    if problems:
        return (violation(rule_id, path, f"{message}; {'; '.join(problems)}"),)
    return ()


def _paths_under(
    provider: FactsProvider, prefix: str, suffixes: tuple[str, ...]
) -> tuple[str, ...]:
    """Inventory paths under ``prefix`` whose name ends with one of ``suffixes``.

    ``inventory_paths`` treats prefixes and suffixes as a union; this helper
    intersects them so a scan stays scoped to (for example) ``src/apm_cli/**``
    AND ``*.py`` instead of every ``*.py`` in the repository.
    """
    return tuple(
        path for path in inventory_paths(provider, prefixes=(prefix,)) if path.endswith(suffixes)
    )


def _src_python(provider: FactsProvider, *, exclude: Iterable[str] = ()) -> tuple[str, ...]:
    excluded = frozenset(exclude)
    return tuple(path for path in _paths_under(provider, _SRC_PREFIX, _PY) if path not in excluded)


def _subdir_python(provider: FactsProvider, prefix: str) -> tuple[str, ...]:
    return _paths_under(provider, prefix, _PY)


_PLUGIN_PARSER = "src/apm_cli/deps/plugin_parser.py"


_PROJECTION = "src/apm_cli/agent_plugins/projection.py"


_VALIDATION = "src/apm_cli/models/validation.py"


def _definition_line(node: ast.AST | None) -> int:
    return max(getattr(node, "lineno", 1), 1)
