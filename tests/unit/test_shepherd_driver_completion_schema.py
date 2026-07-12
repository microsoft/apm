"""Regression tests for shepherd-driver's canonical-owner completion gate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator

ROOT = Path(__file__).parents[2]
CANONICAL_SCHEMA = ROOT / "packages/shepherd-driver/assets/completion-schema.json"
MIRROR_SCHEMA = ROOT / ".agents/skills/shepherd-driver/assets/completion-schema.json"


def _validator() -> Draft7Validator:
    """Load the canonical shepherd-driver completion schema."""
    schema = json.loads(CANONICAL_SCHEMA.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def _ready_completion(classification: str, *, dual_guardrail_required: bool) -> dict[str, Any]:
    """Build the smallest ready-to-merge return relevant to owner-gate tests."""
    return {
        "kind": "completion",
        "pr": 1,
        "status": "ready-to-merge",
        "ci_evidence": "green",
        "lint_evidence": "green",
        "head_sha": "a" * 40,
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "ci_status": "green",
        "architecture_evidence": {
            "classification": classification,
            "decisions": [],
            "dual_guardrail_required": dual_guardrail_required,
            "boundary_lint": "exit 0 on exact head",
        },
    }


def test_completion_schema_mirror_is_byte_identical() -> None:
    """The deployed skill schema must not drift from its package source."""
    assert CANONICAL_SCHEMA.read_bytes() == MIRROR_SCHEMA.read_bytes()


@pytest.mark.parametrize("classification", ["new-owner", "split-authority-repair"])
def test_authority_creating_classification_cannot_disable_dual_guardrail(
    classification: str,
) -> None:
    """Authority-creating fixes must fail closed instead of self-exempting."""
    document = _ready_completion(classification, dual_guardrail_required=False)

    assert list(_validator().iter_errors(document))


def test_new_owner_requires_complete_dual_guardrail_evidence() -> None:
    """A new owner cannot become terminal with only the boolean assertion."""
    document = _ready_completion("new-owner", dual_guardrail_required=True)

    assert list(_validator().iter_errors(document))

    document["architecture_evidence"].update(
        {
            "behavioral_test": "tests/unit/test_owner.py",
            "static_guard": "scripts/lint-architecture-boundaries.sh AC9",
            "architecture_test": "tests/integration/test_architecture_owner.py",
            "mutation_break": "both tests fail when the owner routing is removed",
        }
    )
    assert list(_validator().iter_errors(document)) == []


@pytest.mark.parametrize(
    "classification",
    ["ordinary-fix", "owner-extension", "not-applicable"],
)
def test_non_creating_classification_can_reuse_existing_guardrails(
    classification: str,
) -> None:
    """Non-creating fixes may validly document that existing guards apply."""
    document = _ready_completion(classification, dual_guardrail_required=False)

    assert list(_validator().iter_errors(document)) == []
