"""Required, subprocess-free contracts for lifecycle interaction obligations."""

import itertools
import json
import shlex
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

from tests.utils.lifecycle_interaction_report import main as report_main
from tests.utils.lifecycle_interaction_report import read_executions
from tests.utils.lifecycle_interactions import (
    DYNAMIC_REFUSAL_ROWS,
    HIGH_RISK_TRIPLES,
    INTERACTION_ROWS,
    KNOWN_IDEMPOTENCY_GAP,
    ROUTING_ROWS,
    TRANSITION_ROWS,
    CaseExecution,
    RoutingRow,
    assert_complete_evidence,
    candidate_rows,
    catalog_rows,
    coverage_report,
    execution_from_mapping,
    factors,
    generate_interaction_rows,
    invalid_reason,
    known_gap_for,
    required_interactions,
    required_laws,
    required_transitions,
    validate_campaign,
    validate_routing_rows,
)
from tests.workflow_contracts import load_workflow, shell_tokens, workflow_job, workflow_step

RATCHET_TEST_SCOPE = "repository"
pytestmark = pytest.mark.component
_ALL_ROWS = (*ROUTING_ROWS, *INTERACTION_ROWS)


def test_lifecycle_routing_cells_cover_the_live_target_catalog() -> None:
    validate_routing_rows(ROUTING_ROWS)
    validate_campaign(_ALL_ROWS)


@pytest.mark.parametrize("required", (*catalog_rows()[:1], *TRANSITION_ROWS, *DYNAMIC_REFUSAL_ROWS))
def test_routing_ratchet_rejects_missing_cells_and_transitions(required: RoutingRow) -> None:
    with pytest.raises(AssertionError):
        validate_routing_rows(tuple(row for row in ROUTING_ROWS if row.id != required.id))


def test_transition_metadata_cannot_be_replaced_by_a_noop() -> None:
    required = TRANSITION_ROWS[0]
    rows = tuple(
        replace(row, widen_targets=(), narrow_targets=()) if row.id == required.id else row
        for row in ROUTING_ROWS
    )
    with pytest.raises(AssertionError, match="Missing obligation"):
        validate_routing_rows(rows)


def test_target_transitions_cover_both_scopes_without_inventing_global_prune() -> None:
    assert {row.user_scope for row in TRANSITION_ROWS} == {False, True}
    user_row = next(row for row in TRANSITION_ROWS if row.user_scope)
    assert user_row.widen_targets and user_row.narrow_targets
    assert required_transitions(user_row) == {
        "install",
        "widen",
        "reinstall-widened",
        "narrow",
        "uninstall",
    }
    assert all(
        "prune" in required_transitions(row) for row in TRANSITION_ROWS if not row.user_scope
    )


def test_candidate_universe_does_not_hide_valid_nondefault_combinations() -> None:
    """All combinations of these reviewed domains are legal for each static cell."""
    expected_variants = {
        (shape, source, ref, cache, integrity, command)
        for shape, source, integrity, command in itertools.product(
            ("direct", "transitive"),
            ("git", "local"),
            ("clean", "tampered"),
            ("reinstall", "audit", "update"),
        )
        for ref in (("pinned", "tag") if source == "git" else ("none",))
        for cache in (("cold", "warm") if source == "git" else ("none",))
    }
    by_cell: dict[tuple, set[tuple[str, ...]]] = {}
    for row in candidate_rows():
        assert invalid_reason(row) is None
        key = (row.targets, row.primitives, row.user_scope)
        variant = (
            row.dependency_shape,
            row.source_kind,
            row.ref_state,
            row.cache_state,
            row.integrity_state,
            row.command,
        )
        by_cell.setdefault(key, set()).add(variant)
    expected_cells = {(row.targets, row.primitives, row.user_scope) for row in catalog_rows()}
    assert set(by_cell) == expected_cells
    assert all(variants == expected_variants for variants in by_cell.values())


@pytest.mark.parametrize(
    "changes",
    (
        {"source_kind": "local"},
        {"ref_state": "none"},
        {"cache_state": "none"},
        {"command": "unimplemented"},
        {"targets": ("not-a-target",)},
    ),
)
def test_semantic_constraints_reject_impossible_or_unknown_states(
    changes: dict[str, object],
) -> None:
    assert invalid_reason(replace(catalog_rows()[0], **changes)) is not None


