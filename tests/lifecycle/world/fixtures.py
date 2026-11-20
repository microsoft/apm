"""Pytest fixtures of the lifecycle world (#4094).

``lifecycle_template`` builds one template per session, which under xdist is
one per worker; ``lifecycle_world`` clones it per test and clears the egress
log, so every test starts from the same bytes and an empty log.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.lifecycle.world.builder import build_template
from tests.lifecycle.world.clone import clone
from tests.lifecycle.world.contract import World


@pytest.fixture(scope="session")
def lifecycle_template(tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    template = build_template(tmp_path_factory.mktemp("lifecycle-template") / "world")
    try:
        yield template
    finally:
        template.services.close()


@pytest.fixture
def lifecycle_world(lifecycle_template: World, tmp_path: Path) -> World:
    world = clone(lifecycle_template, tmp_path / "world")
    world.services.reset()
    return world
