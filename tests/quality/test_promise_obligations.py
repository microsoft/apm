"""The lifecycle promise-obligation ratchet (#4093).

``tests/quality/promise_obligations.json`` splits the obligation universe
(the approved invariant x operation APPLY table plus the GATE cells) into
``required`` rows, each witnessed by a lifecycle test that must earn an oracle
receipt for it, and a ``baseline`` of still-uncovered rows owned by a later
lane. The witness check runs inside the lifecycle shards
(``ReceiptWitnessPlugin``); this module checks the catalog, the negative
control and, when a receipts directory is present, the merged receipts.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from tests.lifecycle import oracles, receipts
from tests.lifecycle.obligations import (
    OBLIGATIONS_PATH,
    PROMISE_PREFIX,
    Obligation,
    load_catalog,
    promise_ids,
    uncovered,
    witness_failure,
)
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.lifecycle_state import LifecycleStateSnapshot

RATCHET_TEST_SCOPE = "repository"

_ROOT = Path(__file__).parents[2]
_OPS = [
    "install",
    "update",
    "uninstall",
    "prune",
    "compile",
    "lock",
    "pack",
    "bundle-install",
    "mcp-install",
    "approve-deny",
    "audit",
    "marketplace",
]
# The steward-approved APPLY table (#4093). Changing it is an approved rule change.
_APPROVED_APPLY = {
    "I1": ["install", "update", "uninstall", "prune", "lock", "bundle-install", "mcp-install"],
    "I2": [op for op in _OPS if op != "audit"],
    "I3": ["install", "update", "bundle-install", "mcp-install", "audit"],
    "I4": [
        "install",
        "update",
        "uninstall",
        "lock",
        "bundle-install",
        "mcp-install",
        "approve-deny",
        "audit",
    ],
    **{law: list(_OPS) for law in ("I5", "I6", "I7", "I8", "I9")},
}
_APPROVED_GATE_CELLS = 107
_LANES = {
    "T-1": "#4095",
    "T-2": "#4096",
    "T-3": "#4097",
    "T-4": "#4098",
    "T-5": "#4099",
    "T-6a": "#4100",
    "T-6b": "#4101",
    "T-6c": "#4102",
    "T-6d": "#4103",
    "T-6e": "#4104",
    "T-6f": "#4105",
    "T-6g": "#4106",
    "T-7a": "#4107",
    "T-7b": "#4108",
    "T-7c": "#4109",
    "T-8a": "#4110",
    "T-8b": "#4111",
    "T-8c": "#4112",
}
_WITNESS_ROOTS = ("tests/lifecycle/", "tests/integration/")


def test_universe_is_the_approved_apply_table_plus_the_gate_cells() -> None:
    catalog = load_catalog()
    data = json.loads(OBLIGATIONS_PATH.read_text(encoding="ascii"))
    assert data["schema_version"] == 1
    assert data["ops"] == _OPS
    assert {law: sorted(ops) for law, ops in data["invariant_ops"].items()} == {
        law: sorted(ops) for law, ops in _APPROVED_APPLY.items()
    }
    pairs = [key for key in catalog.obligations if key.startswith(PROMISE_PREFIX)]
    assert len(pairs) == 91
    assert len(catalog.obligations) - len(pairs) == _APPROVED_GATE_CELLS
    required, baseline = set(catalog.required), set(catalog.baseline)
    assert not required & baseline, sorted(required & baseline)
    assert required | baseline == set(catalog.obligations), (
        f"unlisted: {sorted(set(catalog.obligations) - required - baseline)}; "
        f"unknown: {sorted((required | baseline) - set(catalog.obligations))}"
    )
    assert list(catalog.required) == sorted(catalog.required)
    assert list(catalog.baseline) == sorted(catalog.baseline)


def test_promise_ids_resolve_in_the_promise_catalog() -> None:
    catalog = promise_ids()
    for obligation in load_catalog().obligations.values():
        if obligation.law is not None:
            assert obligation.law in oracles.ORACLE_LAWS, obligation
            assert obligation.promise in catalog, obligation


def test_baseline_rows_name_a_real_lane() -> None:
    catalog = load_catalog()
    assert dict(catalog.lanes) == _LANES
    for key, lane in catalog.baseline.items():
        assert lane in _LANES, (key, lane)


def test_required_witnesses_are_lifecycle_smoke_tests() -> None:
    for key, witness in load_catalog().required.items():
        path, _, test = witness.partition("::")
        assert path.startswith(_WITNESS_ROOTS), (key, witness)
        source = (_ROOT / path).read_text(encoding="utf-8")
        name = re.sub(r"\[.*\]$", "", test)
        assert re.search(rf"^def {re.escape(name)}\(", source, re.MULTILINE), (key, witness)
        assert "pytest.mark.lifecycle_smoke" in source, (key, witness)
        assert "lifecycle_merge_group" not in source, (key, witness)


# -- negative control ---------------------------------------------------------


class _Model:
    def __init__(self, converged: bool) -> None:
        self._converged = converged

    def allowed_surfaces(self) -> frozenset[str]:
        return frozenset()

    def converged_before(self) -> bool:
        return self._converged

    def expected_cells(self, op, before, after, result):
        return {}

    def record_expected(self, cell_id: str) -> None:
        raise AssertionError(cell_id)


def _earn_install_receipts(tmp_path: Path) -> list[receipts.Receipt]:
    """One successful and one refused install through the oracle, on a still world."""
    state = LifecycleStateSnapshot.capture(tmp_path)
    artifacts = ArtifactSnapshotSet.capture({"project": tmp_path})
    observation = oracles.Observation(
        state, artifacts, frozenset(), tmp_path, None, frozenset({"absent"})
    )
    start = len(receipts.earned())
    # A converged install repeated as a no-op, then a refused one.
    for code, fields, out in (
        (0, {"paired": {"audit": 0}}, "[+] done\n"),
        (2, {}, "[x] Usage error: no target\n"),
    ):
        result = CommandResult(("apm", "install"), code, out, "", tmp_path)
        op = oracles.operation_for(("install",), forbidden={"project": ("*",)}, **fields)
        oracles.check_cross_cutting(observation, observation, result, _Model(code == 0), op)
    return list(receipts.earned()[start:])


def _install_obligations() -> list[Obligation]:
    return [
        obligation
        for obligation in load_catalog().obligations.values()
        if obligation.op == "install"
    ]


@pytest.fixture
def isolated_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep synthetic receipts out of the session directory."""
    monkeypatch.setattr(receipts, "_EARNED", [])
    monkeypatch.delenv(receipts.DIRECTORY_ENV, raising=False)


