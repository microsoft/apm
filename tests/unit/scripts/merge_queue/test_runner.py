"""Merge-queue v2 shadow runner: ticks build and prove trains, never write to main."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scripts.merge_queue import metrics, runner, state
from scripts.merge_queue.api import REPO, Response
from tests.unit.scripts.merge_queue._fake_api import FakeApi

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-validation-topology")]

MAIN = "0" * 40
HEADS = {21: "a" * 40, 22: "c" * 40}


def _clock():
    times = iter(f"2026-11-21T19:{minute:02d}:00Z" for minute in range(60))
    return lambda: next(times)


def _repo(verdict: str | None = None) -> FakeApi:
    api = FakeApi()
    api.routes[f"repos/{REPO}/commits/main"] = {"sha": MAIN}
    api.routes[f"repos/{REPO}/git/matching-refs/heads/queue"] = []
    api.routes[f"repos/{REPO}/git/matching-refs/heads/queue/t"] = []
    api.routes[f"repos/{REPO}/git/matching-refs/heads/queue-build/t"] = []
    api.routes[f"repos/{REPO}/actions/workflows/merge-train.yml/runs"] = {"workflow_runs": []}
    api.routes[f"repos/{REPO}/issues"] = [{"number": pr, "pull_request": {}} for pr in HEADS] + [
        {"number": 30}
    ]
    for pr, head in HEADS.items():
        api.routes[f"repos/{REPO}/pulls/{pr}"] = {
            "head": {"sha": head, "repo": {"full_name": REPO}},
            "base": {"ref": "main"},
            "labels": [{"name": "queue:ready"}],
        }
        api.routes[f"repos/{REPO}/commits/{head}/check-runs"] = {
            "check_runs": [
                {"name": "gate", "conclusion": "success", "status": "completed"},
                {"name": "Analyze (python)", "conclusion": "success", "status": "completed"},
            ]
        }
        api.routes[f"repos/{REPO}/issues/{pr}/comments"] = [
            {
                "body": f"<!-- apm-queue-ticket head={head} prio=P3 fixes=[] depends=[] -->",
                "created_at": f"2026-11-21T10:{pr}:00Z",
            }
        ]
        api.routes[f"repos/{REPO}/pulls/{pr}/files"] = [{"filename": f"src/f{pr}.py", "changes": 3}]
    shas = (f"{n:040x}" for n in range(1, 99))

    def merge(path, body):
        if not path.endswith("/merges"):
            return Response(201, {})
        sha = next(shas)
        return Response(201, {"sha": sha, "commit": {"tree": {"sha": "f" + sha[1:]}}})

    api.post_handler = merge
    if verdict:
        api.routes[f"repos/{REPO}/actions/workflows/merge-train.yml/runs"] = {
            "workflow_runs": [
                {
                    "id": 99,
                    "head_sha": f"{2:040x}",
                    "head_branch": "queue/t0001/p2-full",
                    "status": "completed",
                    "conclusion": verdict,
                    "created_at": "x",
                }
            ]
        }
    return api


def _writes(api: FakeApi) -> list[str]:
    return [
        f"{method} {path} {body.get('ref') or body.get('base') if body else ''}"
        for method, path, body in api.calls
        if method != "GET"
    ]


def test_a_tick_builds_and_publishes_one_train_without_touching_main() -> None:
    api, current, log = _repo(), state.State(), runner.Log(None, _clock())
    runner.tick(api, current, log)
    item = current.trains[1]
    assert [m.pr for m in item.members] == [21, 22]
    assert item.refs == ["queue/t0001/p1-guard", "queue/t0001/p2-full"]
    assert _writes(api) == [
        f"POST repos/{REPO}/git/refs refs/heads/queue-build/t0001",
        f"POST repos/{REPO}/merges queue-build/t0001",
        f"POST repos/{REPO}/merges queue-build/t0001",
        f"POST repos/{REPO}/git/refs refs/heads/queue/t0001/p1-guard",
        f"POST repos/{REPO}/git/refs refs/heads/queue/t0001/p2-full",
    ]
    assert [e["event"] for e in log.events] == ["published"]


def test_a_green_proof_logs_would_land_per_member_and_cleans_the_build_ref() -> None:
    api, current, log = _repo("success"), state.State(), runner.Log(None, _clock())
    runner.tick(api, current, log, depth=1)
    runner.tick(api, current, log, depth=0)
    assert current.trains[1].status == state.GREEN and current.trains[1].run_id == 99
    landings = [(e["pr"], e["commit"], e["tree"]) for e in log.events if e["event"] == "would-land"]
    assert landings == [
        (21, f"{1:040x}", "f" + f"{1:040x}"[1:]),
        (22, f"{2:040x}", "f" + f"{2:040x}"[1:]),
    ]
    assert f"DELETE repos/{REPO}/git/refs/heads/queue-build/t0001 " in _writes(api)
    assert current.window == 5


def test_a_red_proof_halves_the_window_and_lands_nothing() -> None:
    api, current, log = _repo("failure"), state.State(window=6), runner.Log(None, _clock())
    runner.tick(api, current, log, depth=1)
    runner.tick(api, current, log, depth=0)
    assert current.trains[1].status == state.RED and current.window == 3
    assert not [e for e in log.events if e["event"] == "would-land"]


def test_queued_prs_are_not_composed_twice() -> None:
    api, current, log = _repo(), state.State(), runner.Log(None, _clock())
    runner.tick(api, current, log, depth=2)
    runner.tick(api, current, log, depth=2)
    assert list(current.trains) == [1]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", f"repos/{REPO}/pulls/21/merge", {}),
        ("post", f"repos/{REPO}/git/refs", {"ref": "refs/heads/main", "sha": MAIN}),
        ("post", f"repos/{REPO}/merges", {"base": "main", "head": MAIN}),
        ("post", f"repos/{REPO}/merges", {"ref": "refs/heads/queue/t1", "base": "main"}),
        ("post", f"repos/{REPO}/merges", {"base": "queue/t0001/p1-full", "head": MAIN}),
        ("delete", f"repos/{REPO}/git/refs/heads/main", None),
        ("delete", f"repos/{REPO}/git/refs/tags/v1", None),
    ],
)
def test_the_shadow_guard_refuses_every_write_outside_queue_refs(method, path, body) -> None:
    guard = runner.ShadowApi(FakeApi())
    with pytest.raises(runner.ShadowViolation):
        guard.post(path, body) if method == "post" else guard.delete(path)


def test_only_one_runner_holds_the_lock(tmp_path) -> None:
    lock = tmp_path / "runner.lock"
    with runner.single_instance(lock) as first, runner.single_instance(lock) as second:
        assert (first, second) == (True, False)
    assert runner.main(["tick", "--state-dir", str(tmp_path)], api=_repo()) == 0
    assert (tmp_path / "state.json").is_file() and (tmp_path / "heartbeat").is_file()


def test_metrics_summarize_the_shadow_log() -> None:
    events = [
        {"at": "2026-11-21T19:00:00Z", "event": "published", "train": 1},
        {"at": "2026-11-21T19:20:00Z", "event": "verdict", "train": 1, "verdict": "green"},
        {"at": "2026-11-21T19:20:00Z", "event": "would-land", "train": 1},
        {"at": "2026-11-21T19:21:00Z", "event": "published", "train": 2},
        {"at": "2026-11-21T19:51:00Z", "event": "verdict", "train": 2, "verdict": "red"},
        {"at": "2026-11-21T19:51:00Z", "event": "deferred", "train": 2},
    ]
    assert metrics.summarize(events) == {
        "trains_published": 2,
        "verdicts": {"green": 1, "red": 1},
        "green_rate": 0.5,
        "would_land": 1,
        "deferred": 1,
        "writes_to_main": 0,
        "mean_train_minutes": 25.0,
    }


def test_a_stacked_train_starts_on_the_previous_head_but_keeps_main_as_queue_base() -> None:
    api, current, log = _repo(), state.State(), runner.Log(None, _clock())
    first = state.Train(1, MAIN, [state.Member(9, "9" * 40, 1, "e" * 40)], ["queue/t0001/p1-full"])
    current.trains[1], current.max_id = first, 1
    runner.tick(api, current, log, depth=2)
    created = [b for m, p, b in api.calls if m == "POST" and p.endswith("/git/refs")]
    assert created[0] == {"ref": "refs/heads/queue-build/t0002", "sha": "e" * 40}
    merges = [b["commit_message"] for m, p, b in api.calls if m == "POST" and p.endswith("/merges")]
    assert all(f"Queue-Base: {MAIN}" in message for message in merges)
    assert current.trains[2].base == MAIN


def test_a_failed_publication_rolls_back_and_records_no_train() -> None:
    api, current, log = _repo(), state.State(), runner.Log(None, _clock())
    inner = api.post_handler

    def refuse_full(path, body):
        if str(body.get("ref", "")).endswith("-full"):
            return Response(422, "Reference update failed")
        return inner(path, body)

    api.post_handler = refuse_full
    runner.tick(api, current, log)
    assert current.trains == {}
    deleted = [p.rsplit("/heads/", 1)[1] for m, p, b in api.calls if m == "DELETE"]
    assert deleted == ["queue/t0001/p1-guard", "queue-build/t0001"]
    assert [e["reason"] for e in log.events if e["event"] == "launch-failed"] == [
        "ref publication failed"
    ]


def test_a_merged_dependency_counts_as_satisfied() -> None:
    api, current, log = _repo(), state.State(), runner.Log(None, _clock())
    api.routes[f"repos/{REPO}/issues/22/comments"] = [
        {
            "body": f"<!-- apm-queue-ticket head={HEADS[22]} prio=P3 fixes=[] depends=[#5] -->",
            "created_at": "2026-11-21T10:22:00Z",
        }
    ]
    api.routes[f"repos/{REPO}/pulls/5"] = {"merged": True}
    runner.tick(api, current, log)
    assert [m.pr for m in current.trains[1].members] == [21, 22]


def test_the_janitor_sweeps_old_untracked_queue_refs() -> None:
    api, current, log = _repo(), state.State(), runner.Log(None, _clock())
    api.routes[f"repos/{REPO}/git/matching-refs/heads/queue-build/t"] = [
        {"ref": "refs/heads/queue-build/t0040", "object": {"sha": "4" * 40}}
    ]
    api.routes[f"repos/{REPO}/commits/{'4' * 40}"] = {
        "commit": {"committer": {"date": "2026-11-20T01:00:00Z"}}
    }
    now = datetime(2026, 11, 21, 20, tzinfo=UTC)
    runner.tick(api, current, log, depth=0, now=now)
    assert f"DELETE repos/{REPO}/git/refs/heads/queue-build/t0040 " in _writes(api)
    assert [e["ref"] for e in log.events if e["event"] == "janitor"] == ["queue-build/t0040"]


def test_a_restart_with_a_stale_cache_adopts_the_live_train(tmp_path) -> None:
    api = _repo()
    stale = state.State(max_id=0)
    state.save(stale, tmp_path / "state.json")
    api.routes[f"repos/{REPO}/git/matching-refs/heads/queue/t"] = [
        {"ref": "refs/heads/queue/t0001/p1-full", "object": {"sha": "7" * 40}}
    ]
    api.routes[f"repos/{REPO}/commits/{'7' * 40}"] = {
        "commit": {
            "message": f"q\n\nApm-Train: t0001 k=1 pr=#21 head={HEADS[21]} base={MAIN}",
            "tree": {"sha": "8" * 40},
            "committer": {"date": "2026-11-21T19:00:00Z"},
        },
        "parents": [{"sha": MAIN}],
    }
    assert runner.main(["tick", "--state-dir", str(tmp_path), "--depth", "1"], api=api) == 0
    recovered = state.load(tmp_path / "state.json")
    assert [m.pr for m in recovered.trains[1].members] == [21] and list(recovered.trains) == [1]


def test_a_build_where_every_member_conflicts_deletes_its_build_ref() -> None:
    api, current, log = _repo(), state.State(), runner.Log(None, _clock())
    api.post_handler = lambda path, body: (
        Response(409, "conflict") if path.endswith("/merges") else Response(201, {})
    )
    runner.tick(api, current, log)
    assert current.trains == {}
    assert f"DELETE repos/{REPO}/git/refs/heads/queue-build/t0001 " in _writes(api)
    assert [e["reason"] for e in log.events if e["event"] == "launch-failed"] == [
        "no member merged"
    ]
