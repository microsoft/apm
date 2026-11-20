"""Shrink-only baseline, generator and negative control of the receipts gate (#4122)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import check_promise_receipts as gate
from tests.lifecycle import receipts
from tests.lifecycle.obligations import load_catalog

_OBLIGATIONS = Path("tests/quality/promise_obligations.json")
_IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false")


def _receipt(node: str, law: str, op: str, outcome: str = "success") -> receipts.Receipt:
    return receipts.Receipt(node, {"op": op, "cells": []}, "", (), "project", "", law, outcome)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / _OBLIGATIONS
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"baseline": {"P-a": "T-1", "P-b": "T-2"}}), encoding="ascii")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, *_IDENTITY, "commit", "-qm", "base")
    return tmp_path


def _set_baseline(root: Path, rows: dict[str, str]) -> None:
    (root / _OBLIGATIONS).write_text(json.dumps({"baseline": rows}), encoding="ascii")


def test_a_shrinking_baseline_passes_the_ratchet(repo: Path) -> None:
    _set_baseline(repo, {"P-a": "T-1"})
    assert gate.main(["--root", str(repo), "--base-ref", "HEAD"]) == 0


def test_an_added_baseline_row_fails_the_ratchet(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_baseline(repo, {"P-a": "T-1", "P-c": "T-1"})
    assert gate.main(["--root", str(repo), "--base-ref", "HEAD"]) == 1
    assert "  P-c" in capsys.readouterr().err.splitlines()


def test_a_base_without_the_obligation_file_skips_the_ratchet(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, *_IDENTITY, "commit", "-qm", "e", "--allow-empty")
    (tmp_path / _OBLIGATIONS).parent.mkdir(parents=True)
    _set_baseline(tmp_path, {"P-a": "T-1"})
    assert gate.main(["--root", str(tmp_path), "--base-ref", "HEAD"]) == 0


def test_the_generator_promotes_only_receipted_baseline_rows() -> None:
    catalog = load_catalog()
    covered, residual = sorted(catalog.baseline)[:2]
    law, op = catalog.obligations[covered].law, catalog.obligations[covered].op
    assert law is not None and op is not None
    earned = [_receipt("tests/lifecycle/test_x.py::test_y", law, op)]
    data = gate.regenerate(catalog, earned)
    assert covered not in data["baseline"] and residual in data["baseline"]
    assert data["required"][covered] == "tests/lifecycle/test_x.py::test_y"
    assert set(data["required"]) | set(data["baseline"]) == set(catalog.obligations)
    assert gate.regenerate(catalog, []) == json.loads(json.dumps(catalog.raw))


def test_the_negative_control_drops_exactly_the_one_load_bearing_receipt(tmp_path: Path) -> None:
    catalog = load_catalog()
    key = sorted(catalog.required)[0]
    witness, obligation = catalog.required[key], catalog.obligations[key]
    assert obligation.law is not None and obligation.op is not None
    load_bearing = _receipt(witness, obligation.law, obligation.op, obligation.outcome)
    other = _receipt(witness, "I6", "install")
    lines = [load_bearing.to_json(), other.to_json()]
    (tmp_path / "session-a.jsonl").write_text("".join(f"{x}\n" for x in lines), encoding="ascii")

    assert gate.drop_receipts(catalog, tmp_path) == (key, witness, 1)
    assert receipts.load(tmp_path) == [other]