@pytest.mark.usefixtures("isolated_receipts")
def test_the_oracle_earns_every_install_obligation(tmp_path: Path) -> None:
    assert uncovered(_install_obligations(), _earn_install_receipts(tmp_path)) == []


@pytest.mark.usefixtures("isolated_receipts")
@pytest.mark.parametrize("law", sorted(oracles._CHECKS))
def test_removing_one_oracle_call_uncovers_its_obligations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, law: str
) -> None:
    """Negative control: without the law's check, its obligations lose their receipts."""
    monkeypatch.delitem(oracles._CHECKS, law)
    missing = uncovered(_install_obligations(), _earn_install_receipts(tmp_path))
    assert missing == [f"{PROMISE_PREFIX}{law}|op:install"]


# -- closed-world guards and their mutation cases ------------------------------


def _forged() -> receipts.Receipt:
    return receipts.Receipt(
        node="forged",
        params={"op": "install", "cells": []},
        pre_digest="",
        argv=("apm", "install"),
        scope="project",
        post_digest="",
        oracle_id="I4",
        outcome="success",
    )


@pytest.mark.usefixtures("isolated_receipts")
def test_only_the_oracle_can_write_a_receipt() -> None:
    with pytest.raises(receipts.ReceiptForgeryError, match=r"only by tests\.lifecycle\.oracles"):
        receipts.emit(_forged())
    assert receipts.earned() == ()


