"""Two-way lifecycle shards built on the shared CI sharding plugin."""

from __future__ import annotations

import pytest

from scripts import ci_sharding

SHARD_COUNT = 2


def partition_items(
    items: list[pytest.Item],
    durations: dict[str, float] | None = None,
) -> tuple[list[pytest.Item], list[pytest.Item]]:
    """Balance whole xdist affinity units across the two lifecycle shards."""
    if len(items) < SHARD_COUNT:
        raise pytest.UsageError(
            "Lifecycle shards must be nonempty and partition every selected node exactly once; "
            "check collection inputs and xdist groups."
        )
    try:
        shards = ci_sharding.partition(items, SHARD_COUNT, durations).shards
    except pytest.UsageError as exc:
        raise pytest.UsageError(
            f"Lifecycle shards must be nonempty and partition every selected node exactly once; "
            f"check collection inputs and xdist groups. ({exc})"
        ) from exc
    return list(shards[0]), list(shards[1])


def pytest_addoption(parser: pytest.Parser) -> None:
    """Expose only the fixed two-way lifecycle selection."""
    parser.addoption(
        "--lifecycle-shard",
        type=int,
        choices=(1, 2),
        default=None,
        help="Run lifecycle shard 1 or 2 while retaining complete xdist groups.",
    )
    parser.addoption(
        "--lifecycle-durations",
        default=None,
        help="Optional JSON {node_id: seconds} used to balance the two shards.",
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
    durations = ci_sharding.load_durations(config.getoption("lifecycle_durations"))
    partitions = partition_items(items, durations)
    items[:] = partitions[shard - 1]
    config.hook.pytest_deselected(items=partitions[SHARD_COUNT - shard])
