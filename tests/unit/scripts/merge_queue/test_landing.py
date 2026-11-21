"""Merge-queue v2 landing loop: proven trains land exactly as proven, or landing HALTs."""

from __future__ import annotations

import pytest

from scripts.merge_queue import landing, runner, state
from scripts.merge_queue.api import REPO, Response
from tests.unit.scripts.merge_queue._fake_api import FakeApi

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-validation-topology")]

TIP, TIP_TREE = "0" * 40, "9" * 40


def _sha(tag: str) -> str:
    return (tag * 40)[:40]


class World:
    """A tiny GitHub: main, PRs and issues; ``merge`` lands a PR like gh pr merge."""

    def __init__(self, prs: dict[int, tuple[str, ...]], closing: dict[int, tuple[int, ...]]):
        self.api = FakeApi()
        self.main, self.main_tree = TIP, TIP_TREE
        self.admin = True
        self.merged: dict[int, str] = {}
        self.merge_calls: list[tuple[int, str]] = []
        self.refuse: list[str] = []
        self.messages: dict[int, list[str]] = {}
        self.github_closes = True
        self.crash_after_merge = False
        self.wrong_tree = False
        self.issues: dict[int, str] = {}
        self.prs, self.closing = prs, closing
        self.trees: dict[str, str] = {TIP: TIP_TREE}
        self.parents: dict[str, list[str]] = {}
        self._route()
        self.add_commit(TIP, TIP_TREE, [])

    def _route(self) -> None:
        routes = self.api.routes
        routes[f"repos/{REPO}"] = lambda q: {"permissions": {"admin": self.admin}}
        routes[f"repos/{REPO}/commits/main"] = lambda q: self._commit(self.main)
        routes[f"repos/{REPO}/pulls"] = []
        for pr, fixes in self.prs.items():
            routes[f"repos/{REPO}/pulls/{pr}"] = lambda q, pr=pr: {
                "merged": pr in self.merged,
                "merge_commit_sha": self.merged.get(pr),
                "head": {"sha": _sha(str(pr % 10)), "ref": f"branch-{pr}"},
                "base": {"ref": "main"},
            }
            routes[f"repos/{REPO}/pulls/{pr}/commits"] = lambda q, pr=pr: [
                {"commit": {"message": m}} for m in self.messages.get(pr, ["work"])
            ]
            marker = " ".join(f"#{n}" for n in fixes)
            routes[f"repos/{REPO}/issues/{pr}/comments"] = [
                {
                    "body": f"<!-- apm-queue-ticket head={_sha(str(pr % 10))} prio=P2 "
                    f"fixes=[{marker}] depends=[] -->",
                    "created_at": "2026-11-21T10:00:00Z",
                }
            ]
        self.api.post_handler = self._graphql

    def _commit(self, sha: str) -> dict:
        return {
            "sha": sha,
            "commit": {"tree": {"sha": self.trees[sha]}},
            "parents": [{"sha": p} for p in self.parents.get(sha, [])],
        }

    def _graphql(self, path, body) -> Response:
        pr = body["variables"]["pr"]
        nodes = [{"number": n} for n in self.closing.get(pr, ())]
        data = {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": nodes}}}}
        return Response(200, {"data": data})

    def add_commit(self, sha: str, tree: str, parents: list[str]) -> None:
        self.trees[sha], self.parents[sha] = tree, parents
        self.api.routes[f"repos/{REPO}/commits/{sha}"] = lambda q, sha=sha: self._commit(sha)

    def issue(self, number: int, status: str) -> None:
        self.issues[number] = status
        self.api.routes[f"repos/{REPO}/issues/{number}"] = lambda q: {"state": self.issues[number]}

    def merge(self, pr: int, head: str) -> tuple[bool, str]:
        self.merge_calls.append((pr, head))
        if self.refuse:
            return False, self.refuse.pop(0)
        sha = _sha(f"m{pr}")[:40]
        tree = "e" * 40 if self.wrong_tree else self.proven_tree[pr]
        self.add_commit(sha, tree, [self.main, head])
        self.main, self.merged[pr] = sha, sha
        for issue in self.closing.get(pr, ()) if self.github_closes else ():
            self.issues[issue] = "closed"
        if self.crash_after_merge:
            raise RuntimeError("connection reset after the merge")
        return True, ""

    def train(self, train_id: int, prs: list[int], tip: str = TIP) -> state.Train:
        """A proven train built on ``tip``, its member commits and trees registered."""
        members, parent = [], tip
        self.proven_tree = getattr(self, "proven_tree", {})
        for k, pr in enumerate(prs, 1):
            sha, tree = _sha(f"p{pr}"), _sha(f"t{pr}")
            self.add_commit(sha, tree, [parent, _sha(str(pr % 10))])
            self.proven_tree[pr] = tree
            members.append(state.Member(pr, _sha(str(pr % 10)), k, sha, tree))
            parent = sha
        refs = [f"queue/t{train_id:04d}/p{k}-full" for k in range(1, len(prs) + 1)]
        return state.Train(train_id, TIP, members, refs, status=state.GREEN)


class Log:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: str, **fields) -> None:
        self.events.append({"event": event, **fields})

    def of(self, event: str) -> list[dict]:
        return [e for e in self.events if e["event"] == event]


