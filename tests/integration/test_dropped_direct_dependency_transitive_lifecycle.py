"""Dropping a direct dependency that is still a transitive keeps it installed (#3649).

``d`` depends on ``e`` and the project also declares ``e`` directly.  When
the direct ``e`` line is hand-removed from apm.yml, ``e`` is still reachable
through ``d``.  ``apm install`` re-records ``e`` as a depth-2 transitive
resolved by ``d``, ``--frozen`` replays the same lock, and uninstalling ``d``
removes both packages and their deployed files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import NO_POLICY, World, remote

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


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
