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


def test_bisect_of_a_single_member_ejects_it() -> None:
    decision = classify.classify(_report(["tests/unit/test_z.py::t"]), MEMBERS[:1], None, 1)
    assert (decision.action, decision.culprit) == ("eject", 1)


LOG = """2026-11-21T20:34:41.1Z ============ short test summary info ============
2026-11-21T20:34:41.2Z FAILED tests/unit/test_a.py::test_one - AssertionError: boom
2026-11-21T20:34:41.3Z ERROR tests/integration/test_b.py::TestX::test_two[case-1]
2026-11-21T20:34:41.4Z FAILED: not a node id
"""


def test_failing_ids_come_from_pytest_summary_lines() -> None:
    assert gate.failing_ids(LOG) == {
        "tests/unit/test_a.py::test_one",
        "tests/integration/test_b.py::TestX::test_two[case-1]",
    }


def _jobs(*conclusions: tuple[str, str]) -> list[dict]:
    return [{"id": n, "name": name, "conclusion": c} for n, (name, c) in enumerate(conclusions)]


def test_only_inherited_failures_excuse_the_train() -> None:
    jobs = _jobs(
        ("tier2 / Integration Tests Shard 3 (Linux)", "failure"),
        ("tier2 / Integration Tests (Linux)", "failure"),
        ("guard", "success"),
    )
    logs = {0: LOG}
    covered = gate.evaluate(jobs, logs, gate.failing_ids(LOG))
    assert covered["inherited_only"] and covered["derived_jobs"] == [
        "tier2 / Integration Tests (Linux)"
    ]
    partial = gate.evaluate(jobs, logs, {"tests/unit/test_a.py::test_one"})
    assert not partial["inherited_only"]
    assert partial["residual"] == ["tests/integration/test_b.py::TestX::test_two[case-1]"]


@pytest.mark.parametrize(
    "jobs",
    [
        _jobs(("tier1 / Lint", "failure")),
        _jobs(("tier2 / Integration Tests Shard 3 (Linux)", "cancelled")),
        _jobs(("tier2 / Integration Tests (Linux)", "failure")),
    ],
)
def test_a_failure_without_node_ids_is_never_excused(jobs) -> None:
    result = gate.evaluate(jobs, {}, {"tests/unit/test_a.py::test_one"})
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
