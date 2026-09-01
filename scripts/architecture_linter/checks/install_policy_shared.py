"""Shared, side-effect-free helpers for install policy/intent analyzers.

Every helper here is used by two or more of the cohesive check-family
modules under this catalog; splitting them out avoids duplicating the same
lexical/AST primitives in every family module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import EXEMPT_MARKER, checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

GROUP = "install_deployment"


_TESTS_TREE = "tests/"


def _numbered(facts: object) -> tuple[tuple[int, str], ...]:
    """Return ``(line_number, text)`` pairs for one file's cached lines."""
    return tuple(enumerate(getattr(facts, "lines", ()), start=1))


def _configured(
    provider: FactsProvider, path: str, rule_id: str
) -> tuple[tuple[tuple[int, str], ...], tuple[Violation, ...]]:
    """Read one configured owner/consumer file, failing closed on read/parse.

    A configured path is wired into the rule, not discovered: if it cannot be
    read (or no longer parses as Python) the boundary is unverifiable, which
    the engine reports rather than silently treating as "nothing to check".
    """
    facts, failures = checked_facts(provider, path, rule_id, require_python=path.endswith(".py"))
    if failures:
        return (), failures
    return _numbered(facts), ()


def _scanned(
    provider: FactsProvider, path: str, rule_id: str
) -> tuple[tuple[tuple[int, str], ...], tuple[Violation, ...]]:
    """Return numbered lines for a *discovered* tree path, failing closed.

    Tree scans are purely lexical -- they replace ``grep -r``, not an AST
    query -- so they pull bytes straight from the provider's memoized source
    cache instead of forcing a parse and a tree walk for every file in a
    scanned tree. The read itself is still shared: a file another rule also
    inspects is read exactly once for the whole run. Unlike ``grep -r``, an
    unreadable file is reported rather than skipped, so a scan cannot be
    evaded by making a file unreadable.
    """
    text, read_error = provider.source_cache.read(path)
    if text is None:
        return (), (_report(rule_id, path, f"cannot inspect: {read_error}"),)
    return tuple(enumerate(text.splitlines(), start=1)), ()


def _has_text(lines: Sequence[tuple[int, str]], needle: str) -> bool:
    """Return whether any line contains `needle` (``grep -Fq``)."""
    return any(needle in text for _, text in lines)


def _has_re(lines: Sequence[tuple[int, str]], pattern: re.Pattern[str]) -> bool:
    """Return whether any line matches `pattern` (``grep -Eq``)."""
    return any(pattern.search(text) is not None for _, text in lines)


def _count_re(lines: Sequence[tuple[int, str]], pattern: re.Pattern[str]) -> int:
    """Return how many lines match `pattern` (``grep -Ec``)."""
    return sum(1 for _, text in lines if pattern.search(text) is not None)


def _first_line(lines: Sequence[tuple[int, str]], needle: str) -> int:
    """Return the first line number containing `needle`, else 1."""
    for number, text in lines:
        if needle in text:
            return number
    return 1


def _matches(
    lines: Sequence[tuple[int, str]],
    pattern: re.Pattern[str],
    *,
    respect_exempt: bool,
) -> tuple[tuple[int, int], ...]:
    """Return ``(line, column)`` for every match (``grep -En`` + exempt filter).

    `respect_exempt` mirrors whether the legacy grep pipeline filtered
    ``architecture-authority-exempt:`` lines; guards that deliberately did not
    offer an exemption escape hatch keep that stricter behavior here.
    """
    hits: list[tuple[int, int]] = []
    for number, text in lines:
        if respect_exempt and EXEMPT_MARKER in text:
            continue
        match = pattern.search(text)
        if match is not None:
            hits.append((number, match.start() + 1))
    return tuple(hits)


def _after_context(
    lines: Sequence[tuple[int, str]], needle: str, count: int
) -> tuple[tuple[int, str], ...]:
    """Return every match plus its `count` trailing lines (``grep -A<count>``).

    Overlapping windows are merged exactly once, as GNU grep does, so a
    presence check over the result cannot double-count a shared region.
    """
    selected: dict[int, str] = {}
    total = len(lines)
    for index, (_, text) in enumerate(lines):
        if needle not in text:
            continue
        for offset in range(index, min(index + count + 1, total)):
            number, line_text = lines[offset]
            selected[number] = line_text
    return tuple(sorted(selected.items()))


