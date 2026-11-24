"""M-SOURCE bin spine: a whole Claude plugin's bin/ through every resolving command (#4096).

PROMISE: P-INV-I1..I9 -- every command over a dependency whose package ships
bin/ keeps the cross-cutting laws, whatever form declared it (source:
tests/lifecycle/oracles.py).
PROMISE: P-OWN-executable-item-admission -- a trusted Claude plugin that ships
bin/ deploys whole to ``.agents/plugins/<plugin>/`` at project scope, the
commands that do not resolve leave that root byte-identical, and uninstall or
an orphan prune removes the root, bin/ included, with its harness
declarations (source: claude_plugins/admission.py, native_plugins/uninstall.py).
CELL: OP x SRC x PRIM=bin (T3) at project scope.

Loose bin/ never deploys at project scope (install/exec_gate.py
``plugin_bin_deployable``), so the M-SOURCE spine's p-exec steps prove nothing
about bin/ there. A whole plugin carries bin/ to project scope (U-BIN), so
this spine declares ``p-native-claude-bin`` by each form that resolves it --
a local path (``native``) and git shorthand (``shorthand``) -- and drives
each through install, lock, compile, ``audit --ci``, update, approve,
uninstall and an orphan prune. Every deploying install passes ``--trust-bin``;
no executables block is declared until approve turns the gate on, approving
the plugin. Each step runs I1-I9 and credits ``SB|<op>|<form>``, which the
generator reads as {OP, SRC, PRIM=bin}. ``source_bin_cells`` labels the same
script statically.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from tests.lifecycle.machines._gate_plugin_bin import (
    BIN_PATH,
    KINDS,
    PLUGIN,
    ROOT,
    SETTINGS,
    TARGETS,
)
from tests.lifecycle.machines._kit import Bench, Step, StepRunner
from tests.lifecycle.model.expected import expected_deployed
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.oracles import Evidence, check_obligation

# Forms of the plugin this spine drives -> the world package key of their spec.
FORMS = {"native": PLUGIN, "shorthand": f"{PLUGIN}:git"}
# Commands that resolve the graph but must leave a deployed, trusted root alone.
_KEEPING = ("lock", "compile", "audit", "update", "approve-deny")
_REMOVING = ("uninstall", "prune")


@dataclass
class SourceBinState:
    declared: bool = False
    approved: bool = False

    def model(self) -> ModelState:
        return ModelState(
            declared=frozenset({PLUGIN}) if self.declared else frozenset(),
            decisions={(PLUGIN, "bin"): "allowed"} if self.approved else {},
            targets=TARGETS,
            exec_gate=self.approved,
        )


class SourceBin(StepRunner):
    """Run (or, when *bench* is None, only label) the source bin spine at project scope."""

    def __init__(self, bench: Bench | None) -> None:
        super().__init__(bench, ModelState(targets=TARGETS), KINDS)
        self.dry = bench is None
        self.state = SourceBinState()
        self.credited: set[str] = set()
        self.root_files: frozenset[str] = frozenset()
        self.root_digest: str | None = None

    def run(self, op: str, form: str, *words: str) -> None:
        """Run one command for *form*, check I1-I9 and its outcome, credit its id."""
        cell = f"SB|{op}|{form}"
        self.credited.add(cell)
        requested = self._requested(op)
        if not self.dry:
            fields: dict[str, object] = {"mutating": op not in ("audit",)}
            if op == "audit":
                fields["read_only"] = True
            step = self.step(
                words, exits=frozenset({0}), cells=(cell,), model=requested.model(), **fields
            )
            self._check_outcome(op, requested, step)
            self._credit(op, cell, step)
        self.state = requested

    def _requested(self, op: str) -> SourceBinState:
        state = replace(self.state)
        if op == "install":
            state.declared = True
        if op == "approve-deny":
            state.approved = True
        if op == "uninstall":
            state.declared = False
        return state

    def _check_outcome(self, op: str, requested: SourceBinState, step: Step) -> None:
        """What I1-I9 cannot see: the root's bytes stay, or the whole root goes."""
        after = set(step.after.deployed)
        wanted = {"/".join(c) for c in expected_deployed(requested.model(), KINDS)}
        assert after == wanted, f"{op}: deployed {sorted(after)}, model {sorted(wanted)}"
        if op == "install":
            self.root_files = self._root_files()
            self.root_digest = self._digest()
            assert BIN_PATH in self.root_files, sorted(self.root_files)
            files = {entry.split(":", 1)[0] for entry in self._declarations()}
            assert files == set(SETTINGS), f"install declared it in {sorted(files)}"
            return
        if op in _KEEPING:
            assert self._root_files() == self.root_files, f"{op} changed the plugin root"
            assert self._digest() == self.root_digest, f"{op} changed the plugin bytes"
            return
        root = self.bench.deploy_root / ROOT
        assert not root.exists(), f"{op} kept {ROOT}: {sorted(self._root_files())}"
        assert not self._declarations(), f"{op} kept declarations: {self._declarations()}"
        lock = step.after.lock
        rows = [k for k in (lock.dependencies if lock else {}) if PLUGIN in k]
        assert not rows, f"{op} kept lock rows: {rows}"

    def _credit(self, op: str, cell: str, step: Step) -> None:
        present: dict[str, list[str]] = {}
        absent: dict[str, list[str]] = {}
        unchanged: dict[str, tuple[str, ...]] = {"outside": ("*",)}
        if op in _REMOVING:
            absent["project"] = sorted(self.root_files)
        else:
            present["project"] = [BIN_PATH]
            if op in _KEEPING:
                unchanged["project"] = (ROOT,)
        check_obligation(
            cell,
            strength="e2e-evidence",
            before=step.before,
            after=step.after,
            result=step.result,
            evidence=Evidence(
                absent=absent, present=present, unchanged=unchanged, exit=frozenset({0})
            ),
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

    def _digest(self) -> str:
        digest = hashlib.sha256()
        for rel in sorted(self._root_files()):
            digest.update(rel.encode() + b"\0" + (self.bench.deploy_root / rel).read_bytes())
        return digest.hexdigest()

    def _declarations(self) -> list[str]:
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
        def change(data: dict) -> None:
            data["targets"] = sorted(TARGETS)
            data.pop("executables", None)

        self._edit_manifest("claude-gate-off", change)
        self.state = SourceBinState()

    def undeclare(self) -> None:
        self._edit_manifest("undeclare-" + PLUGIN, lambda data: data["dependencies"].update(apm=[]))
        self.state = replace(self.state, declared=False)

    def spec(self, form: str) -> str:
        return "" if self.dry else self.bench.world.packages[FORMS[form]].spec


def replay_source_bin(machine: SourceBin) -> None:
    """Each form: deploy the root, run every command that keeps it, then remove it twice."""
    for form in FORMS:
        machine.setup()
        spec = machine.spec(form)
        machine.run("install", form, "install", "--trust-bin", spec)
        machine.run("lock", form, "lock")
        machine.run("compile", form, "compile")
        machine.run("audit", form, "audit", "--ci")
        machine.run("update", form, "update", "--yes")
        machine.run("approve-deny", form, "approve", PLUGIN)
        machine.run("uninstall", form, "uninstall", spec)
        machine.run("install", form, "install", "--trust-bin", spec)
        machine.undeclare()
        machine.run("prune", form, "prune")


def source_bin_cells() -> frozenset[str]:
    """Every machine id the source bin spine credits (labels only, no CLI)."""
    machine = SourceBin(None)
    replay_source_bin(machine)
    return frozenset(machine.credited)
