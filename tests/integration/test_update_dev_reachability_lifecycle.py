"""Update re-pins children and devDependency reachability survives uninstall (#3649).

``apm update`` fetches a branch-tracking parent whose new commit re-pins one
child, drops another and adds a third (#3571); lock, apm_modules and
deployed files follow in one run and ``--frozen`` replays from a cold cache.

Uninstalling the only production parent of a dependency that a
devDependency also needs keeps that dependency installed and resolved by the
devDependency.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import NO_POLICY, World, remote, set_deps

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


def test_update_follows_parent_repin_drop_and_add(tmp_path: Path, apm_binary_path: Path) -> None:
    world = World.create(tmp_path, apm_binary_path)
    rules = world.publish("e") / ".apm/instructions/e.instructions.md"
    for version in ("v1", "v2"):
        rules.write_text(rules.read_text(encoding="ascii") + f"{version}\n", encoding="ascii")
        commit = world.factory.commit(world.repos["e"], message=version)
        world.factory.tag(world.repos["e"], version, commit)
    world.publish("f")
    world.publish("g")
    c = world.publish("c", [f"{remote('e')}#v1", remote("f")])
    world.manifest([remote("c")])

    world.apm("install", NO_POLICY)
    assert set(world.locked()) == {"test/c", "test/e", "test/f"}
    assert world.locked()["test/e"]["resolved_ref"] == "v1"

    set_deps(c, [f"{remote('e')}#v2", remote("g")])
    world.republish("c")
    world.apm("update", "--yes")

    locked = world.locked()
    assert set(locked) == {"test/c", "test/e", "test/g"}
    assert locked["test/e"]["resolved_ref"] == "v2"
    assert world.deployed() == {"c.instructions.md", "e.instructions.md", "g.instructions.md"}
    deployed_e = (world.project / ".github/instructions/e.instructions.md").read_text("ascii")
    assert deployed_e.endswith("v2\n")
    world.apm("prune")
    world.cross_check(present={"test/c", "test/e", "test/g"}, absent=frozenset({"test/f"}))

    lock_before = world.lock_bytes()
    world.apm("install", NO_POLICY)
    assert world.lock_bytes() == lock_before
    shutil.rmtree(world.project / "apm_modules")
    world.clean_cache()
    world.apm("install", NO_POLICY, "--frozen")
    assert world.lock_bytes() == lock_before


def _dev_world(tmp_path: Path, apm_binary_path: Path) -> World:
    world = World.create(tmp_path, apm_binary_path)
    world.publish("e")
    world.publish("c", [remote("e")])
    world.publish("dev", [remote("e")])
    world.manifest([remote("c")])
    manifest = world.project / "apm.yml"
    manifest.write_text(
        manifest.read_text(encoding="ascii") + f"devDependencies:\n  apm:\n  - {remote('dev')}\n",
        encoding="ascii",
    )
    world.apm("install", NO_POLICY)
    locked = world.locked()
    assert locked["test/dev"]["is_dev"] is True
    assert not locked["test/e"].get("is_dev")
    return world


def test_uninstalling_prod_parent_keeps_dev_reachable_dep(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _dev_world(tmp_path, apm_binary_path)
    world.apm("uninstall", "test/c")
    assert set(world.locked()) == {"test/dev", "test/e"}
    assert world.locked()["test/e"]["resolved_by"] == "test/dev"
    assert world.deployed() == {"dev.instructions.md", "e.instructions.md"}
    world.cross_check(present={"test/dev", "test/e"}, absent=frozenset({"test/c"}))