def test_selection_is_deterministic_and_covers_ledger_driven_triples() -> None:
    generate_interaction_rows.cache_clear()
    assert generate_interaction_rows() == INTERACTION_ROWS
    ids = [row.id for row in _ALL_ROWS]
    assert len(ids) == len(set(ids))
    assert len(INTERACTION_ROWS) < len(candidate_rows())
    for triple in HIGH_RISK_TRIPLES:
        assert any(set(triple.values) <= set(factors(row)) for row in _ALL_ROWS), triple.id


def test_campaign_laws_and_named_exceptions_have_ledger_backing() -> None:
    path = Path(__file__).resolve().parents[1] / "fixtures/lifecycle_bug_ledger.json"
    ledger = json.loads(path.read_text(encoding="ascii"))
    law_ids = {row["id"] for row in ledger["property_catalog"]}
    assert set().union(*(required_laws(row) for row in _ALL_ROWS)) <= law_ids
    issues = {bug["issue"] for bug in ledger["bugs"]}
    assert {triple.issue for triple in HIGH_RISK_TRIPLES if triple.issue is not None} <= issues
    gap = next(gap for gap in ledger["known_gaps"] if gap["id"] == KNOWN_IDEMPOTENCY_GAP)
    assert "idempotency.byte_stable" in gap["properties"]
    assert gap["bounded_by"] and gap["next_decision"]


def _execution(row: RoutingRow, **changes: object) -> CaseExecution:
    evidence = CaseExecution(
        case_id=row.id,
        status="executed",
        evaluated_laws=tuple(sorted(required_laws(row))),
        transitions=tuple(sorted(required_transitions(row))),
        duration_seconds=1.0,
    )
    return replace(evidence, **changes)


def test_generated_rows_never_count_as_executed_coverage() -> None:
    report = coverage_report(_ALL_ROWS, (), input_revision="test-fixture")
    assert report["covered_pairs"] == []
    assert report["uncovered_pairs"] == sorted(required_interactions())
    assert report["unexecuted_case_ids"] == sorted(row.id for row in _ALL_ROWS)
    assert all(not triple["witnesses"] for triple in report["triples"])


def test_complete_execution_accounting_has_deterministic_witnesses() -> None:
    evidence = tuple(_execution(row) for row in _ALL_ROWS)
    report = coverage_report(_ALL_ROWS, evidence, input_revision="test-fixture")
    reverse = coverage_report(_ALL_ROWS, reversed(evidence), input_revision="test-fixture")
    assert json.dumps(report, sort_keys=True) == json.dumps(reverse, sort_keys=True)
    assert report["uncovered_pairs"] == []
    assert report["rejected_evidence"] == {}
    assert report["unexecuted_case_ids"] == []
    assert all(triple["witnesses"] for triple in report["triples"])
    assert_complete_evidence(_ALL_ROWS, evidence)


def test_combined_gate_requires_cases_even_when_all_pairs_have_other_witnesses() -> None:
    omitted = DYNAMIC_REFUSAL_ROWS[0].id
    evidence = tuple(_execution(row) for row in _ALL_ROWS if row.id != omitted)
    report = coverage_report(_ALL_ROWS, evidence, input_revision="test-fixture")
    assert report["uncovered_pairs"] == []
    with pytest.raises(AssertionError, match="Missing lifecycle execution"):
        assert_complete_evidence(_ALL_ROWS, evidence)


def test_known_idempotency_gap_does_not_waive_safety_laws_or_actions() -> None:
    row = next(row for row in ROUTING_ROWS if row.id == "copilot-instructions-user")
    bounded = _execution(
        row,
        status="known_gap",
        evaluated_laws=tuple(sorted(required_laws(row) - {"idempotency.byte_stable"})),
        reason=KNOWN_IDEMPOTENCY_GAP,
    )
    other = tuple(_execution(item) for item in _ALL_ROWS if item.id != row.id)
    assert_complete_evidence(_ALL_ROWS, (*other, bounded))
    for corrupted in (
        replace(bounded, evaluated_laws=()),
        replace(bounded, transitions=()),
        replace(bounded, status="failed"),
        replace(bounded, reason="unreviewed-exception"),
        replace(
            bounded,
            transitions=tuple(step for step in bounded.transitions if step != "converge-known-gap"),
        ),
    ):
        with pytest.raises(AssertionError, match="Unmet lifecycle obligation"):
            assert_complete_evidence(_ALL_ROWS, (*other, corrupted))


