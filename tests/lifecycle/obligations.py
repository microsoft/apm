"""Promise obligations and the receipt ratchet of the lifecycle family (#4093).

The universe of obligations is the invariant x operation APPLY table in
``tests/quality/promise_obligations.json`` plus the GATE cells in
``tests/quality/gate_cell_obligations.json``. Every obligation is either
``required`` (it names the witness test whose oracle receipts must cover it)
or in the ``baseline`` of still-uncovered obligations (it names the lane and
issue that will cover it). The 0.37 release needs an empty baseline.

``ReceiptWitnessPlugin`` fails a passing witness test whose receipts do not
cover its required obligations, so the ratchet holds inside the existing
lifecycle shards.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import pytest

from tests.lifecycle import receipts

QUALITY_DIR = Path(__file__).parents[1] / "quality"
OBLIGATIONS_PATH = QUALITY_DIR / "promise_obligations.json"
GATE_CELLS_PATH = QUALITY_DIR / "gate_cell_obligations.json"
PROMISE_PREFIX = "P-INV-"
# The I7 promise is about failed commands, so only a refusal witnesses it.
_REFUSAL_LAWS = frozenset({"I7"})
# I3 is an agreement between two commands; the paired probe witnesses both.
_PAIRED_LAWS = frozenset({"I3"})


@dataclass(frozen=True)
class Obligation:
    """One promise cell a receipt must cover."""

    id: str
    law: str | None = None
    op: str | None = None
    cell: str | None = None

    @property
    def outcome(self) -> str:
        if self.law in _REFUSAL_LAWS:
            return "refusal"
        return "success" if self.law else "any"

    def covered_by(self, receipt: receipts.Receipt) -> bool:
        if self.outcome not in ("any", receipt.outcome):
            return False
        params = receipt.params
        if self.cell is not None:
            return self.cell in params.get("cells", ())
        participants = {params.get("op")}
        if self.law in _PAIRED_LAWS:
            participants.update(params.get("paired", ()))
        return receipt.oracle_id == self.law and self.op in participants


@dataclass(frozen=True)
class Catalog:
    """The obligation universe and its required / baseline split."""

    obligations: Mapping[str, Obligation]
    required: Mapping[str, str]
    baseline: Mapping[str, str]
    lanes: Mapping[str, str]
    raw: Mapping[str, object]

    @cached_property
    def _by_witness(self) -> Mapping[str, tuple[Obligation, ...]]:
        index: dict[str, list[Obligation]] = {}
        for key, witness in self.required.items():
            index.setdefault(witness, []).append(self.obligations[key])
        return {witness: tuple(items) for witness, items in index.items()}

    def witnessed_by(self, node: str) -> tuple[Obligation, ...]:
        return self._by_witness.get(receipts.normalize_node(node), ())


def invariant_op_id(law: str, op: str) -> str:
    return f"{PROMISE_PREFIX}{law}|op:{op}"


def load_catalog(path: Path = OBLIGATIONS_PATH, gate_cells_path: Path = GATE_CELLS_PATH) -> Catalog:
    data = json.loads(path.read_text(encoding="ascii"))
    obligations: dict[str, Obligation] = {}
    for law, ops in data["invariant_ops"].items():
        for op in ops:
            key = invariant_op_id(law, op)
            obligations[key] = Obligation(key, law=law, op=op)
    for cell in json.loads(gate_cells_path.read_text(encoding="ascii"))["cells"]:
        obligations[cell] = Obligation(cell, cell=cell)
    return Catalog(obligations, data["required"], data["baseline"], data["lanes"], data)


def uncovered(obligations: Iterable[Obligation], earned: Collection[receipts.Receipt]) -> list[str]:
    """Ids of *obligations* that no receipt in *earned* covers."""
    return [item.id for item in obligations if not any(item.covered_by(r) for r in earned)]


def witness_failure(
    catalog: Catalog, node: str, earned: Collection[receipts.Receipt]
) -> str | None:
    """Why a passing *node* fails the ratchet: required obligations it did not earn."""
    missing = uncovered(catalog.witnessed_by(node), earned)
    if not missing:
        return None
    return (
        f"{node} is the witness of {len(missing)} required obligation(s) "
        "that no oracle receipt covered:\n  " + "\n  ".join(sorted(missing))
    )


class ReceiptWitnessPlugin:
    """Session wiring for receipts plus the per-witness ratchet check."""

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    def pytest_configure(self, config: pytest.Config) -> None:
        if not os.environ.get(receipts.DIRECTORY_ENV):
            os.environ[receipts.DIRECTORY_ENV] = str(_receipts_dir(config))
        if not os.environ.get("PYTEST_XDIST_WORKER"):
            os.environ.setdefault(receipts.SESSION_ENV, uuid.uuid4().hex[:12])

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item: pytest.Item, call: pytest.CallInfo):
        outcome = yield
        report = outcome.get_result()
        if call.when != "call" or not report.passed:
            return
        failure = witness_failure(self._catalog, item.nodeid, receipts.earned(item.nodeid))
        if failure:
            report.outcome = "failed"
            report.longrepr = failure

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if os.environ.get("PYTEST_XDIST_WORKER"):
            return
        directory = os.environ.get(receipts.DIRECTORY_ENV)
        session_id = os.environ.get(receipts.SESSION_ENV)
        if directory and session_id:
            receipts.merge_session(Path(directory), session_id)


def _receipts_dir(config: pytest.Config) -> Path:
    cache = getattr(config, "cache", None)
    if cache is not None:
        return Path(cache.mkdir("lifecycle-receipts"))
    # CI runs with -p no:cacheprovider; keep the same layout under the root.
    return config.rootpath / ".pytest_cache" / "d" / "lifecycle-receipts"
