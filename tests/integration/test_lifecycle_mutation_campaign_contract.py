"""Fast mutation-accounting contracts; no installed CLI or E2E prerequisites."""

from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.utils.lifecycle_mutations import (
    ISOLATION_CHECKS,
    MUTATIONS,
    LifecycleMutation,
    RunObservation,
    assert_complete_mutation_evidence,
    classify_mutation,
    mutation_report,
    observe_run,
    read_mutation_results,
    write_report,
)

pytestmark = [pytest.mark.component, pytest.mark.lifecycle_smoke]


def _observation(
    mutation: LifecycleMutation = MUTATIONS[0],
    *,
    mutant: bool = False,
    outcome: str = "executed",
) -> RunObservation:
    context = {
        "mutant_id": mutation.id,
        "mode": "mutant" if mutant else "baseline",
        "command": ["install"],
    }
    original, replacement = "/fixture/.agents/skills", "/fixture/.claude/skills"
    destination = replacement if mutant else original
    effect = (
        {
            "original": original,
            "replacement": replacement,
            "written": [f"{destination}/{Path(mutation.witness_path).parent.name}"],
        }
        if mutation.id == "wrong-target-deployment"
        else {"paths": [mutation.witness_path], "existed": True}
    )
    detail = (
        f"missing={{('copilot', '{mutation.witness_path}')}}, unexpected=set()"
        if mutation.id == "omitted-ledger-claim"
        else mutation.witness_path
    )
    return RunObservation(
        outcome,
        f"AssertionError: {mutation.failure_prefix} {detail}" if mutant else "",
        mutation.failure_file if mutant else "",
        mutation.failure_function if mutant else "",
        0.25,
        (
            {**context, "event": "command_start"},
            {**context, **effect, "event": "reach", "changed": mutant},
            {**context, "event": "command_end", "returncode": 0},
        ),
    )


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda mutation: mutation.id)
def test_only_intended_reached_failure_is_killed(mutation: LifecycleMutation) -> None:
    """A green baseline plus a located intended assertion is positive evidence."""
    result = classify_mutation(
        mutation, _observation(mutation), _observation(mutation, mutant=True, outcome="assertion")
    )
    assert result["status"] == "killed"
    assert result["reached"] is True
    assert result["baseline_green"] is True


@pytest.mark.parametrize(
    "fault",
    (
        "baseline-red",
        "baseline-unreached",
        "baseline-cli-error",
        "unreached",
        "cli-error",
        "incomplete-command",
        "wrong-command",
        "wrong-assertion",
        "wrong-file",
        "wrong-function",
        "wrong-path",
        "exception",
        "timeout",
        "collection-error",
        "wrong-mutant",
        "wrong-mode",
        "reach-outside-command",
        "reversed-command",
        "boolean-exit",
        "empty-command",
        "missing-effect",
        "baseline-mutated",
        "neighbor-path",
    ),
)
def test_infrastructure_and_unrelated_failures_are_errors(fault: str) -> None:
    """Never import the pilot's mutmut-only nonzero-exit-to-kill mapping."""
    baseline = _observation()
    mutant = _observation(mutant=True, outcome="assertion")
    if fault == "baseline-red":
        baseline = replace(baseline, outcome="assertion")
    elif fault == "baseline-unreached":
        baseline = replace(baseline, events=(baseline.events[0], baseline.events[-1]))
    elif fault in {"baseline-cli-error", "cli-error"}:
        original = baseline if fault == "baseline-cli-error" else mutant
        broken = replace(
            original, events=(*original.events[:-1], {**original.events[-1], "returncode": 1})
        )
        if fault == "baseline-cli-error":
            baseline = broken
        else:
            mutant = broken
    elif fault == "unreached":
        mutant = replace(mutant, events=(mutant.events[0], mutant.events[-1]))
    elif fault == "incomplete-command":
        mutant = replace(mutant, events=mutant.events[:-1])
    elif fault == "wrong-command":
        mutant = replace(
            mutant, events=(*mutant.events[:-1], {**mutant.events[-1], "command": ["uninstall"]})
        )
    elif fault == "wrong-assertion":
        mutant = replace(mutant, diagnostic="AssertionError: install: exit=1")
    elif fault == "wrong-file":
        mutant = replace(mutant, failure_file="test_unrelated.py")
    elif fault == "wrong-function":
        mutant = replace(mutant, failure_function="unrelated_assertion")
    elif fault == "wrong-path":
        mutant = replace(
            mutant, diagnostic=f"AssertionError: {MUTATIONS[0].failure_prefix} ['unrelated']"
        )
    elif fault == "neighbor-path":
        mutant = replace(mutant, diagnostic=mutant.diagnostic + "-neighbor")
    elif fault in {"wrong-mutant", "wrong-mode", "empty-command", "boolean-exit"}:
        key, value = {
            "wrong-mutant": ("mutant_id", MUTATIONS[1].id),
            "wrong-mode": ("mode", "baseline"),
            "empty-command": ("command", []),
            "boolean-exit": ("returncode", False),
        }[fault]
        mutant = replace(mutant, events=tuple({**event, key: value} for event in mutant.events))
    elif fault == "reach-outside-command":
        mutant = replace(mutant, events=(mutant.events[1], mutant.events[0], mutant.events[2]))
    elif fault == "reversed-command":
        mutant = replace(mutant, events=tuple(reversed(mutant.events)))
    elif fault == "missing-effect":
        mutant = replace(
            mutant,
            events=(mutant.events[0], {**mutant.events[1], "written": []}, mutant.events[2]),
        )
    elif fault == "baseline-mutated":
        baseline = replace(
            baseline,
            events=(
                baseline.events[0],
                {**baseline.events[1], "changed": True},
                baseline.events[2],
            ),
        )
    else:
        mutant = replace(mutant, outcome="error", diagnostic=fault)
    result = classify_mutation(MUTATIONS[0], baseline, mutant)
    assert result["status"] == "error"


