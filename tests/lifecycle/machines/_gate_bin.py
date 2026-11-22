"""M-GATE bin spine: a marketplace plugin's bin/ at user scope at every gate level (#4095).

PROMISE: P-INV-I4 -- a deny, revoked approval or the gate turning on never
widens what is deployed; the next resolving command retires bin/ the gate no
longer allows (source: tests/lifecycle/oracles.py).
PROMISE: P-OWN-executable-item-admission -- bin/ of a Claude marketplace plugin
deploys only at user scope, only when approved (or the gate is off) and only
with ``--trust-bin`` (source: src/apm_cli/install/exec_gate.py
plugin_bin_deployable, src/apm_cli/security/executables.py compose_trust_bin).
CELL: OP x PRIM=bin x GATE (T1).

bin/ deploys nowhere else (U-BIN), so this spine is the only place the GATE
matrix can observe it. It drives ``p-native-claude-bin@lifecycle-market`` on
the claude target of the user scope through five gate levels: gate off
(``none``), ``allowed`` (the user manifest opts into the gate), ``revoked-after-deploy`` (deny after a deploy),
``denied-executable`` and ``unapproved-exec`` (decisions dropped from
``APM_HOME/config.json``). Every resolving install passes ``--trust-bin`` so
the gate decision alone decides bin/. Every step runs I1-I9; installs and
updates must leave exactly the model's deployed set, and ``install --mcp``
leaves bin/ byte-identical (U7). The dependency-deny level has no user-scope
command: org policy does not govern user scope (#3703). ``bin_cells`` labels
the same script statically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from tests.lifecycle import driver
from tests.lifecycle.machines._kit import Bench, Step, StepRunner
from tests.lifecycle.model.expected import expected_deployed
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.oracles import Evidence, check_obligation
from tests.lifecycle.world.services import REGISTRY_SERVER

PLUGIN = "p-native-claude-bin"
SPEC = f"{PLUGIN}@lifecycle-market"
KINDS = {PLUGIN: frozenset({"bin"})}
TARGETS = frozenset({"claude"})
BIN_PATH = f".agents/plugins/{PLUGIN}/bin/{PLUGIN}"
_PLUGINS = ".agents/plugins"


@dataclass
class BinState:
    """The user's gate inputs for the plugin, as the spine has set them."""

    declared: bool = False
    gate_on: bool = False
    decision: str | None = None
    revoked: bool = False
    deployed: bool = False

    def gate(self) -> str:
        if not self.gate_on:
            return "none"
        if self.revoked:
            return "revoked-after-deploy"
        if self.decision == "allowed":
            return "allowed"
        return "denied-executable" if self.decision == "denied" else "unapproved-exec"

    def allows(self) -> bool:
        return self.gate() in ("none", "allowed")

    def model(self) -> ModelState:
        decisions = {(PLUGIN, "bin"): self.decision} if self.decision else {}
        return ModelState(
            declared=frozenset({PLUGIN}) if self.declared else frozenset(),
            decisions=decisions,
            revoked=frozenset({(PLUGIN, "bin")}) if self.revoked else frozenset(),
            targets=TARGETS,
            scope="user",
            exec_gate=self.gate_on,
        )


