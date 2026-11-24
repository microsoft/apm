"""M-SOURCE bundle spine: a local bundle's hook, LSP and canvas through every command (#4096).

PROMISE: P-INV-I1..I9 -- every command over a local bundle that ships a hook, an
LSP server and a canvas extension keeps the cross-cutting laws (source:
tests/lifecycle/oracles.py).
PROMISE: P-OWN-executable-item-admission -- a local bundle's executables are
admitted by its exact-content approval key (source:
src/apm_cli/install/local_bundle_pending.py).
CELL: OP x SRC=local-bundle x PRIM in {hook, lsp, canvas} (T3) at project scope.

The M-SOURCE spine installs the world's ``p-pack`` bundle but never observes
its executables at lock, compile, audit or approve
(``witnesses.SOURCE_SPINE_UNOBSERVED``). This spine installs the bundle with
the executables gate off, so every kind deploys, runs lock, compile and
``audit --ci``, which must leave every executable surface byte-identical,
then turns the gate on: the reinstall parks the executables under the bundle's
exact-content key, ``apm approve <key>`` decides them without writing a
surface, and the next install deploys them again. Each step runs I1-I9 and an
exact deployed-set check, and credits ``SB2|<op>|bundle|<prim>``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from tests.lifecycle.machines._kit import Bench, Step, StepRunner
from tests.lifecycle.model.expected import expected_deployed
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.oracles import Evidence, check_obligation

PACKAGE = "p-pack"
FORM = "bundle"
PRIMS = ("hook", "lsp", "canvas")
KINDS = {PACKAGE: frozenset({"skill", *PRIMS})}
TARGETS = frozenset({"copilot"})
SURFACES = (
    ".github/hooks",
    ".github/lsp.json",
    f".github/extensions/{PACKAGE}",
    f".agents/skills/{PACKAGE}",
)
_PENDING = Path("apm_modules") / ".local-bundle-pending.json"
_KEEPING = ("lock", "compile", "audit", "approve-deny")


@dataclass
class BundleState:
    installed: bool = False
    gate_on: bool = False
    approved: bool = False

    def model(self) -> ModelState:
        decisions = {(PACKAGE, kind): "allowed" for kind in PRIMS} if self.approved else {}
        return ModelState(
            declared=frozenset({PACKAGE}) if self.installed else frozenset(),
            decisions=decisions,
            targets=TARGETS,
            exec_gate=self.gate_on,
        )


class SourceBundle(StepRunner):
    """Run (or, when *bench* is None, only label) the bundle spine at project scope."""

    def __init__(self, bench: Bench | None) -> None:
        super().__init__(bench, ModelState(targets=TARGETS), KINDS)
        self.dry = bench is None
        self.state = BundleState()
        self.credited: set[str] = set()
        self.key = ""

    def run(self, op: str, *words: str, credit: bool = True) -> None:
        cells = [f"SB2|{op}|{FORM}|{prim}" for prim in PRIMS] if credit else []
        self.credited.update(cells)
        requested = replace(self.state)
        if op == "install":
            requested.installed = True
        if op == "approve-deny":
            requested.approved = True
        if not self.dry:
            fields: dict[str, object] = {"mutating": op != "audit"}
            if op == "audit":
                fields["read_only"] = True
            step = self.step(
                words,
                exits=frozenset({0}),
                cells=tuple(cells),
                model=requested.model() if op != "approve-deny" else self.state.model(),
                converged=op == "audit",
                **fields,
            )
            self._check_outcome(op, requested, step)
            for cell in cells:
                self._credit(op, cell, step)
        self.state = requested

    def _check_outcome(self, op: str, requested: BundleState, step: Step) -> None:
        after = set(step.after.deployed)
        model = requested.model() if op != "approve-deny" else self.state.model()
        wanted = {"/".join(c) for c in expected_deployed(model, KINDS)}
        assert after == wanted, f"{op}: deployed {sorted(after)}, model {sorted(wanted)}"
        kinds = {entry.split("/")[1] for entry in after}
        if op in ("lock", "compile", "audit") or (op == "install" and not requested.gate_on):
            assert set(PRIMS) <= kinds, f"{op}: executables not deployed: {sorted(after)}"
        if op == "install" and requested.gate_on and not requested.approved:
            assert not set(PRIMS) & kinds, f"unapproved executables deployed: {sorted(after)}"
            record = self.bench.manifest_dir / _PENDING
            bundles = json.loads(record.read_text(encoding="utf-8")).get("bundles") or {}
            keys = [
                e["approval_key"]
                for e in bundles.values()
                if str(e.get("approval_key", "")).startswith(PACKAGE)
            ]
            assert len(keys) == 1, f"parked executables are not pending: {bundles}"
            self.key = keys[0]

    def _credit(self, op: str, cell: str, step: Step) -> None:
        unchanged: dict[str, tuple[str, ...]] = {"outside": ("*",)}
        if op in _KEEPING:
            unchanged["project"] = SURFACES
        check_obligation(
            cell,
            strength="e2e-evidence",
            before=step.before,
            after=step.after,
            result=step.result,
            evidence=Evidence(unchanged=unchanged, exit=frozenset({0})),
        )

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

        self._edit_manifest("copilot-gate-off", change)
        if not self.dry:
            # compile needs project content to render; a bundle alone gives it none.
            def author() -> None:
                doc = self.bench.manifest_dir / ".apm" / "instructions" / "w.instructions.md"
                doc.parent.mkdir(parents=True, exist_ok=True)
                doc.write_text('---\napplyTo: "**"\n---\n# project rule\n', encoding="ascii")

            self.edit("project-instruction", author)

    def gate_on(self) -> None:
        self._edit_manifest("gate-on", lambda data: data.update(executables={}))
        self.state = replace(self.state, gate_on=True)

    def spec(self) -> str:
        return "" if self.dry else self.bench.world.packages[f"{PACKAGE}:bundle"].spec


def replay_source_bundle(machine: SourceBundle) -> None:
    """Deploy the bundle ungated, keep it through every read, then park, approve, redeploy."""
    machine.setup()
    spec = machine.spec()
    # The project's own instruction deploys first, so compile and audit see it recorded.
    machine.run("project-install", "install", credit=False)
    machine.run("install", "install", spec, credit=False)
    machine.run("lock", "lock")
    machine.run("compile", "compile")
    machine.run("audit", "audit", "--ci")
    machine.gate_on()
    machine.run("install", "install", spec, credit=False)
    machine.run("approve-deny", "approve", machine.key)
    machine.run("install", "install", spec, credit=False)


def source_bundle_cells() -> frozenset[str]:
    """Every machine id the bundle spine credits (labels only, no CLI)."""
    machine = SourceBundle(None)
    replay_source_bundle(machine)
    return frozenset(machine.credited)