def test_reached_mutation_with_passing_oracle_survives() -> None:
    """Survival is visible, never silently accepted into an allowlist."""
    result = classify_mutation(MUTATIONS[0], _observation(), _observation(mutant=True))
    assert result["status"] == "survived"


def test_runtime_exception_is_not_an_assertion_kill(tmp_path: Path) -> None:
    """Runner timeouts remain errors even if a mutation was previously reached."""

    def timeout() -> None:
        raise subprocess.TimeoutExpired(("apm", "install"), 1)

    observation = observe_run(timeout, tmp_path / "absent.jsonl")
    assert observation.outcome == "error"
    assert observation.diagnostic.startswith("TimeoutExpired:")
    assert observation.events == ()


def test_report_is_sorted_ascii_and_complete(tmp_path: Path) -> None:
    """Reuse atomic report conventions without executing or importing mutmut."""
    result = classify_mutation(
        MUTATIONS[0], _observation(), _observation(mutant=True, outcome="assertion")
    )
    path = tmp_path / "report.json"
    report = write_report(path, (result,))
    first = path.read_bytes()
    write_report(path, (result,))
    assert path.read_bytes() == first
    assert all(byte < 128 for byte in first)
    assert b"\r" not in first
    assert json.loads(first) == report
    assert report["counts"] == {"killed": 1, "survived": 0, "error": 0, "total": 1}
    assert "not a frozen binary" in report["execution_boundary"]


def test_catalog_is_exactly_three_non_equivalent_green_cases() -> None:
    """The bounded campaign must not absorb known-red or user-relative cases."""
    assert {mutation.id for mutation in MUTATIONS} == {
        "wrong-target-deployment",
        "omitted-ledger-claim",
        "skipped-target-cleanup",
    }
    assert len({mutation.owner for mutation in MUTATIONS}) == 3
    assert {mutation.case_id for mutation in MUTATIONS} == {
        "copilot-skills-project",
        "copilot-prompt-widen-narrow",
    }


def test_wrong_target_has_a_distinct_native_destination(tmp_path: Path) -> None:
    """Shared .agents skill targets would make this mutant equivalent."""
    from apm_cli.integration.skill_integrator import SkillIntegrator
    from apm_cli.integration.targets import KNOWN_TARGETS

    original = SkillIntegrator._target_skills_root(KNOWN_TARGETS["copilot"], tmp_path)
    wrong = SkillIntegrator._target_skills_root(KNOWN_TARGETS["claude"], tmp_path)
    assert original != wrong
    assert (
        tmp_path / MUTATIONS[0].witness_path == original / "skills-copilot-skills-project/SKILL.md"
    )
    assert wrong == tmp_path / ".claude/skills"


def _result(mutation: LifecycleMutation = MUTATIONS[0]) -> dict[str, Any]:
    return {
        **classify_mutation(
            mutation,
            _observation(mutation),
            _observation(mutation, mutant=True, outcome="assertion"),
        ),
        **dict.fromkeys(ISOLATION_CHECKS, True),
    }


def _junit(tmp_path: Path, report: dict[str, Any], *, pytest_status: str | None = None) -> Path:
    root = ET.Element("testsuite")
    case = ET.SubElement(root, "testcase", name="mutation")
    properties = ET.SubElement(case, "properties")
    ET.SubElement(properties, "property", name="lifecycle_mutation", value=json.dumps(report))
    if pytest_status is not None:
        ET.SubElement(case, pytest_status)
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(path)
    return path


def test_cleanup_reach_requires_an_existing_deletion_candidate() -> None:
    mutation = MUTATIONS[2]
    mutant = _observation(mutation, mutant=True, outcome="assertion")
    mutant = replace(
        mutant,
        events=(mutant.events[0], {**mutant.events[1], "existed": False}, mutant.events[2]),
    )
    result = classify_mutation(mutation, _observation(mutation), mutant)
    assert result["reached"] is False
    assert result["status"] == "error"


