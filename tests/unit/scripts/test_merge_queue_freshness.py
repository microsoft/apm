"""Merge-queue freshness alarm: main must not move without a merge_group run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import merge_queue_freshness

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-LAW-outcome.status_matches_state")]

NOW = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
REPO = "o/r"


def _fake(last_run_age: timedelta | None, commits_by_since: dict[str, list[str]]):
    calls: list[tuple[str, dict[str, list[str]]]] = []

    def fetch(path: str) -> object:
        parts = urlsplit(path)
        query = parse_qs(parts.query)
        calls.append((parts.path, query))
        if parts.path == f"repos/{REPO}/actions/workflows/ci-integration.yml/runs":
            assert query == {"event": ["merge_group"], "status": ["completed"], "per_page": ["1"]}
            if last_run_age is None:
                return {"workflow_runs": []}
            completed = (NOW - last_run_age).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {
                "workflow_runs": [
                    {
                        "id": 42,
                        "html_url": "https://github.com/o/r/actions/runs/42",
                        "created_at": completed,
                        "updated_at": completed,
                    }
                ]
            }
        assert parts.path == f"repos/{REPO}/commits"
        assert query["sha"] == ["main"]
        return [{"sha": sha} for sha in commits_by_since.get(query["since"][0], [])]

    return fetch, calls


WINDOW_START = "2026-11-17T08:00:00Z"
LAST_RUN = "2026-09-09T08:00:00Z"


def test_recent_merge_group_run_is_fresh_without_listing_commits() -> None:
    fetch, calls = _fake(timedelta(days=1), {WINDOW_START: ["a" * 40]})

    verdict = merge_queue_freshness.evaluate(fetch, REPO, "main", 3, NOW)

    assert verdict.fresh
    assert len(calls) == 1


def test_quiet_main_is_fresh_even_without_merge_group_runs() -> None:
    fetch, _ = _fake(None, {})

    assert merge_queue_freshness.evaluate(fetch, REPO, "main", 3, NOW).fresh


def test_bypassed_queue_names_last_run_and_commits_since_it(capsys) -> None:
    fetch, _ = _fake(timedelta(days=72), {WINDOW_START: ["b" * 40], LAST_RUN: ["b" * 40, "c" * 40]})

    code = merge_queue_freshness.main(
        {"REPOSITORY": REPO, "BRANCH": "main", "WINDOW_DAYS": "3"}, fetch=fetch, now=NOW
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "last merge_group run 42 completed 2026-09-09T08:00:00Z" in out
    assert "https://github.com/o/r/actions/runs/42" in out
    assert f"2 main commit(s) since {LAST_RUN}" in out
    assert "b" * 40 in out and "c" * 40 in out
    assert "::error title=Merge queue is not gating main::" in out


def test_never_queued_main_fails_and_lists_window_commits(capsys) -> None:
    fetch, _ = _fake(None, {WINDOW_START: ["d" * 40]})

    code = merge_queue_freshness.main({"REPOSITORY": REPO}, fetch=fetch, now=NOW)

    out = capsys.readouterr().out
    assert code == 1
    assert "no completed merge_group run of ci-integration.yml was found" in out
    assert f"1 main commit(s) since {WINDOW_START}" in out


def test_run_exactly_at_window_edge_is_fresh() -> None:
    fetch, _ = _fake(timedelta(days=3), {WINDOW_START: ["e" * 40]})

    assert merge_queue_freshness.evaluate(fetch, REPO, "main", 3, NOW).fresh
