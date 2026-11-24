"""The real-harness verdict gate: expected.json is a red-first oracle, BLOCKED never fails."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.real_harness import probe

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-validation-topology")]


def _cell(out: Path, cell: str, verdict: str) -> None:
    root = out / cell
    root.mkdir(parents=True)
    payload = {"cell": cell, "verdict": verdict, "reason": "fixture"}
    (root / "cell.json").write_text(json.dumps(payload), encoding="utf-8")


def _expected(tmp_path: Path) -> Path:
    path = tmp_path / "expected.json"
    cells = {
        "claude-headless-user": {"verdict": "LOADS"},
        "claude-headless-project": {"verdict": "PARTIAL", "issue": 5176},
    }
    path.write_text(json.dumps({"cells": cells}), encoding="utf-8")
    return path


def test_matching_and_blocked_cells_pass(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _cell(out, "claude-headless-user", "LOADS")
    _cell(out, "claude-headless-project", "BLOCKED")
    assert probe.check(out, _expected(tmp_path)) == []


def test_known_bug_cell_that_starts_loading_fails_until_flipped(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _cell(out, "claude-headless-project", "LOADS")
    (problem,) = probe.check(out, _expected(tmp_path))
    assert problem.startswith("claude-headless-project: expected PARTIAL (tracked in #5176)")


def test_regression_and_unclassified_cells_fail(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _cell(out, "claude-headless-project", "ERROR")
    _cell(out, "claude-headless-user", "DOES NOT LOAD")
    _cell(out, "copilot-headless-user", "LOADS")
    problems = probe.check(out, _expected(tmp_path))
    assert [problem.split(":")[0] for problem in problems] == [
        "claude-headless-project",
        "claude-headless-user",
        "copilot-headless-user",
    ]
    assert "no expectation" in problems[2]


def test_committed_expectations_name_only_known_cells() -> None:
    expected = json.loads(
        (Path(probe.__file__).with_name("expected.json")).read_text(encoding="utf-8")
    )["cells"]
    known = {
        f"{harness}-{env}-{scope}"
        for harness in probe.HARNESSES
        for env in probe.ENVS + probe.DIAG_ENVS
        for scope in probe.SCOPES
    }
    assert set(expected) <= known
    assert {row["verdict"] for row in expected.values()} <= {"LOADS", "PARTIAL", "DOES NOT LOAD"}
