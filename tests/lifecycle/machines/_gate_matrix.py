"""M-GATE matrix: every command under every gate level on the shared world (#4095).

PROMISE: P-INV-I4 -- a deny, revoked approval or stricter policy never widens
what is deployed, whichever command runs next (source: tests/lifecycle/oracles.py).
PROMISE: P-OWN-executable-item-admission -- the executable gate governs only
executable primitives: a plain file beside a revoked executable survives, an
org dependency deny blocks the whole package (source: src/apm_cli/commands/approve.py).
CELL: OP x PRIM x GATE (T1) and FROZEN x SCOPE x GATE (T2).

The spine drives one shared lifecycle world (``tests/lifecycle/world``) through
six gate levels in turn: ``none`` (no layer opts into the gate), ``allowed``,
``revoked-after-deploy`` (deny after a deploy), ``denied-executable`` (deny
before a deploy), ``unapproved-exec`` and ``denied-dependency`` (an org policy
dependency deny). At each level it runs the commands of that level; every
step goes through ``StepRunner.step`` (I1-I9 and I4 against
``expected_deployed``), then ``oracles.check_obligation`` credits one machine
id ``G1|<op>|<prim>|<gate>|<scope>`` per primitive of every package the command
acts on, with the GATE level that package had BEFORE the command.

``matrix_cells`` replays the same script without running anything, so the
witness table (``tests/lifecycle/generator/witnesses.py``) knows every id the
spine credits.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tests.lifecycle.machines._kit import Bench, Step, StepRunner
from tests.lifecycle.model.expected import expected_deployed
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.oracles import Evidence, check_obligation
from tests.lifecycle.rows import _SURFACE_GLOBS
from tests.lifecycle.world.scopes import use_policy
from tests.lifecycle.world.services import REGISTRY_SERVER
from tests.utils.local_package import LocalPackageFactory

MATRIX_POLICY = "deny-all-deps"
MIXED = "p-mixed"
EXEC_PACKAGES = ("p-exec", "p-lsp", "p-canvas")
PACKAGES = ("p-skill", *EXEC_PACKAGES, MIXED)
# Primitive kinds of each package. p-mixed carries a skill and an instruction
# (``other-file``) beside a hook, so its plain files sit next to an executable.
KINDS: Mapping[str, frozenset[str]] = {
    "p-skill": frozenset({"skill"}),
    "p-exec": frozenset({"hook", "bin", "mcp"}),
    "p-lsp": frozenset({"lsp"}),
    "p-canvas": frozenset({"canvas"}),
    MIXED: frozenset({"skill", "other-file", "hook"}),
}
# Kinds the bench observes (an instruction has no gate surface).
OBSERVED: Mapping[str, frozenset[str]] = {
    name: kinds - {"other-file"} for name, kinds in KINDS.items()
}
# Kinds no spine step observes an outcome for, so the matrix credits none:
# bin/ deploys only for a Claude marketplace plugin at user scope (U-BIN,
# install/exec_gate.py plugin_bin_deployable); the p-mixed instruction
# (other-file) has no compile-staged output to assert for the copilot target.
_UNOBSERVED = frozenset({"bin", "other-file"})
_PLAIN = frozenset({"skill", "other-file"})
_MIXED_SKILL = f".agents/skills/{MIXED}/SKILL.md"
# Project surfaces of every primitive but MCP: install --mcp must leave them alone (U7).
_NON_MCP = (
    ".agents/skills",
    ".github/hooks",
    ".github/lsp.json",
    ".github/extensions",
    ".github/instructions",
)
_DURABLE = ("apm.yml", "apm.lock.yaml", "apm_modules", ".agents", ".github", ".mcp.json")
_HOOK = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo mixed"}]}]}}
_INSTRUCTION = '---\napplyTo: "**"\n---\n\nKeep the lifecycle world mixed.\n'
_SKILL = "---\nname: p-mixed\ndescription: Lifecycle world mixed skill.\n---\n\nUse p-mixed.\n"


def author_mixed(root: Path) -> Path:
    """Author p-mixed (skill, instruction and hook) under *root*; return its path."""
    factory = LocalPackageFactory(root)
    package = factory.create(MIXED, version="1.0.0")
    factory.add_skill(package, MIXED, _SKILL)
    factory.add_instruction(package, MIXED, _INSTRUCTION)
    factory.add_hook(package, "session", _HOOK)
    return package.root


@dataclass
class GateState:
    """The user's gate inputs, as the spine has set them (pure, no filesystem)."""

    declared: list[str] = field(default_factory=list)
    gate_on: bool = True
    decisions: dict[str, str] = field(default_factory=dict)
    revoked: set[str] = field(default_factory=set)
    exec_deployed: set[str] = field(default_factory=set)
    denied_by_policy: frozenset[str] = frozenset()

    def gate(self, package: str, kind: str) -> str:
        """The GATE factor value of one (package, kind) unit."""
        if package in self.denied_by_policy:
            return "denied-dependency"
        if kind in _PLAIN:
            # U2: a plain file reads the level of the executable beside it only
            # when that executable was revoked after a deploy.
            return "revoked-after-deploy" if package in self.revoked else "none"
        if not self.gate_on:
            return "none"
        if package in self.revoked:
            return "revoked-after-deploy"
        decision = self.decisions.get(package)
        if decision == "allowed":
            return "allowed"
        return "denied-executable" if decision == "denied" else "unapproved-exec"

    def model(self) -> ModelState:
        decisions = {
            (package, kind): self.decisions[package]
            for package in self.declared
            if package in self.decisions
            for kind in KINDS[package] - _PLAIN
        }
        return ModelState(
            declared=frozenset(self.declared),
            decisions=decisions,
            policy=MATRIX_POLICY if self.denied_by_policy else "none",
            policy_denied=self.denied_by_policy,
            exec_gate=self.gate_on,
        )