def _run(world: World, current: state.State, lander: landing.LandingState, log: Log) -> None:
    landing.tick(world.api, current, lander, log, world.merge, sleep=lambda s: None)


@pytest.mark.parametrize(
    ("probes", "changes"),
    [
        ([True, False, True], [None, None, None]),
        ([False, False, False, True], [None, "lapse", None, "resume"]),
        ([False, False, True, False, False], [None, "lapse", "resume", None, "lapse"]),
    ],
)
def test_two_failed_probes_in_a_row_are_a_lapse(probes, changes) -> None:
    elevation = landing.Elevation()
    assert [elevation.observe(admin) for admin in probes] == changes


def test_a_proven_train_lands_member_by_member_at_its_proven_heads_and_trees() -> None:
    world = World({11: (101,), 12: ()}, {11: (101,)})
    world.issue(101, "open")
    item = world.train(1, [11, 12])
    current, lander, log = state.State(trains={1: item}), landing.LandingState(), Log()
    _run(world, current, lander, log)
    assert world.merge_calls == [(11, _sha("1")), (12, _sha("2"))]
    assert item.status == state.LANDED and not lander.halted
    assert [(m.merged, world.trees[m.merged]) for m in item.members] == [
        (_sha("m11"), _sha("t11")),
        (_sha("m12"), _sha("t12")),
    ]
    assert lander.main_seen == world.main == _sha("m12")
    assert [e["pr"] for e in log.of("landed")] == [11, 12]


def test_landing_is_off_unless_the_runner_is_started_with_land(tmp_path) -> None:
    world = World({11: ()}, {})
    item = world.train(1, [11])
    state.save(state.State(trains={1: item}, max_id=1), tmp_path / "state.json")
    for route in (
        "issues",
        "git/matching-refs/heads/queue",
        "git/matching-refs/heads/queue/t",
        "git/matching-refs/heads/queue-build/t",
    ):
        world.api.routes[f"repos/{REPO}/{route}"] = []
    assert (
        runner.main(["tick", "--state-dir", str(tmp_path)], api=world.api, merge=world.merge) == 0
    )
    assert world.merge_calls == [] and not (tmp_path / "landing.json").exists()
    assert (
        runner.main(
            ["tick", "--state-dir", str(tmp_path), "--land"], api=world.api, merge=world.merge
        )
        == 0
    )
    assert world.merge_calls == [(11, _sha("1"))]
    assert state.load(tmp_path / "state.json").trains[1].status == state.LANDED


def test_a_tree_other_than_the_proven_one_halts_landing() -> None:
    world = World({11: (), 12: ()}, {})
    world.wrong_tree = True
    item = world.train(1, [11, 12])
    current, lander, log = state.State(trains={1: item}), landing.LandingState(), Log()
    _run(world, current, lander, log)
    assert world.merge_calls == [(11, _sha("1"))]
    assert "not the proven T_k" in lander.halted and item.status == state.GREEN
    _run(world, current, lander, log)
    assert world.merge_calls == [(11, _sha("1"))] and log.of("halted")


