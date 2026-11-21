"""Merge-queue v2 red handling: the inherited baseline (gate.py) and the classifier."""

from __future__ import annotations

import json

import pytest

from scripts.merge_queue import classify, gate

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-validation-topology")]

MEMBERS = [
    (1, ("src/apm_cli/a.py",)),
    (2, ("src/apm_cli/b.py", "tests/unit/test_shared.py")),
    (3, ("docs/c.md", "tests/unit/test_shared.py")),
    (4, ("src/apm_cli/d.py",)),
]


def _report(residual=(), unidentified=()) -> dict:
    return {"residual": list(residual), "unidentified_jobs": list(unidentified)}


@pytest.mark.parametrize(
    ("report", "red_guard", "reruns", "expected"),
    [
        (_report(["tests/unit/test_x.py::t"]), 3, 0, ("own-guard", "eject", 3, ())),
        (_report(unidentified=["tier1 / Lint"]), None, 0, ("infra", "rerun", None, ())),
        (_report(unidentified=["tier1 / Lint"]), None, 1, ("infra", "halt", None, ())),
        ({}, None, 0, ("infra", "rerun", None, ())),
        (_report(["tests/unit/test_b.py::t"]), None, 0, ("own-attributed", "eject", 2, ())),
        (_report(["tests/unit/test_d.py::t[x]"]), None, 0, ("own-attributed", "eject", 4, ())),
        (
            _report(["tests/unit/test_shared.py::t"]),
            None,
            0,
            ("flake-candidate", "rerun", None, ()),
        ),
        (
            _report(["tests/unit/test_shared.py::t"]),
            None,
            1,
            ("own-unattributed", "bisect", None, (1, 2)),
        ),
    ],
)
def test_a_red_train_is_classified_in_design_order(report, red_guard, reruns, expected) -> None:
    decision = classify.classify(report, MEMBERS, red_guard, reruns)
    assert (decision.klass, decision.action, decision.culprit, decision.first_half) == expected


def test_a_report_that_failed_to_collect_evidence_is_infra_not_a_flake() -> None:
    report = {**_report(), "error": "GET jobs failed (502)"}
    first = classify.classify(report, MEMBERS, None, 0)
    second = classify.classify(report, MEMBERS, None, 1)
    assert (first.klass, first.action, second.action) == ("infra", "rerun", "halt")


def test_bisect_of_a_single_member_ejects_it() -> None:
    decision = classify.classify(_report(["tests/unit/test_z.py::t"]), MEMBERS[:1], None, 1)
    assert (decision.action, decision.culprit) == ("eject", 1)


LOG = """2026-11-21T20:34:41.1Z ============ short test summary info ============
2026-11-21T20:34:41.2Z FAILED tests/unit/test_a.py::test_one - AssertionError: boom
2026-11-21T20:34:41.3Z ERROR tests/integration/test_b.py::TestX::test_two[case-1]
2026-11-21T20:34:41.4Z FAILED: not a node id
2026-11-21T20:34:41.5Z FAILED tests/unit/test_c.py::test_value[case A] - assert 1 == 2
"""


def test_failing_ids_come_from_pytest_summary_lines() -> None:
    assert gate.failing_ids(LOG) == {
        "tests/unit/test_a.py::test_one",
        "tests/integration/test_b.py::TestX::test_two[case-1]",
        "tests/unit/test_c.py::test_value[case A]",
    }


def _jobs(*conclusions: tuple) -> list[dict]:
    """(name, conclusion[, failed step names])."""
    return [
        {
            "id": n,
            "name": spec[0],
            "conclusion": spec[1],
            "steps": [
                {"name": step, "conclusion": "failure"}
                for step in (spec[2] if len(spec) > 2 else ())
            ],
        }
        for n, spec in enumerate(conclusions)
    ]


def test_parameter_cases_with_spaces_stay_distinct() -> None:
    train_log = "FAILED tests/unit/test_c.py::test_value[case B] - boom\n"
    result = gate.evaluate(
        _jobs(("tier1 / Unit", "failure")), {0: train_log}, gate.failing_ids(LOG)
    )
    assert not result["inherited_only"]
    assert result["residual"] == ["tests/unit/test_c.py::test_value[case B]"]


def test_only_inherited_failures_excuse_the_train() -> None:
    jobs = _jobs(
        ("tier2 / Integration Tests Shard 3 (Linux)", "failure"),
        ("tier2 / Integration Tests (Linux)", "failure", ("Aggregate shard results",)),
        ("guard", "success"),
    )
    logs = {0: LOG}
    covered = gate.evaluate(jobs, logs, gate.failing_ids(LOG))
    assert covered["inherited_only"] and covered["derived_jobs"] == [
        "tier2 / Integration Tests (Linux)"
    ]
    partial = gate.evaluate(jobs, logs, {"tests/unit/test_a.py::test_one"})
    assert not partial["inherited_only"]
    assert partial["residual"] == [
        "tests/integration/test_b.py::TestX::test_two[case-1]",
        "tests/unit/test_c.py::test_value[case A]",
    ]


