"""Canonical lexical helpers shared across architecture check domains."""

from __future__ import annotations

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
from scripts.architecture_linter.models import FileFacts, Violation

_SENTINEL = "pyproject.toml"
_SRC_PREFIX = "src/apm_cli/"
_PY: tuple[str, ...] = (".py",)


def load_required(
    provider: FactsProvider,
    inventory: frozenset[str],
    rule_id: str,
    path: str,
    *,
    parse: bool = False,
) -> tuple[FileFacts | None, tuple[Violation, ...]]:
    """Read one required source, failing closed when it cannot be inspected."""
    if path not in inventory:
        return None, (
            violation(rule_id, _SENTINEL, f"required source missing from inventory: {path}"),
        )
    facts, failures = checked_facts(provider, path, rule_id, require_python=parse)
    return (None, failures) if failures else (facts, ())


def has_regex(facts: FileFacts, pattern: str | re.Pattern[str]) -> bool:
    """Return whether any cached lexical line matches a regex."""
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    return any(compiled.search(line) for line in facts.lines)


def count_regex(facts: FileFacts, pattern: str | re.Pattern[str]) -> int:
    """Count cached lexical lines matching a regex."""
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    return sum(1 for line in facts.lines if compiled.search(line))


def count_literal(facts: FileFacts, needle: str) -> int:
    """Count cached lexical lines containing a literal."""
    return sum(1 for line in facts.lines if needle in line)


def require_literals(
    provider: FactsProvider,
    inventory: frozenset[str],
    rule_id: str,
    path: str,
    needles: Sequence[str],
    message: str,
    *,
    parse: bool = False,
) -> tuple[Violation, ...]:
    """Require literal fragments in one source file."""
    facts, failures = load_required(provider, inventory, rule_id, path, parse=parse)
    if failures:
        return failures
    missing = [needle for needle in needles if needle not in source_text(facts)]
    if not missing:
        return ()
    rendered = ", ".join(repr(item) for item in missing)
    return (violation(rule_id, path, f"{message}; missing: {rendered}"),)


def require_regexes(
    provider: FactsProvider,
    inventory: frozenset[str],
    rule_id: str,
    path: str,
    patterns: Sequence[re.Pattern[str]],
    message: str,
    *,
    parse: bool = False,
) -> tuple[Violation, ...]:
    """Require regex patterns in one source file."""
    facts, failures = load_required(provider, inventory, rule_id, path, parse=parse)
    if failures:
        return failures
    missing = [pattern.pattern for pattern in patterns if not has_regex(facts, pattern)]
    if not missing:
        return ()
    return (violation(rule_id, path, f"{message}; missing pattern(s): {', '.join(missing)}"),)


def forbid_scan(
    provider: FactsProvider,
    inventory: frozenset[str],
    rule_id: str,
    paths: Iterable[str],
    pattern: str | re.Pattern[str],
    message: str,
    *,
    exempt: bool,
) -> tuple[Violation, ...]:
    """Report matching lines in the requested in-inventory paths."""
    present = tuple(path for path in paths if path in inventory)
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


def count_contracts(
    provider: FactsProvider,
    inventory: frozenset[str],
    rule_id: str,
    path: str,
    checks: Sequence[tuple[str, str, int, str]],
    message: str,
    *,
    parse: bool = False,
) -> tuple[Violation, ...]:
    """Enforce literal or regex occurrence counts in one source file."""
    facts, failures = load_required(provider, inventory, rule_id, path, parse=parse)
    if failures:
        return failures
    problems: list[str] = []
    for kind, target, expected, comparison in checks:
        found = (
            count_literal(facts, target)
            if kind == "sub"
            else count_regex(facts, re.compile(target))
        )
        satisfied = found == expected if comparison == "eq" else found >= expected
        if not satisfied:
            bound = ">=" if comparison == "ge" else "=="
            problems.append(f"{target!r} matched {found} line(s), expected {bound} {expected}")
    return () if not problems else (violation(rule_id, path, f"{message}; {'; '.join(problems)}"),)


def paths_under(
    provider: FactsProvider,
    prefix: str,
    suffixes: tuple[str, ...],
) -> tuple[str, ...]:
    """Return inventory paths matching both a prefix and one of the suffixes."""
    return tuple(
        path for path in inventory_paths(provider, prefixes=(prefix,)) if path.endswith(suffixes)
    )


def source_python_paths(
    provider: FactsProvider,
    *,
    exclude: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return Python product-source paths minus explicit exclusions."""
    excluded = frozenset(exclude)
    return tuple(path for path in paths_under(provider, _SRC_PREFIX, _PY) if path not in excluded)


def python_paths(
    provider: FactsProvider, *, under: str, exclude: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Return Python inventory paths under a prefix."""
    return tuple(
        path
        for path in provider.inventory
        if path.startswith(under)
        and path.endswith(".py")
        and not (exclude and path.startswith(exclude))
    )


def read_required_python(
    provider: FactsProvider,
    rule_id: str,
    paths: Sequence[str],
) -> tuple[dict[str, FileFacts], tuple[Violation, ...]]:
    """Read required Python files through the shared fact provider."""
    facts_by_path: dict[str, FileFacts] = {}
    failures: list[Violation] = []
    for path in paths:
        facts, read_failures = checked_facts(provider, path, rule_id, require_python=True)
        facts_by_path[path] = facts
        failures.extend(read_failures)
    return facts_by_path, tuple(failures)


def captured_body(
    lines: Sequence[str],
    start: re.Pattern[str],
    boundary: re.Pattern[str],
    keep: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    """Capture a source block using the legacy awk boundary semantics."""
    keep_pattern = keep if keep is not None else start
    body: list[str] = []
    capturing = False
    for line in lines:
        if not capturing:
            if start.search(line) is not None:
                capturing = True
                body.append(line)
            continue
        if boundary.search(line) is not None and keep_pattern.search(line) is None:
            break
        body.append(line)
    return tuple(body)


def captured_facts_body(
    facts: object,
    start: re.Pattern[str],
    boundary: re.Pattern[str],
    keep: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    """Capture a block from an object's cached lexical lines."""
    lines = getattr(facts, "lines", ())
    return captured_body(lines if isinstance(lines, tuple) else (), start, boundary, keep)


def body_has(body: Sequence[str], needle: str) -> bool:
    """Return whether a captured body contains a literal."""
    return any(needle in line for line in body)


def body_has_regex(body: Sequence[str], pattern: re.Pattern[str]) -> bool:
    """Return whether a captured body contains a regex match."""
    return any(pattern.search(line) is not None for line in body)


def duplicate_definition_lines(
    provider: FactsProvider,
    *,
    rule_id: str,
    prefix: str,
    pattern: re.Pattern[str],
    owner: str,
    message: str,
    respect_exempt: bool,
) -> list[Violation]:
    """Flag matching definitions outside their canonical owner."""
    findings: list[Violation] = []
    for path in python_paths(provider, under=prefix):
        if path == owner:
            continue
        facts = provider.file_facts(path)
        if facts.read_error is not None:
            continue
        for number, line in enumerate(facts.lines, start=1):
            if respect_exempt and EXEMPT_MARKER in line:
                continue
            match = pattern.search(line)
            if match is not None:
                findings.append(
                    violation(rule_id, path, message, line=number, column=match.start() + 1)
                )
    return findings