@dataclass(frozen=True)
class Command:
    """One spine command: its machine op, argv words and the packages it acts on."""

    op: str
    args: tuple[str, ...]
    packages: tuple[str, ...]
    named: tuple[str, ...]
    exits: frozenset[int]


class GateMatrix(StepRunner):
    """Run (or, when *bench* is None, only label) the GATE matrix spine at one scope."""

    def __init__(self, bench: Bench | None, scope: str) -> None:
        super().__init__(bench, ModelState(scope=scope), OBSERVED)
        if bench is not None:
            self.mixed = author_mixed(bench.world.root / "gate-matrix")
        self.dry = bench is None
        self.scope = scope
        self.state = GateState()
        self.credited: set[str] = set()

    # -- labels ------------------------------------------------------------------

    def cells(self, command: Command) -> list[str]:
        unobserved = _UNOBSERVED
        if command.op == "mcp-install":
            # U7 credits the collateral primitives only: the MCP level would be
            # the requested registry server's, which the spine never gates.
            unobserved = unobserved | {"mcp"}
        return sorted(
            {
                f"G1|{command.op}|{kind}|{self.state.gate(package, kind)}|{self.scope}"
                for package in command.packages
                for kind in KINDS[package] - unobserved
            }
        )

    # -- commands ----------------------------------------------------------------

    def spec(self, package: str) -> str:
        if package == MIXED:
            return str(self.mixed)
        return self.bench.world.packages[package].spec

    def run(self, op: str, *words: str, packages: Sequence[str] = (), exits=(0,)) -> None:
        """Run one command, check I1-I9, credit its cells, then advance the state."""
        named = tuple(packages)
        command = Command(op, words, named or tuple(self.state.declared), named, frozenset(exits))
        cells = self.cells(command)
        self.credited.update(cells)
        requested = self._requested(command)
        if not self.dry:
            args = (*words, *(self.spec(p) for p in named))
            servers = _mcp_servers(self.bench.deploy_root)
            step = self.step(
                args,
                exits=command.exits,
                cells=cells,
                model=requested.model(),
                converged=False,
                mutating=self._reconciles(op, words),
            )
            self._check_outcome(command, requested, step, servers)
            self._credit(command, cells, step)
        # Every exit is pinned: a refused command leaves the inputs as they were.
        if 0 in command.exits:
            self.state = requested

    def _reconciles(self, op: str, words: Sequence[str]) -> bool:
        """Whether I4 holds this command to the model's allowed set afterwards."""
        if op == "mcp-install" or words[0] not in ("install", "update", "uninstall", "prune"):
            return False
        # Q&L ruling 2026-11-21: prune removes only undeclared packages; denied
        # declared deps stay until removed; I4 = never widen.
        return not (op == "prune" and self.state.denied_by_policy)

    def edit_inputs(self, name: str, change, transition) -> None:
        """An input edit (manifest, approval store, org policy): no command, no credit."""
        if not self.dry:
            self.edit(name, change)
        transition(self.state)

    def _requested(self, command: Command) -> GateState:
        state = GateState(
            list(self.state.declared),
            self.state.gate_on,
            dict(self.state.decisions),
            set(self.state.revoked),
            set(self.state.exec_deployed),
            self.state.denied_by_policy,
        )
        verb, named = command.args[0], command.named
        if verb == "install" and command.op == "install" and named:
            state.declared += [p for p in named if p not in state.declared]
        if verb == "uninstall":
            state.declared = [p for p in state.declared if p not in named]
            state.exec_deployed -= set(named)
            state.revoked -= set(named)
        if verb == "approve":
            state.gate_on = True
            for package in named:
                state.decisions[package] = "allowed"
                state.revoked.discard(package)
        if verb == "deny":
            for package in named:
                state.decisions[package] = "denied"
                if package in state.exec_deployed:
                    state.revoked.add(package)
        if verb in ("install", "update") and command.op in ("install", "update"):
            state.exec_deployed = {
                p
                for p in state.declared
                if all(state.gate(p, k) in ("none", "allowed") for k in KINDS[p] - _PLAIN)
            }
        return state

    def _check_outcome(
        self, command: Command, requested: GateState, step: Step, servers: frozenset[str]
    ) -> None:
        """What I1-I9 cannot see: the deployed set a step must leave behind."""
        before, after = step.before.deployed, step.after.deployed
        if command.op == "mcp-install" and step.result.returncode == 0:
            allowed = {"p-exec-mcp"} if self._admits("p-exec", "mcp") else set()
            problems = mcp_install_problems(
                servers,
                _mcp_servers(self.bench.deploy_root),
                self.bench.manifest(),
                step.after.lock,
                frozenset(allowed),
            )
            assert not problems, "; ".join(problems)
        elif command.op == "prune" and self.state.denied_by_policy:
            # The prune ruling's floor: under a dependency deny prune never widens.
            assert after <= before, f"prune deployed {sorted(after - before)}"
        elif command.op in ("install", "update") and step.result.returncode == 0:
            # I4 rejects what is not allowed; a resolving command must also deploy
            # everything it allows (as RowReplay.check_then does).
            wanted = {
                "/".join(cell)
                for cell in expected_deployed(requested.model(), OBSERVED)
                if (cell[1], cell[2]) in _SURFACE_GLOBS
            }
            assert set(after) == wanted, f"deployed {sorted(after)}, model {sorted(wanted)}"

    def _admits(self, package: str, kind: str) -> bool:
        """Whether the current gate level lets *package*'s *kind* stay deployed."""
        return package in self.state.declared and self.state.gate(package, kind) in (
            "none",
            "allowed",
        )

    def _credit(self, command: Command, cells: Collection[str], step: Step) -> None:
        exit_code = step.result.returncode
        unchanged: dict[str, tuple[str, ...]] = {"outside": ("*",)}
        present: dict[str, list[str]] = {}
        if command.op == "mcp-install":
            unchanged["project"] = _NON_MCP
        elif exit_code != 0:
            unchanged["project"] = _DURABLE
        if (
            MIXED in command.packages
            and MIXED in self.state.revoked
            and command.args[0] != "uninstall"
            and exit_code == 0
        ):
            present["project"] = [_MIXED_SKILL]
        evidence = Evidence(unchanged=unchanged, present=present, exit=command.exits)
        for cell in cells:
            check_obligation(
                cell,
                strength="e2e-evidence",
                before=step.before,
                after=step.after,
                result=step.result,
                evidence=evidence,
                scope=self.scope,
            )

    # -- the spine ---------------------------------------------------------------

    def gate_off(self) -> None:
        def change() -> None:
            data = self.bench.manifest()
            data.pop("executables", None)
            self.bench.write_manifest(data)

        self.edit_inputs("gate-off", change, lambda state: setattr(state, "gate_on", False))

    def revoke(self, packages: Sequence[str]) -> None:
        def change() -> None:
            data = self.bench.manifest()
            for store in ("allow", "deny"):
                entries = (data.get("executables") or {}).get(store) or {}
                for key in [k for k in entries if any(self.spec(p) in k for p in packages)]:
                    del entries[key]
            self.bench.write_manifest(data)

        def transition(state: GateState) -> None:
            for package in packages:
                state.decisions.pop(package, None)
                if package in state.exec_deployed:
                    state.revoked.add(package)

        self.edit_inputs("revoke-" + "-".join(packages), change, transition)

    def deny_by_policy(self) -> None:
        def change() -> None:
            use_policy(self.bench.world, MATRIX_POLICY)

        def transition(state: GateState) -> None:
            state.denied_by_policy = frozenset(PACKAGES)

        self.edit_inputs("policy-deny-all-deps", change, transition)

    def probes(self, *ops: str, exits: Mapping[str, int] | None = None) -> None:
        """Run the package-wide commands *ops* over the declared set."""
        exits = exits or {}
        words = {
            "install": ("install",),
            "install-frozen": ("install", "--frozen"),
            "update": ("update", "--yes"),
            "lock": ("lock",),
            "audit": ("audit",),
            "audit-ci": ("audit", "--ci"),
            "compile": ("compile",),
            "prune": ("prune",),
            "mcp-install": ("install", "--mcp", REGISTRY_SERVER),
        }
        for op in ops:
            self.run(op, *words[op], exits=(exits.get(op, 0),))


