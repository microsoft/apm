"""Regression tests for shepherd-driver's version 2 completion evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator

ROOT = Path(__file__).parents[2]
CANONICAL_SCHEMA = ROOT / "packages/shepherd-driver/assets/completion-schema.json"
MIRROR_SCHEMA = ROOT / ".agents/skills/shepherd-driver/assets/completion-schema.json"
HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
DECISION = "Accepted target vocabulary"


def _validator() -> Draft7Validator:
    """Load and check the canonical shepherd-driver completion schema."""
    schema = json.loads(CANONICAL_SCHEMA.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def _owner_touch_report(*, touched: bool) -> dict[str, Any]:
    """Build a structurally valid deterministic owner-touch report."""
    touched_owners = []
    changed_files = [".apm/agents/test-coverage-expert.agent.md"]
    if touched:
        changed_files = ["src/apm_cli/models/package.py"]
        touched_owners = [
            {
                "decision": DECISION,
                "owner": "src/apm_cli/models/package.py",
                "selectors": ["src/apm_cli/models/package.py"],
                "matched_files": ["src/apm_cli/models/package.py"],
            }
        ]
    return {
        "version": "1",
        "owner_table": ".apm/instructions/architecture.instructions.md",
        "owner_table_sha256": "c" * 64,
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
        "changed_files": changed_files,
        "touched_owners": touched_owners,
    }


def _functional_test() -> dict[str, Any]:
    """Build one exact-head passing functional test execution."""
    return {
        "test_id": "tests/unit/test_models.py::test_targets",
        "command": "uv run pytest tests/unit/test_models.py::test_targets -q",
        "outcome": "passed",
        "head_sha": HEAD_SHA,
        "owner_decisions": [DECISION],
        "run_evidence": "1 passed in 0.42s",
    }


def _ready_completion(
    classification: str,
    *,
    touched: bool = False,
    dual_guardrail_required: bool = False,
) -> dict[str, Any]:
    """Build the smallest ready return relevant to owner-gate tests."""
    return {
        "kind": "completion",
        "pr": 1,
        "status": "ready-to-merge",
        "ci_evidence": "green",
        "lint_evidence": "green",
        "head_sha": HEAD_SHA,
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "ci_status": "green",
        "architecture_evidence": {
            "version": "2",
            "classification": classification,
            "owner_touch_report": _owner_touch_report(touched=touched),
            "functional_tests": [_functional_test()] if touched else [],
            "dual_guardrail_required": dual_guardrail_required,
            "boundary_lint": "exit 0 on exact head",
        },
    }


def test_completion_schema_mirror_is_byte_identical() -> None:
    """The deployed skill schema must not drift from its package source."""
    assert CANONICAL_SCHEMA.read_bytes() == MIRROR_SCHEMA.read_bytes()


def test_ready_completion_requires_architecture_evidence_v2() -> None:
    """Terminal version 1 evidence is intentionally migrated and rejected."""
    document = _ready_completion("ordinary-fix")
    document["architecture_evidence"] = {
        "classification": "ordinary-fix",
        "decisions": [],
        "dual_guardrail_required": False,
        "boundary_lint": "exit 0 on exact head",
    }

    assert list(_validator().iter_errors(document))


def test_blocked_completion_remains_compatible_without_v2_evidence() -> None:
    """Non-terminal failures do not require an unavailable evidence packet."""
    document = {
        "kind": "completion",
        "pr": 1,
        "status": "blocked",
        "blocker": "functional test fixture is unavailable",
    }

    assert list(_validator().iter_errors(document)) == []


@pytest.mark.parametrize("classification", ["ordinary-fix", "not-applicable"])
def test_unrelated_diff_allows_empty_functional_evidence(classification: str) -> None:
    """A diff outside canonical owners does not invent a functional-test gate."""
    document = _ready_completion(classification)

    assert list(_validator().iter_errors(document)) == []


def test_owner_touch_requires_functional_evidence() -> None:
    """A detected owner touch cannot become terminal without a test execution."""
    document = _ready_completion("owner-extension", touched=True)
    document["architecture_evidence"]["functional_tests"] = []

    assert list(_validator().iter_errors(document))


@pytest.mark.parametrize("classification", ["ordinary-fix", "not-applicable"])
def test_owner_touch_rejects_false_self_classification(classification: str) -> None:
    """LLM classification cannot self-exempt a deterministic owner touch."""
    document = _ready_completion(classification, touched=True)

    assert list(_validator().iter_errors(document))


@pytest.mark.parametrize("classification", ["new-owner", "split-authority-repair"])
def test_authority_creation_requires_dual_guardrail(classification: str) -> None:
    """Authority creation or repair fails closed without the dual guardrail."""
    document = _ready_completion(
        classification,
        touched=True,
        dual_guardrail_required=False,
    )

    assert list(_validator().iter_errors(document))


def test_new_owner_accepts_complete_dual_guardrail() -> None:
    """A new owner is valid with functional evidence and all static guards."""
    document = _ready_completion(
        "new-owner",
        touched=True,
        dual_guardrail_required=True,
    )
    document["architecture_evidence"].update(
        {
            "behavioral_test": "tests/unit/test_owner.py",
            "static_guard": "scripts/lint-architecture-boundaries.sh AC9",
            "architecture_test": "tests/integration/test_architecture_owner.py",
            "mutation_break": "both tests fail when owner routing is removed",
        }
    )

    assert list(_validator().iter_errors(document)) == []
