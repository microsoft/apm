"""Machine witnesses of generated lifecycle obligations (#4096, #4097, #4099).

The state machines credit their own cell ids through
``oracles.check_obligation``: M-SOURCE ``T3|<op>|<form>|<kind>``,
``T5|<dry-run|real>|<op>|<form>`` and the context ids
``X|<op>|<form>|<FACTOR>=<value>`` and the rule cells it witnesses directly
(``rule|...``), M-SCOPE-LOCK ``T4|<op>|<scope>|<lock>`` (its lockfile spine and
its drift spine: stale and absent lockfiles, side-door commands),
the M-GATE matrix ``G1|<op>|<prim>|<gate>|<scope>``.
This module translates each machine id into the factor assignment it proves
and lists the generated obligations that assignment implies (every pair and
named triple whose values it fixes). Such an obligation is ``required`` with
the machine spine as witness, and the receipt of any listed machine id
discharges it (``credited_by``). A machine id the machine records as a strict
known cell with an owning issue makes the obligation ``known-failing``.

A GATE matrix id credits only the implied ids that name GATE (its anchor):
the step proves what the gate level did to that command, so a refusal under
a dependency deny never discharges a success cell without a GATE value.
A lockfile cell also carries what its step acted on (``lock_context``: source,
primitive, gate level, target, network, prior deployment) and credits only the
implied ids that name LOCK (its anchor), so a refused write under a newer
lockfile never discharges a success cell.

Machine vocabulary that has no factor value here (``deps``/``outdated``
reads, ``corrupt``/``symlink`` locks, the ``native`` kind) implies nothing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import combinations

from tests.lifecycle.generator.factors import ON, ORDER, TRIPLES, cell_id, parse_cell

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
    "local-mkt": "marketplace-local",
    "file-mkt": "marketplace-file",
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
    "deny": ("approve-deny", ()),
    "mcp-install": ("mcp-install", ()),
    "bundle": ("bundle-install", ()),
    "marketplace": ("marketplace", ()),
}
LOCK_STATES: Mapping[str, str] = {
    "consistent": "consistent",
    "newer": "newer-generator",
    "stale": "stale",
    "absent": "absent",
}
DRIFT_SPINE = (
    "tests/lifecycle/machines/test_m_scope_lock.py::"
    "test_scope_lock_drift_spine_credits_stale_absent_and_side_doors[{scope}]"
)
GATE_SPINE = (
    "tests/lifecycle/machines/test_m_gate.py::test_gate_matrix_credits_op_prim_gate_cells[{scope}]"
)
GATE_BUNDLE_SPINE = (
    "tests/lifecycle/machines/test_m_gate.py::test_gate_bundle_spine_credits_bundle_install_cells"
)
MATRIX_RENDER = "G1|compile|other-file|"
GATE_PACK_SPINE = "tests/lifecycle/machines/test_m_gate.py::test_gate_pack_spine_credits_pack_cells"
GATE_BIN_SPINE = (
    "tests/lifecycle/machines/test_m_gate.py::test_gate_bin_spine_credits_user_scope_bin_cells"
)
GATE_PLUGIN_BIN_SPINE = (
    "tests/lifecycle/machines/test_m_gate.py::"
    "test_gate_plugin_bin_spine_credits_project_scope_prune_cells"
)
_OWNERSHIP = "tests/lifecycle/scenarios/test_ownership_siblings.py::"
# Witnesses whose package can deploy a bin/ artifact, with the scope it lands at
# (#4981). Loose bin/ deploys only for a user-scope marketplace plugin with a
# Claude target, and a whole trusted Claude plugin carries bin/ to either scope
# (U-BIN). A row or spine acting on p-exec or a bundle at project scope never
# deploys bin/, so it cannot credit a bin cell: the gate decided nothing there.
BIN_WITNESSES: Mapping[str, str] = {
    GATE_BIN_SPINE: "user",
    GATE_PLUGIN_BIN_SPINE: "project",
    **{
        _OWNERSHIP + name: "user"
        for name in (
            "test_a_users_file_at_the_plugin_bin_path_refuses_the_plugin",
            "test_a_plugin_bin_without_a_lock_is_kept",
            "test_an_edited_plugin_bin_is_restored_and_said",
        )
    },
    "tests/lifecycle/scenarios/test_user_scope.py::"
    "test_a_plugin_bin_deploys_under_home_once_trusted": "user",
    "tests/lifecycle/scenarios/test_policy_pairs.py::"
    "test_a_denied_plugin_with_bin_is_refused_at_user_scope": "user",
    **{
        "tests/lifecycle/scenarios/test_lock_states.py::"
        f"test_user_bin_plugin_under_a_drifted_lock[{lock}]": "user"
        for lock in ("stale", "newer-generator")
    },
    **{
        "tests/lifecycle/scenarios/test_user_scope.py::"
        f"test_a_target_change_keeps_a_plugin_bin_only_while_claude_is_declared[{change}]": "user"
        for change in ("single-narrowed", "single-widened", "multi-narrowed", "multi-widened")
    },
}


def credits_bin(node: str, cell: Mapping[str, str]) -> bool:
    """Whether *node* may credit *cell*: a non-bin cell, or bin/ deployed at its scope."""
    if cell.get("PRIM") != "bin":
        return True
    scope = BIN_WITNESSES.get(node)
    return scope is not None and cell.get("SCOPE", scope) == scope


# (OP, SRC, PRIM) source-spine steps whose primitive the spine never observed on a
# surface (dimension audit, lifecycle-journeys run 45258714621): a registry or local
# bundle package's executable at a non-install step. The spine records no gate level
# there, so neither a deploy nor a parked decision is proven (#4981 generalized).
SOURCE_SPINE_UNOBSERVED: frozenset[tuple[str, str, str]] = frozenset(
    {
        *(
            (op, "pkg-registry", prim)
            for op in ("audit", "lock", "prune", "uninstall", "update")
            for prim in ("hook", "lsp", "canvas")
        ),
        ("compile", "pkg-registry", "canvas"),
        *((op, "local-bundle", "canvas") for op in ("approve-deny", "audit", "compile", "lock")),
        *((op, "local-bundle", "hook") for op in ("audit", "lock")),
        *((op, "local-bundle", "lsp") for op in ("approve-deny", "audit", "lock")),
    }
)


def deployable_prims(scope: str) -> frozenset[str]:
    """Primitives a lifecycle world can deploy at *scope*: the kinds of that scope's
    surface table (``rows._SURFACE_GLOBS``, ``machines._kit.USER_SURFACE_GLOBS``),
    plus skills and project instructions. bin/ is decided by ``BIN_WITNESSES``."""
    from tests.lifecycle.machines._kit import USER_SURFACE_GLOBS
    from tests.lifecycle.rows import _SURFACE_GLOBS

    table = _SURFACE_GLOBS if scope == "project" else USER_SURFACE_GLOBS
    extra = {"skill", "other-file"} if scope == "project" else {"skill"}
    return frozenset({kind for kind, _ in table} | extra) - {"bin"}


def credits_dimension(node: str, cell: Mapping[str, str]) -> bool:
    """Whether *node* exercises *cell*'s primitive: it deploys (or the gate decides) that
    primitive at the cell's scope. A witness that never does proves nothing about it."""
    prim = cell.get("PRIM")
    if prim is None:
        return True
    if prim == "bin":
        return credits_bin(node, cell)
    if "SCOPE" in cell and prim not in deployable_prims(cell["SCOPE"]):
        return False
    step = (cell.get("OP"), cell.get("SRC"), prim)
    return not (node == SOURCE_SPINE and step in SOURCE_SPINE_UNOBSERVED)