def replay_matrix(machine: GateMatrix) -> None:
    """Walk the six gate levels; each level runs only the commands that earn a new id.

    Dropped as redundant (the covering replay or an earlier level holds them):
    a converged install and install --frozen with the gate off, install --frozen
    after a revoke, and plain audit beside audit --ci under the dependency deny.
    """
    exec_and_mixed = (*EXEC_PACKAGES, MIXED)
    reads = ("update", "lock", "audit", "compile", "prune")
    # none: no layer opts into the gate, every executable deploys.
    machine.gate_off()
    machine.run("install", "install", packages=PACKAGES)
    machine.probes(*reads, "mcp-install")
    machine.run("uninstall", "uninstall", packages=EXEC_PACKAGES)
    machine.run("install", "install", packages=EXEC_PACKAGES)
    # approve under the open gate turns the gate on (allowed).
    machine.run("approve", "approve", packages=exec_and_mixed)
    machine.probes("install", "mcp-install")
    # revoked-after-deploy: deny what is deployed; the plain files of p-mixed stay.
    machine.run("deny", "deny", packages=exec_and_mixed)
    machine.probes("mcp-install", "install", *reads)
    machine.run("deny", "deny", packages=(MIXED,))
    machine.run("uninstall", "uninstall", packages=exec_and_mixed)
    # denied-executable: denied before any deploy.
    machine.run("install", "install", packages=EXEC_PACKAGES)
    machine.probes("mcp-install")
    machine.run("uninstall", "uninstall", packages=("p-lsp",))
    # unapproved-exec: the decisions are gone.
    machine.revoke(EXEC_PACKAGES)
    machine.run("install", "install", packages=("p-lsp",))
    machine.probes("mcp-install", "prune")
    machine.run("uninstall", "uninstall", packages=EXEC_PACKAGES)
    # denied-dependency: the org policy denies every package of the project.
    machine.run("install", "install", packages=exec_and_mixed)
    machine.deny_by_policy()
    refused = {
        "install": 1,
        "install-frozen": 1,
        "update": 1,
        "lock": 1,
        "audit-ci": 1,
        "compile": 1,
    }
    probes = ("install", "install-frozen", "update", "lock", "audit-ci", "compile", "prune")
    machine.probes(*probes, "mcp-install", exits=refused)
    machine.run("approve", "approve", packages=exec_and_mixed)
    machine.run("uninstall", "uninstall", packages=PACKAGES)


