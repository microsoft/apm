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

from tests.lifecycle.machines._gate_bin import BIN_PATH, GateBin, bin_cells, replay_bin
from tests.lifecycle.machines._gate_bundle import (
    GateBundles,
    GatePack,
    bundle_cells,
    pack_cells,
    replay_bundles,
    replay_pack,
)
from tests.lifecycle.machines._gate_kit import (
    COMPLETED_SPINES,
    KNOWN_CELLS,
    SCOPES,
    GateMachine,
    GateWorld,
)
from tests.lifecycle.machines._gate_matrix import GateMatrix, matrix_cells, replay_matrix
from tests.lifecycle.machines._kit import Bench, WorldPool, earned_cells
from tests.lifecycle.oracles import ExpectedCell, Observation, Operation, check_cross_cutting
from tests.lifecycle.receipts import current_node
from tests.lifecycle.snapshot import restore_tree
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I4", "P-INV-I7"),
]

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
    # Refusals witness I7 for approve-deny, uninstall and update (#4133).
    ("decide_nothing", "approve"),
    ("decide_nothing", "deny"),
    ("uninstall", "plugin"),
    ("update_package", "plugin"),
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
    # Approving the plugin now takes effect (#4042); revoke it so update and
    # deny still meet the plugin while its approval is pending.
    ("decide", "plugin", "revoke"),
    ("install", "plain"),
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
    ("uninstall", "plugin"),
    ("update_package", "plugin"),
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


# Scopes the GATE matrix spine runs at (user scope runs its own, shorter spine).
MATRIX_SCOPES = SCOPES


@pytest.mark.parametrize("scope", MATRIX_SCOPES)
def test_gate_matrix_credits_op_prim_gate_cells(
    lifecycle_template, tmp_path: Path, scope: str
) -> None:
    """Every command under every gate level keeps I1-I9 and earns its OP x PRIM x GATE ids."""
    machine = GateMatrix(Bench(WorldPool(lifecycle_template, tmp_path).fresh(), scope), scope)
    replay_matrix(machine)
    assert machine.expected_seen == set(), sorted(machine.expected_seen)
    earned = earned_cells(current_node(), "G1|") | earned_cells(current_node(), "rule|")
    missing = matrix_cells(scope) - earned
    assert not missing, f"GATE matrix ids without an oracle receipt: {sorted(missing)}"
    bench = machine.bench
    if scope == "user":
        # The user spine ends with every package declared and recorded in the user lock.
        declared = (bench.manifest().get("dependencies") or {}).get("apm") or []
        assert bench.lock_path.exists() and len(declared) == len(machine.state.declared)
        return
    # The project spine ends by uninstalling every package under the dependency deny.
    surfaces = (".agents/skills", ".github/hooks", ".github/extensions")
    leftovers = [
        p
        for p in surfaces
        if (bench.deploy_root / p).exists() and any((bench.deploy_root / p).iterdir())
    ]
    assert not leftovers, f"uninstall left deployed surfaces: {leftovers}"


def test_gate_bundle_spine_credits_bundle_install_cells(lifecycle_template, tmp_path: Path) -> None:
    """A local bundle at every gate level keeps I1-I9 and earns its OP x PRIM x GATE ids."""
    machine = GateBundles(Bench(WorldPool(lifecycle_template, tmp_path).fresh(), "project"))
    replay_bundles(machine)
    missing = bundle_cells() - earned_cells(current_node(), "G1|bundle|")
    assert not missing, f"bundle ids without an oracle receipt: {sorted(missing)}"
    # With the gate off at the end, every executable of both bundles is deployed.
    hooks = machine.bench.deploy_root / ".github" / "hooks"
    assert any(path.name.startswith("p-bmix") for path in hooks.iterdir())


def test_gate_pack_spine_credits_pack_cells(lifecycle_template, tmp_path: Path) -> None:
    """``apm pack`` ships exactly the dependency executables each gate level deploys."""
    machine = GatePack(Bench(WorldPool(lifecycle_template, tmp_path).fresh(), "project"))
    replay_pack(machine)
    missing = pack_cells() - earned_cells(current_node(), "G1|pack|")
    assert not missing, f"pack ids without an oracle receipt: {sorted(missing)}"
    # The gate is off at the end: the hook stays deployed in the project.
    assert (machine.bench.deploy_root / ".github/hooks/p-exec-session.json").exists()


def test_gate_bin_spine_credits_user_scope_bin_cells(lifecycle_template, tmp_path: Path) -> None:
    """bin/ of a Claude marketplace plugin at every user-scope gate level keeps I1-I9."""
    machine = GateBin(Bench(WorldPool(lifecycle_template, tmp_path).fresh(), "user"))
    replay_bin(machine)
    missing = bin_cells() - earned_cells(current_node(), "G1|")
    assert not missing, f"bin ids without an oracle receipt: {sorted(missing)}"
    # The spine ends with the plugin uninstalled: no bin/ is left in HOME.
    assert not (machine.bench.deploy_root / BIN_PATH).exists()


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