# M-GATE matrix and side-door spine op -> (OP, modifiers).
GATE_OPS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "bundle": ("bundle-install", ()),
    "pack": ("pack", ()),
    "install": ("install", ()),
    "install-frozen": ("install", ("FROZEN",)),
    "update": ("update", ()),
    "lock": ("lock", ()),
    "audit": ("audit", ()),
    "audit-ci": ("audit", ()),
    "compile": ("compile", ()),
    "prune": ("prune", ()),
    "uninstall": ("uninstall", ()),
    "approve": ("approve-deny", ()),
    "deny": ("approve-deny", ()),
    "mcp-install": ("mcp-install", ()),
    "marketplace": ("marketplace", ()),
}


@dataclass(frozen=True)
class MachineCell:
    """One machine cell id, its witness node and the factor values it proves."""

    id: str
    witness: str
    lane: str
    assignment: Mapping[str, str]
    tracking: str | None = None
    # When set, only implied ids that name this factor are credited.
    anchor: str | None = None


def _source_assignment(key: str) -> dict[str, str] | None:
    family, *parts = key.split("|")
    if family == "X":
        op, form, *contexts = parts
        op = "bundle-install" if form == "bundle" and op == "install" else op
        assignment = {"OP": op, "SRC": FORM_SOURCES[form]}
        assignment.update(context.split("=", 1) for context in contexts)
        return assignment
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