def matrix_cells(scope: str) -> frozenset[str]:
    """Every machine id the spine credits at *scope* (labels only, no CLI)."""
    machine = GateMatrix(None, scope)
    replay_matrix(machine)
    return frozenset(machine.credited)


# The requested registry server, by id and by the short name .mcp.json keys it under.
REQUESTED_NAMES = frozenset({REGISTRY_SERVER, REGISTRY_SERVER.rsplit("/", 1)[-1]})


def mcp_install_problems(
    before: frozenset[str],
    after: frozenset[str],
    manifest: Mapping[str, object],
    lock,
    allowed: frozenset[str],
) -> list[str]:
    """Why an admitted ``install --mcp <registry server>`` did not do exactly that.

    It must declare, lock and configure the requested server, configure no
    other server, and keep every server it did not request unless the gate no
    longer *allowed* it (a retirement narrows; it never widens).
    """
    problems = []
    declared = {
        str(entry.get("name") if isinstance(entry, Mapping) else entry)
        for entry in ((manifest.get("dependencies") or {}).get("mcp") or [])
    }
    if not REQUESTED_NAMES & declared:
        problems.append(f"{REGISTRY_SERVER} is not declared in apm.yml")
    if lock is None or not REQUESTED_NAMES & set(lock.mcp_servers):
        problems.append(f"{REGISTRY_SERVER} is not recorded in the lockfile")
    if not REQUESTED_NAMES & after:
        problems.append(f"{REGISTRY_SERVER} is not configured in .mcp.json")
    if added := after - before - REQUESTED_NAMES:
        problems.append(f"install --mcp configured other servers: {sorted(added)}")
    if removed := (before - after) - (before - allowed - REQUESTED_NAMES):
        problems.append(f"install --mcp removed servers it may keep: {sorted(removed)}")
    return problems


def _mcp_servers(deploy_root: Path) -> frozenset[str]:
    """Server names in the deploy root's .mcp.json, read now (an immutable set)."""
    path = deploy_root / ".mcp.json"
    if not path.is_file():
        return frozenset()
    return frozenset(json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {})
