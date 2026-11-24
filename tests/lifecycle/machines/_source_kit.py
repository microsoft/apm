"""M-SOURCE kit spine: a registry package's hook, LSP and canvas through every command (#4096).

PROMISE: P-INV-I1..I9 -- every command over a package that ships a hook, an LSP
server and a canvas extension keeps the cross-cutting laws, whatever form
declared it (source: tests/lifecycle/oracles.py).
CELL: OP x SRC=pkg-registry x PRIM in {hook, lsp, canvas} (T3) at project scope.

The M-SOURCE spine installs ``p-kit`` from the package registry but never
observes its executables at the later steps (``witnesses.SOURCE_SPINE_UNOBSERVED``),
so those cells were baseline. This spine declares ``p-kit`` by registry id
with the executables gate off (no ``executables`` block), so every kind
deploys to the copilot surfaces, and drives it through install, lock,
compile, ``audit --ci``, update, uninstall, a reinstall and an orphan prune.
Each step runs I1-I9 and an exact deployed-set check against the model; a
command that keeps the package must leave every executable surface
byte-identical, and uninstall and prune must remove every one. Each step
credits ``SK|<op>|pkg-registry|<prim>`` for hook, lsp and canvas, which the
generator reads as {OP, SRC, PRIM}. ``KNOWN`` lists the strict known cells.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from tests.lifecycle.machines._kit import Bench, KnownCell, Step, StepRunner
from tests.lifecycle.model.expected import expected_deployed
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.oracles import Evidence, check_obligation
from tests.lifecycle.world import registry

PACKAGE = "p-kit"
FORM = "pkg-registry"
REGISTRY_ID = f"lifecycle/{PACKAGE}"
# The executables this spine witnesses (the skill is the M-SOURCE spine's).
PRIMS = ("hook", "lsp", "canvas")
KINDS = {PACKAGE: frozenset({"skill", *PRIMS})}
TARGETS = frozenset({"copilot"})
# Every project path an executable of the package lands on (rows._SURFACE_GLOBS).
SURFACES = (
    ".github/hooks",
    ".github/lsp.json",
    f".github/extensions/{PACKAGE}",
    f".agents/skills/{PACKAGE}",
)
_KEEPING = ("lock", "compile", "audit", "update")
_REMOVING = ("uninstall", "prune")
# Commands that, right after a converged install, must change no byte at all (I2).
_CONVERGED = ("lock", "audit", "update")
# Strict known cells found on main: (op, prim) -> owning issue. An orphan prune keeps
# the pruned package's LSP server registration until the next install (#5250).
KNOWN: dict[tuple[str, str], str] = {("prune", "lsp"): "#5250"}
_KNOWN_LEFT = f"{PACKAGE}/lsp/copilot"
KNOWN_IDS: dict[str, str] = {f"SK|{op}|{FORM}|{prim}": issue for (op, prim), issue in KNOWN.items()}


@dataclass
class KitState:
    declared: bool = False

    def model(self) -> ModelState:
        return ModelState(
            declared=frozenset({PACKAGE}) if self.declared else frozenset(),
            targets=TARGETS,
            exec_gate=False,
        )


class SourceKit(StepRunner):
    """Run (or, when *bench* is None, only label) the registry kit spine at project scope."""

    def __init__(self, bench: Bench | None) -> None:
        super().__init__(bench, ModelState(targets=TARGETS), KINDS)
        self.dry = bench is None
        self.state = KitState()
        self.credited: set[str] = set()
        self._op = ""
        self.known = (
            KnownCell(
                f"SK|prune|{FORM}|lsp",
                "I4",
                "#5250",
                f"{_KNOWN_LEFT} still deployed but not allowed",
                lambda *_args: self._op == "prune",
            ),
        )

    def run(self, op: str, *words: str) -> None:
        """Run one command, check I1-I9 and its outcome, credit its id, then advance."""
        cells = [f"SK|{op}|{FORM}|{prim}" for prim in PRIMS]
        self.credited.update(cells)
        self._op = op
        requested = replace(self.state)
        if op == "install":
            requested.declared = True
        if op == "uninstall":
            requested.declared = False
        if not self.dry:
            fields: dict[str, object] = {"mutating": op != "audit"}
            if op == "audit":
                fields["read_only"] = True
            step = self.step(
                words,
                exits=frozenset({0}),
                cells=tuple(cells),
                model=requested.model(),
                converged=op in _CONVERGED,
                **fields,
            )
            self._check_outcome(op, requested, step)
            for cell, prim in zip(cells, PRIMS, strict=True):
                if (op, prim) not in KNOWN:
                    self._credit(op, cell, step)
        self.state = requested

    def _check_outcome(self, op: str, requested: KitState, step: Step) -> None:
        after = set(step.after.deployed)
        wanted = {"/".join(c) for c in expected_deployed(requested.model(), KINDS)}
        if (op, "lsp") in KNOWN:
            # Strict: the moment #5250 is fixed this fails; flip the cell to required.
            assert _KNOWN_LEFT in after, f"{op} now removes the LSP server: drop KNOWN"
            after.discard(_KNOWN_LEFT)
        assert after == wanted, f"{op}: deployed {sorted(after)}, model {sorted(wanted)}"
        kinds = {entry.split("/")[1] for entry in after}
        if op == "install" or op in _KEEPING:
            assert set(PRIMS) <= kinds, f"{op}: executables not deployed: {sorted(after)}"
        if op in _REMOVING:
            assert not kinds, f"{op} kept {sorted(after)}"

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

        self._edit_manifest("copilot-gate-off", change)

    def declare(self) -> None:
        entry = {"registry": registry.NAME, "id": REGISTRY_ID, "version": "1.0.0"}
        self._edit_manifest(
            "declare-" + PACKAGE,
            lambda data: data.setdefault("dependencies", {}).update(apm=[entry]),
        )

    def undeclare(self) -> None:
        self._edit_manifest(
            "undeclare-" + PACKAGE, lambda data: data["dependencies"].update(apm=[])
        )
        self.state = replace(self.state, declared=False)


def replay_source_kit(machine: SourceKit) -> None:
    """Deploy the kit, run every command that keeps it, then remove it twice."""
    machine.setup()
    machine.declare()
    machine.run("install", "install")
    machine.run("lock", "lock")
    machine.run("compile", "compile")
    machine.run("audit", "audit", "--ci")
    machine.run("update", "update", "--yes")
    machine.run("uninstall", "uninstall", REGISTRY_ID)
    machine.declare()
    machine.run("install", "install")
    machine.undeclare()
    machine.run("prune", "prune")


def source_kit_cells() -> frozenset[str]:
    """Every machine id the registry kit spine credits (labels only, no CLI)."""
    machine = SourceKit(None)
    replay_source_kit(machine)
    return frozenset(machine.credited)
