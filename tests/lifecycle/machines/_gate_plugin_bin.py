"""M-GATE plugin bin spine: a whole Claude plugin's bin/ at project scope (#4894).

PROMISE: P-INV-I4 -- a deny, revoked approval or stricter policy never widens
what is deployed; the next resolving command retires bin/ the gate no longer
allows (source: tests/lifecycle/oracles.py).
PROMISE: P-OWN-executable-item-admission -- a native plugin under
.agents/plugins that the current approvals or policy no longer allow is
retired by ``apm prune`` as one unit: its root, bin/ included, and its
declarations in the harness settings files go (source:
docs/src/content/docs/reference/cli/prune.md, src/apm_cli/native_plugins/
uninstall.py retire_gated_native_plugins).
CELL: OP x PRIM=bin x GATE (T1) at project scope.

Loose bin/ deploys only at user scope (install/exec_gate.py
plugin_bin_deployable), but a trusted Claude plugin that ships bin/ deploys
whole to ``.agents/plugins/<plugin>/`` at project scope
(claude_plugins/admission.py), so prune and marketplace reach bin/ there.
The spine drives ``p-native-claude-bin`` from git on the claude target
through three gate levels: ``none`` (no executables block), then
``unapproved-exec`` (an empty block turns the gate on over the deployed
plugin) and ``denied-dependency`` (org policy denies the plugin). Every
deploying install passes ``--trust-bin`` so the gate alone decides bin/.
Each prune that retires the plugin must remove every file of its root and
its ``enabledPlugins`` keys and drop its lockfile record; every step runs
I1-I9. ``plugin_bin_cells`` labels the same script statically.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace

from tests.lifecycle.machines._kit import Bench, Step, StepRunner
from tests.lifecycle.model.expected import expected_deployed
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.oracles import Evidence, check_obligation
from tests.lifecycle.world.scopes import use_policy

PLUGIN = "p-native-claude-bin"
KINDS = {PLUGIN: frozenset({"bin"})}
TARGETS = frozenset({"claude"})
ROOT = f".agents/plugins/{PLUGIN}"
BIN_PATH = f"{ROOT}/bin/{PLUGIN}"
DENY_POLICY = "deny-native"
# Harness settings files whose enabledPlugins declare a project-scope plugin.
SETTINGS = (".claude/settings.json", ".github/copilot/settings.json")
_RETIRED = r"1 native plugin\(s\) removed"


class PluginBinBench(Bench):
    """A project bench that also observes the plugin's bin/ as the claude bin surface."""

    def surface_globs(self) -> Mapping[tuple[str, str], str | tuple[str, str]]:
        return {**super().surface_globs(), ("bin", "claude"): ".agents/plugins/{pkg}/bin/{pkg}"}


@dataclass
class PluginBinState:
    """The user's inputs for the plugin, as the spine has set them."""

    declared: bool = False
    gate_on: bool = False
    approved: bool = False
    denied_by_policy: bool = False

    def gate(self) -> str:
        if self.denied_by_policy:
            return "denied-dependency"
        if not self.gate_on:
            return "none"
        return "allowed" if self.approved else "unapproved-exec"

    def model(self) -> ModelState:
        model = ModelState(
            declared=frozenset({PLUGIN}) if self.declared else frozenset(),
            decisions={(PLUGIN, "bin"): "allowed"} if self.approved else {},
            targets=TARGETS,
            exec_gate=self.gate_on,
        )
        return model.with_policy(DENY_POLICY, [PLUGIN]) if self.denied_by_policy else model