def test_known_gap_is_not_inherited_by_new_interaction_shapes() -> None:
    row = next(row for row in ROUTING_ROWS if row.id == "copilot-instructions-user")
    assert known_gap_for(row) == KNOWN_IDEMPOTENCY_GAP
    for changed in (
        replace(row, id="new-interaction"),
        replace(row, command="update"),
        replace(row, dependency_shape="transitive"),
        replace(row, integrity_state="tampered"),
    ):
        assert known_gap_for(changed) is None


def test_tracked_product_bugs_do_not_become_coverage_exemptions() -> None:
    path = Path(__file__).resolve().parents[1] / "fixtures/lifecycle_bug_ledger.json"
    ledger = json.loads(path.read_text(encoding="ascii"))
    tracked = [gap for gap in ledger["known_gaps"] if "issue" in gap]
    assert {2815, 2816} <= {gap["issue"] for gap in tracked}
    rows = {row.id: row for row in _ALL_ROWS}
    for gap in tracked:
        assert gap["regression_case"].startswith(gap["bounded_by"] + "[")
        if gap["interaction_case"] is not None:
            row = rows[gap["interaction_case"]]
            assert known_gap_for(row) is None
            evidence = tuple(
                _execution(item, status="known_gap", reason=gap["id"])
                if item.id == row.id
                else _execution(item)
                for item in _ALL_ROWS
            )
            with pytest.raises(AssertionError, match="Unmet lifecycle obligation"):
                assert_complete_evidence(_ALL_ROWS, evidence)


def test_known_gap_cannot_be_applied_to_unrelated_cells() -> None:
    row = ROUTING_ROWS[0]
    assert row.id != "copilot-instructions-user"
    evidence = tuple(
        _execution(item, status="known_gap", reason=KNOWN_IDEMPOTENCY_GAP)
        if item.id == row.id
        else _execution(item)
        for item in _ALL_ROWS
    )
    with pytest.raises(AssertionError, match="Unmet lifecycle obligation"):
        assert_complete_evidence(_ALL_ROWS, evidence)


