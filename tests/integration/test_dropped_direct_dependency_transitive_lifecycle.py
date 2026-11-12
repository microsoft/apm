"""Dropping a direct dependency that is still a transitive keeps it installed (#3649).

``d`` depends on ``e`` and the project also declares ``e`` directly.  When
the direct ``e`` line is hand-removed from apm.yml, ``e`` is still reachable
through ``d``.  ``apm install`` re-records ``e`` as a depth-2 transitive
resolved by ``d``, ``--frozen`` replays the same lock, and uninstalling ``d``
removes both packages and their deployed files.

``apm prune`` run before that install agrees (#3668): ``e`` is still
needed by ``d``, so prune keeps it while a genuinely unneeded stale direct
entry is still pruned; the next install re-records ``e`` as transitive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import NO_POLICY, World, remote

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_E_FILE = "e.instructions.md"


def _world(tmp_path: Path, apm_binary_path: Path) -> World:
    world = World.create(tmp_path, apm_binary_path)
    world.publish("e")
    world.publish("d", [remote("e")])
    world.manifest([remote("d"), remote("e")])
    world.apm("install", NO_POLICY)
    assert world.locked()["test/e"].get("depth") in (None, 1)
    world.manifest([remote("d")])
    return world


def test_install_after_drop_keeps_shared_transitive(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _world(tmp_path, apm_binary_path)
    world.apm("install", NO_POLICY)
    locked = world.locked()
    assert locked["test/e"]["depth"] == 2
    assert locked["test/e"]["resolved_by"] == "test/d"
    world.cross_check(present={"test/d", "test/e"})
    lock_before = world.lock_bytes()
    world.apm("install", NO_POLICY, "--frozen")
    assert world.lock_bytes() == lock_before
    world.apm("uninstall", "test/d")
    assert not world.modules()
    assert world.deployed() == set()


def test_prune_after_drop_keeps_still_reachable_transitive(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = World.create(tmp_path, apm_binary_path)
    world.publish("e")
    world.publish("x")
    world.publish("d", [remote("e")])
    world.manifest([remote("d"), remote("e"), remote("x")])
    world.apm("install", NO_POLICY)
    world.manifest([remote("d")])

    dry = world.apm("prune", "--dry-run").stdout
    assert "test/x (would be removed)" in dry, dry
    assert "test/e" not in dry, dry
    assert "test/e" in world.apm("deps", "list").stdout

    world.apm("prune")
    assert world.modules() == {"test/d", "test/e"}
    assert world.deployed() == {"d.instructions.md", _E_FILE}
    assert set(world.locked()) == {"test/d", "test/e"}
    assert "No orphaned packages found" in world.apm("prune", "--dry-run").stdout
    # Prune only removes packages; the lock still records e as direct until
    # install re-records it, which audit reports with that remedy.
    audit = world.apm("audit", "--ci", NO_POLICY, expect=1).stdout
    assert "run 'apm install'" in " ".join(audit.split())

    world.apm("install", NO_POLICY)
    assert world.locked()["test/e"]["resolved_by"] == "test/d"
    world.cross_check(present={"test/d", "test/e"}, absent=frozenset({"test/x"}))
