"""Shared, side-effect-free helpers for transport/platform analyzers.

Every helper here is used by two or more of the cohesive check-family
modules (:mod:`transport_auth_platform`, :mod:`transport_cache_identity`,
:mod:`transport_sparse_and_updates`, :mod:`transport_network_and_runtime`);
splitting them out avoids duplicating the same grep/require/forbid
primitives in every family module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from scripts.architecture_linter.checks import repository_cache_identity as cache_identity
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

GROUP = "transport_platform"


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


def _has(facts: FileFacts, needle: str) -> bool:
    """Return True when a single-line literal appears anywhere (grep -q / -Fq)."""
    return needle in source_text(facts)


def _has_re(facts: FileFacts, pattern: re.Pattern[str]) -> bool:
    """Return True when any line matches the anchored ERE (grep -qE)."""
    return any(pattern.search(line) for line in facts.lines)


def _count_re(facts: FileFacts, pattern: re.Pattern[str]) -> int:
    """Count lines matching the ERE (grep -Ec)."""
    return sum(1 for line in facts.lines if pattern.search(line))


def _count_sub(facts: FileFacts, needle: str) -> int:
    """Count lines containing the literal (grep -Fc / grep -c)."""
    return sum(1 for line in facts.lines if needle in line)


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
    """Require every literal fragment in one source file, failing closed on read."""
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
    """Require every anchored ERE in one source file (grep -q '^...')."""
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
    """Report every matching line across in-inventory files (grep -rEn | grep -v ...)."""
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
    """Assert canonical occurrence counts in one file.

    Each check is ``(kind, target, expected, comparison)`` where ``kind`` is
    ``"sub"`` (literal line count) or ``"re"`` (regex line count) and
    ``comparison`` is ``"eq"`` or ``"ge"``.
    """
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
    """All ``src/apm_cli/**/*.py`` inventory paths, minus explicit exclusions."""
    excluded = frozenset(exclude)
    return tuple(path for path in _paths_under(provider, _SRC_PREFIX, _PY) if path not in excluded)


_TIERED = cache_identity.TIERED_RESOLVER_PATH


_GH_DOWNLOADER = "src/apm_cli/deps/github_downloader.py"


_NET_OWNER_DEFS = re.compile(r"^def (parse_host_address|is_loopback_host)\(")
