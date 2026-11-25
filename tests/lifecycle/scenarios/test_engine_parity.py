"""Install engine parity: journeys the fast engines claim, cleanup and W4's change table.

PROMISE: P-INV-I2 -- repeating a converged install changes no bytes, whichever
engine answers. PROMISE: P-INV-I1 -- the lockfile matches the deployed files.

LANDING GATE for lane C S0 (no-op) and S2 (diff-apply); oracle and cell contract in
tests/lifecycle/scenarios/_engine_parity.py.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from tests.lifecycle.scenarios._deploy_diff import CHANGES, SKILL_PATH, digest, set_inputs
from tests.lifecycle.scenarios._engine_parity import (
    BOTH,
    CHANGE_EXIT,
    CLONE_DOUBTS,
    DIFF,
    FULL,
    NEEDS_ANY,
    NEEDS_DIFF,
    NEEDS_NOOP,
    NOOP,
    TRUST_CHANGES,
    Twin,
    _assert_decision,
    _checkout,
    _converge,
    _converge_exec_user_approved,
    _declare,
    _deployed,
    _publish_skill,
    _remove_deployed,
    _snapshot,
    _user_and_orphan_files,
    make_twin,
)
from tests.lifecycle.world import World
from tests.lifecycle.world.services import ORG

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I1", "P-INV-I2", "P-INV-I4"),
]


@pytest.fixture
def twin(lifecycle_template: World, _world_dir: Path) -> Twin:
    # The short world root (Windows: git's '$GIT_DIR too big' workaround), both clones.
    return make_twin(lifecycle_template, _world_dir.parent)


@NEEDS_NOOP
def test_reinstall_of_a_converged_project_is_a_noop(twin: Twin) -> None:
    _converge(twin, *BOTH)
    _assert_decision(twin.ok("install"), NOOP)
    _assert_decision(twin.ok("install"), NOOP)


@NEEDS_NOOP
def test_frozen_reinstall_of_a_converged_project_is_a_noop(twin: Twin) -> None:
    _converge(twin, *BOTH)
    locks = {w.root: (w.project / "apm.lock.yaml").read_bytes() for w in twin.worlds}
    decision = twin.ok("install", "--frozen")
    for world in twin.worlds:
        assert (world.project / "apm.lock.yaml").read_bytes() == locks[world.root], "frozen rewrote"
    _assert_decision(decision, NOOP)


@NEEDS_ANY
def test_install_after_uninstall_matches_the_full_pipeline(twin: Twin) -> None:
    _converge(twin, *BOTH)
    twin.ok("uninstall", f"{ORG}/p-skill")
    _assert_decision(twin.ok("install"), NOOP, FULL)


@NEEDS_DIFF
def test_warm_locked_install_applies_the_diff(twin: Twin) -> None:
    """Lockfile and store present, deployed tree gone: a fresh checkout on a warm machine."""
    _converge(twin, *BOTH)
    twin.each(_remove_deployed)
    _assert_decision(twin.ok("install"), DIFF)


@pytest.mark.parametrize("clone", sorted(CLONE_DOUBTS))
@NEEDS_ANY
def test_a_clone_without_bound_receipts_takes_the_full_pipeline(twin: Twin, clone: str) -> None:
    """No bound receipts: the whole project is the diff, and the full pipeline is cheaper.

    A copied identity keeps the full run's own refusal of unbound receipts, in both worlds.
    """
    _converge(twin, *BOTH)
    twin.each(CLONE_DOUBTS[clone])
    _assert_decision(twin.run("install"), FULL)
    assert twin.codes[-1] == (0 if clone == "clone-with-lock" else 1), twin.evidence


@NEEDS_DIFF
def test_frozen_ci_on_a_warm_store_applies_the_diff(twin: Twin) -> None:
    _converge(twin, *BOTH)
    twin.each(_remove_deployed)
    _assert_decision(twin.ok("install", "--frozen"), DIFF)


@NEEDS_ANY
def test_branch_switch_lockfile_swap_matches_the_full_pipeline(twin: Twin) -> None:
    """Install v1, move to v2, then check out v1's manifest and lockfile again.

    The full pipeline keeps v2's bytes as a local edit of the swapped-in lock
    ("1 local edit(s) to managed files"), so a diff decision is valid only if
    it lands the same bytes; declining to FULL is the parity-safe answer.
    """
    _converge(twin, "p-skill:git")
    v1 = {w.root: _snapshot(w) for w in twin.worlds}
    _publish_skill(twin, "Version two.", tag="v2.0.0")

    def to_v2(world: World) -> None:
        manifest = world.project / "apm.yml"
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(text.replace("p-skill#v1.0.0", "p-skill#v2.0.0"), encoding="utf-8")

    twin.each(to_v2)
    twin.ok("install")
    twin.each(lambda w: _checkout(w, v1[w.root]))
    _assert_decision(twin.ok("install"), DIFF, FULL)


@NEEDS_ANY
def test_lockfile_that_drops_a_deployed_package_declines_to_the_full_pipeline(
    twin: Twin,
) -> None:
    """Branch switch to a lockfile without p-exec, whose files are still deployed (orphans).

    The fast world must end byte-identical to the full pipeline (the same files
    deleted or kept), with a user file outside the ledger untouched.
    """
    _converge(twin, "p-skill:git")
    without_exec = {w.root: _snapshot(w) for w in twin.worlds}
    _converge(twin, *BOTH)

    def switch(world: World) -> None:
        (world.project / "notes.md").write_bytes(b"the user's own file\n")
        _checkout(world, without_exec[world.root])

    twin.each(switch)
    decision = twin.ok("install")
    for world in twin.worlds:
        assert (world.project / "notes.md").read_bytes() == b"the user's own file\n"
    _assert_decision(decision, FULL)


def test_update_matches_the_full_pipeline(twin: Twin) -> None:
    """No engine claims ``update``: parity guard, green at main, must stay green."""
    _converge(twin, "p-skill:git")
    _publish_skill(twin, "Moved tag.", tag="v1.0.0")
    _assert_decision(twin.ok("update", "--yes") or "full run (not install)", FULL)


@NEEDS_NOOP
def test_noop_hit_changes_no_project_file(twin: Twin) -> None:
    """A hit skips lockfile, cleanup and finalize: not one project byte moves."""
    _converge(twin, *BOTH)
    twin.each(_user_and_orphan_files)
    before = digest(twin.fast.project, (".git",), twin.fast)
    decision = twin.ok("install")
    _assert_decision(decision, NOOP)
    assert digest(twin.fast.project, (".git",), twin.fast) == before, "a no-op hit wrote"


@NEEDS_ANY
def test_cleanup_on_a_doubt_prunes_like_the_full_pipeline(twin: Twin) -> None:
    """Dropping p-exec from apm.yml is a doubt: FULL prunes it; user and unclaimed files stay."""
    _converge(twin, *BOTH)
    extra = {w.root: _user_and_orphan_files(w) for w in twin.worlds}
    twin.each(lambda w: _declare(w, "p-skill:git"))
    decision = twin.ok("install")
    for world in twin.worlds:
        for rel, data in extra[world.root].items():
            assert (world.project / rel).read_bytes() == data, f"cleanup touched {rel}"
        assert _deployed(world, "p-skill"), "a still-claimed file was pruned"
    _assert_decision(decision, FULL)


def _can_symlink() -> bool:
    """Windows creates directory symlinks only with the privilege or developer mode."""
    with tempfile.TemporaryDirectory() as scratch:
        try:
            os.symlink(scratch, Path(scratch) / "probe", target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
    return True


_NO_SYMLINKS = pytest.mark.skipif(
    not _can_symlink(), reason="this host cannot create directory symlinks"
)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            c, marks=(*NEEDS_ANY.marks, *((_NO_SYMLINKS,) if c == "symlinked-destination" else ()))
        )
        for c in sorted(CHANGES)
    ],
)
def test_install_after_a_change_matches_the_full_pipeline(twin: Twin, change: str) -> None:
    """Same history in both worlds, so nothing is exempt: an edited or symlinked path is compared."""
    spec = CHANGES[change]
    twin.each(lambda w: set_inputs(w, spec.start))
    twin.ok("install")  # every setup step is itself a parity step
    if spec.start.approved:
        twin.ok("approve", "--user", "p-exec")
        twin.ok("install")
    for world in twin.worlds:
        assert (world.project / SKILL_PATH).is_file(), "the kit skill must deploy"
    twin.each(spec.apply)
    outside = {w.root: digest(w.root / "outside-skill", (), w) for w in twin.worlds}
    decision = twin.run("install")
    code = CHANGE_EXIT.get(change, 0)
    assert twin.codes[-1] == code, (
        f"{change}: exit {twin.codes[-1]}, expected {code}{twin.evidence}"
    )
    for world in twin.worlds:
        after = digest(world.root / "outside-skill", (), world)
        assert after == outside[world.root], "install wrote through a symlinked destination"
    _assert_decision(decision, DIFF, FULL)


@pytest.mark.parametrize(
    "trust", [pytest.param(t, marks=NEEDS_NOOP.marks) for t in sorted(TRUST_CHANGES)]
)
def test_trust_change_after_a_noop_hit_declines(twin: Twin, trust: str) -> None:
    """Manifest and lockfile bytes unchanged: only the trust input moved, so FULL decides."""
    _converge_exec_user_approved(twin)
    _assert_decision(twin.ok("install"), NOOP)
    pinned = {w.root: _snapshot(w) for w in twin.worlds}
    TRUST_CHANGES[trust](twin)
    for world in twin.worlds:
        assert _snapshot(world) == pinned[world.root], (
            "the trust change touched apm.yml or the lock"
        )
    _assert_decision(twin.ok("install"), FULL)


@NEEDS_NOOP
def test_one_missing_deployed_file_after_a_noop_hit_is_restored(twin: Twin) -> None:
    """Git-only: prove a no-op hit, then delete ONE hashed deployed file (manifest, lock
    and apm_modules kept). The engine must not call that a no-op: it restores the bytes
    (diff) or declines (full), byte-identical to the full pipeline either way.
    """
    _converge(twin, *BOTH)
    _assert_decision(twin.ok("install"), NOOP)
    missing = {w.root: _deployed(w)[0] for w in twin.worlds}
    original = {root: path.read_bytes() for root, path in missing.items()}
    for path in missing.values():
        path.unlink()
    decision = twin.ok("install")
    for root, path in missing.items():
        assert path.read_bytes() == original[root], f"{path.name} was not restored"
    _assert_decision(decision, DIFF, FULL)