def test_an_external_merge_to_main_halts_before_any_landing() -> None:
    world = World({11: ()}, {})
    item = world.train(1, [11])
    current, lander, log = state.State(), landing.LandingState(), Log()
    _run(world, current, lander, log)
    world.add_commit(_sha("x"), _sha("y"), [TIP])
    world.main = _sha("x")
    current.trains[1] = item
    _run(world, current, lander, log)
    assert world.merge_calls == [] and "main moved outside the runner" in lander.halted


def test_a_lapse_pauses_landing_and_resume_lands_the_backlog_in_order() -> None:
    world = World({11: (), 12: (), 13: ()}, {})
    first = world.train(1, [11, 12])
    second = world.train(2, [13], tip=first.head_sha)
    current, lander, log = state.State(trains={2: second, 1: first}), landing.LandingState(), Log()
    world.admin = False
    _run(world, current, lander, log)
    _run(world, current, lander, log)
    _run(world, current, lander, log)
    assert world.merge_calls == [] and lander.elevation.lapsed
    assert [e["lapse"] for e in log.of("needs-input")] == [1]
    world.admin = True
    _run(world, current, lander, log)
    assert [pr for pr, _ in world.merge_calls] == [11, 12, 13]
    assert (first.status, second.status) == (state.LANDED, state.LANDED)
    assert log.of("resume") and not lander.halted


def test_a_rule_violation_is_retried_then_pauses_without_halting() -> None:
    world = World({11: ()}, {})
    world.refuse = [
        "Repository rule violations found: changes must be made through the merge queue"
    ] * 3
    item = world.train(1, [11])
    current, lander, log = state.State(trains={1: item}), landing.LandingState(), Log()
    _run(world, current, lander, log)
    assert len(world.merge_calls) == landing.MAX_MERGE_ATTEMPTS
    assert item.status == state.GREEN and not lander.halted and log.of("merge-refused")
    _run(world, current, lander, log)
    assert item.status == state.LANDED


def test_a_closure_mismatch_ejects_the_train_before_any_merge() -> None:
    """#11 declares fixes=[101]; a commit message also says "Fixes #102"."""
    world = World({11: (101,), 12: ()}, {11: (101,)})
    world.messages[11] = ["feat: thing\n\nFixes #102"]
    item = world.train(1, [11, 12])
    current, lander, log = state.State(trains={1: item}), landing.LandingState(), Log()
    _run(world, current, lander, log)
    assert world.merge_calls == [] and item.status == state.ABORTED and not lander.halted
    notices = log.of("ejection-notice")
    assert [e["pr"] for e in notices] == [11, 12]
    assert "would close [101, 102]" in notices[0]["reason"]


def test_a_withdrawn_ticket_ejects_the_train() -> None:
    world = World({11: ()}, {})
    world.api.routes[f"repos/{REPO}/issues/11/comments"] = []
    item = world.train(1, [11])
    current, lander, log = state.State(trains={1: item}), landing.LandingState(), Log()
    _run(world, current, lander, log)
    assert world.merge_calls == [] and item.status == state.ABORTED
    assert "no valid ticket" in log.of("ejection-notice")[0]["reason"]


def test_a_fix_github_left_open_is_closed_and_stacked_prs_retarget() -> None:
    world = World({11: (101,)}, {11: (101,)})
    world.issue(101, "open")
    world.github_closes = False
    current = state.State(trains={1: world.train(1, [11])})
    lander, log = landing.LandingState(), Log()
    _run(world, current, lander, log)
    assert lander.pending == [{"pr": 11, "fixes": [101], "head_ref": "branch-11"}]
    world.api.routes[f"repos/{REPO}/pulls"] = [{"number": 15}]
    _run(world, current, lander, log)
    patches = [(p, b) for m, p, b in world.api.calls if m == "PATCH"]
    assert patches == [
        (f"repos/{REPO}/issues/101", {"state": "closed", "state_reason": "completed"}),
        (f"repos/{REPO}/pulls/15", {"base": "main"}),
    ]
    assert lander.pending == []


