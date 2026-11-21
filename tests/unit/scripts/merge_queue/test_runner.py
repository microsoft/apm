"""Merge-queue v2 shadow runner: ticks build and prove trains, never write to main."""

from __future__ import annotations

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
    shas = iter(["1" * 40, "2" * 40])
    api.post_handler = lambda path, body: (
        Response(201, {"sha": next(shas)}) if path.endswith("/merges") else Response(201, {})
    )
    if verdict:
        api.routes[f"repos/{REPO}/actions/workflows/merge-train.yml/runs"] = {
            "workflow_runs": [
                {
                    "id": 99,
                    "head_sha": "2" * 40,
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
    landings = [(e["pr"], e["tree"]) for e in log.events if e["event"] == "would-land"]
    assert landings == [(21, "1" * 40), (22, "2" * 40)]
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
