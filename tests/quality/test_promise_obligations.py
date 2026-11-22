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
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.lifecycle import oracles, receipts
from tests.lifecycle.obligations import (
    OBLIGATIONS_PATH,
    PROMISE_PREFIX,
    Catalog,
    Obligation,
    ReceiptWitnessPlugin,
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
# 107 less the 12 user-scope org-deny cells (user scope is not governed by org
# policy, #4087) plus the user-scope undeclare|plugin|deny cell.
_APPROVED_GATE_CELLS = 96
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


def _receipt(node: str) -> receipts.Receipt:
    return receipts.Receipt(node, {"op": "install"}, "", (), "project", "", "I4", "pass")


def _plugin_session(monkeypatch: pytest.MonkeyPatch, default: Path) -> ReceiptWitnessPlugin:
    """A plugin configured in a fresh main process on the default cache *default*."""
    for name in (receipts.DIRECTORY_ENV, receipts.SESSION_ENV, "PYTEST_XDIST_WORKER"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    plugin = ReceiptWitnessPlugin(load_catalog())
    plugin.pytest_configure(SimpleNamespace(cache=SimpleNamespace(mkdir=lambda _name: default)))
    return plugin


def _stale_session(directory: Path, node: str) -> receipts.Receipt:
    stale = _receipt(node)
    (directory / "session-old.jsonl").write_text(stale.to_json() + "\n", encoding="ascii")
    return stale


def _run_session(plugin: ReceiptWitnessPlugin, directory: Path, *nodes: str) -> None:
    """Write this session's per-process receipts, then finish the session."""
    session = os.environ[receipts.SESSION_ENV]
    lines = "".join(_receipt(node).to_json() + "\n" for node in nodes)
    if lines:
        (directory / f"{session}-main-1.part.jsonl").write_text(lines, encoding="ascii")
    plugin.pytest_sessionfinish(SimpleNamespace())


def test_a_witness_session_replaces_stale_default_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Earlier partial runs in the default cache must not judge this run's witnesses."""
    witness = next(iter(load_catalog().required.values()))
    plugin = _plugin_session(monkeypatch, tmp_path)
    _stale_session(tmp_path, witness)
    assert os.environ[receipts.DIRECTORY_ENV] == str(tmp_path)
    _run_session(plugin, tmp_path, witness, witness)
    assert receipts.load(tmp_path) == [_receipt(witness), _receipt(witness)]


@pytest.mark.parametrize("nodes", [(), ("tests/lifecycle/test_unrelated.py::test_x",)])
def test_a_session_without_a_witness_keeps_earlier_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nodes: tuple[str, ...]
) -> None:
    """A collect-only run (no receipts) or a run of no witness never drops receipts."""
    witness = next(iter(load_catalog().required.values()))
    plugin = _plugin_session(monkeypatch, tmp_path)
    stale = _stale_session(tmp_path, witness)
    _run_session(plugin, tmp_path, *nodes)
    assert stale in receipts.load(tmp_path)


def test_an_explicit_receipts_directory_keeps_every_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    witness = next(iter(load_catalog().required.values()))
    _plugin_session(monkeypatch, tmp_path / "default")
    monkeypatch.setenv(receipts.DIRECTORY_ENV, str(tmp_path))
    plugin = ReceiptWitnessPlugin(load_catalog())
    plugin.pytest_configure(SimpleNamespace(cache=None))
    stale = _stale_session(tmp_path, witness)
    _run_session(plugin, tmp_path, witness)
    assert stale in receipts.load(tmp_path)
    assert len(receipts.load(tmp_path)) == 2


def test_required_receipts_fail_when_one_witness_did_not_report() -> None:
    """A full-set run that deselected one witness fails once receipts are required (#4586)."""
    catalog = load_catalog()
    scope = "tests/lifecycle/,!tests/lifecycle/journeys/"
    every = set(catalog.required.values()) | set(catalog.generated_required.values())
    witnesses = sorted(node for node in every if in_scope(node, scope))
    assert any("/security/" in node for node in witnesses), "generated witnesses are in scope"
    assert not any("/journeys/" in node for node in witnesses)
    for dropped in (sorted(set(catalog.required.values()))[0], witnesses[-1]):
        earned = [_receipt(node) for node in witnesses if node != dropped]
        with pytest.raises(AssertionError, match=re.escape(dropped)):
            _hold_the_ratchet(catalog, earned, required=True, scope=scope)


def test_receipt_scope_includes_and_excludes_by_prefix() -> None:
    journey = "tests/lifecycle/journeys/test_journeys.py::test_journey[J01]"
    table = "tests/lifecycle/security/test_egress.py::test_x[E1|a]"
    assert in_scope(journey, "") and in_scope(table, "")
    assert not in_scope(journey, "tests/lifecycle/,!tests/lifecycle/journeys/")
    assert in_scope(table, "tests/lifecycle/,!tests/lifecycle/journeys/")
    assert in_scope(journey, "tests/lifecycle/journeys/")
    assert not in_scope(table, "tests/lifecycle/journeys/")


def test_the_full_lifecycle_run_requires_its_receipts() -> None:
    """lifecycle-full writes receipts to one directory and then requires a receipt per witness."""
    workflow = yaml.safe_load(
        (_ROOT / ".github/workflows/lifecycle-journeys.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["lifecycle-full"]["steps"]
    run = next(step for step in steps if "--ignore=tests/lifecycle/journeys" in step.get("run", ""))
    check = next(
        step for step in steps if "test_merged_receipts_hold_the_ratchet" in step.get("run", "")
    )
    assert steps.index(check) > steps.index(run)
    assert check["env"]["APM_LIFECYCLE_RECEIPTS_REQUIRED"] == "1"
    assert check["env"][RECEIPTS_SCOPE_ENV] == "tests/lifecycle/,!tests/lifecycle/journeys/"
    directory = run["env"][receipts.DIRECTORY_ENV]
    assert directory and check["env"][receipts.DIRECTORY_ENV] == directory
    journeys = workflow["jobs"]["journeys"]["steps"]
    run = next(step for step in journeys if "test_journeys.py" in step.get("run", ""))
    check = next(
        step for step in journeys if "test_merged_receipts_hold_the_ratchet" in step.get("run", "")
    )
    assert check["env"][RECEIPTS_SCOPE_ENV] == "tests/lifecycle/journeys/"
    assert check["env"]["APM_LIFECYCLE_RECEIPTS_REQUIRED"] == "1"
    assert check["env"][receipts.DIRECTORY_ENV] == run["env"][receipts.DIRECTORY_ENV]


def test_required_receipts_never_pass_vacuously() -> None:
    catalog = load_catalog()
    with pytest.raises(AssertionError, match="no lifecycle receipts"):
        _hold_the_ratchet(catalog, [], required=True)
    with pytest.raises(AssertionError, match="no required witness reported"):
        _hold_the_ratchet(
            catalog, [_receipt("tests/lifecycle/test_unrelated.py::test_x")], required=True
        )
    _hold_the_ratchet(catalog, [], required=False)


# -- merged receipts of a lifecycle run ---------------------------------------


def _receipts_dir() -> Path:
    configured = os.environ.get(receipts.DIRECTORY_ENV)
    return Path(configured) if configured else _ROOT / ".pytest_cache/d/lifecycle-receipts"


def test_merged_receipts_hold_the_ratchet() -> None:
    """Each witness that reported covers its rows; a covered baseline row must be promoted.

    With ``APM_LIFECYCLE_RECEIPTS_REQUIRED=1`` every required witness in the job's
    scope (``APM_LIFECYCLE_RECEIPTS_SCOPE``) must have reported, the promise
    witnesses and the generated (security, journey, replay, machine) witnesses alike.
    """
    required = os.environ.get("APM_LIFECYCLE_RECEIPTS_REQUIRED") == "1"
    scope = os.environ.get(RECEIPTS_SCOPE_ENV, "")
    _hold_the_ratchet(
        load_catalog(), receipts.load(_receipts_dir()), required=required, scope=scope
    )


RECEIPTS_SCOPE_ENV = "APM_LIFECYCLE_RECEIPTS_SCOPE"


def in_scope(node: str, scope: str) -> bool:
    """*scope*: comma-separated node path prefixes; a ``!`` prefix excludes. Empty: every node."""
    rules = [rule.strip() for rule in scope.split(",") if rule.strip()]
    include = [rule for rule in rules if not rule.startswith("!")]
    exclude = [rule[1:] for rule in rules if rule.startswith("!")]
    return (not include or any(node.startswith(p) for p in include)) and not any(
        node.startswith(p) for p in exclude
    )


def _hold_the_ratchet(
    catalog: Catalog, earned: list[receipts.Receipt], *, required: bool, scope: str = ""
) -> None:
    by_node: dict[str, list[receipts.Receipt]] = {}
    for receipt in earned:
        by_node.setdefault(receipt.node, []).append(receipt)
    witnesses = set(catalog.required.values())
    if required:
        expected = {
            node
            for node in witnesses | set(catalog.generated_required.values())
            if in_scope(node, scope)
        }
        assert earned, f"no lifecycle receipts under {_receipts_dir()}; run tests/lifecycle first"
        assert expected & set(by_node), "no required witness reported a receipt"
        assert expected <= set(by_node), sorted(expected - set(by_node))
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


def test_i9_leaves_a_duplicate_owned_by_a_known_output_cell_to_the_global_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.utils import lifecycle_evidence

    # A synthetic cell keeps this law independent of the live known-cell inventory.
    cell = lifecycle_evidence.KnownOutputCell(
        "synthetic-repeated-warning",
        "I9",
        "#0",
        lambda args, _scenario: lifecycle_evidence._command_name(args) == "approve",
        r"\[!\] Synthetic repeated warning",
    )
    monkeypatch.setattr(lifecycle_evidence, "KNOWN_OUTPUT_CELLS", (cell,))
    warning = "[!] Synthetic repeated warning"
    stdout = f"[i] p declares:\n{warning}\n{warning}\n[i] a\n[i] a\n[+] Approved p\n"
    result = CommandResult(("apm", "approve", "p"), 0, stdout, "", tmp_path)
    found = [v.message for v in oracles._i9_no_noise(None, None, result, None, None)]
    assert found == ["identical consecutive line: '[i] a'"]


def test_generated_witnesses_match_the_collected_tables_and_journeys() -> None:
    """generated_obligations.json is current: a dropped or added known cell needs --write."""
    root = Path(__file__).parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "tests.lifecycle.witnesses"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    fix = (
        "generated_obligations.json is stale: run "
        "`python -m tests.lifecycle.witnesses --write` and commit generated_obligations.json"
    )
    assert result.returncode == 0, f"{fix}\n{result.stdout}{result.stderr}"
    assert "[+] up to date:" in result.stdout, f"{fix}\n{result.stdout}"


def collected_scenario_nodes(run=subprocess.run) -> set[str]:
    """Node ids of ``tests/lifecycle/scenarios``, from a collection that succeeded.

    pytest still prints the node ids of the modules it could collect when another
    module fails to import, then exits nonzero; trusting that partial list would
    hide every credit of the broken module. Exit 5 (nothing collected) is a
    failure too: the scenario directory is never empty.
    """
    root = Path(__file__).parents[2]
    result = run(
        [sys.executable, "-m", "pytest", "tests/lifecycle/scenarios", "--collect-only", "-q"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    tail = "\n".join(f"{result.stdout}\n{result.stderr}".strip().splitlines()[-20:])
    assert result.returncode == 0, (
        f"scenario collection failed (exit {result.returncode}); fix it before "
        f"trusting its node ids:\n{tail}"
    )
    collected = {line.strip() for line in result.stdout.splitlines() if "::" in line}
    assert collected, f"collection found no scenario nodes\n{tail}"
    return collected


def test_every_scenario_credit_names_a_collected_node() -> None:
    """A CREDITS key that matches no collected node id credits nothing real (#4586).

    Parametrize ids join the values bottom decorator first, so a hand-built key
    in the wrong order still imports and still feeds the generator a witness.
    """
    from tests.lifecycle.generator.witnesses import scenario_credits

    collected = collected_scenario_nodes()
    stray = sorted({credit.node for credit in scenario_credits()} - collected)
    assert not stray, f"CREDITS name nodes pytest never collects: {stray[:5]}"


def test_a_partial_scenario_collection_is_never_trusted() -> None:
    """Node ids printed by a collection that exited nonzero fail the guard."""
    calls: list[list[str]] = []

    def partial(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args,
            2,
            stdout="tests/lifecycle/scenarios/test_a.py::test_one\n1 error\n",
            stderr="ImportError while importing test module test_b.py\n",
        )

    with pytest.raises(AssertionError, match=r"scenario collection failed \(exit 2\)") as failed:
        collected_scenario_nodes(run=partial)
    assert "ImportError while importing test module test_b.py" in str(failed.value)
    assert calls and "--collect-only" in calls[0], calls
