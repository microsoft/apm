"""Quality contract for the APMLifecycle escaped-defect learning ledger."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.lifecycle_contracts import command_inventory, validate_contracts

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_LEDGER_PATH = _REPOSITORY_ROOT / "tests/fixtures/lifecycle_bug_ledger.json"
_FAILURE_MODES = frozenset(
    {
        "auth",
        "cache",
        "cleanup",
        "idempotency",
        "observation",
        "outcome",
        "ownership",
        "portability",
        "reference",
        "routing",
        "transaction",
    }
)
RATCHET_TEST_SCOPE = "repository"


def _load_ledger() -> dict[str, object]:
    payload = json.loads(_LEDGER_PATH.read_text(encoding="ascii"))
    assert isinstance(payload, dict)
    return payload


def test_lifecycle_bug_ledger_has_valid_taxonomy_and_unique_references() -> None:
    ledger = _load_ledger()

    assert ledger["schema_version"] == 1
    assert "not an issue-count census" in str(ledger["scope"])
    property_rows = ledger["property_catalog"]
    bug_rows = ledger["bugs"]
    known_gaps = ledger["known_gaps"]
    assert isinstance(property_rows, list)
    assert isinstance(bug_rows, list)
    assert isinstance(known_gaps, list)
    property_ids = [row["id"] for row in property_rows]
    assert len(property_ids) == len(set(property_ids))
    assert all(row["law"] for row in property_rows)
    phases = {row["phase"] for row in property_rows}
    assert phases
    assert phases <= {0, 1}
    assert {row["oracle_tier"] for row in property_rows} == {
        "open-world",
        "outcome",
        "semantic",
    }

    issues = [row["issue"] for row in bug_rows]
    assert issues == sorted(issues)
    assert len(issues) == len(set(issues))
    referenced_properties: set[str] = set()
    for row in bug_rows:
        assert row["summary"]
        assert set(row["failure_modes"]) <= _FAILURE_MODES
        assert row["failure_modes"]
        assert row["properties"]
        assert set(row["properties"]) <= set(property_ids)
        assert row["regression_tests"]
        referenced_properties.update(row["properties"])

    contracts = validate_contracts(ledger, command_inventory())
    for contract in contracts.values():
        referenced_properties.update(contract["properties"])
    assert referenced_properties == set(property_ids)
    gap_ids = [gap["id"] for gap in known_gaps]
    assert gap_ids
    assert len(gap_ids) == len(set(gap_ids))
    for gap in known_gaps:
        assert set(gap["properties"]) <= set(property_ids)
        assert gap["bounded_by"]
        assert gap["next_decision"]


def test_lifecycle_bug_ledger_regression_nodeids_exist() -> None:
    """Legacy bug rows may name parameter families; pytest must collect each one.

    General feature contracts require exact parameterized nodes at execution.
    This inventory check is not a substitute for their executing gate.
    """
    ledger = _load_ledger()
    references = {nodeid for row in ledger["bugs"] for nodeid in row["regression_tests"]} | {
        gap["bounded_by"] for gap in ledger["known_gaps"]
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
            "-o",
            "addopts=",
            *sorted(references),
        ],
        cwd=_REPOSITORY_ROOT,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    collected = {line.strip() for line in result.stdout.splitlines() if line.startswith("tests/")}
    for nodeid in references:
        assert any(
            candidate == nodeid or candidate.startswith(f"{nodeid}[") for candidate in collected
        ), f"Uncollected regression reference: {nodeid}"