def _lock_assignment(key: str, *, context: bool = False) -> dict[str, str] | None:
    from tests.lifecycle.machines.test_m_scope_lock import lock_context

    _, op, scope, lock = key.split("|")
    if op not in LOCK_OPS or lock not in LOCK_STATES:
        return None
    name, modifiers = LOCK_OPS[op]
    return {
        **(lock_context(op, lock) if context else {}),
        "OP": name,
        "SCOPE": scope,
        "LOCK": LOCK_STATES[lock],
        **dict.fromkeys(modifiers, ON),
    }


def _gate_assignment(key: str) -> dict[str, str]:
    if key.startswith("rule|"):
        return {}
    _, op, prim, gate, scope, *_ = key.split("|")
    name, flags = GATE_OPS[op]
    return {"OP": name, "PRIM": prim, "GATE": gate, "SCOPE": scope, **dict.fromkeys(flags, ON)}


def machine_cells() -> list[MachineCell]:
    """Every machine cell id with a factor translation, sorted by id."""
    from tests.lifecycle.machines import test_m_gate, test_m_scope_lock, test_m_source
    from tests.lifecycle.machines._gate_bin import KNOWN_DD, bin_cells
    from tests.lifecycle.machines._gate_bundle import KNOWN_PACK, bundle_cells, pack_cells
    from tests.lifecycle.machines._gate_matrix import matrix_cells
    from tests.lifecycle.machines._gate_plugin_bin import deploys_bin, plugin_bin_cells
    from tests.lifecycle.machines.test_m_scope_lock import drift_cells

    cells: list[MachineCell] = []
    known_source = test_m_source.KNOWN_CELLS_BY_ID
    for key in sorted(test_m_source.rule_cells()):
        known = known_source.get(key)
        tracking = known.tracking if known else None
        cells.append(MachineCell(key, SOURCE_SPINE, "T-2", {}, tracking))
    source_cells = test_m_source.t3_cells() | test_m_source.t5_cells()
    for key in sorted(source_cells | test_m_source.x_cells()):
        assignment = _source_assignment(key)
        if assignment:
            known = known_source.get(key)
            tracking = known.tracking if known else None
            cells.append(MachineCell(key, SOURCE_SPINE, "T-2", assignment, tracking))
    spines = ((SCOPE_LOCK_SPINE, test_m_scope_lock.t4_cells), (DRIFT_SPINE, drift_cells))
    for scope in test_m_scope_lock.SCOPES:
        for spine, enumerate_cells in spines:
            witness = spine.format(scope=scope)
            for key in sorted(enumerate_cells(scope)):
                known = test_m_scope_lock.KNOWN_T4.get(key)
                tracking = known.tracking if known else None
                assignment = _lock_assignment(key)
                if assignment:
                    cells.append(MachineCell(key, witness, "T-3", assignment, tracking))
                    # The step's context, credited only on the ids that name LOCK.
                    context = _lock_assignment(key, context=True) or {}
                    cells.append(MachineCell(key, witness, "T-3", context, tracking, "LOCK"))
    side_doors = (
        (GATE_BUNDLE_SPINE, bundle_cells()),
        (GATE_PACK_SPINE, pack_cells()),
        (GATE_BIN_SPINE, bin_cells()),
        (GATE_PLUGIN_BIN_SPINE, plugin_bin_cells()),
    )
    for witness, keys in side_doors:
        for key in sorted(keys):
            assignment = _gate_assignment(key)
            if witness == GATE_BUNDLE_SPINE:
                assignment = {**assignment, "SRC": "local-bundle"}
            tracking = KNOWN_DD.get(key) or KNOWN_PACK.get(key)
            cells.append(MachineCell(key, witness, "T-1", assignment, tracking, anchor="GATE"))
            if assignment["GATE"] == "denied-dependency":
                # A refusal proves its GATE level only, never a success cell.
                continue
            if witness == GATE_PACK_SPINE:
                # A real pack over the primitive also proves the OP x PRIM pair.
                context = {"OP": assignment["OP"], "PRIM": assignment["PRIM"]}
                cells.append(MachineCell(key, witness, "T-1", context, tracking, anchor="PRIM"))
            if witness == GATE_BUNDLE_SPINE and assignment["GATE"] in ("none", "allowed"):
                # An install that deploys the primitive proves what it acts on: a
                # local bundle carrying that primitive (OP x SRC x PRIM and its pairs).
                source = {"OP": assignment["OP"], "SRC": "local-bundle", "PRIM": assignment["PRIM"]}
                cells.append(MachineCell(key, witness, "T-1", source, tracking))
    for key in sorted(filter(deploys_bin, plugin_bin_cells())):
        # A whole plugin's bin/ deployed at project scope (#4894): no GATE anchor needed.
        assignment = {"PRIM": "bin", "SCOPE": "project"}
        cells.append(MachineCell(key, GATE_PLUGIN_BIN_SPINE, "T-1", assignment))
    for scope in test_m_gate.MATRIX_SCOPES:
        for key in sorted(matrix_cells(scope)):
            witness = GATE_SPINE.format(scope=scope)
            assignment = _gate_assignment(key)
            cells.append(MachineCell(key, witness, "T-1", assignment, anchor="GATE"))
            if key.startswith(MATRIX_RENDER):
                # The forced render is the matrix's only compile x instruction step.
                context = {"OP": assignment["OP"], "PRIM": assignment["PRIM"]}
                cells.append(MachineCell(key, witness, "T-1", context, anchor="PRIM"))
    return cells


