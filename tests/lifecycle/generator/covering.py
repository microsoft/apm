"""Greedy covering rows for the obligations the replay can discharge (#4099).

A candidate row is a script over the lifecycle world (``tests/lifecycle/world``)
at project scope: install a package set from local paths or the world's git
server, take a gate decision, then probe every command and modifier the
replay can run, and finally uninstall. ``simulate`` walks the script with an
abstract state (declared set, lock, deployed set, gate decision per package)
and lists the obligation cells each step visits; a cell that needs a state
fact (``e2e-evidence``) counts only at a step that names one.

``cover`` picks rows greedily by new obligations, prunes probe steps that
credit nothing new, and returns the chosen rows with, per step, the cells it
credits. Refusal cells get their own rows, one pinned-exit step each.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import combinations, product

from tests.lifecycle.generator.factors import (
    ON,
    ORDER,
    PLAIN_PRIMS,
    TRIPLES,
    cell_id,
    row_targets,
)
from tests.lifecycle.generator.space import Entry
from tests.lifecycle.world.packages import GIT_PUBLISHED
from tests.lifecycle.world.services import REGISTRY_SERVER

# Kinds of the world packages a row may use (tests/lifecycle/world/packages.py
# author_packages); test_covering_replay.py checks them against the world.
WORLD_KINDS: Mapping[str, tuple[str, ...]] = {
    "p-skill": ("skill",),
    "p-exec": ("bin", "hook", "mcp"),
    "p-lsp": ("lsp",),
    "p-canvas": ("canvas",),
}
# Project-scope surface prefixes of the non-MCP primitives (tests/lifecycle/rows.py
# _SURFACE_GLOBS): an mcp-install must leave them byte-identical (U7).
NON_MCP_SURFACES = (".agents/skills", ".github/hooks", ".github/lsp.json", ".github/extensions")
# Executable configuration compile must never write (U10: compile renders only).
EXEC_CONFIG_SURFACES = (".github/hooks", ".mcp.json", ".github/lsp.json")
OPENINGS = ("force", "update", "lock", "mcp")
EVERYTHING = ("*",)
MAX_ROWS = 24
_GATE_STATES = {
    "unapproved": "unapproved-exec",
    "allowed": "allowed",
    "denied": "denied-executable",
    "revoked": "revoked-after-deploy",
}
_PACKAGE_OPS = frozenset(
    {"install", "update", "uninstall", "prune", "compile", "lock", "pack", "audit", "approve-deny"}
)
_FLAGS = {"FROZEN": "--frozen", "DRYRUN": "--dry-run", "FORCE": "--force", "UPDATE": "--update"}
_FLAGS["NOPOLICY"] = "--no-policy"


@dataclass(frozen=True)
class Step:
    line: str
    op: str
    mods: frozenset[str] = frozenset()
    names: tuple[str, ...] = ()
    essential: bool = False
    exit: int | None = None
    fixed: tuple[str, ...] = ()  # refusal and rule cells this step witnesses
    unchanged: tuple[str, ...] = ()
    cells: tuple[str, ...] = ()  # credited cells, filled by ``cover``


@dataclass(frozen=True)
class Plan:
    """The parameters of one candidate row."""

    packages: tuple[str, ...]
    git: bool
    multi: bool
    gate: str  # approve | deny | unapproved
    mcp: bool
    # How the row reaches its first install: plain, force, update, lock (an
    # ``apm lock`` on the empty project first) or mcp (an MCP install first).
    opening: str = "plain"

    @property
    def slug(self) -> str:
        parts = [
            "-".join(name.removeprefix("p-") for name in self.packages),
            "git" if self.git else "local",
            "multi" if self.multi else "single",
            self.gate,
        ]
        parts += ["mcp"] if self.mcp else []
        return "-".join([*parts, self.opening] if self.opening != "plain" else parts)


@dataclass
class Row:
    id: str
    kind: str  # gate | refusal
    packages: tuple[str, ...]
    targets: tuple[str, ...]
    sources: Mapping[str, str]
    steps: list[Step]
    cell: Mapping[str, str] = field(default_factory=dict)


@dataclass
class _State:
    declared: tuple[str, ...] = ()
    lock: str = "absent"
    deployed: frozenset[str] = frozenset()
    gate: dict[str, str] = field(default_factory=dict)
    exec_deployed: frozenset[str] = frozenset()


def _cmd(op: str, *parts: str, mods: Iterable[str] = (), **fields) -> Step:
    words = ["apm", *parts]
    for modifier in sorted(mods):
        if modifier in _FLAGS:
            words.append(_FLAGS[modifier])
    return Step(" ".join(words), op, frozenset(mods), **fields)


def _executables(packages: Sequence[str]) -> tuple[str, ...]:
    return tuple(name for name in packages if set(WORLD_KINDS[name]) - set(PLAIN_PRIMS))


def _probes(plan: Plan, target: str, first: str, decided: str | None) -> list[Step]:
    """Probe steps over the current state: every command and modifier the replay runs."""
    select = ("--target", target)
    steps = [
        *(_cmd("install", "install", mods={mod}) for mod in ("DRYRUN", "FORCE", "UPDATE", "NOPOLICY")),
        _cmd("install", "install", *select, mods={"SELECT"}),
        *(_cmd("install", "install", mods={"FROZEN", *extra}) for extra in ((), ("DRYRUN",), ("FORCE",), ("NOPOLICY",))),
        _cmd("install", "install", *select, mods={"FROZEN", "SELECT"}),
        _cmd("install", "install", *select, mods={"DRYRUN", "FORCE", "UPDATE", "NOPOLICY", "SELECT"}),
        _cmd("install", "install", *select, mods={"FROZEN", "DRYRUN", "FORCE", "NOPOLICY", "SELECT"}),
        _cmd("update", "update", mods={"DRYRUN"}),
        _cmd("update", "update"),
        _cmd("update", "update", first, mods={"SELECT"}, names=(first,)),
        _cmd("update", "update", mods={"FORCE"}),
        _cmd("update", "update", first, mods={"DRYRUN", "FORCE", "SELECT"}, names=(first,)),
        _cmd("compile", "compile", mods={"DRYRUN"}),
        _cmd(
            "compile",
            "compile",
            fixed=("rule|U10|compile-render-only-non-mutation",),
            unchanged=EXEC_CONFIG_SURFACES,
        ),
        _cmd("compile", "compile", *select, mods={"SELECT"}),
        _cmd("compile", "compile", *select, mods={"DRYRUN", "SELECT"}),
        _cmd("compile", "compile", "--validate", fixed=("rule|U10|compile-validate-reads-mcp",)),
        _cmd("lock", "lock"),
        *(_cmd("lock", "lock", mods={mod}) for mod in ("UPDATE", "NOPOLICY")),
        _cmd("lock", "lock", *select, mods={"SELECT"}),
        _cmd("lock", "lock", *select, mods={"UPDATE", "NOPOLICY", "SELECT"}),
        _cmd("audit", "audit"),
        *(_cmd("audit", "audit", mods={mod}) for mod in ("DRYRUN", "NOPOLICY")),
        _cmd("audit", "audit", first, mods={"SELECT"}, names=(first,)),
        _cmd("audit", "audit", first, mods={"DRYRUN", "NOPOLICY", "SELECT"}, names=(first,)),
        _cmd("audit", "audit", "--ci"),
        _cmd("approve-deny", "approve", "--list", fixed=("rule|U9|approve-list-read-only",), unchanged=EVERYTHING),
        _cmd("approve-deny", "approve", "--pending", fixed=("rule|U9|approve-pending-read-only",), unchanged=EVERYTHING),
    ]
    if plan.git and set(plan.packages) <= set(GIT_PUBLISHED):
        # pack refuses local-path dependencies; pack x SRC is not a coordinate (U6).
        steps += [
            _cmd("pack", "pack", mods={"DRYRUN"}),
            _cmd("pack", "pack", mods={"DRYRUN", "FORCE"}),
            _cmd("pack", "pack", *select, mods={"DRYRUN", "FORCE", "SELECT"}),
        ]
    executables = _executables(plan.packages)
    if decided and executables:
        verb = "approve" if decided == "allowed" else "deny"
        steps.append(_cmd("approve-deny", verb, *executables, mods={"SELECT"}, names=executables))
    return steps


def _mcp_block(target: str) -> list[Step]:
    server = REGISTRY_SERVER
    return [
        _cmd("mcp-install", "install", "--mcp", server, mods=mods, unchanged=NON_MCP_SURFACES)
        if not extra
        else _cmd("mcp-install", "install", "--mcp", server, *extra, mods=mods, unchanged=NON_MCP_SURFACES)
        for mods, extra in (
            ({"DRYRUN"}, ()),
            (set(), ()),
            ({"SELECT"}, ("--target", target)),
            ({"FORCE"}, ()),
            ({"NOPOLICY"}, ()),
            ({"DRYRUN", "FORCE", "NOPOLICY", "SELECT"}, ("--target", target)),
        )
    ]


def script(plan: Plan, target: str) -> list[Step]:
    """The full candidate script of *plan*; probes may be pruned later."""
    packages = plan.packages
    executables = _executables(packages)
    first = packages[0]
    steps = [
        _cmd("compile", "compile", mods={"DRYRUN"}),
        _cmd("audit", "audit"),
        _cmd("prune", "prune", mods={"DRYRUN"}),
        _cmd("update", "update", mods={"DRYRUN"}),
        _cmd("approve-deny", "approve", "--pending", fixed=("rule|U9|approve-pending-read-only",), unchanged=EVERYTHING),
        _cmd("install", "install", mods={"FROZEN"}),
        _cmd("pack", "pack", mods={"DRYRUN"}),
        _cmd("mcp-install", "install", "--mcp", REGISTRY_SERVER, mods={"DRYRUN"}, unchanged=NON_MCP_SURFACES),
    ]
    if plan.opening == "lock":
        steps.append(_cmd("lock", "lock", essential=True))
    if plan.opening == "mcp":
        steps.append(_cmd("mcp-install", "install", "--mcp", REGISTRY_SERVER, essential=True, unchanged=NON_MCP_SURFACES))
    opening = {"force": {"FORCE"}, "update": {"UPDATE"}}.get(plan.opening, set())
    steps.append(_cmd("install", "install", *packages, mods={"SELECT", *opening}, names=packages, essential=True))
    decided = None
    if executables and plan.gate in ("approve", "deny"):
        verb = plan.gate
        decided = "allowed" if verb == "approve" else "denied"
        steps.append(_cmd("approve-deny", verb, *executables, mods={"SELECT"}, names=executables, essential=True))
    steps.append(_cmd("install", "install", essential=True))
    steps += _probes(plan, target, first, decided)
    if plan.mcp:
        steps += _mcp_block(target)
    if executables and plan.gate in ("approve", "unapproved"):
        # unapproved -> approve; approve -> revoke after deploy.
        verb = "approve" if plan.gate == "unapproved" else "deny"
        steps.append(_cmd("approve-deny", verb, *executables, mods={"SELECT"}, names=executables, essential=True))
        steps.append(_cmd("install", "install", essential=True))
        steps += _probes(plan, target, first, "allowed" if verb == "approve" else None)
    steps += [_cmd("prune", "prune", mods={"DRYRUN"}), _cmd("prune", "prune")]
    if "p-lsp" in packages:
        # With an LSP server in the lock, apm uninstall exits 1 (#4161).
        return steps
    order = packages
    steps.append(_cmd("uninstall", "uninstall", order[0], mods={"DRYRUN", "SELECT"}, names=(order[0],)))
    for name in order:
        steps.append(_cmd("uninstall", "uninstall", name, mods={"SELECT"}, names=(name,)))
    return steps


def _apply(state: _State, step: Step, kinds: Mapping[str, tuple[str, ...]]) -> None:
    if step.exit is not None or "DRYRUN" in step.mods:
        return
    verb = step.line.split()[1]
    if step.op == "install" and step.names:
        state.declared = tuple(dict.fromkeys((*state.declared, *step.names)))
    if step.op == "uninstall":
        state.declared = tuple(name for name in state.declared if name not in step.names)
        state.deployed = state.deployed - set(step.names)
        state.exec_deployed = state.exec_deployed - set(step.names)
    if verb == "approve" and step.names:
        state.gate.update(dict.fromkeys(step.names, "allowed"))
    if verb == "deny":
        for name in step.names:
            state.gate[name] = "revoked" if name in state.exec_deployed else "denied"
    if step.op in ("install", "update", "uninstall", "mcp-install") and "FROZEN" not in step.mods:
        state.lock = "consistent"
    if step.op == "lock":
        state.lock = "consistent"
    if step.op in ("install", "update"):
        state.deployed = frozenset(state.declared)
        state.exec_deployed = frozenset(
            name
            for name in state.declared
            if state.gate.get(name) == "allowed" and set(kinds[name]) - set(PLAIN_PRIMS)
        )


def _units(state: _State, step: Step, plan: Plan, kinds) -> list[dict[str, str]]:
    if step.op == "mcp-install":
        units = [{"SRC": "registry", "PRIM": "mcp"}]
        return units + [
            {"PRIM": kind} for name in state.declared for kind in kinds[name] if kind != "mcp"
        ]
    if step.op not in _PACKAGE_OPS:
        return []
    names = step.names or state.declared
    units = []
    for name in names:
        source = "git" if plan.git and name in GIT_PUBLISHED else "local-path"
        for kind in kinds[name]:
            gate = "none" if kind in PLAIN_PRIMS else _GATE_STATES[state.gate.get(name, "unapproved")]
            units.append({"SRC": source, "PRIM": kind, "GATE": gate})
    return units


def context_cells(scalar: Mapping[str, str], units: Sequence[Mapping[str, str]]) -> set[str]:
    """Every pair and named-triple cell one step context visits."""
    cells: set[str] = set()
    for unit in units or ({},):
        assignment = {**scalar, **unit}
        names = [name for name in ORDER if name in assignment]
        for first, second in combinations(names, 2):
            cells.add(cell_id("pair", {first: assignment[first], second: assignment[second]}))
        for family, factors in TRIPLES.items():
            if all(name in assignment for name in factors):
                cells.add(cell_id(family, {name: assignment[name] for name in factors}))
    return cells


def simulate(plan: Plan, steps: Sequence[Step], targets: Sequence[str]) -> list[set[str]]:
    """Cells each step visits, in order."""
    state = _State()
    tgt = "multi-unchanged" if len(targets) > 1 else "single-unchanged"
    visited: list[set[str]] = []
    for step in steps:
        if step.exit is not None:
            visited.append(set(step.fixed))
            continue
        scalar = {
            "OP": step.op,
            "SCOPE": "project",
            "LOCK": state.lock,
            "DEPLOYED": "clean" if state.deployed else "absent",
            "TGT": tgt,
            "NET": "online",
            **dict.fromkeys(step.mods, ON),
        }
        visited.append(context_cells(scalar, _units(state, step, plan, WORLD_KINDS)) | set(step.fixed))
        _apply(state, step, WORLD_KINDS)
    return visited


def _creditable(cells: set[str], step: Step, obligations: Mapping[str, Entry]) -> set[str]:
    return {
        cell
        for cell in cells
        if cell in obligations and (obligations[cell].strength != "e2e-evidence" or step.unchanged)
    }


def plans() -> list[Plan]:
    names = tuple(WORLD_KINDS)
    result = []
    for size in range(1, len(names) + 1):
        for packages in combinations(names, size):
            gits = (False, True) if set(packages) & set(GIT_PUBLISHED) else (False,)
            gates = ("approve", "deny", "unapproved") if _executables(packages) else ("approve",)
            for git, multi, gate, mcp in product(gits, (False, True), gates, (False, True)):
                result.append(Plan(packages, git, multi, gate, mcp))
    # Opening variants on the smallest row reach the empty-project cells.
    result += [Plan(("p-skill",), False, False, "approve", False, opening) for opening in OPENINGS]
    return result


def _gate_row(index: int, plan: Plan, obligations, covered: set[str]) -> tuple[Row, set[str]]:
    single, multi = row_targets()
    targets = multi if plan.multi else single
    steps = script(plan, targets[0])
    kept: list[Step] = []
    new: set[str] = set()
    for step, cells in zip(steps, simulate(plan, steps, targets), strict=True):
        fresh = sorted(_creditable(cells, step, obligations) - covered - new)
        if fresh or step.essential:
            kept.append(replace(step, cells=tuple(fresh)))
            new.update(fresh)
    sources = {name: "git" for name in plan.packages if plan.git and name in GIT_PUBLISHED}
    row = Row(f"cover-{index:02d}-{plan.slug}", "gate", plan.packages, targets, sources, kept)
    row.cell = {"OP": "install", "SRC": "git" if plan.git else "local-path", "TGT": "multi-unchanged" if plan.multi else "single-unchanged"}
    return row, new


def cover(obligations: Mapping[str, Entry]) -> list[Row]:
    """Greedy covering rows: gate rows first, then the refusal rows."""
    single, multi = row_targets()
    candidates = []
    for plan in plans():
        targets = multi if plan.multi else single
        steps = script(plan, targets[0])
        reach = set()
        for step, cells in zip(steps, simulate(plan, steps, targets), strict=True):
            reach |= _creditable(cells, step, obligations)
        candidates.append((plan, reach))
    rows: list[Row] = []
    covered: set[str] = set()
    while len(rows) < MAX_ROWS:
        plan, reach = max(candidates, key=lambda item: (len(item[1] - covered), -len(item[0].packages), item[0].slug))
        if not reach - covered:
            break
        row, new = _gate_row(len(rows) + 1, plan, obligations, covered)
        rows.append(row)
        covered |= new
    rows += refusal_rows(obligations, len(rows) + 1)
    return rows


# -- refusal rows -----------------------------------------------------------

_REFUSAL_BASE = {
    "install": "apm install",
    "update": "apm update",
    "uninstall": "apm uninstall p-skill",
    "prune": "apm prune",
    "compile": "apm compile",
    "lock": "apm lock",
    "pack": "apm pack",
    "bundle-install": "apm install p-exec",
    "mcp-install": f"apm install --mcp {REGISTRY_SERVER}",
    "approve-deny": "apm approve p-skill",
    "audit": "apm audit",
    "marketplace": "apm marketplace list",
}
REFUSALS_PER_ROW = 15


def refusal_line(cell: Mapping[str, str]) -> str:
    """The command line that witnesses one refusal cell."""
    words = [_REFUSAL_BASE[cell["OP"]]]
    for name in ORDER:
        if cell.get(name) == ON:
            words.append("p-skill" if name == "SELECT" else _FLAGS[name])
    if cell.get("SCOPE") == "user":
        words.append("-g")
    return " ".join(words)


def refusal_rows(obligations: Mapping[str, Entry], start: int) -> list[Row]:
    refusals = [entry for entry in obligations.values() if entry.family == "refusal"]
    steps = [
        Step(
            refusal_line(entry.cell),
            entry.cell["OP"],
            exit=entry.exit,
            fixed=(entry.id,),
            unchanged=EVERYTHING,
            cells=(entry.id,),
        )
        for entry in sorted(refusals, key=lambda item: item.id)
    ]
    rows = []
    for offset in range(0, len(steps), REFUSALS_PER_ROW):
        chunk = steps[offset : offset + REFUSALS_PER_ROW]
        index = start + offset // REFUSALS_PER_ROW
        setup = [_cmd("install", "install", "p-skill", mods={"SELECT"}, names=("p-skill",), essential=True)]
        row = Row(
            f"cover-{index:02d}-refusals",
            "refusal",
            ("p-skill", "p-exec"),
            row_targets()[0],
            {"p-exec": "bundle"},
            setup + chunk,
            {"OP": "install", "FROZEN": ON},
        )
        rows.append(row)
    return rows
