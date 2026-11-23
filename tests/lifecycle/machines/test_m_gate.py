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

from tests.lifecycle.machines._gate_bin import BIN_PATH, KNOWN_DD, GateBin, bin_cells, replay_bin
from tests.lifecycle.machines._gate_bundle import (
    KNOWN_PACK,
    PACK_SETS,
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
from tests.lifecycle.machines._gate_plugin_bin import ROOT as PLUGIN_ROOT
from tests.lifecycle.machines._gate_plugin_bin import (
    GatePluginBin,
    PluginBinBench,
    plugin_bin_cells,
    replay_plugin_bin,
)
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
    # A new approval under the org deny is refused, names the org layer (#4919), and
    # writes nothing (#4923).
    ("decide", "hook", "revoke"),
    ("decide", "hook", "approve"),
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
    pool, seen = WorldPool(lifecycle_template, tmp_path), set()
    for packs in PACK_SETS:
        machine = GatePack(Bench(pool.fresh(), "project"), packs)
        replay_pack(machine)
        seen |= machine.expected_seen
    missing = pack_cells() - earned_cells(current_node(), "G1|pack|") - set(KNOWN_PACK)
    assert not missing, f"pack ids without an oracle receipt: {sorted(missing)}"
    # Every strict known cell failed on its step.
    assert seen == set(KNOWN_PACK)
    # The gate is off at the end: the canvas stays deployed in the project.
    assert (machine.bench.deploy_root / ".github/extensions/p-canvas/extension.mjs").exists()


def test_gate_bin_spine_credits_user_scope_bin_cells(lifecycle_template, tmp_path: Path) -> None:
    """bin/ of a Claude marketplace plugin at every user-scope gate level keeps I1-I9."""
    machine = GateBin(Bench(WorldPool(lifecycle_template, tmp_path).fresh(), "user"))
    replay_bin(machine)
    missing = bin_cells() - set(KNOWN_DD) - earned_cells(current_node(), "G1|")
    assert not missing, f"bin ids without an oracle receipt: {sorted(missing)}"
    strict = {cell.label for cell in machine.known}
    assert machine.expected_seen == strict, sorted(strict - machine.expected_seen)
    # The spine ends with the plugin uninstalled: no bin/ is left in HOME.
    assert not (machine.bench.deploy_root / BIN_PATH).exists()


def test_gate_plugin_bin_spine_credits_project_scope_prune_cells(
    lifecycle_template, tmp_path: Path
) -> None:
    """A whole plugin's bin/ at project scope: prune retires the root at every gate level."""
    world = WorldPool(lifecycle_template, tmp_path).fresh()
    machine = GatePluginBin(PluginBinBench(world, "project"))
    replay_plugin_bin(machine)
    missing = plugin_bin_cells() - earned_cells(current_node(), "G1|")
    assert not missing, f"plugin bin ids without an oracle receipt: {sorted(missing)}"
    # The spine ends with the denied plugin retired: its root is gone.
    assert not (machine.bench.deploy_root / PLUGIN_ROOT).exists()


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


def test_approve_under_an_org_deny_refuses_and_names_the_org_layer(
    tmp_path: Path, lifecycle_entry_point: tuple[str, ...]
) -> None:
    """Nightly falsifier (#4919): deny wins, so approving an org-denied hook exits 1,
    names the org layer, and the hook stays undeployed."""
    machine = GateMachine(GateWorld.create(tmp_path / "world", lifecycle_entry_point, "project"))
    machine.set_policy("org-deny")
    machine.declare("hook")
    machine.update()
    machine.decide("hook", "approve")
    machine.install("plain")
    world = machine.world
    manifest = (world.workspace / "apm.yml").read_text(encoding="utf-8")
    # A repeat is not a new grant: decide() checks exit 1, the org layer and I7.
    machine.decide("hook", "approve")
    assert (world.workspace / "apm.yml").read_text(encoding="utf-8") == manifest
    assert not (world.deploy_root / ".github/hooks/hook-kit-session.json").is_file()


def test_approving_a_denied_hook_under_an_org_deny_writes_nothing(
    tmp_path: Path, lifecycle_entry_point: tuple[str, ...]
) -> None:
    """Review replay (#4923): approve under the org deny keeps the package's own deny
    and records no grant; the refusal comes before any write."""
    machine = GateMachine(GateWorld.create(tmp_path / "world", lifecycle_entry_point, "project"))
    machine.declare("hook")
    machine.install("plain")
    machine.decide("hook", "deny")
    machine.set_policy("org-deny")
    world = machine.world
    before = world.store()
    machine.decide("hook", "approve")
    assert world.store() == before
    assert not (world.deploy_root / ".github/hooks/hook-kit-session.json").is_file()


def test_frozen_refuses_a_declared_package_the_lock_lacks(
    tmp_path: Path, lifecycle_entry_point: tuple[str, ...]
) -> None:
    """Nightly falsifier: after a package is declared on a locked project, audit --ci
    and install --frozen both refuse (install.md "Frozen mode"); the lock is untouched."""
    machine = GateMachine(GateWorld.create(tmp_path / "world", lifecycle_entry_point, "project"))
    machine.declare("hook")
    machine.update()
    machine.declare("plugin")
    world = machine.world
    lock = (world.workspace / "apm.lock.yaml").read_text(encoding="utf-8")
    machine.audit_ci(json_output=False)
    assert (world.workspace / "apm.lock.yaml").read_text(encoding="utf-8") == lock
    assert str(world.packages["plugin"]) not in lock


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
    """A deploy root with one native plugin root recorded by its lock (#4225, #3921).

    The record carries the per-machine digest at user scope and the portable
    path-and-bytes digest at project scope.
    """
    from apm_cli.claude_plugins.admission import fingerprint_native_claude_source
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
        compute_native_plugin_hash(root)
        if user_scope
        else fingerprint_native_claude_source(root, portable=True),
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
    _expect_i1(project, [])


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


# P-OWN-executable-item-admission: an approval pins the definition of what it
# admits. A hook's definition is every file deployed with it (its script, the
# modules the script imports, the package.json files that set their module
# type), so a change to any of them parks the hook until it is approved again.
_BUNDLE_HOOK = {
    "hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": "node ./run.mjs"}]}]}
}


