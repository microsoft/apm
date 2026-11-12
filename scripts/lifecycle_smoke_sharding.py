"""Adapt pytest-split to preserve xdist affinity in the two lifecycle shards."""

from __future__ import annotations

from collections import defaultdict

import pytest
from pytest_split.algorithms import LeastDurationAlgorithm

SHARD_COUNT = 2


def partition_items(items: list[pytest.Item]) -> tuple[list[pytest.Item], list[pytest.Item]]:
    """Balance member counts with pytest-split without splitting loadgroup units."""
    node_ids = {item.nodeid for item in items}
    if len(node_ids) != len(items):
        raise pytest.UsageError("Duplicate lifecycle node IDs; remove duplicate collection inputs.")

    units: dict[tuple[str, str], list[pytest.Item]] = defaultdict(list)
    for item in items:
        names = {
            str(mark.args[0] if mark.args else mark.kwargs.get("name", "default"))
            for mark in item.iter_markers("xdist_group")
        }
        # Match the composite labels used by xdist's loadgroup worker.
        key = ("group", "_".join(sorted(names))) if names else ("node", item.nodeid)
        units[key].append(item)

    members = {unit[0].nodeid: unit for unit in units.values()}
    representatives = [unit[0] for unit in units.values()]
    weights = {node_id: float(len(unit)) for node_id, unit in members.items()}
    groups = LeastDurationAlgorithm()(SHARD_COUNT, representatives, weights)
    selected = [
        {
            item.nodeid
            for representative in group.selected
            for item in members[representative.nodeid]
        }
        for group in groups
    ]
    if (
        len(selected) != SHARD_COUNT
        or not all(selected)
        or selected[0] & selected[1]
        or selected[0] | selected[1] != node_ids
    ):
        raise pytest.UsageError(
            "Lifecycle shards must be nonempty and partition every selected node exactly once; "
            "check collection inputs and xdist groups."
        )
    return (
        [item for item in items if item.nodeid in selected[0]],
        [item for item in items if item.nodeid in selected[1]],
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    """Expose only the fixed two-way lifecycle selection."""
    parser.addoption(
        "--lifecycle-shard",
        type=int,
        choices=(1, 2),
        default=None,
        help="Run lifecycle shard 1 or 2 while retaining complete xdist groups.",
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Apply affinity-aware sharding after pytest's marker selection."""
    shard = config.getoption("lifecycle_shard")
    if shard is None:
        return
    if config.getoption("splits", default=None) or config.getoption("group", default=None):
        raise pytest.UsageError(
            "Do not combine --lifecycle-shard with --splits/--group; use one shard selector."
        )
    partitions = partition_items(items)
    items[:] = partitions[shard - 1]
    config.hook.pytest_deselected(items=partitions[SHARD_COUNT - shard])
