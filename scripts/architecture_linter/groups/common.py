"""Shared, side-effect-free helpers for architecture rule groups."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import FileFacts, Violation

EXEMPT_MARKER = "architecture-authority-exempt:"


def violation(
    rule_id: str,
    path: str,
    message: str,
    *,
    line: int = 1,
    column: int = 1,
) -> Violation:
    """Create a consistently shaped rule violation."""
    return Violation(
        rule_id=rule_id,
        path=path,
        line=max(line, 1),
        column=max(column, 1),
        message=message,
    )


def checked_facts(
    provider: FactsProvider,
    path: str,
    rule_id: str,
    *,
    require_python: bool = False,
) -> tuple[FileFacts, tuple[Violation, ...]]:
    """Read one file through the shared cache and fail closed on read/parse."""
    facts = provider.file_facts(path)
    failures: list[Violation] = []
    if facts.read_error is not None:
        failures.append(
            violation(rule_id, path, f"required source unavailable: {facts.read_error}")
        )
    elif require_python and facts.parse_error is not None:
        failures.append(
            violation(rule_id, path, f"Python syntax analysis failed: {facts.parse_error}")
        )
    return facts, tuple(failures)


def source_text(facts: FileFacts) -> str:
    """Rebuild normalized source text from cached lexical lines."""
    return "\n".join(facts.lines)


def inventory_paths(
    provider: FactsProvider,
    *,
    exact: Iterable[str] = (),
    prefixes: Iterable[str] = (),
    suffixes: Iterable[str] = (),
    names: Iterable[str] = (),
    excluded_prefixes: Iterable[str] = (),
) -> tuple[str, ...]:
    """Select paths only from the one central deterministic inventory."""
    exact_set = frozenset(exact)
    prefix_tuple = tuple(prefixes)
    suffix_tuple = tuple(suffixes)
    name_set = frozenset(names)
    excluded_tuple = tuple(excluded_prefixes)
    selected: list[str] = []
    for path in provider.inventory:
        if excluded_tuple and path.startswith(excluded_tuple):
            continue
        path_name = PurePosixPath(path).name
        if (
            path in exact_set
            or (prefix_tuple and path.startswith(prefix_tuple))
            or (suffix_tuple and path.endswith(suffix_tuple))
            or path_name in name_set
        ):
            selected.append(path)
    return tuple(selected)


def line_pattern_violations(
    provider: FactsProvider,
    *,
    rule_id: str,
    paths: Sequence[str],
    pattern: str | re.Pattern[str],
    message: str,
    exempt_marker: str | None,
    flags: int = 0,
) -> tuple[Violation, ...]:
    """Report every matching lexical line, mirroring the legacy grep helper."""
    compiled = re.compile(pattern, flags) if isinstance(pattern, str) else pattern
    findings: list[Violation] = []
    for path in paths:
        lines, read_error = provider.lexical_lines(path)
        if read_error is not None:
            findings.append(violation(rule_id, path, f"required source unavailable: {read_error}"))
            continue
        for number, line in enumerate(lines, start=1):
            if exempt_marker is not None and exempt_marker in line:
                continue
            match = compiled.search(line)
            if match is not None:
                findings.append(
                    violation(
                        rule_id,
                        path,
                        message,
                        line=number,
                        column=match.start() + 1,
                    )
                )
    return tuple(findings)


def require_text(
    provider: FactsProvider,
    *,
    rule_id: str,
    path: str,
    needles: Sequence[str],
    message: str,
) -> tuple[Violation, ...]:
    """Require every literal fragment in one cached source file."""
    facts, failures = checked_facts(
        provider,
        path,
        rule_id,
        require_python=path.endswith(".py"),
    )
    if failures:
        return failures
    text = source_text(facts)
    missing = tuple(needle for needle in needles if needle not in text)
    if not missing:
        return ()
    return (
        violation(
            rule_id,
            path,
            f"{message}; missing: {', '.join(repr(item) for item in missing)}",
        ),
    )


def forbid_text(
    provider: FactsProvider,
    *,
    rule_id: str,
    path: str,
    needles: Sequence[str],
    message: str,
) -> tuple[Violation, ...]:
    """Reject literal fragments in one cached source file."""
    facts, failures = checked_facts(
        provider,
        path,
        rule_id,
        require_python=path.endswith(".py"),
    )
    if failures:
        return failures
    findings: list[Violation] = []
    for number, line in enumerate(facts.lines, start=1):
        if EXEMPT_MARKER in line:
            continue
        for needle in needles:
            column = line.find(needle)
            if column >= 0:
                findings.append(
                    violation(
                        rule_id,
                        path,
                        message,
                        line=number,
                        column=column + 1,
                    )
                )
    return tuple(findings)


def merge_findings(*groups: Iterable[Violation]) -> tuple[Violation, ...]:
    """Flatten violation iterables without changing their semantic contents."""
    return tuple(item for group in groups for item in group)