def implied(assignment: Mapping[str, str]) -> Iterator[str]:
    """Pair and named-triple ids whose every value *assignment* fixes."""
    names = [name for name in ORDER if name in assignment]
    for first, second in combinations(names, 2):
        yield cell_id("pair", {first: assignment[first], second: assignment[second]})
    for family, factors in TRIPLES.items():
        if all(name in assignment for name in factors):
            yield cell_id(family, {name: assignment[name] for name in factors})


def credits(cell: MachineCell) -> Iterator[str]:
    """Obligation ids a receipt of *cell* discharges.

    A rule cell id (``rule|...``) is its own obligation and implies nothing
    else; an anchored cell credits only the implied ids that name its anchor.
    """
    if cell.id.startswith("rule|"):
        yield cell.id
        return
    for key in implied(cell.assignment):
        if not cell.anchor or f"|{cell.anchor}=" in key:
            yield key


@dataclass(frozen=True)
class Witness:
    node: str
    lane: str
    credited_by: tuple[str, ...]
    tracking: str | None = None


def machine_witnesses(obligations: Mapping[str, object]) -> dict[str, Witness]:
    """Obligation id -> its machine witness (the first spine, by node, that passes it).

    Receipts of passing machine ids discharge the obligation, so the witness is
    the first spine with a passing implying id; one spine whose ids are all
    strict known cells never hides another spine's proof. Only when every
    implying id of every spine is a strict known cell is the obligation
    known-failing, with the first spine's first owning issue.
    """
    by_node: dict[str, dict[str, list[MachineCell]]] = {}
    for cell in machine_cells():
        # The whole step, not only the projected id: an unexercised step credits no pair.
        if not credits_dimension(cell.witness, cell.assignment):
            continue
        for key in credits(cell):
            if key in obligations and credits_dimension(cell.witness, parse_cell(key)[1]):
                by_node.setdefault(key, {}).setdefault(cell.witness, []).append(cell)
    witnesses: dict[str, Witness] = {}
    for key, nodes in sorted(by_node.items()):
        for node in sorted(nodes):
            cells = nodes[node]
            passing = tuple(sorted(cell.id for cell in cells if cell.tracking is None))
            if passing:
                witnesses[key] = Witness(node, cells[0].lane, passing)
                break
        else:
            node = sorted(nodes)[0]
            cells = nodes[node]
            issues = sorted(str(cell.tracking) for cell in cells)
            witnesses[key] = Witness(
                node, cells[0].lane, tuple(sorted(c.id for c in cells)), issues[0]
            )
    return witnesses


# -- scenario witnesses ----------------------------------------------------------

SCENARIO_MODULES = (
    "test_cache_presence",
    "test_compile_source_files",
    "test_concurrency",
    "test_deployed_absent",
    "test_failure_phase",
    "test_file_routing",
    "test_graph_version",
    "test_history_upgrade",
    "test_host_identity_pairs",
    "test_lock_states",
    "test_mcp_ownership_history",
    "test_mcp_write_receipts",
    "test_network_offline",
    "test_ownership_siblings",
    "test_pack_lock_states",
    "test_platform",
    "test_policy_pairs",
    "test_target_history_sources",
    "test_user_scope",
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
            if (
                key in obligations
                and lanes.get(key) in SCENARIO_LANES
                and credits_dimension(credit.node, parse_cell(key)[1])
            ):
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