@pytest.mark.parametrize(
    "jobs",
    [
        _jobs(("tier1 / Lint", "failure")),
        _jobs(("tier2 / Integration Tests Shard 3 (Linux)", "cancelled")),
        _jobs(("tier2 / Integration Tests (Linux)", "failure", ("Aggregate shard results",))),
        _jobs(
            (
                "tier2 / Integration Tests (Linux)",
                "failure",
                ("Enforce integration coverage gate",),
            ),
            ("tier2 / Integration Tests Shard 3 (Linux)", "failure"),
        ),
        _jobs(("tier1 / Coverage Combine (Linux)", "failure", ("Enforce unit coverage gate",))),
    ],
)
def test_a_failure_without_node_ids_is_never_excused(jobs) -> None:
    logs = {1: LOG} if len(jobs) > 1 else {}
    result = gate.evaluate(jobs, logs, gate.failing_ids(LOG))
    assert not result["inherited_only"] and result["unidentified_jobs"] == [jobs[0]["name"]]


def test_gate_main_reads_the_base_probe_and_publishes_the_report(tmp_path, monkeypatch) -> None:
    base = "a" * 40
    calls: list[list[str]] = []

    def fake_gh(args: list[str]) -> str:
        calls.append(args)
        if args[0] == "run":
            directory = args[args.index("-D") + 1]
            with open(f"{directory}/train-failures.json", "w", encoding="utf-8") as handle:
                json.dump({"failing": sorted(gate.failing_ids(LOG))}, handle)
            return ""
        if args[1] == "--paginate":
            return (
                json.dumps(
                    {
                        "id": 7,
                        "name": "tier2 / Integration Tests Shard 3 (Linux)",
                        "conclusion": "failure",
                    }
                )
                + "\n"
            )
        if "/logs" in args[1]:
            return LOG
        return "555\n"

    output, summary = tmp_path / "out.txt", tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report_path = tmp_path / "train-failures.json"
    assert (
        gate.main(["--run-id", "9", "--base-sha", base, "--out", str(report_path)], run=fake_gh)
        == 0
    )
    assert output.read_text(encoding="utf-8") == "inherited_only=true\n"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["inherited"] == sorted(gate.failing_ids(LOG)) and report["base"] == base
    assert f"branch=queue/b{base[:12]}/p0-full" in calls[2][1]
    assert "baseline: run 555" in summary.read_text(encoding="utf-8")


def test_gate_main_never_fails_the_job_on_an_api_error(tmp_path, monkeypatch) -> None:
    def broken(args: list[str]) -> str:
        raise OSError("gh not found")

    output = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    path = tmp_path / "r.json"
    assert gate.main(["--run-id", "9", "--base-sha", "b" * 40, "--out", str(path)], run=broken) == 0
    assert output.read_text(encoding="utf-8") == "inherited_only=false\n"
    assert "gh not found" in json.loads(path.read_text(encoding="utf-8"))["error"]


def _guarded_train(results: dict[str, dict]):
    """A 2-member train whose prefix runs' guard jobs conclude as given per ref."""
    from scripts.merge_queue import state
    from scripts.merge_queue.api import REPO
    from tests.unit.scripts.merge_queue._fake_api import FakeApi

    api = FakeApi()
    refs = ["queue/t0001/p1-guard", "queue/t0001/p2-full"]
    members = [state.Member(1, "1" * 40, 1, "a" * 40), state.Member(2, "2" * 40, 2, "b" * 40)]
    item = state.Train(1, "0" * 40, members, refs)
    runs = {
        ref: {
            "id": n,
            "head_sha": m.sha,
            "head_branch": ref,
            "status": job.pop("run", "completed"),
            "created_at": "x",
        }
        for n, (ref, m, job) in enumerate(
            ((r, m, results[r]) for r, m in zip(refs, members, strict=True)), 10
        )
        if job is not None
    }
    api.routes[f"repos/{REPO}/actions/workflows/merge-train.yml/runs"] = lambda q: {
        "workflow_runs": [runs[q["branch"][0]]] if q["branch"][0] in runs else []
    }
    for ref, run in runs.items():
        api.routes[f"repos/{REPO}/actions/runs/{run['id']}/jobs"] = {
            "jobs": [{"name": "guard", **results[ref]}]
        }
    return api, item


LINT_RED = {
    "status": "completed",
    "conclusion": "failure",
    "steps": [{"name": "Lint chain", "conclusion": "failure"}],
}
GREEN = {"status": "completed", "conclusion": "success"}


@pytest.mark.parametrize(
    ("p1", "p2", "expected"),
    [
        (GREEN, LINT_RED, ("own", 2)),
        (LINT_RED, LINT_RED, ("own", 1)),
        (
            {"status": "in_progress", "conclusion": None, "run": "in_progress"},
            LINT_RED,
            ("wait", None),
        ),
        ({"status": "completed", "conclusion": "timed_out"}, LINT_RED, ("infra", None)),
        (GREEN, GREEN, ("none", None)),
    ],
)
def test_guard_attribution_needs_every_earlier_prefix_green(p1, p2, expected) -> None:
    from scripts.merge_queue import red

    api, item = _guarded_train({"queue/t0001/p1-guard": dict(p1), "queue/t0001/p2-full": dict(p2)})
    assert red.red_guard(api, item) == expected