@pytest.mark.usefixtures("isolated_receipts")
def test_mutation_without_the_caller_guard_a_test_body_earns_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling only the caller guard lets a test body forge an I4 receipt."""
    monkeypatch.setattr(receipts, "OWNER_MODULE", __name__)
    receipts.emit(_forged())
    assert uncovered([Obligation("P-INV-I4|op:install", "I4", "install")], receipts.earned()) == []


def test_a_witness_without_its_receipts_fails_the_ratchet() -> None:
    catalog = load_catalog()
    witness = next(iter(catalog.required.values()))
    failure = witness_failure(catalog, witness, ())
    assert failure is not None and "no oracle receipt covered" in failure
    assert witness_failure(catalog, "tests/lifecycle/test_unrelated.py::test_x", ()) is None


def test_a_refusal_never_witnesses_a_success_obligation() -> None:
    receipt = receipts.Receipt("n", {"op": "install"}, "", (), "project", "", "I4", "refusal")
    assert not Obligation("P-INV-I4|op:install", "I4", "install").covered_by(receipt)
    assert Obligation("P-INV-I7|op:install", "I7", "install").covered_by(
        receipts.Receipt("n", {"op": "install"}, "", (), "project", "", "I7", "refusal")
    )


# -- merged receipts of a lifecycle run ---------------------------------------


def _receipts_dir() -> Path:
    configured = os.environ.get(receipts.DIRECTORY_ENV)
    return Path(configured) if configured else _ROOT / ".pytest_cache/d/lifecycle-receipts"


def test_merged_receipts_hold_the_ratchet() -> None:
    """Each witness that reported covers its rows; a covered baseline row must be promoted.

    With ``APM_LIFECYCLE_RECEIPTS_REQUIRED=1`` every witness must have reported.
    """
    catalog = load_catalog()
    earned = receipts.load(_receipts_dir())
    by_node: dict[str, list[receipts.Receipt]] = {}
    for receipt in earned:
        by_node.setdefault(receipt.node, []).append(receipt)
    witnesses = set(catalog.required.values())
    if os.environ.get("APM_LIFECYCLE_RECEIPTS_REQUIRED") == "1":
        assert witnesses <= set(by_node), sorted(witnesses - set(by_node))
    problems = [
        failure
        for witness in sorted(witnesses & set(by_node))
        if (failure := witness_failure(catalog, witness, by_node[witness]))
    ]
    witnessed = [receipt for node in witnesses & set(by_node) for receipt in by_node[node]]
    promotable = [
        key for key in catalog.baseline if not uncovered([catalog.obligations[key]], witnessed)
    ]
    assert not promotable, f"baseline rows now covered; move them to required: {promotable}"
    assert not problems, "\n".join(problems)


def test_i9_reports_one_violation_per_duplicated_cause(tmp_path: Path) -> None:
    stdout = (
        "[i] a\n[i] a\n[i] b\n[i] b\n[+] p\n  |-- 1 skill integrated\n  |-- 1 skill integrated\n"
    )
    result = CommandResult(("apm", "install"), 0, stdout, "", tmp_path)
    found = [v.message for v in oracles._i9_no_noise(None, None, result, None, None)]
    assert found == [
        "identical consecutive line: '[i] a'",
        "identical consecutive line: '[i] b'",
        "identical consecutive line: '|-- 1 skill integrated'",
    ]
    nonadjacent = "[+] p\n  |-- 1 skill integrated\n  [i] n\n  |-- 1 skill integrated\n"
    result = CommandResult(("apm", "install"), 0, nonadjacent, "", tmp_path)
    found = [v.message for v in oracles._i9_no_noise(None, None, result, None, None)]
    assert found == ["duplicate 'integrated' line: '|-- 1 skill integrated'"]