class GatePluginBin(StepRunner):
    """Run (or, when *bench* is None, only label) the plugin bin spine at project scope."""

    def __init__(self, bench: Bench | None) -> None:
        super().__init__(bench, ModelState(targets=TARGETS), KINDS)
        self.dry = bench is None
        self.state = PluginBinState()
        self.credited: set[str] = set()
        # Every file of the deployed plugin root, bin/ included (set by a deploy).
        self.root_files: frozenset[str] = frozenset()

    def run(self, op: str, *words: str, retires: bool = False) -> None:
        """Run one command, check I1-I9 and its outcome, credit its id, then advance."""
        cell = f"G1|{op}|bin|{self.state.gate()}|project"
        self.credited.add(cell)
        requested = self._requested(op)
        if not self.dry:
            fields: dict[str, object] = {"mutating": op != "marketplace"}
            if op == "marketplace":
                fields.update(forbidden={"project": ("*",), "outside": ("*",)}, allowed={})
            step = self.step(
                words, exits=frozenset({0}), cells=(cell,), model=requested.model(), **fields
            )
            self._check_outcome(op, requested, step, retires)
            self._credit(op, cell, step, retires)
        self.state = requested

    def _requested(self, op: str) -> PluginBinState:
        state = replace(self.state)
        if op == "install":
            state.declared = True
        if op == "approve":
            state.gate_on = state.approved = True
        return state

    def _check_outcome(self, op: str, requested: PluginBinState, step: Step, retires: bool) -> None:
        """What I1-I9 cannot see: the whole root and its declarations go or stay together."""
        after = set(step.after.deployed)
        wanted = {"/".join(c) for c in expected_deployed(requested.model(), KINDS)}
        if op == "install":
            assert after == wanted, f"install: deployed {sorted(after)}, model {sorted(wanted)}"
            if after:
                self.root_files = self._root_files()
                assert BIN_PATH in self.root_files, sorted(self.root_files)
                # A claude-only plugin is declared to Claude Code and pinned off for
                # Copilot, so each settings file holds a key prune must remove.
                files = {entry.split(":", 1)[0] for entry in self._declarations()}
                assert files == set(SETTINGS), f"install declared it in {sorted(files)}"
            return
        if not retires:
            assert after == set(step.before.deployed), f"{op} changed bin/: {sorted(after)}"
            return
        root = self.bench.deploy_root / ROOT
        assert not root.exists(), f"prune kept {ROOT}: {sorted(self._root_files())}"
        assert not self._declarations(), f"prune kept declarations: {self._declarations()}"
        declared = (self.bench.manifest().get("dependencies") or {}).get("apm") or []
        assert bool(declared) == requested.declared, f"prune changed apm.yml: {declared}"
        lock, before = step.after.lock, step.before.lock
        assert before is not None, "prune ran without a readable lockfile"
        kept = {k for k in before.dependencies if PLUGIN in k}
        others = set(before.dependencies) - kept
        if not requested.declared and not others:
            # prune.md: a prune that leaves zero dependencies deletes apm.lock.yaml.
            assert not self.bench.lock_path.exists(), "orphan prune kept an empty lockfile"
            return
        assert lock is not None, "prune left no readable lockfile"
        rows = {k for k in lock.dependencies if PLUGIN in k}
        if not requested.declared:
            assert not rows, f"orphan prune kept lock rows: {sorted(rows)}"
            return
        # A gate retirement keeps the declared dependency's row, minus its record.
        assert kept and rows == kept, f"prune changed the rows: {sorted(kept)} -> {sorted(rows)}"
        records = [k for k in rows if lock.dependencies[k].native_plugin]
        assert not records, f"prune kept the native plugin record: {records}"

    def _credit(self, op: str, cell: str, step: Step, retires: bool) -> None:
        present: dict[str, list[str]] = {}
        absent: dict[str, list[str]] = {}
        unchanged: dict[str, tuple[str, ...]] = {"outside": ("*",)}
        matches: tuple[str, ...] = ()
        if retires:
            absent["project"] = sorted(self.root_files)
            matches = (_RETIRED,)
        elif op != "install":
            # A catalog command leaves every project byte alone; approve leaves the root.
            unchanged["project"] = ("*",) if op == "marketplace" else (ROOT, *SETTINGS)
        if not retires and step.after.deployed:
            present["project"] = [BIN_PATH]
        evidence = Evidence(
            absent=absent,
            present=present,
            unchanged=unchanged,
            output_matches=matches,
            exit=frozenset({0}),
        )
        check_obligation(
            cell,
            strength="e2e-evidence",
            before=step.before,
            after=step.after,
            result=step.result,
            evidence=evidence,
        )

    def _root_files(self) -> frozenset[str]:
        root = self.bench.deploy_root / ROOT
        if not root.is_dir():
            return frozenset()
        return frozenset(
            path.relative_to(self.bench.deploy_root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        )

    def _declarations(self) -> list[str]:
        """``enabledPlugins`` keys of the plugin in every harness settings file."""
        found = []
        for name in SETTINGS:
            path = self.bench.deploy_root / name
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            keys = (data.get("enabledPlugins") or {}).keys()
            found += [f"{name}:{key}" for key in keys if key.split("@", 1)[0] == PLUGIN]
        return found

    # -- input edits (no command, no credit) -----------------------------------

    def _edit_manifest(self, name: str, change) -> None:
        if self.dry:
            return

        def apply() -> None:
            data = self.bench.manifest()
            change(data)
            self.bench.write_manifest(data)

        self.edit(name, apply)

    def setup(self) -> None:
        """Target claude with no executables block: no layer opts into the gate."""

        def change(data: dict) -> None:
            data["targets"] = sorted(TARGETS)
            data.pop("executables", None)

        self._edit_manifest("claude-gate-off", change)

    def undeclare(self) -> None:
        self._edit_manifest("undeclare-" + PLUGIN, lambda data: data["dependencies"].update(apm=[]))
        self.state.declared = False

    def gate_on(self) -> None:
        """An empty executables block turns the gate on over the deployed plugin."""
        self._edit_manifest("gate-on", lambda data: data.update(executables={}))
        self.state.gate_on = True

    def deny_by_policy(self) -> None:
        if not self.dry:
            self.edit("policy-" + DENY_POLICY, lambda: use_policy(self.bench.world, DENY_POLICY))
        self.state.denied_by_policy = True

    def spec(self) -> str:
        return "" if self.dry else self.bench.world.packages[f"{PLUGIN}:git"].spec

    def marketplace_url(self) -> str:
        return "" if self.dry else self.bench.world.services.marketplace_url


def replay_plugin_bin(machine: GatePluginBin) -> None:
    """Deploy the plugin whole, then let prune retire it at each gate level."""
    install = ("install", "--trust-bin")
    machine.setup()
    # none: an orphaned plugin is pruned whole.
    machine.run("install", *install, machine.spec())
    machine.undeclare()
    machine.run("prune", "prune", retires=True)
    # unapproved-exec: the gate turns on over a plugin deployed with it off.
    machine.run("install", *install, machine.spec())
    machine.gate_on()
    machine.run("prune", "prune", retires=True)
    # denied-dependency: org policy denies the approved, deployed plugin.
    machine.run("approve", "approve", PLUGIN)
    machine.run("install", *install)
    machine.deny_by_policy()
    machine.run("marketplace", "marketplace", "add", machine.marketplace_url())
    machine.run("prune", "prune", retires=True)


def plugin_bin_cells() -> frozenset[str]:
    """Every machine id the plugin bin spine credits (labels only, no CLI)."""
    machine = GatePluginBin(None)
    replay_plugin_bin(machine)
    return frozenset(machine.credited)


def deploys_bin(cell: str) -> bool:
    """Whether the step behind *cell* deploys the plugin's bin/ at project scope."""
    _, op, _, gate, _ = cell.split("|")
    return op == "install" and gate in ("none", "allowed")
