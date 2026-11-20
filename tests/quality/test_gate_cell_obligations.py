"""The GATE obligation list is well formed and the visited GATE cells cover it (#4031).

The session hook in tests/integration/conftest.py writes the cells a run
visited to ``build/gate-cells/visited-<worker>.json`` (or
``$APM_GATE_CELLS_DIR``). For every scope whose mandatory GATE spine completed
in that run, the visited cells must cover that scope's obligations. With
``APM_GATE_CELLS_REQUIRED=1`` (the CI job that merges every lifecycle shard's
artifact) both scopes must have reported.
"""

from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path

from tests.integration._gate_machine_kit import KINDS, KNOWN_CELLS, SCOPES, GateMachine
from tests.utils.lifecycle_oracle import ORACLE_LAWS, ledger_property_ids

_ROOT = Path(__file__).parents[2]
_OBLIGATIONS = Path(__file__).with_name("gate_cell_obligations.json")
_CELL = re.compile(
    r"^cell:(?P<op>[a-z-]+)\|(?P<src>path|policy|none)\|"
    r"(?P<prim>hook|lsp|plugin|all|none)\|"
    r"(?P<gate>pending|allow|deny|org-deny|org-deny-all|none)\|(?P<scope>project|user)$"
)
_GATE_OPS = {
    "declare",
    "undeclare",
    "approve",
    "deny",
    "revoke",
    "install",
    "install-frozen",
    "update",
    "uninstall",
}


def _obligations() -> list[str]:
    data = json.loads(_OBLIGATIONS.read_text(encoding="ascii"))
    assert data["schema_version"] == 1
    return data["cells"]


def _visited() -> tuple[set[str], set[str]]:
    directory = Path(os.environ.get("APM_GATE_CELLS_DIR") or _ROOT / "build" / "gate-cells")
    cells: set[str] = set()
    spines: set[str] = set()
    for path in sorted(directory.glob("visited-*.json")) if directory.is_dir() else ():
        data = json.loads(path.read_text(encoding="ascii"))
        cells.update(data["cells"])
        spines.update(data["spines"])
    return cells, spines


def test_obligation_cells_are_sorted_unique_and_well_formed() -> None:
    cells = _obligations()
    assert cells == sorted(set(cells))
    for cell in cells:
        assert _CELL.match(cell), cell


def test_obligations_cover_every_gate_operation_primitive_and_scope() -> None:
    parsed = [_CELL.match(cell).groupdict() for cell in _obligations()]
    for scope in SCOPES:
        rows = [row for row in parsed if row["scope"] == scope]
        assert {row["op"] for row in rows} >= _GATE_OPS, scope
        assert set(KINDS) <= {row["prim"] for row in rows}, scope
        assert {"pending", "allow", "deny", "org-deny", "org-deny-all"} <= {
            row["gate"] for row in rows
        }, scope
    project_ops = {row["op"] for row in parsed if row["scope"] == "project"}
    assert {"audit-ci", "prune"} <= project_ops


def test_gate_machine_keeps_at_most_ten_rules() -> None:
    rules = [
        name
        for name, value in inspect.getmembers(GateMachine)
        if getattr(value, "hypothesis_stateful_rule", None) is not None
    ]
    assert 0 < len(rules) <= 10, rules


def test_oracle_laws_alias_existing_ledger_properties() -> None:
    known = ledger_property_ids()
    for law in ORACLE_LAWS.values():
        assert set(law.ledger_properties) <= known, law


def test_known_failing_cells_are_neutral_and_tracked() -> None:
    for cell in KNOWN_CELLS:
        assert re.fullmatch(r"gate-cell-[a-z0-9-]+", cell.cell_id), cell
        assert re.fullmatch(r"#\d+", cell.tracking), cell
        assert cell.law in ORACLE_LAWS, cell


def test_visited_cells_cover_the_obligations_of_every_completed_spine() -> None:
    cells, spines = _visited()
    if os.environ.get("APM_GATE_CELLS_REQUIRED") == "1":
        # CI merges every lifecycle shard's visited set: both spines must report.
        assert spines == set(SCOPES), f"GATE spines reported: {sorted(spines)}"
    obligations = set(_obligations())
    for scope in spines:
        wanted = {cell for cell in obligations if cell.endswith(f"|{scope}")}
        missing = wanted - cells
        assert not missing, f"{scope}: GATE obligation cells not visited: {sorted(missing)}"