def _bundled_hook(root: Path) -> Path:
    """h-bundle: a hook whose script imports a helper under a package module type."""
    from tests.utils.local_package import LocalPackageFactory

    package = LocalPackageFactory(root).create("h-bundle", version="1.0.0")
    hooks = package.root / ".apm" / "hooks"
    (hooks / "lib").mkdir(parents=True)
    (hooks / "run.mjs").write_text("import './lib/helper.mjs';\n", encoding="ascii")
    (hooks / "lib" / "helper.mjs").write_text("export const ok = 1;\n", encoding="ascii")
    (hooks / "lib" / "package.json").write_text('{"type": "module"}\n', encoding="ascii")
    (package.root / "package.json").write_text('{"type": "module"}\n', encoding="ascii")
    (hooks / "run.json").write_text(json.dumps(_BUNDLE_HOOK), encoding="ascii")
    return package.root


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        (".apm/hooks/lib/helper.mjs", "export const ok = 2;\n"),
        (".apm/hooks/lib/package.json", '{"type": "commonjs"}\n'),
        ("package.json", '{"type": "commonjs"}\n'),
    ],
    ids=["imported-module", "nested-package-json", "package-json"],
)
def test_an_approved_hook_reparks_when_any_file_of_its_bundle_changes(
    lifecycle_template, tmp_path: Path, relative: str, content: str
) -> None:
    """Install parks the changed hook and retires it (I4); approving it again re-pins it."""
    from tests.lifecycle.machines._kit import StepRunner
    from tests.lifecycle.model.state import ModelState

    bench = Bench(WorldPool(lifecycle_template, tmp_path).fresh(), "project")
    source = _bundled_hook(bench.world.root / "hook-bundle")
    kinds = {"h-bundle": frozenset({"hook"})}
    unapproved = ModelState(declared=frozenset({"h-bundle"}))
    approved = ModelState(
        declared=frozenset({"h-bundle"}), decisions={("h-bundle", "hook"): "allowed"}
    )
    runner = StepRunner(bench, unapproved, kinds)
    hook = bench.deploy_root / ".github" / "hooks" / "h-bundle-run.json"

    runner.step(("install", str(source)), exits={0}, mutating=True)
    runner.step(("approve", str(source)), exits={0})
    runner.step(("install",), exits={0}, model=approved, mutating=True)
    assert hook.is_file(), "the approved hook did not deploy"

    runner.edit("change-" + relative, lambda: (source / relative).write_text(content, "ascii"))
    step = runner.step(("install",), exits={0}, model=unapproved, mutating=True)
    assert not hook.exists(), "a hook whose bundle changed stayed deployed"
    output = " ".join((step.result.stdout + step.result.stderr).split())
    assert "definition changed" in output, output

    runner.step(("approve", str(source)), exits={0})
    runner.step(("install",), exits={0}, model=approved, mutating=True)
    assert hook.is_file(), "re-approving the changed hook did not deploy it"