@pytest.mark.parametrize("pytest_status", (None, "failure", "error", "skipped"))
def test_pytest_result_vetoes_a_recorded_mutation_kill(
    tmp_path: Path, pytest_status: str | None
) -> None:
    path = _junit(tmp_path, mutation_report((_result(),)), pytest_status=pytest_status)
    results = read_mutation_results(path)
    assert results[0]["status"] == ("killed" if pytest_status is None else "error")
    assert results[0]["pytest_outcome"] == (pytest_status or "passed")


@pytest.mark.parametrize("field", ISOLATION_CHECKS)
def test_isolation_failure_cannot_earn_a_kill(tmp_path: Path, field: str) -> None:
    result = {**_result(), field: False}
    observed = read_mutation_results(_junit(tmp_path, mutation_report((result,))))
    assert observed[0]["status"] == "error"
    assert observed[0][field] is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("status", "survived"),
        ("owner", "unrelated.owner"),
        ("case_id", "unrelated-case"),
        ("intended_property", "unrelated-law"),
        ("reached", False),
        ("baseline_green", False),
        ("parent_catalog_unchanged", None),
    ),
)
def test_junit_cannot_replace_observed_facts(tmp_path: Path, field: str, value: object) -> None:
    report = mutation_report((_result(),))
    report["results"][0][field] = value
    with pytest.raises(ValueError, match=r"Mutation evidence disagrees|isolation check"):
        read_mutation_results(_junit(tmp_path, report))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", 2),
        ("schema_version", True),
        ("execution_boundary", "frozen-binary"),
        ("mutation_isolation", "parent-process"),
        ("counts", {"killed": 3, "survived": 0, "error": 0, "total": 3}),
        ("counts", {"killed": True, "survived": 0, "error": 0, "total": 1}),
    ),
)
def test_report_envelope_cannot_invent_coverage(tmp_path: Path, field: str, value: object) -> None:
    report = {**mutation_report((_result(),)), field: value}
    with pytest.raises(ValueError, match=r"Invalid mutation report|counts must be integers"):
        read_mutation_results(_junit(tmp_path, report))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("elapsed_seconds", True),
        ("elapsed_seconds", float("nan")),
        ("elapsed_seconds", -1),
        ("events", [None]),
        ("events", "not-events"),
        ("diagnostic", None),
    ),
)
def test_malformed_observations_are_rejected(tmp_path: Path, field: str, value: object) -> None:
    report = mutation_report((_result(),))
    report["results"][0]["mutant"][field] = value
    with pytest.raises(ValueError, match="Mutation"):
        read_mutation_results(_junit(tmp_path, report))


def test_nonobject_child_event_is_an_error(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("null\n", encoding="ascii")
    observed = observe_run(lambda: None, events)
    assert observed.outcome == "error"
    assert observed.events == ()
    assert "Reach events must be objects" in observed.diagnostic


def test_complete_gate_requires_all_unique_mutants_and_passed_nodes(tmp_path: Path) -> None:
    results = [
        read_mutation_results(_junit(tmp_path, mutation_report((_result(mutation),))))[0]
        for mutation in MUTATIONS
    ]
    assert_complete_mutation_evidence(results)
    with pytest.raises(AssertionError, match="Missing lifecycle mutation evidence"):
        assert_complete_mutation_evidence(results[:-1])
    with pytest.raises(ValueError, match="duplicate"):
        mutation_report([*results, results[0]])
    with pytest.raises(ValueError, match="Unknown"):
        mutation_report([{**results[0], "mutant_id": "unknown"}])
    for change in (
        {"status": "survived"},
        {"status": "error"},
        {"pytest_outcome": "skipped"},
        {"parent_owners_unchanged": False},
    ):
        with pytest.raises(AssertionError, match="Unmet lifecycle mutation obligation"):
            assert_complete_mutation_evidence([{**results[0], **change}, *results[1:]])


def test_mutation_report_persists_before_completeness_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.utils.lifecycle_interaction_report import main

    junit = tmp_path / "empty.xml"
    ET.ElementTree(ET.Element("testsuite")).write(junit)
    output = tmp_path / "mutations.json"
    interactions = tmp_path / "interactions.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "lifecycle-report",
            "--junit",
            str(junit),
            "--revision",
            "contract-fixture",
            "--output",
            str(interactions),
            "--mutation-output",
            str(output),
            "--require-mutations",
            "--require-complete",
        ],
    )
    with pytest.raises(AssertionError, match="Missing lifecycle mutation evidence"):
        main()
    report = json.loads(output.read_text(encoding="ascii"))
    assert report["input_revision"] == "contract-fixture"
    assert report["unexecuted_mutant_ids"] == sorted(mutation.id for mutation in MUTATIONS)
    assert report["counts"] == {"killed": 0, "survived": 0, "error": 0, "total": 0}
    assert interactions.is_file()
