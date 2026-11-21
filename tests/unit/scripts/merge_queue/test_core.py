"""Merge-queue v2 runner core: tickets, composition, refs, proofs and state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.merge_queue import proof, refs, state, tickets, train
from scripts.merge_queue.api import REPO, Response
from tests.unit.scripts.merge_queue._fake_api import FakeApi

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-validation-topology")]

HEAD = "a" * 40
BASE = "b" * 40


def _ticket(pr: int, prio: str = "P3", at: str = "2026-11-21T10:00:00Z", **extra) -> tickets.Ticket:
    return tickets.Ticket(pr, HEAD, prio, at, **extra)


def test_ticket_marker_parses_lane_fixes_and_depends() -> None:
    body = f"<!-- apm-queue-ticket head={HEAD} prio=P1 fixes=[#12, 13] depends=[#7] -->"
    ticket = tickets.parse_ticket(5, body, "2026-11-21T10:00:00Z")
    assert ticket == tickets.Ticket(5, HEAD, "P1", "2026-11-21T10:00:00Z", (12, 13), (7,))
    assert tickets.parse_ticket(5, "no marker here", "t") is None


def test_the_newest_ticket_wins() -> None:
    old = f"<!-- apm-queue-ticket head={'c' * 40} prio=P3 fixes=[] depends=[] -->"
    new = f"<!-- apm-queue-ticket head={HEAD} prio=P2 fixes=[] depends=[] -->"
    comments = [
        {"body": new, "created_at": "2026-11-21T11:00:00Z"},
        {"body": old, "created_at": "2026-11-21T10:00:00Z"},
    ]
    assert tickets.latest_ticket(9, comments).head == HEAD


FACTS = tickets.PrFacts(HEAD, (tickets.READY_LABEL,), "main", True, True, True)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({}, None),
        ({"labels": ()}, "missing the queue:ready label"),
        ({"head": "c" * 40}, "head moved"),
        ({"base": "release/0.38"}, "base is release/0.38"),
        ({"same_repo": False}, "fork PR"),
        ({"gate_green": False}, "gate is not green"),
        ({"codeql_done": False}, "CodeQL has not completed"),
    ],
)
def test_admission_names_the_first_blocking_reason(change: dict, reason: str | None) -> None:
    facts = tickets.PrFacts(**{**FACTS.__dict__, **change})
    got = tickets.admission(_ticket(1), facts)
    assert (got is None) if reason is None else (reason in got)


def test_compose_orders_by_lane_then_age_and_caps_the_window() -> None:
    ready = [
        _ticket(1, "P3", "2026-11-21T09:00:00Z"),
        _ticket(2, "P1", "2026-11-21T11:00:00Z"),
        _ticket(3, "P3", "2026-11-21T08:00:00Z"),
        _ticket(4, "P0", "2026-11-21T12:00:00Z"),
    ]
    assert [t.pr for t in train.compose(ready, window=3, landed=set())] == [4, 2, 3]


def test_compose_holds_a_dependent_until_its_dependency_is_aboard_or_landed() -> None:
    ready = [
        _ticket(5, at="2026-11-21T08:00:00Z", depends=(6,)),
        _ticket(6, at="2026-11-21T09:00:00Z"),
    ]
    assert [t.pr for t in train.compose(ready, 4, landed=set())] == [6]
    assert [t.pr for t in train.compose(ready, 4, landed={6})] == [5, 6]


def test_compose_takes_one_big_pr_first_in_its_lane() -> None:
    ready = [
        _ticket(1, at="2026-11-21T08:00:00Z"),
        _ticket(2, at="2026-11-21T09:00:00Z", changed_lines=2000),
        _ticket(3, at="2026-11-21T10:00:00Z", changed_lines=3000),
    ]
    assert [t.pr for t in train.compose(ready, 4, landed=set())] == [2, 1]


def test_a_workflow_change_trains_alone() -> None:
    wf = ".github/workflows/ci.yml"
    first = [
        _ticket(1, at="2026-11-21T08:00:00Z", files=(wf,)),
        _ticket(2, at="2026-11-21T09:00:00Z"),
    ]
    later = [
        _ticket(1, at="2026-11-21T08:00:00Z"),
        _ticket(2, at="2026-11-21T09:00:00Z", files=(wf,)),
    ]
    assert [t.pr for t in train.compose(first, 4, set())] == [1]
    assert [t.pr for t in train.compose(later, 4, set())] == [1]


@pytest.mark.parametrize(
    ("window", "outcome", "expected"),
    [
        (4, "green", 5),
        (6, "green", 6),
        (6, "red-unattributed", 3),
        (2, "red-unattributed", 2),
        (5, "red-attributed", 5),
    ],
)
def test_window_adapts_like_zuul(window: int, outcome: str, expected: int) -> None:
    assert train.next_window(window, outcome) == expected


def test_ref_names_round_trip_and_batch_puts_full_on_the_head() -> None:
    assert refs.prefix_refs(7, 3) == [
        "queue/t0007/p1-guard",
        "queue/t0007/p2-guard",
        "queue/t0007/p3-full",
    ]
    assert refs.prefix_refs(7, 2, pipeline=True) == ["queue/t0007/p1-full", "queue/t0007/p2-full"]
    assert refs.parse_train_ref("queue/t0007/p3-full") == refs.TrainRef(7, 3, "full")
    assert refs.parse_train_ref("queue/b0123456789ab/p0-full") is None
    assert refs.base_probe_ref("0123456789abcdef") == "queue/b0123456789ab/p0-full"


def test_train_ids_stay_monotonic_across_state_and_live_refs() -> None:
    assert refs.next_train_id(["queue/t0009/p1-full", "queue-build/t0011", "main"], 5) == 12
    assert refs.next_train_id([], 5) == 6


def test_janitor_deletes_only_old_untracked_queue_refs() -> None:
    now = datetime(2026, 11, 21, 20, tzinfo=UTC)
    old, fresh = now - timedelta(hours=13), now - timedelta(hours=1)
    listed = [
        ("queue/t0001/p1-full", old),
        ("queue/t0002/p1-full", old),
        ("queue/t0003/p1-full", fresh),
        ("feature", old),
    ]
    assert refs.janitor(listed, known_ids={2}, now=now) == ["queue/t0001/p1-full"]


def _runs(*runs: dict) -> dict:
    return {"workflow_runs": list(runs)}


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        (_runs(), proof.MISSING),
        (
            _runs(
                {
                    "id": 1,
                    "head_sha": HEAD,
                    "head_branch": "queue/t0001/p1-full",
                    "status": "in_progress",
                    "created_at": "1",
                }
            ),
            proof.PENDING,
        ),
        (
            _runs(
                {
                    "id": 1,
                    "head_sha": HEAD,
                    "head_branch": "queue/t0001/p1-full",
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "1",
                    "run_attempt": 1,
                },
                {
                    "id": 1,
                    "head_sha": HEAD,
                    "head_branch": "queue/t0001/p1-full",
                    "status": "completed",
                    "conclusion": "success",
                    "created_at": "1",
                    "run_attempt": 2,
                },
            ),
            proof.GREEN,
        ),
        (
            _runs(
                {
                    "id": 2,
                    "head_sha": "c" * 40,
                    "head_branch": "queue/t0001/p1-full",
                    "status": "completed",
                    "conclusion": "success",
                    "created_at": "1",
                }
            ),
            proof.MISSING,
        ),
    ],
)
def test_verdict_reads_the_newest_run_on_the_exact_branch_and_sha(
    runs: dict, expected: str
) -> None:
    api = FakeApi()
    path = f"repos/{REPO}/actions/workflows/merge-train.yml/runs"
    api.routes[path] = runs
    assert proof.verdict(api, "queue/t0001/p1-full", HEAD)[0] == expected
    assert api.queries(path) == [
        {"branch": ["queue/t0001/p1-full"], "head_sha": [HEAD], "per_page": ["5"]}
    ]


def test_build_merges_members_in_order_and_defers_a_conflict() -> None:
    api = FakeApi()
    shas = iter(["1" * 40, "3" * 40])

    def handler(path, body):
        if path.endswith("/git/refs"):
            return Response(201, {})
        return (
            Response(409, "conflict")
            if "#2" in body["commit_message"]
            else Response(201, {"sha": next(shas)})
        )

    api.post_handler = handler
    result = train.build(api, 4, BASE, [_ticket(1), _ticket(2), _ticket(3)])
    assert [t.pr for t in result.members] == [1, 3] and result.prefix_shas == ["1" * 40, "3" * 40]
    assert list(result.deferred) == [2]
    first_merge = api.calls[1][2]
    assert first_merge["base"] == "queue-build/t0004"
    assert f"Apm-Train: t0004 k=1 pr=#1 head={HEAD} base={BASE}" in first_merge["commit_message"]
    assert f"Queue-Base: {BASE}" in first_merge["commit_message"]


def test_train_status_moves_forward_only() -> None:
    item = state.Train(1, BASE, [state.Member(1, HEAD, 1, "1" * 40)], ["queue/t0001/p1-full"])
    item.move(state.GREEN)
    with pytest.raises(ValueError, match="not allowed"):
        item.move(state.TESTING)


def test_state_round_trips_and_rebuilds_from_refs_and_trailers(tmp_path) -> None:
    api = FakeApi()
    api.routes[f"repos/{REPO}/git/matching-refs/heads/queue/t"] = [
        {"ref": "refs/heads/queue/t0003/p2-full", "object": {"sha": "2" * 40}},
        {"ref": "refs/heads/queue/t0003/p1-guard", "object": {"sha": "1" * 40}},
    ]
    for k, sha, pr in ((1, "1" * 40, 10), (2, "2" * 40, 11)):
        api.routes[f"repos/{REPO}/commits/{sha}"] = {
            "commit": {
                "message": f"queue\n\nApm-Train: t0003 k={k} pr=#{pr} head={HEAD} base={BASE}\nQueue-Base: {BASE}"
            }
        }
    rebuilt = state.rebuild(api)
    assert rebuilt.max_id == 3
    item = rebuilt.trains[3]
    assert [(m.pr, m.k) for m in item.members] == [(10, 1), (11, 2)]
    assert item.head_ref == "queue/t0003/p2-full" and item.base == BASE
    path = tmp_path / "state.json"
    state.save(rebuilt, path)
    assert state.load(path) == rebuilt