class GateBin(StepRunner):
    """Run (or, when *bench* is None, only label) the bin spine at user scope."""

    def __init__(self, bench: Bench | None) -> None:
        super().__init__(bench, ModelState(scope="user", targets=TARGETS), KINDS)
        self.dry = bench is None
        self.state = BinState()
        self.credited: set[str] = set()

    def run(self, op: str, *words: str, exits=(0,)) -> None:
        """Run one command, check I1-I9 and its outcome, credit its id, then advance."""
        cell = f"G1|{op}|bin|{self.state.gate()}|user"
        self.credited.add(cell)
        requested = self._requested(op)
        if not self.dry:
            step = self.step(
                words,
                exits=frozenset(exits),
                cells=(cell,),
                model=requested.model(),
                converged=False,
                mutating=op in ("install", "update", "uninstall"),
            )
            self._check_outcome(op, requested, step)
            self._credit(op, cell, step)
        self.state = requested

    def _requested(self, op: str) -> BinState:
        state = BinState(**vars(self.state))
        if op == "install":
            state.declared = True
        if op == "uninstall":
            state.declared = state.revoked = False
        if op == "approve":
            state.decision, state.revoked = "allowed", False
        if op == "deny":
            state.decision = "denied"
            state.revoked = state.deployed
        if op in ("install", "update", "uninstall"):
            state.deployed = state.declared and state.allows()
        return state

    def _check_outcome(self, op: str, requested: BinState, step: Step) -> None:
        """What I1-I9 cannot see: the exact bin/ a step must leave behind."""
        after = set(step.after.deployed)
        if op in ("install", "update", "uninstall"):
            wanted = {"/".join(c) for c in expected_deployed(requested.model(), KINDS)}
            assert after == wanted, f"{op}: deployed {sorted(after)}, model {sorted(wanted)}"
        else:
            assert after == set(step.before.deployed), f"{op} changed bin/: {sorted(after)}"
        if op == "mcp-install":
            servers = (self.bench.manifest().get("dependencies") or {}).get("mcp") or []
            names = {entry.get("name") if isinstance(entry, dict) else entry for entry in servers}
            assert REGISTRY_SERVER in names, f"install --mcp did not declare it: {names}"

    def _credit(self, op: str, cell: str, step: Step) -> None:
        unchanged: dict[str, tuple[str, ...]] = {"outside": ("*",)}
        present: dict[str, list[str]] = {}
        if op not in ("install", "update", "uninstall"):
            unchanged["home"] = (_PLUGINS,)
            if self.state.deployed:
                present["home"] = [BIN_PATH]
        evidence = Evidence(unchanged=unchanged, present=present, exit=frozenset({0}))
        check_obligation(
            cell,
            strength="e2e-evidence",
            before=step.before,
            after=step.after,
            result=step.result,
            evidence=evidence,
            scope="user",
        )

    # -- input edits (no command, no credit) -----------------------------------

    def setup(self) -> None:
        """Register the marketplace and target claude with the gate off."""
        if self.dry:
            return
        world = self.bench.world
        result = driver.run(world, ("marketplace", "add", world.services.marketplace_url))
        assert result.returncode == 0, result.stdout + result.stderr

        def change() -> None:
            data = self.bench.manifest()
            data["targets"] = sorted(TARGETS)
            data.pop("executables", None)
            self.bench.write_manifest(data)

        self.edit("claude-gate-off", change)

    def gate_on(self) -> None:
        """Opt the user manifest into the gate; a user allow alone never does
        (security/executables.py: manifest block, a user deny or an org signal)."""
        if not self.dry:

            def change() -> None:
                data = self.bench.manifest()
                data["executables"] = {"allow": {}}
                self.bench.write_manifest(data)

            self.edit("gate-on", change)
        self.state.gate_on = True

    def revoke(self) -> None:
        """Drop every decision on the plugin from the user approval store."""
        if not self.dry:

            def change() -> None:
                config = self.bench.world.apm_home / "config.json"
                data = json.loads(config.read_text(encoding="utf-8"))
                for store in ("allow", "deny"):
                    entries = (data.get("executables") or {}).get(store) or {}
                    for key in [k for k in entries if PLUGIN in k]:
                        del entries[key]
                config.write_text(json.dumps(data, indent=2), encoding="utf-8")

            self.edit("revoke-" + PLUGIN, change)
        self.state.decision = None
        self.state.revoked = self.state.deployed


def replay_bin(machine: GateBin) -> None:
    """Walk the five user-scope gate levels over the plugin."""
    install = ("install", "--trust-bin")
    mcp = ("install", "--mcp", REGISTRY_SERVER)
    machine.setup()
    # none: no layer opts into the gate; --trust-bin deploys bin/.
    machine.run("install", *install, SPEC)
    machine.run("mcp-install", *mcp)
    machine.run("lock", "lock")
    machine.run("compile", "compile")
    machine.run("update", "update", "--yes")
    machine.run("uninstall", "uninstall", SPEC)
    machine.run("install", *install, SPEC)
    # approve under the open gate, then the manifest opts in (allowed).
    machine.run("approve", "approve", SPEC)
    machine.gate_on()
    machine.run("mcp-install", *mcp)
    # revoked-after-deploy: install --mcp keeps bin/ (U7); install retires it.
    machine.run("deny", "deny", SPEC)
    machine.run("mcp-install", *mcp)
    machine.run("install", *install)
    machine.run("uninstall", "uninstall", SPEC)
    # denied-executable: denied before any deploy.
    machine.run("install", *install, SPEC)
    machine.run("mcp-install", *mcp)
    # unapproved-exec: the decision is gone.
    machine.revoke()
    machine.run("mcp-install", *mcp)
    machine.run("install", *install)
    machine.run("uninstall", "uninstall", SPEC)


def bin_cells() -> frozenset[str]:
    """Every machine id the bin spine credits (labels only, no CLI)."""
    machine = GateBin(None)
    replay_bin(machine)
    return frozenset(machine.credited)
