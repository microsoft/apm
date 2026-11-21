"""Local-path graph edits stay in agreement across commands (#3571, #3649).

Local package ``a`` depends on local ``b`` and remote ``c``; ``c`` depends on
remote ``e``.  Editing ``a`` to drop ``c`` and add ``d`` (which also needs
``e``) must reconcile apm.lock.yaml, apm_modules and deployed files at once
(#3571), keep the shared ``e``, and leave every read command agreeing.  The
chain then exercises prune, a cold cache, a hand-deleted module, stray
directories, ``--frozen`` and uninstall reachability.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import (
    NO_POLICY,
    World,
    remote,
    set_deps,
    write,
    write_package,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


def _world(tmp_path: Path, apm_binary_path: Path) -> World:
    world = World.create(tmp_path, apm_binary_path)
    world.publish("e")
    world.publish("c", [remote("e")])
    world.publish("d", [remote("e")])
    write_package(world.project / "packages" / "b", "b")
    write_package(world.project / "packages" / "a", "a", ["../b", remote("c")])
    world.manifest(["./packages/a"])
    write(world.project / ".github/instructions/mine.instructions.md", "user owned\n")
    return world


def test_local_parent_edit_reconciles_graph_and_keeps_shared_dep(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path)

    world.apm("install", NO_POLICY)
    assert set(world.locked()) == {"_local/a", "_local/b", "test/c", "test/e"}
    assert world.deployed() == {
        "a.instructions.md",
        "b.instructions.md",
        "c.instructions.md",
        "e.instructions.md",
        "mine.instructions.md",
    }
    world.cross_check(present={"test/c", "test/e"})

    # Step: idempotent re-run is a no-op for the lockfile.
    lock_before = world.lock_bytes()
    world.apm("install", NO_POLICY)
    assert world.lock_bytes() == lock_before

    # Step: edit the local parent -- drop c, add d (d shares e).
    set_deps(world.project / "packages" / "a", ["../b", remote("d")])
    world.apm("install", NO_POLICY)
    locked = world.locked()
    assert set(locked) == {"_local/a", "_local/b", "test/d", "test/e"}
    assert locked["test/e"]["resolved_by"] == "test/d"
    assert world.deployed() == {
        "a.instructions.md",
        "b.instructions.md",
        "d.instructions.md",
        "e.instructions.md",
        "mine.instructions.md",
    }
    # The dropped checkout is an orphan only prune removes; every reader agrees.
    assert "test/c" in world.modules()
    listing = world.apm("deps", "list").stdout
    assert "test/c" in listing and "orphaned" in listing
    dry = world.apm("prune", "--dry-run").stdout
    assert "test/c (would be removed)" in dry
    assert "test/c" in world.modules()
    world.apm("prune")
    assert "test/c" not in world.modules()
    assert {"_local/a", "test/d", "test/e"} <= world.modules()
    world.cross_check(present={"test/d", "test/e"}, absent=frozenset({"test/c"}))

    # Step: cold cache + --frozen replays the same lock bytes.
    lock_before = world.lock_bytes()
    world.clean_cache()
    world.apm("install", NO_POLICY, "--frozen")
    assert world.lock_bytes() == lock_before

    # Step: hand-deleted module is restored by --frozen without lock churn.
    shutil.rmtree(world.project / "apm_modules" / "test" / "e")
    world.apm("install", NO_POLICY, "--frozen")
    assert "test/e" in world.modules()
    assert world.lock_bytes() == lock_before

    # Step: stray non-package directories are never pruned as packages,
    # and user-owned deployed files survive.
    write(world.project / "apm_modules" / "stray" / "notes.txt", "user\n")
    world.apm("prune")
    assert (world.project / "apm_modules" / "stray" / "notes.txt").exists()
    assert (world.project / ".github/instructions/mine.instructions.md").read_text(
        encoding="ascii"
    ) == "user owned\n"

    # Step: uninstall the local root removes exactly its reachable set.
    world.apm("uninstall", "./packages/a")
    assert world.deployed() == {"mine.instructions.md"}
    assert not ({"test/d", "test/e"} & world.modules())
    lock_path = world.project / "apm.lock.yaml"
    assert not lock_path.exists() or not world.locked()


def test_local_source_edited_between_install_and_frozen_fails_closed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path)
    world.apm("install", NO_POLICY)
    lock_before = world.lock_bytes()
    deployed = world.project / ".github/instructions/a.instructions.md"
    deployed_before = deployed.read_bytes()

    source = world.project / "packages/a/.apm/instructions/a.instructions.md"
    source.write_text(source.read_text(encoding="ascii") + "edited\n", encoding="ascii")

    for _ in range(2):
        result = world.apm("install", NO_POLICY, "--frozen", expect=1)
        assert "content differs from its locked snapshot" in result.stdout + result.stderr
        assert world.lock_bytes() == lock_before
        assert deployed.read_bytes() == deployed_before
