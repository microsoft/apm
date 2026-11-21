"""A marketplace listing reads the user registry and never writes it (#4600).

PROMISE: P-INV-I5 -- each command writes only its declared write set: ``apm
marketplace list`` reads ``~/.apm/marketplaces.json`` and creates nothing
under ``~/.apm`` (source: src/apm_cli/marketplace/registry.py ``_load``).
CELL: OP=marketplace SCOPE=project LOCK=consistent (``T4|marketplace|project|consistent``).
OWNER: src/apm_cli/marketplace/registry.py -- reads treat a missing registry as
empty; only ``_save`` (a registration change) creates the file.

Controls: ``apm marketplace add`` still creates the registry and ``list`` then
shows the entry; ``list`` leaves an existing registry byte-identical. Every
step runs I1-I9 through ``check_cross_cutting``.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

import pytest

from tests.lifecycle import driver
from tests.lifecycle.oracles import (
    Evidence,
    Observation,
    check_cross_cutting,
    check_obligation,
    operation_for,
)
from tests.lifecycle.rows import Row, RowReplay
from tests.lifecycle.world.services import MARKETPLACE_NAME
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I5", "P-INV-I6", "P-INV-I7", "P-INV-I8", "P-INV-I9"),
]

CELL = "T4|marketplace|project|consistent"
REGISTRY = ".apm/marketplaces.json"
# HOME paths any command may touch: logs, receipts and caches, never state.
_HOME_BOOKKEEPING = (".apm/logs", ".apm/deployment-receipts", ".cache/apm", ".local/state/gh")
# ``marketplace add`` registers the source and caches its catalog.
_ADD_WRITES = (REGISTRY, ".apm/marketplaces.lock", ".apm/cache")
_NO_MARKETPLACES = r"No marketplaces registered\. Run 'apm marketplace add <source>'"


@dataclass(frozen=True)
class _Unchanged:
    """OracleModel of a step that deploys nothing: the seeded surfaces stay allowed."""

    surfaces: frozenset[str]

    def allowed_surfaces(self) -> frozenset[str]:
        return self.surfaces

    def converged_before(self) -> bool:
        return False

    def expected_cells(self, op, before, after, result) -> dict:
        del op, before, after, result
        return {}

    def record_expected(self, cell_id: str) -> None:
        raise AssertionError(f"no known cell expected here: {cell_id}")


def _seeded(world) -> RowReplay:
    """A project with p-skill installed: the lockfile is consistent."""
    row = Row(
        id="T4/marketplace/project/consistent",
        promises=("P-INV-I5",),
        packages=("p-skill",),
        targets=("copilot",),
        scope="project",
        given=("apm install p-skill",),
        when=(),
    )
    replay = RowReplay(world, row)
    replay.replay()
    return replay


def _step(
    replay: RowReplay, argv: tuple[str, ...], *, writes: Collection[str] = ()
) -> tuple[Observation, Observation, CommandResult]:
    """Run one marketplace command; HOME changes are limited to *writes*."""
    op = operation_for(
        argv,
        read_only=not writes,
        forbidden={"home": ("*",), "project": ("*",)},
        allowed={"home": (*_HOME_BOOKKEEPING, *writes)},
    )
    before = replay.observe()
    result = driver.run(replay.world, argv)
    after = replay.observe()
    assert result.returncode == 0, f"{argv} -> {result.returncode}\n{result.stdout}{result.stderr}"
    check_cross_cutting(before, after, result, _Unchanged(before.deployed), op)
    return before, after, result


def test_marketplace_list_never_creates_or_rewrites_the_registry(lifecycle_world) -> None:
    replay = _seeded(lifecycle_world)
    registry = lifecycle_world.home / REGISTRY
    assert not registry.exists(), "the template world already registers a marketplace"

    # Decisive: a fresh HOME lists nothing and creates nothing under ~/.apm.
    before, after, result = _step(replay, ("marketplace", "list"))
    check_obligation(
        CELL,
        strength="e2e-evidence",
        before=before,
        after=after,
        result=result,
        evidence=Evidence(
            absent={"home": (REGISTRY,)},
            unchanged={"home": (".apm",), "project": ("*",)},
            exit=0,
            output_matches=(_NO_MARKETPLACES,),
            ascii_output=True,
        ),
    )

    # Control: the first registration creates the registry.
    _step(
        replay, ("marketplace", "add", lifecycle_world.services.marketplace_url), writes=_ADD_WRITES
    )
    assert registry.is_file(), "marketplace add did not create the registry"
    registered = registry.read_bytes()

    # Control: listing an existing registry shows the entry and keeps its bytes.
    before, after, result = _step(replay, ("marketplace", "list"))
    check_obligation(
        CELL,
        strength="e2e-evidence",
        before=before,
        after=after,
        result=result,
        evidence=Evidence(
            present={"home": (REGISTRY,)},
            unchanged={"home": (".apm",), "project": ("*",)},
            exit=0,
            output_matches=(rf"\b{MARKETPLACE_NAME}\b",),
            output_forbids=(r"No marketplaces registered",),
            ascii_output=True,
        ),
    )
    assert registry.read_bytes() == registered, "marketplace list rewrote the registry"