def test_a_rejected_closure_write_stays_pending_for_the_next_tick() -> None:
    world = World({11: (101,)}, {11: (101,)})
    world.issue(101, "open")
    world.github_closes = False
    world.api.patch_handler = lambda path, body: Response(403, "Resource not accessible")
    current = state.State(trains={1: world.train(1, [11])})
    lander, log = landing.LandingState(), Log()
    _run(world, current, lander, log)
    _run(world, current, lander, log)
    assert [e["pr"] for e in log.of("closure-retry")] == [11] and len(lander.pending) == 1
    world.api.patch_handler = lambda path, body: Response(200, {})
    _run(world, current, lander, log)
    assert lander.pending == [] and [e["pr"] for e in log.of("closure-settled")] == [11]


def test_a_crash_after_a_merge_is_recovered_from_the_intent_journal() -> None:
    world = World({11: (), 12: ()}, {})
    item = world.train(1, [11, 12])
    current, lander, log = state.State(trains={1: item}), landing.LandingState(), Log()
    saved: list[dict] = []
    world.crash_after_merge = True
    with pytest.raises(RuntimeError, match="connection reset"):
        landing.tick(
            world.api,
            current,
            lander,
            log,
            world.merge,
            sleep=lambda s: None,
            save=lambda: saved.append(dict(lander.intent)),
        )
    assert saved[-1]["pr"] == 11 and item.members[0].merged == ""
    world.crash_after_merge = False
    _run(world, current, lander, log)
    assert [e["pr"] for e in log.of("recovered")] == [11] and not lander.halted
    assert [pr for pr, _ in world.merge_calls] == [11, 12] and item.status == state.LANDED
    assert lander.intent == {}


def test_a_failed_admin_probe_counts_as_a_miss() -> None:
    def broken(query):
        raise RuntimeError("GET repos/microsoft/apm failed (502)")

    world = World({11: ()}, {})
    world.api.routes[f"repos/{REPO}"] = broken
    current = state.State(trains={1: world.train(1, [11])})
    lander, log = landing.LandingState(), Log()
    _run(world, current, lander, log)
    _run(world, current, lander, log)
    assert world.merge_calls == [] and [e["lapse"] for e in log.of("needs-input")] == [1]


def test_a_green_train_waits_behind_an_earlier_train_still_testing() -> None:
    world = World({11: (), 13: ()}, {})
    first = world.train(1, [11])
    first.status = state.TESTING
    second = world.train(2, [13], tip=first.head_sha)
    current, lander, log = state.State(trains={1: first, 2: second}), landing.LandingState(), Log()
    _run(world, current, lander, log)
    assert world.merge_calls == [] and not lander.halted


def test_ejection_notices_still_go_out_while_landing_is_halted() -> None:
    world = World({11: ()}, {})
    item = world.train(1, [11])
    item.status, item.run_id, item.ejected = state.RED, 5, 11
    current = state.State(trains={1: item})
    lander, log = landing.LandingState(halted="main moved"), Log()
    _run(world, current, lander, log)
    assert [e["pr"] for e in log.of("ejection-notice")] == [11] and log.of("halted")


def test_only_the_ejected_member_of_a_red_train_gets_a_notice_once() -> None:
    world = World({11: (), 12: ()}, {})
    item = world.train(1, [11, 12])
    item.status, item.run_id, item.ejected = state.RED, 77, 12
    item.red_class, item.residual = "own-attributed", ["tests/unit/test_b.py::t"]
    current, lander, log = state.State(trains={1: item}), landing.LandingState(), Log()
    _run(world, current, lander, log)
    _run(world, current, lander, log)
    notices = log.of("ejection-notice")
    assert [(e["pr"], e["run"], e["klass"]) for e in notices] == [(12, 77, "own-attributed")]
    assert "tests/unit/test_b.py::t" in notices[0]["body"]


def test_landing_state_round_trips(tmp_path) -> None:
    lander = landing.LandingState(
        landing.Elevation(1, True, 2), "halt", "a" * 40, [{"pr": 1}], [3], {"pr": 4}
    )
    landing.save(lander, tmp_path / "landing.json")
    assert landing.load(tmp_path / "landing.json") == lander
