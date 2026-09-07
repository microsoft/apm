"""Quality contract for the APMLifecycle escaped-defect learning ledger."""

from __future__ import annotations

import ast
import json
from pathlib import Path

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


def _defined_test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            names.add(node.name)
    return names


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

    assert referenced_properties == set(property_ids)
    gap_ids = [gap["id"] for gap in known_gaps]
    assert gap_ids
    assert len(gap_ids) == len(set(gap_ids))
    for gap in known_gaps:
        assert set(gap["properties"]) <= set(property_ids)
        assert gap["bounded_by"]
        assert gap["next_decision"]


def test_lifecycle_bug_ledger_regression_nodeids_exist() -> None:
    ledger = _load_ledger()
    defined_by_path: dict[Path, set[str]] = {}

    for row in ledger["bugs"]:
        for nodeid in row["regression_tests"]:
            path_text, separator, test_name = nodeid.partition("::")
            leaf_name = test_name.split("::")[-1]
            assert separator and leaf_name.startswith("test_"), f"Invalid test nodeid: {nodeid}"
            path = _REPOSITORY_ROOT / path_text
            assert path.is_file(), f"Missing regression test file: {path_text}"
            names = defined_by_path.setdefault(path, _defined_test_names(path))
            assert leaf_name in names, f"Missing regression test: {nodeid}"

    for gap in ledger["known_gaps"]:
        path_text, separator, test_name = gap["bounded_by"].partition("::")
        assert separator and test_name.startswith("test_")
        path = _REPOSITORY_ROOT / path_text
        assert path.is_file()
        assert test_name in _defined_test_names(path)
