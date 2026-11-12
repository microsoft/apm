"""Targeted install and uninstall keep a shared dependency graph reachable (#3649).

``apm install PKG`` reconciles only what the named package no longer
declares (#3528, #3635): unrelated lock entries are byte-identical and a
transitive still needed elsewhere is kept.  ``apm uninstall`` then removes
exactly the reachable set of the removed root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import (
    NO_POLICY,
    World,
    remote,
    set_deps,
    write_package,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]


def test_targeted_install_drops_only_named_package_closure(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = World.create(tmp_path, apm_binary_path)
    world.publish("e")
    world.publish("c", [remote("e")])
    world.publish("d", [remote("e")])
    write_package(world.project / "packages" / "a", "a", [remote("c")])
    write_package(world.project / "packages" / "x", "x", [remote("d")])
    world.manifest(["./packages/a", "./packages/x"])

    world.apm("install", NO_POLICY)
    before = world.locked()
    assert set(before) == {"_local/a", "_local/x", "test/c", "test/d", "test/e"}

    set_deps(world.project / "packages" / "a", [])
    world.apm("install", NO_POLICY, "./packages/a")

    after = world.locked()
    assert set(after) == {"_local/a", "_local/x", "test/d", "test/e"}
    for key in ("_local/x", "test/d"):
        assert after[key] == before[key], key
    assert after["test/e"]["resolved_by"] == "test/d"
    assert world.deployed() == {
        "a.instructions.md",
        "d.instructions.md",
        "e.instructions.md",
        "x.instructions.md",
    }
    assert "test/c" not in world.modules()
    world.cross_check(present={"test/d", "test/e"}, absent=frozenset({"test/c"}))

    lock_before = world.lock_bytes()
    world.apm("install", NO_POLICY, "--frozen")
    assert world.lock_bytes() == lock_before

    world.apm("uninstall", "./packages/x")
    assert world.deployed() == {"a.instructions.md"}
    assert set(world.locked()) == {"_local/a"}
    assert not ({"test/d", "test/e"} & world.modules())


def test_targeted_install_keeps_dep_still_needed_by_untouched_entry(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """An edited but unselected sibling must not make a needed dep removable.

    ``x`` was edited to drop ``d`` but not reinstalled, so its lock entry and
    ``d`` stay untouched (the #3528 promise).  ``d`` still needs ``e``; the
    targeted ``apm install ./packages/a`` must not remove ``e`` and leave
    ``d`` locked without its dependency.
    """
    world = World.create(tmp_path, apm_binary_path)
    world.publish("e")
    world.publish("c", [remote("e")])
    world.publish("d", [remote("e")])
    write_package(world.project / "packages" / "a", "a", [remote("c")])
    write_package(world.project / "packages" / "x", "x", [remote("d")])
    world.manifest(["./packages/a", "./packages/x"])
    world.apm("install", NO_POLICY)
    before = world.locked()

    set_deps(world.project / "packages" / "a", [])
    set_deps(world.project / "packages" / "x", [])
    world.apm("install", NO_POLICY, "./packages/a")

    after = world.locked()
    for key in ("_local/x", "test/d"):
        assert after[key] == before[key], key
    assert "test/e" in after
    assert {"test/d", "test/e"} <= world.modules()
    assert {"d.instructions.md", "e.instructions.md"} <= world.deployed()

    world.apm("install", NO_POLICY)
    assert set(world.locked()) == {"_local/a", "_local/x"}
    assert world.deployed() == {"a.instructions.md", "x.instructions.md"}
    world.apm("prune")
    assert not ({"test/c", "test/d", "test/e"} & world.modules())
    world.cross_check(present=set(), absent=frozenset({"test/c", "test/d", "test/e"}))