def test_combined_gate_requires_executed_triples(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = tuple(_execution(row) for row in _ALL_ROWS)
    report = coverage_report(_ALL_ROWS, evidence, input_revision="test-fixture")
    report["triples"][0]["witnesses"] = []
    monkeypatch.setattr(
        "tests.utils.lifecycle_interactions.coverage_report", lambda *args, **kwargs: report
    )
    with pytest.raises(AssertionError, match="Uncovered high-risk triples"):
        assert_complete_evidence(_ALL_ROWS, evidence)


@pytest.mark.parametrize("status", ("skipped", "setup_failed", "failed", "known_gap"))
def test_nonexecuted_or_known_gap_evidence_earns_no_credit(status: str) -> None:
    row = INTERACTION_ROWS[0]
    report = coverage_report(
        _ALL_ROWS, (_execution(row, status=status, reason="fixture"),), input_revision="fixture"
    )
    assert report["covered_pairs"] == []
    assert report["rejected_evidence"][row.id]["status"] == status


@pytest.mark.parametrize("field", ("evaluated_laws", "transitions"))
def test_disabling_assertions_or_actions_loses_execution_credit(field: str) -> None:
    row = INTERACTION_ROWS[0]
    report = coverage_report(
        _ALL_ROWS, (_execution(row, **{field: ()}),), input_revision="test-fixture"
    )
    assert report["covered_pairs"] == []
    assert row.id in report["rejected_evidence"]


def test_report_rejects_duplicate_unknown_and_invalid_execution_records() -> None:
    evidence = _execution(INTERACTION_ROWS[0])
    for invalid in (
        (evidence, evidence),
        (replace(evidence, case_id="unknown"),),
        (replace(evidence, status="pretend-pass"),),
        (replace(evidence, duration_seconds=-1),),
        (replace(evidence, duration_seconds=float("nan")),),
    ):
        with pytest.raises(ValueError):
            coverage_report(_ALL_ROWS, invalid, input_revision="test-fixture")


def test_execution_artifact_parser_rejects_malformed_evidence() -> None:
    payload = {
        "case_id": "case",
        "status": "executed",
        "evaluated_laws": ["law"],
        "transitions": ["install"],
        "duration_seconds": 1,
    }
    assert execution_from_mapping(payload).case_id == "case"
    for field, value in (
        ("case_id", None),
        ("evaluated_laws", "not-an-array"),
        ("transitions", [1]),
        ("duration_seconds", True),
        ("reason", []),
    ):
        with pytest.raises(ValueError):
            execution_from_mapping({**payload, field: value})


@pytest.mark.parametrize("pytest_status", ("failure", "error", "skipped", None))
def test_junit_result_overrules_earlier_success_evidence(
    tmp_path: Path, pytest_status: str | None
) -> None:
    root = ET.Element("testsuite")
    case = ET.SubElement(root, "testcase", name="lifecycle")
    properties = ET.SubElement(case, "properties")
    evidence = _execution(INTERACTION_ROWS[0])
    payload = {
        "case_id": evidence.case_id,
        "status": evidence.status,
        "evaluated_laws": list(evidence.evaluated_laws),
        "transitions": list(evidence.transitions),
        "duration_seconds": evidence.duration_seconds,
    }
    ET.SubElement(properties, "property", name="lifecycle_execution", value=json.dumps(payload))
    if pytest_status:
        ET.SubElement(case, pytest_status)
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(path)
    observed = read_executions(path)
    expected = (
        "executed"
        if pytest_status is None
        else ("skipped" if pytest_status == "skipped" else "failed")
    )
    assert observed[0].status == expected
    report = coverage_report(_ALL_ROWS, observed, input_revision="test-fixture")
    assert bool(report["covered_pairs"]) is (pytest_status is None)


def test_ci_uploads_observed_lifecycle_evidence_even_after_test_failure() -> None:
    path = Path(__file__).resolve().parents[2] / ".github/workflows/ci-integration.yml"
    workflow = load_workflow(path)
    assert set(workflow["on"]) == {"merge_group"}
    job = workflow_job(workflow, "integration-tests-shard")
    run = workflow_step(job, "Run integration tests (sharded + parallelized)")
    assert "--junitxml=lifecycle-junit.xml" in shlex.split(run["env"]["PYTEST_EXTRA_ARGS"])
    assert "junit_family=legacy" in shlex.split(run["env"]["PYTEST_EXTRA_ARGS"])
    report = workflow_step(job, "Report observed lifecycle interactions")
    assert report["if"] == "always() && hashFiles('lifecycle-junit.xml') != ''"
    assert shell_tokens(report) == [
        "uv",
        "run",
        "--frozen",
        "--extra",
        "dev",
        "python",
        "-m",
        "tests.utils.lifecycle_interaction_report",
        "--junit",
        "lifecycle-junit.xml",
        "--revision",
        "${{ github.sha }}",
        "--output",
        "lifecycle-interactions.json",
        "--mutation-output",
        "lifecycle-mutations.json",
    ]
    upload = workflow_step(job, "Upload lifecycle evidence")
    assert upload["if"] == "always()"
    assert set(upload["with"]["path"].splitlines()) == {
        "lifecycle-junit.xml",
        "lifecycle-interactions.json",
        "lifecycle-mutations.json",
    }
    assert upload["with"]["retention-days"] >= 7
    fan_in = workflow_job(workflow, "integration-tests")
    download = workflow_step(fan_in, "Download lifecycle evidence")
    assert download["with"]["pattern"] == "lifecycle-evidence-shard-*"
    assert download["with"].get("merge-multiple", False) is False
    combined = workflow_step(fan_in, "Enforce combined lifecycle evidence")
    assert combined["if"] == "always()"
    assert combined.get("continue-on-error", False) is False
    assert "--require-complete" in shell_tokens(combined)
    assert "--junit-dir" in shell_tokens(combined)
    assert "--require-mutations" in shell_tokens(combined)
    assert "--mutation-output" in shell_tokens(combined)
    combined_upload = workflow_step(fan_in, "Upload combined lifecycle evidence")
    assert combined_upload["if"] == "always()"
    assert set(combined_upload["with"]["path"].splitlines()) == {
        "lifecycle-interactions-combined.json",
        "lifecycle-mutations-combined.json",
    }


def test_report_command_persists_missing_evidence_before_failing_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    junit_dir = tmp_path / "shards"
    junit_dir.mkdir()
    ET.ElementTree(ET.Element("testsuite")).write(junit_dir / "junit.xml")
    output = tmp_path / "combined.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "lifecycle-report",
            "--junit-dir",
            str(junit_dir),
            "--revision",
            "test-fixture",
            "--output",
            str(output),
            "--require-complete",
        ],
    )
    with pytest.raises(AssertionError, match="Missing lifecycle execution"):
        report_main()
    report = json.loads(output.read_text(encoding="ascii"))
    assert report["input_revision"] == "test-fixture"
    assert report["covered_pairs"] == []
    assert report["unexecuted_case_ids"] == sorted(row.id for row in _ALL_ROWS)
    assert {bug["issue"] for bug in report["tracked_product_bugs"]} >= {2815, 2816}