def _awk_match_index(text: str, probe: re.Pattern[str]) -> int:
    """Return awk's 1-based ``match()`` index for `probe`, 0 when unmatched."""
    found = probe.search(text)
    return found.start() + 1 if found is not None else 0


def _indent_scoped_branch(
    lines: Sequence[tuple[int, str]],
    *,
    start: re.Pattern[str],
    terminator: re.Pattern[str],
    probe: re.Pattern[str],
    include_start: bool,
    restart_skips: bool,
) -> tuple[tuple[int, str], ...]:
    """Capture one indentation-scoped branch, as the legacy awk filters did.

    Capture opens at `start` and closes at the first `terminator` line whose
    first non-indent column equals the opening line's -- the awk
    ``branch_indent=match($0, /[^ ]/)`` comparison. `include_start` and
    `restart_skips` distinguish the two legacy variants: the GitLab facade
    filter consumed its opening line via ``next``, while the cached
    Claude-Skill filter printed it.
    """
    captured: list[tuple[int, str]] = []
    branch_index: int | None = None
    for number, text in lines:
        if start.search(text) is not None:
            branch_index = _awk_match_index(text, probe)
            if restart_skips:
                continue
            if include_start:
                captured.append((number, text))
                continue
        if branch_index is None:
            continue
        if terminator.search(text) is not None and _awk_match_index(text, probe) == branch_index:
            break
        captured.append((number, text))
    return tuple(captured)


def _tree_python_paths(
    provider: FactsProvider, prefix: str, *, excluded: Iterable[str] = ()
) -> tuple[str, ...]:
    """Return inventory Python paths under `prefix`, minus `excluded`."""
    skip = frozenset(excluded)
    return tuple(
        path
        for path in provider.inventory
        if path.startswith(prefix) and path.endswith(".py") and path not in skip
    )


def _report(rule_id: str, path: str, message: str, line: int = 1, column: int = 1) -> Violation:
    """Return one violation attributed to a specific file position."""
    return violation(rule_id, path, message, line=line, column=column)


def _banned(
    provider: FactsProvider,
    *,
    rule_id: str,
    paths: Sequence[str],
    pattern: re.Pattern[str],
    message: str,
    configured: bool = True,
    respect_exempt: bool,
) -> list[Violation]:
    """Report every banned-pattern hit across `paths`, one violation per hit."""
    findings: list[Violation] = []
    for path in paths:
        if configured:
            lines, failures = _configured(provider, path, rule_id)
            findings.extend(failures)
            if failures:
                continue
        else:
            lines, failures = _scanned(provider, path, rule_id)
            findings.extend(failures)
            if failures:
                continue
        for line, column in _matches(lines, pattern, respect_exempt=respect_exempt):
            findings.append(_report(rule_id, path, message, line, column))
    return findings


def _require_all(
    rule_id: str,
    path: str,
    lines: Sequence[tuple[int, str]],
    needles: Sequence[str],
    message: str,
) -> list[Violation]:
    """Require every literal fragment, reporting one violation per absence."""
    return [
        _report(rule_id, path, f"{message}; missing: {needle}")
        for needle in needles
        if not _has_text(lines, needle)
    ]


_SKILL_INTEGRATOR = "src/apm_cli/integration/skill_integrator.py"


_POLICY_DISCOVERY = "src/apm_cli/policy/discovery.py"


_ELSE_TERMINATOR = re.compile(r"^\s*else:")


_APM_RESOLVER = "src/apm_cli/deps/apm_resolver.py"


_DEPS_LOCKFILE = "src/apm_cli/deps/lockfile.py"


def _semantic_rule(rule_id: str, description: str, check) -> Rule:
    """Build one guard-less rule whose id is its stable semantic name."""
    return Rule(
        id=rule_id,
        group=GROUP,
        guard_ids=(),
        description=description,
        check=check,
    )
