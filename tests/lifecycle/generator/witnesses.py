"""Machine witnesses of generated lifecycle obligations (#4096, #4097, #4099).

The state machines credit their own cell ids through
``oracles.check_obligation``: M-SOURCE ``T3|<op>|<form>|<kind>`` and
``T5|<dry-run|real>|<op>|<form>``, M-SCOPE-LOCK ``T4|<op>|<scope>|<lock>``.
This module translates each machine id into the factor assignment it proves
and lists the generated obligations that assignment implies (every pair and
named triple whose values it fixes). Such an obligation is ``required`` with
the machine spine as witness, and the receipt of any listed machine id
discharges it (``credited_by``). A machine id the machine records as a strict
known cell with an owning issue makes the obligation ``known-failing``.

Machine vocabulary that has no factor value here (``deps``/``outdated``
reads, ``corrupt``/``symlink`` locks, the ``native`` kind) implies nothing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import combinations

from tests.lifecycle.generator.factors import ON, ORDER, TRIPLES, cell_id

SOURCE_SPINE = (
    "tests/lifecycle/machines/test_m_source.py::test_source_spine_covers_every_form_and_op"
)
SCOPE_LOCK_SPINE = (
    "tests/lifecycle/machines/test_m_scope_lock.py::"
    "test_scope_lock_spine_refuses_every_write_and_keeps_reads[{scope}]"
)
# M-SOURCE dependency form -> SRC (the world marketplace is a git repository).
FORM_SOURCES: Mapping[str, str] = {
    "shorthand": "git",
    "git-object": "git",
    "local-path": "local-path",
    "marketplace-object": "marketplace-git",
    "plugin-at-mkt": "marketplace-git",
    "bundle": "local-bundle",
    "native": "native-plugin",
    "pkg-registry": "pkg-registry",
    "mcp-registry": "mcp-registry",
    "inline-mcp": "inline-mcp",
}
# M-SCOPE-LOCK op -> (OP, modifiers).
LOCK_OPS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "install": ("install", ()),
    "install-frozen": ("install", ("FROZEN",)),
    "update": ("update", ()),
    "lock": ("lock", ()),
    "prune": ("prune", ()),
    "compile": ("compile", ()),
    "add": ("install", ("SELECT",)),
    "remove": ("uninstall", ("SELECT",)),
    "audit": ("audit", ()),
    "audit-ci": ("audit", ()),
}
LOCK_STATES: Mapping[str, str] = {"consistent": "consistent", "newer": "newer-generator"}


@dataclass(frozen=True)
class MachineCell:
    """One machine cell id, its witness node and the factor values it proves."""

    id: str
    witness: str
    lane: str
    assignment: Mapping[str, str]
    tracking: str | None = None


def _source_assignment(key: str) -> dict[str, str] | None:
    family, *parts = key.split("|")
    if family == "T3":
        op, form, kind = parts
        if kind == "native":
            return None
        op = "bundle-install" if form == "bundle" and op == "install" else op
        return {"OP": op, "SRC": FORM_SOURCES[form], "PRIM": kind}
    mode, op, form = parts
    op = "bundle-install" if form == "bundle" and op == "install" else op
    assignment = {"OP": op, "SRC": FORM_SOURCES[form]}
    return {**assignment, "DRYRUN": ON} if mode == "dry-run" else assignment


def _lock_assignment(key: str) -> dict[str, str] | None:
    _, op, scope, lock = key.split("|")
    if op not in LOCK_OPS or lock not in LOCK_STATES:
        return None
    name, modifiers = LOCK_OPS[op]
    return {"OP": name, "SCOPE": scope, "LOCK": LOCK_STATES[lock], **dict.fromkeys(modifiers, ON)}


def machine_cells() -> list[MachineCell]:
    """Every machine cell id with a factor translation, sorted by id."""
    from tests.lifecycle.machines import test_m_scope_lock, test_m_source

    cells: list[MachineCell] = []
    known_source = getattr(test_m_source, "KNOWN_CELLS_BY_ID", {})
    for key in sorted(test_m_source.t3_cells() | test_m_source.t5_cells()):
        assignment = _source_assignment(key)
        if assignment:
            cells.append(MachineCell(key, SOURCE_SPINE, "T-2", assignment, known_source.get(key)))
    for scope in test_m_scope_lock.SCOPES:
        for key in sorted(test_m_scope_lock.t4_cells(scope)):
            assignment = _lock_assignment(key)
            if assignment:
                witness = SCOPE_LOCK_SPINE.format(scope=scope)
                tracking = getattr(test_m_scope_lock, "KNOWN_T4", {}).get(key)
                cells.append(MachineCell(key, witness, "T-3", assignment, tracking))
    return cells


def implied(assignment: Mapping[str, str]) -> Iterator[str]:
    """Pair and named-triple ids whose every value *assignment* fixes."""
    names = [name for name in ORDER if name in assignment]
    for first, second in combinations(names, 2):
        yield cell_id("pair", {first: assignment[first], second: assignment[second]})
    for family, factors in TRIPLES.items():
        if all(name in assignment for name in factors):
            yield cell_id(family, {name: assignment[name] for name in factors})


@dataclass(frozen=True)
class Witness:
    node: str
    lane: str
    credited_by: tuple[str, ...]
    tracking: str | None = None


def machine_witnesses(obligations: Mapping[str, object]) -> dict[str, Witness]:
    """Obligation id -> its machine witness (the first spine, by node, that implies it).

    Receipts of passing machine ids discharge the obligation; when every
    implying id of that spine is a strict known cell, the obligation is
    known-failing with the first owning issue.
    """
    by_node: dict[str, dict[str, list[MachineCell]]] = {}
    for cell in machine_cells():
        for key in implied(cell.assignment):
            if key in obligations:
                by_node.setdefault(key, {}).setdefault(cell.witness, []).append(cell)
    witnesses: dict[str, Witness] = {}
    for key, nodes in sorted(by_node.items()):
        node = sorted(nodes)[0]
        cells = nodes[node]
        passing = tuple(sorted(cell.id for cell in cells if cell.tracking is None))
        if passing:
            witnesses[key] = Witness(node, cells[0].lane, passing)
        else:
            issues = sorted(str(cell.tracking) for cell in cells)
            witnesses[key] = Witness(
                node, cells[0].lane, tuple(sorted(c.id for c in cells)), issues[0]
            )
    return witnesses


# -- scenario witnesses ----------------------------------------------------------

SCENARIO_MODULES = (
    "test_cache_presence",
    "test_concurrency",
    "test_failure_phase",
    "test_graph_version",
    "test_history_upgrade",
    "test_ownership_siblings",
    "test_platform",
)


def scenario_credits() -> list:
    """Every ``Credit`` the scenario suites declare in their module ``CREDITS``."""
    import importlib

    found = []
    for name in SCENARIO_MODULES:
        module = importlib.import_module(f"tests.lifecycle.scenarios.{name}")
        found.extend(getattr(module, "CREDITS", ()))
    return found


def scenario_witnesses(
    obligations: Mapping[str, object], lanes: Mapping[str, str]
) -> dict[str, Witness]:
    """Obligation id -> its scenario witness, for ids in a scenario lane.

    The first passing node (by node id) whose credited assignment implies the
    id is its witness; when only strict known rows imply it, the obligation is
    known-failing with the first owning issue. Scenario steps credit the id
    itself (``tests/lifecycle/scenarios/_kit.py`` credit_step), so there are
    no aliases.
    """
    from tests.lifecycle.scenarios._kit import SCENARIO_LANES

    by_key: dict[str, list] = {}
    for credit in scenario_credits():
        for key in implied(credit.assignment):
            if key in obligations and lanes.get(key) in SCENARIO_LANES:
                by_key.setdefault(key, []).append(credit)
    witnesses: dict[str, Witness] = {}
    for key, found in sorted(by_key.items()):
        passing = sorted(credit.node for credit in found if credit.tracking is None)
        if passing:
            witnesses[key] = Witness(passing[0], lanes[key], ())
        else:
            first = sorted(found, key=lambda credit: (credit.node, credit.tracking))[0]
            witnesses[key] = Witness(first.node, lanes[key], (), first.tracking)
    return witnesses
