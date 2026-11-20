"""Guards for the session-scoped architecture-linter cache (#4119, PERF-H6).

Owner: ``architecture-linter-performance`` (scripts/architecture_linter/session_cache.py).

* Guard 1 (transparency): every case's findings are byte-equal with and
  without the session. The always-on test covers the first owner case of
  each rule group; the ``benchmark`` test replays the full owner mutation
  matrix and runs in the scheduled nightly integration lane.
* Guard 2 (parse once): replaying the full mutation matrix in one session
  builds every ``(path, content)`` exactly once and walks the tree once.

Both guards replay every case except the two that mutate the cache's own
source: those prove the cache rules' teeth in the owner matrix, and replaying
them here would test a mutated cache with itself.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import pytest

from scripts.architecture_linter.models import RunReport
from scripts.architecture_linter.runner import registered_rules, run_selected_rules
from scripts.architecture_linter.session_cache import linter_session
from tests.integration.test_architecture_owner_rule_mutations import (
    EXECUTABLE_MUTATIONS,
    ROOT,
    MutationCase,
    _mutate,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-architecture-linter-performance"),
]

_CACHE_GUARD_IDS = (
    "contracts-tooling-linter-cache-content-key",
    "contracts-tooling-linter-cache-parse-once",
)


def _findings_bytes(report: RunReport) -> bytes:
    return repr((report.violations, report.failures, report.exit_code)).encode("ascii")


_REPLAYED_CASES = tuple(
    case for case in EXECUTABLE_MUTATIONS if case.guard_id not in _CACHE_GUARD_IDS
)


def _sample_cases() -> tuple[MutationCase, ...]:
    group_by_rule = {rule.id: rule.group for rule in registered_rules()}
    seen_groups: set[str] = set()
    sample: list[MutationCase] = []
    for case in _REPLAYED_CASES:
        group = group_by_rule[case.rule_id]
        if group not in seen_groups:
            seen_groups.add(group)
            sample.append(case)
    return tuple(sample)


def _assert_cache_transparent(cases: Sequence[MutationCase]) -> str:
    """Replay `cases` in one shared session and compare each to a cold run.

    Each rule first runs unmutated inside the session, so the mutated run
    must reuse every untouched file and rebuild only the overridden one.
    Returns the digest of the cached findings for evidence.
    """
    mismatches: list[str] = []
    digest = hashlib.sha256()
    cold_reports = [
        _findings_bytes(
            run_selected_rules(ROOT, (case.rule_id,), source_overrides={case.path: _mutate(case)})
        )
        for case in cases
    ]
    with linter_session():
        warmed: set[str] = set()
        for index, case in enumerate(cases):
            if case.rule_id not in warmed:
                run_selected_rules(ROOT, (case.rule_id,))
                warmed.add(case.rule_id)
            cached = _findings_bytes(
                run_selected_rules(
                    ROOT, (case.rule_id,), source_overrides={case.path: _mutate(case)}
                )
            )
            digest.update(cached)
            if cached != cold_reports[index]:
                mismatches.append(f"{case.guard_id}: {case.intent}")
    assert not mismatches, f"session cache changed findings for: {mismatches}"
    return digest.hexdigest()


def test_session_cache_findings_match_uncached_runs_per_group() -> None:
    """Guard 1 (always-on sample): cached findings are byte-equal to cold runs."""
    _assert_cache_transparent(_sample_cases())


@pytest.mark.benchmark
def test_session_cache_findings_match_uncached_runs_full_matrix() -> None:
    """Guard 1 (full matrix): every owner mutation case is byte-equal cached and cold."""
    digest = _assert_cache_transparent(_REPLAYED_CASES)
    print(f"full-matrix cached findings sha256={digest} cases={len(_REPLAYED_CASES)}")


def test_session_parses_each_file_content_at_most_once_over_full_matrix() -> None:
    """Guard 2: one session builds each (path, content) once and walks once."""
    runs = [(case, {case.path: _mutate(case)}) for case in _REPLAYED_CASES]
    runs += [(case, None) for case in _REPLAYED_CASES]
    with linter_session() as session:
        for case, overrides in runs:
            run_selected_rules(ROOT, (case.rule_id,), source_overrides=overrides)
            reparsed = sorted(key[0] for key, count in session.fact_builds.items() if count > 1)
            assert not reparsed, f"{case.guard_id}: session re-parsed {reparsed[:10]}"

    assert session.fact_builds, "the session built no facts"
    assert session.max_builds_per_content == 1
    assert session.fact_hits > 0
    assert session.inventory_builds == 1
    assert session.inventory_hits == 2 * len(_REPLAYED_CASES) - 1
