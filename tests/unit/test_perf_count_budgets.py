"""The count-budget table and its comparison (perf promise engine, layer 1).

Every budget row carries a reason and the PR that set it, and sits within the
slack of its measured count, so a budget only moves in an explicit, re-measured
PR. The comparison fails a counter over budget or without a row, and turns a
counter well under budget into a "tighten" note.
"""

from __future__ import annotations

import pytest

from tests.fixtures.perf.count_budgets import (
    JOURNEYS,
    compare,
    flatten,
    load_budgets,
    row_problems,
    slack,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-PERF-count-budgets")]


def _row(measured: int, **extra) -> dict:
    return {
        "budget": measured + slack(measured),
        "measured": measured,
        "reason": "r",
        "pr": "#1",
    } | extra


def test_every_budget_row_has_a_reason_a_pr_and_a_budget_within_slack() -> None:
    assert row_problems(load_budgets()) == []


def test_a_row_without_reason_or_pr_or_beyond_its_slack_is_refused() -> None:
    journeys = {name: {"git.clone": _row(20)} for name in JOURNEYS}
    journeys["install-cold"] = {
        "git.clone": _row(20, reason=" "),
        "http.requests": _row(41, pr="5245"),
        "fs.stat_calls": _row(100, budget=200),
    }
    problems = row_problems(journeys)
    assert "install-cold git.clone: no reason" in problems
    assert "install-cold http.requests: pr must be '#<number>', got '5245'" in problems
    assert any(p.startswith("install-cold fs.stat_calls: budget 200 exceeds") for p in problems)
    assert row_problems({}) == [f"journey {name} has no budget rows" for name in JOURNEYS]


def test_slack_is_one_or_five_percent() -> None:
    assert [slack(n) for n in (0, 10, 20, 21, 1000)] == [1, 1, 1, 2, 50]


def test_over_budget_and_unbudgeted_counters_fail_with_the_diff() -> None:
    rows = {"git.clone": _row(20), "http.requests": _row(41)}
    over, tighten = compare(
        "install-cold", {"git.clone": 22, "http.requests": 41, "git.fetch": 3}, rows
    )
    assert over == [
        "install-cold git.clone: budget 21, observed 22 (+1); a regression, or a justified "
        "change that needs a budget row with reason and pr",
        "install-cold git.fetch: unbudgeted, observed 3 (+3); a regression, or a justified "
        "change that needs a budget row with reason and pr",
    ]
    assert tighten == []


def test_a_counter_well_under_budget_is_a_tighten_note_not_a_failure() -> None:
    over, tighten = compare("install-noop", {"fs.stat_calls": 900}, {"fs.stat_calls": _row(1000)})
    assert over == []
    assert tighten == [
        "install-noop fs.stat_calls: observed 900, budget 1050: tighten to 945 in a PR"
    ]
    # Within the slack of the measured count (run-to-run jitter): neither.
    for jitter in (1000, 960, 1049):
        rows = {"fs.stat_calls": _row(1000)}
        assert compare("install-noop", {"fs.stat_calls": jitter}, rows) == ([], []), jitter


def test_flatten_names_git_verbs_http_and_fs_counters() -> None:
    counts = {"git_verbs": {"clone": 2}, "fs": {"files_written": 7, "hashed_bytes": 9}}
    flat = flatten(counts, {"requests": 4, "connects": 1, "round_trips": 6})
    assert flat["git.clone"] == 2 and flat["http.requests"] == 4 and flat["http.connects"] == 1
    assert flat["fs.files_written"] == 7 and flat["fs.lockfile_reads"] == 0
    assert "http.round_trips" not in flat