@pytest.mark.parametrize("scope", SCOPES)
def test_verdict_on_an_uninstalled_package_ignores_the_operation_lock(
    tmp_path: Path, lifecycle_entry_point: tuple[str, ...], scope: str
) -> None:
    """Nightly falsifier (#4546): the operation lock file is bookkeeping, not a write."""
    machine = GateMachine(GateWorld.create(tmp_path / "world", lifecycle_entry_point, scope))
    manifest = machine.world.workspace / "apm.yml"
    manifest_before = manifest.read_bytes()
    machine.decide("hook", "approve")
    assert manifest.read_bytes() == manifest_before
    assert not (machine.world.workspace / "apm.lock.yaml").exists()


def test_uninstalling_an_undeclared_package_on_a_converged_state_is_a_refusal(
    tmp_path: Path, lifecycle_entry_point: tuple[str, ...]
) -> None:
    """Nightly falsifier (#4546): I2 does not demand exit 0 from a contracted refusal."""
    machine = GateMachine(GateWorld.create(tmp_path / "world", lifecycle_entry_point, "project"))
    machine.prune()
    machine.install("plain")
    machine.update()
    manifest = machine.world.workspace / "apm.yml"
    lock = machine.world.workspace / "apm.lock.yaml"
    before = (manifest.read_bytes(), lock.exists())
    machine.uninstall("lsp")
    assert (manifest.read_bytes(), lock.exists()) == before


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


def _native_after(
    tmp_path: Path, managed: set[str], *, user_scope: bool = True, recorded_name: str = "tool"
) -> Observation:
    """A user deploy root with one native plugin root recorded by its lock (#4225)."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.native_plugins.record import NativePluginRecord
    from apm_cli.utils.content_hash import compute_native_plugin_hash

    root = tmp_path / ".agents/plugins/tool"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name":"tool"}', encoding="ascii")
    (root / "bin").mkdir()
    (root / "bin/tool").write_text("#!/bin/sh\n", encoding="ascii")
    dependency = LockedDependency(repo_url="fixture/tool")
    dependency.native_plugin = NativePluginRecord(
        recorded_name,
        compute_native_plugin_hash(root),
        "apm-fixture",
        str(tmp_path / ".agents/plugins"),
    )
    lock = LockFile(dependencies={"fixture/tool": dependency})
    state = LifecycleStateSnapshot.capture(tmp_path)
    artifacts = ArtifactSnapshotSet.capture({"home": tmp_path})
    return Observation(
        state, artifacts, frozenset(), tmp_path, lock, frozenset(managed), user_scope=user_scope
    )


def _expect_i1(after: Observation, messages: list[str]) -> None:
    from tests.lifecycle.oracles import _i1_lock_deployed

    found = [v.message for v in _i1_lock_deployed(after, after, None, None, None) or []]
    assert found == messages


def test_i1_counts_paths_under_a_verified_native_plugin_root_as_recorded(tmp_path: Path) -> None:
    after = _native_after(tmp_path, {".agents/plugins/tool/bin/tool"})
    _expect_i1(after, [])
    project = _native_after(tmp_path / "p", {".agents/plugins/tool/bin/tool"}, user_scope=False)
    _expect_i1(project, ["deployed file is not in the lock: .agents/plugins/tool/bin/tool"])


def test_i1_fails_a_native_plugin_root_that_differs_from_its_record(tmp_path: Path) -> None:
    after = _native_after(tmp_path, {".agents/plugins/tool/bin/tool"})
    (tmp_path / ".agents/plugins/tool/bin/tool").write_text("changed\n", encoding="ascii")
    _expect_i1(
        after,
        [
            "native plugin root does not match the lock: .agents/plugins/tool",
            "deployed file is not in the lock: .agents/plugins/tool/bin/tool",
        ],
    )


def test_i1_fails_a_path_outside_every_native_plugin_root(tmp_path: Path) -> None:
    after = _native_after(tmp_path, {".agents/plugins/tool/bin/tool", ".agents/plugins/other/x"})
    (tmp_path / ".agents/plugins/other").mkdir()
    (tmp_path / ".agents/plugins/other/x").write_text("x\n", encoding="ascii")
    _expect_i1(after, ["deployed file is not in the lock: .agents/plugins/other/x"])


def test_i1_fails_a_verified_tree_the_lock_has_no_native_plugin_record_for(tmp_path: Path) -> None:
    # Same bytes and fingerprint, but the lock's only record names another root.
    after = _native_after(tmp_path, {".agents/plugins/tool/bin/tool"}, recorded_name="other")
    _expect_i1(after, ["deployed file is not in the lock: .agents/plugins/tool/bin/tool"])
