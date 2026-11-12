"""Affinity-preserving delegation to pytest-split for required lifecycle shards."""

from __future__ import annotations

from collections import Counter
from unittest.mock import MagicMock

import pytest
from pytest_split.algorithms import TestGroup as SplitGroup

from scripts import lifecycle_smoke_sharding as sharding

pytestmark = pytest.mark.unit


def _item(nodeid: str, *marks: pytest.MarkDecorator) -> pytest.Item:
    item = MagicMock(spec=pytest.Item)
    item.nodeid = nodeid
    item.iter_markers.return_value = [mark.mark for mark in marks]
    item.__str__.return_value = nodeid
    return item


def test_partition_retains_every_node_once_and_whole_affinity_groups() -> None:
    grouped = [
        _item(f"tests/test_home.py::test_{index}", pytest.mark.xdist_group(name="home_env"))
        for index in range(17)
    ]
    singles = [_item(f"tests/test_other.py::test_{index:03}") for index in range(394)]
    items = grouped + singles

    first, second = sharding.partition_items(items)

    assert sorted([len(first), len(second)]) == [205, 206]
    assert Counter(item.nodeid for item in first + second) == Counter(item.nodeid for item in items)
    grouped_ids = {item.nodeid for item in grouped}
    assert grouped_ids <= {item.nodeid for item in first} or grouped_ids <= {
        item.nodeid for item in second
    }
    for selected in (first, second):
        selected_ids = {item.nodeid for item in selected}
        assert selected == [item for item in items if item.nodeid in selected_ids]


def test_composite_group_names_match_xdist_labels() -> None:
    first = _item("a", pytest.mark.xdist_group("home"), pytest.mark.xdist_group(name="env"))
    second = _item("b", pytest.mark.xdist_group("env_home"))
    third = _item("c", pytest.mark.xdist_group("home"), pytest.mark.xdist_group("env"))
    partitions = sharding.partition_items([first, second, third, _item("d"), _item("e")])

    assert any({item.nodeid for item in group} >= {"a", "b", "c"} for group in partitions)


def test_partition_delegates_balancing_to_pytest_split(monkeypatch: pytest.MonkeyPatch) -> None:
    grouped = [_item(name, pytest.mark.xdist_group("shared")) for name in ("a", "b")]
    single = _item("c")
    algorithm = MagicMock(
        return_value=[
            SplitGroup([grouped[0]], [single], 2.0),
            SplitGroup([single], [grouped[0]], 1.0),
        ]
    )
    monkeypatch.setattr(sharding, "LeastDurationAlgorithm", lambda: algorithm)

    assert sharding.partition_items([*grouped, single]) == (grouped, [single])
    algorithm.assert_called_once_with(2, [grouped[0], single], {"a": 2.0, "c": 1.0})


@pytest.mark.parametrize("nodes", [[], ["a"], ["a", "a"]])
def test_invalid_collection_fails_closed(nodes: list[str]) -> None:
    with pytest.raises(pytest.UsageError, match=r"Duplicate|nonempty"):
        sharding.partition_items([_item(node) for node in nodes])


def test_single_affinity_group_cannot_silently_leave_an_empty_shard() -> None:
    with pytest.raises(pytest.UsageError, match="nonempty"):
        sharding.partition_items([_item(node, pytest.mark.xdist_group("one")) for node in "ab"])


@pytest.mark.parametrize(
    "selection",
    [
        (("a", "b"), ("b", "c")),
        (("a",), ("b",)),
        (("a",), ()),
        ((), ("b", "c")),
    ],
)
def test_missing_or_duplicated_algorithm_output_fails_closed(
    selection: tuple[tuple[str, ...], tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    items = {node: _item(node) for node in "abc"}
    algorithm = MagicMock(
        return_value=[SplitGroup([items[node] for node in group], [], 1.0) for group in selection]
    )
    monkeypatch.setattr(sharding, "LeastDurationAlgorithm", lambda: algorithm)

    with pytest.raises(pytest.UsageError, match="partition every selected node"):
        sharding.partition_items(list(items.values()))


@pytest.mark.parametrize("count", [0, 1, 3])
def test_wrong_algorithm_partition_count_fails_closed(
    count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [_item("a"), _item("b")]
    algorithm = MagicMock(return_value=[SplitGroup(items, [], 1.0)] * count)
    monkeypatch.setattr(sharding, "LeastDurationAlgorithm", lambda: algorithm)

    with pytest.raises(pytest.UsageError, match="partition every selected node"):
        sharding.partition_items(items)


@pytest.mark.parametrize("shard", [1, 2])
def test_hook_preserves_selected_items_and_reports_only_complement(shard: int) -> None:
    items = [_item(node) for node in "abcdef"]
    partitions = sharding.partition_items(items)
    config = MagicMock(spec=pytest.Config, hook=MagicMock(spec=["pytest_deselected"]))
    config.getoption.side_effect = lambda name, **kwargs: (
        shard if name == "lifecycle_shard" else None
    )

    sharding.pytest_collection_modifyitems(config, items)

    assert items == partitions[shard - 1]
    config.hook.pytest_deselected.assert_called_once_with(items=partitions[2 - shard])


def test_hook_is_inert_without_explicit_shard() -> None:
    items = [_item("only")]
    config = MagicMock(spec=pytest.Config, hook=MagicMock(spec=["pytest_deselected"]))
    config.getoption.return_value = None

    sharding.pytest_collection_modifyitems(config, items)

    assert [item.nodeid for item in items] == ["only"]
    config.hook.pytest_deselected.assert_not_called()


@pytest.mark.parametrize("other_option", ["splits", "group"])
def test_combining_shard_selectors_is_rejected(other_option: str) -> None:
    config = MagicMock(spec=pytest.Config)
    config.getoption.side_effect = lambda name, **kwargs: (
        1 if name in {"lifecycle_shard", other_option} else None
    )

    with pytest.raises(pytest.UsageError, match="one shard selector"):
        sharding.pytest_collection_modifyitems(config, [_item("a"), _item("b")])
