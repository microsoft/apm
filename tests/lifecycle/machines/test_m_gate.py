"""M-GATE: generated executable-trust sequences over the real CLI (#4031).

``python -m apm_cli.cli`` runs every command. The mandatory spine replays the
GATE obligation cells once per scope; the generated test then explores from a
restored pristine world (``max_examples`` 4 on PR, ``APM_GATE_MAX_EXAMPLES``
raises it for the nightly shard). A recorded mutant of the approval owner
must fail the machine.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, Phase, settings
from hypothesis.stateful import run_state_machine_as_test

from tests.lifecycle.machines._gate_kit import (
    COMPLETED_SPINES,
    KNOWN_CELLS,
    SCOPES,
    GateMachine,
    GateWorld,
)
from tests.lifecycle.oracles import ExpectedCell, Observation, Operation, check_cross_cutting
from tests.lifecycle.snapshot import restore_tree
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [pytest.mark.integration, pytest.mark.lifecycle_smoke]

OBLIGATIONS_PATH = Path(__file__).parents[2] / "quality" / "gate_cell_obligations.json"
MUTANT_PATH = Path(__file__).parents[2] / "fixtures" / "gate_mutants" / "skip_approval_check.py"

# One step per tuple: (rule, *arguments). Shared by both scopes; project-only
# rules (audit_ci, prune) are replaced at user scope, where `apm audit` and
# `apm prune` have no --global form.
_SPINE: tuple[tuple[object, ...], ...] = (
    ("install", "plain"),
    ("install", "frozen"),
    ("audit_ci", False),
    ("decide", "hook", "approve"),
    ("declare", "hook"),
    ("declare", "lsp"),
    ("declare", "plugin"),
    ("install", "plain"),
    ("audit_ci", False),
    ("decide", "hook", "approve"),
    ("decide", "lsp", "approve"),
    ("decide", "plugin", "approve"),
    ("install", "plain"),
    ("install", "plain"),
    ("install", "frozen"),
    ("audit_ci", True),
    ("update",),
    ("uninstall", "lsp"),
    ("declare", "lsp"),
    ("install", "plain"),
    ("decide", "lsp", "approve"),
    ("install", "plain"),
    ("decide", "lsp", "deny"),
    ("install", "plain"),
    ("decide", "plugin", "deny"),
    ("decide", "lsp", "revoke"),
    ("install", "plain"),
    ("decide", "hook", "deny"),
    ("audit_ci", False),
    ("install", "plain"),
    ("decide", "hook", "revoke"),
    ("decide", "hook", "approve"),
    ("install", "plain"),
    ("set_policy", "org-deny"),
    ("install", "frozen"),
    ("install", "plain"),
    ("install", "plain"),
    ("set_policy", "org-deny-all"),
    ("install", "plain"),
    ("audit_ci", False),
    ("uninstall", "lsp"),
    ("declare", "plugin"),
    ("prune",),
    ("uninstall", "hook"),
    ("install", "plain"),
)
_USER_REPLACEMENTS = {"audit_ci": ("install", "frozen"), "prune": ("update",)}


def gate_spine(scope: str) -> tuple[tuple[object, ...], ...]:
    """The deterministic GATE spine for *scope*."""
    if scope == "project":
        return _SPINE
    return tuple(_USER_REPLACEMENTS.get(str(step[0]), step) for step in _SPINE)


def replay_gate_spine(machine: GateMachine) -> None:
    for name, *arguments in gate_spine(machine.scope):
        getattr(machine, str(name))(*arguments)


def gate_obligations(scope: str) -> frozenset[str]:
    data = json.loads(OBLIGATIONS_PATH.read_text(encoding="ascii"))
    return frozenset(cell for cell in data["cells"] if cell.endswith(f"|{scope}"))


@pytest.mark.parametrize("scope", SCOPES)
def test_gate_mandatory_spine_covers_obligations(
    tmp_path: Path, lifecycle_entry_point: tuple[str, ...], scope: str
) -> None:
    """Replay the GATE spine through the oracle and cover every obligation cell."""
    machine = GateMachine(GateWorld.create(tmp_path / "world", lifecycle_entry_point, scope))
    replay_gate_spine(machine)
    missing = gate_obligations(scope) - machine.visited
    assert not missing, f"GATE obligation cells not visited: {sorted(missing)}"
    strict = {cell.label for cell in KNOWN_CELLS if scope in cell.scopes}
    assert machine.expected_seen == strict, (
        f"strict expected failures that no longer fail: {sorted(strict - machine.expected_seen)}"
    )
    COMPLETED_SPINES.add(scope)


@pytest.mark.parametrize("scope", SCOPES)
def test_gate_generated_sequences_keep_cross_cutting_laws(
    tmp_path: Path, lifecycle_entry_point: tuple[str, ...], scope: str
) -> None:
    """Generate, shrink and check GATE sequences from one restored pristine world."""
    world = GateWorld.create(tmp_path / "world", lifecycle_entry_point, scope)
    pristine = tmp_path / "world-pristine"
    restore_tree(world.isolated.root, pristine)

    def factory() -> GateMachine:
        restore_tree(pristine, world.isolated.root)
        return GateMachine(world)

    run_state_machine_as_test(
        factory,
        settings=settings(
            database=None,
            deadline=None,
            # PR runs are reproducible; the nightly explores fresh examples and
            # print_blob reports a reproducer for any failure.
            derandomize="APM_GATE_MAX_EXAMPLES" not in os.environ,
            max_examples=int(os.environ.get("APM_GATE_MAX_EXAMPLES", "4")),
            phases=(Phase.generate, Phase.shrink),
            print_blob=True,
            stateful_step_count=8,
            suppress_health_check=(HealthCheck.too_slow,),
        ),
    )


def test_gate_machine_kills_the_recorded_approval_mutant(tmp_path: Path) -> None:
    """Skipping the approval check in the gate owner must fail the machine."""
    mutant = (sys.executable, str(MUTANT_PATH))
    machine = GateMachine(GateWorld.create(tmp_path / "world", mutant, "project"))
    with pytest.raises(AssertionError, match=r"hook deployed=True, expected False"):
        replay_gate_spine(machine)


@pytest.mark.parametrize(
    ("verdict", "surface"), [("deny", "lsp"), ("revoke", "lsp"), ("deny", "plugin")]
)
def test_narrowing_one_surface_keeps_another_approved_surface(
    tmp_path: Path, lifecycle_entry_point: tuple[str, ...], verdict: str, surface: str
) -> None:
    """Regression: narrowing one surface must not flag an approved hook under I4."""
    machine = GateMachine(GateWorld.create(tmp_path / "world", lifecycle_entry_point, "project"))
    machine.declare("hook")
    machine.install("plain")
    machine.decide("hook", "approve")
    machine.decide(surface, verdict)
    machine.install("plain")
    assert "hook" in machine.world.observe().deployed


def test_a_known_cell_does_not_excuse_an_unrelated_surface(tmp_path: Path) -> None:
    """A known cell owns only matching violations; another surface still fails."""
    state = LifecycleStateSnapshot.capture(tmp_path)
    artifacts = ArtifactSnapshotSet.capture({"project": tmp_path})
    before = Observation(state, artifacts, frozenset(), tmp_path, None)
    after = Observation(state, artifacts, frozenset({"hook", "lsp"}), tmp_path, None)

    class _Model:
        def __init__(self) -> None:
            self.seen: set[str] = set()

        def allowed_surfaces(self) -> frozenset[str]:
            return frozenset()

        def converged_before(self) -> bool:
            return False

        def expected_cells(self, op, before, after, result):
            return {"I4": (ExpectedCell("gate-cell-x", r"^hook still deployed"),)}

        def record_expected(self, cell_id: str) -> None:
            self.seen.add(cell_id)

    model = _Model()
    with pytest.raises(AssertionError, match=r"lsp still deployed but not allowed"):
        check_cross_cutting(before, after, None, model, Operation("install", mutating=True))
    assert model.seen == {"gate-cell-x"}


def test_home_allowlist_admits_only_exact_ancestors(tmp_path: Path) -> None:
    """A project step may create ~/.local/state/gh, but not ~/.local/bin."""
    home = tmp_path / "home"
    home.mkdir()
    state = LifecycleStateSnapshot.capture(tmp_path)
    before_artifacts = ArtifactSnapshotSet.capture({"home": home})
    (home / ".local" / "state" / "gh").mkdir(parents=True)
    (home / ".local" / "state" / "gh" / "device-id").write_text("id\n", encoding="ascii")
    (home / ".local" / "bin").mkdir()
    (home / ".local" / "bin" / "evil").write_text("x\n", encoding="ascii")
    after_artifacts = ArtifactSnapshotSet.capture({"home": home})
    before = Observation(state, before_artifacts, frozenset(), tmp_path, None)
    after = Observation(state, after_artifacts, frozenset(), tmp_path, None)

    class _Model:
        def allowed_surfaces(self) -> frozenset[str]:
            return frozenset()

        def converged_before(self) -> bool:
            return False

        def expected_cells(self, op, before, after, result):
            return {}

        def record_expected(self, cell_id: str) -> None:
            raise AssertionError(cell_id)

    op = Operation(
        "install",
        mutating=True,
        forbidden={"home": ("*",)},
        allowed={"home": (".local/state/gh",)},
    )
    with pytest.raises(AssertionError, match=r"\['\.local/bin', '\.local/bin/evil'\]"):
        check_cross_cutting(before, after, None, _Model(), op)
