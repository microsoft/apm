"""The lifecycle bug ledger reads the same in its per-row and legacy layouts."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.lifecycle_ledger import (
    LEDGER_DIR,
    LEGACY_LEDGER,
    LedgerError,
    ledger_files,
    read_ledger,
    write_ledger,
)

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-native-lifecycle-evidence")]

ROOT = Path(__file__).parents[3]


def _ledger(*issues: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": "fixture",
        "property_catalog": [{"id": "idempotency.byte_stable", "phase": 1}],
        "bugs": [
            {"issue": issue, "summary": f"bug {issue}", "properties": ["idempotency.byte_stable"]}
            for issue in issues
        ],
        "lifecycle_contracts": [],
        "known_gaps": [{"id": "gap-a", "properties": []}],
    }


def _write_legacy(root: Path, ledger: dict[str, Any]) -> None:
    path = root / LEGACY_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="ascii")


def test_repository_ledger_reads_the_same_in_both_layouts(tmp_path: Path) -> None:
    ledger = read_ledger(ROOT)
    assert not (ROOT / LEGACY_LEDGER).exists()
    assert len(ledger["bugs"]) == len(list((ROOT / LEDGER_DIR / "bugs").iterdir()))

    legacy_root, directory_root = tmp_path / "legacy", tmp_path / "directory"
    _write_legacy(legacy_root, ledger)
    write_ledger(directory_root, ledger)

    assert read_ledger(legacy_root) == ledger
    assert read_ledger(directory_root) == ledger


def test_directory_layout_wins_over_the_legacy_file(tmp_path: Path) -> None:
    _write_legacy(tmp_path, _ledger(1))
    write_ledger(tmp_path, _ledger(1, 2))

    assert [row["issue"] for row in read_ledger(tmp_path)["bugs"]] == [1, 2]


def test_rows_are_ordered_by_id_whatever_the_file_order(tmp_path: Path) -> None:
    write_ledger(tmp_path, _ledger(10, 9, 100))

    assert [row["issue"] for row in read_ledger(tmp_path)["bugs"]] == [9, 10, 100]


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("bugs/7.json", json.dumps({"issue": 8, "summary": "renamed"})),
        ("bugs/extra/8.json", json.dumps({"issue": 8})),
        ("notes/a.json", json.dumps({"id": "a"})),
    ],
)
def test_misplaced_or_misnamed_rows_fail(tmp_path: Path, path: str, text: str) -> None:
    write_ledger(tmp_path, _ledger(1))
    target = tmp_path / LEDGER_DIR / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="ascii")

    with pytest.raises(LedgerError):
        read_ledger(tmp_path)


def test_duplicate_row_ids_cannot_be_written() -> None:
    ledger = _ledger(1)
    ledger["bugs"].append(copy.deepcopy(ledger["bugs"][0]))

    with pytest.raises(LedgerError, match="Duplicate"):
        ledger_files(ledger)
