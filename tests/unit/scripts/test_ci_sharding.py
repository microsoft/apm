"""Duration-weighted, affinity-preserving CI sharding."""

from __future__ import annotations

import itertools
import json
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import ci_sharding

pytestmark = pytest.mark.unit


def _item(nodeid: str, *marks: pytest.MarkDecorator) -> pytest.Item:
    item = MagicMock(spec=pytest.Item)
    item.nodeid = nodeid
    item.iter_markers.return_value = [mark.mark for mark in marks]
    item.__str__.return_value = nodeid
    return item


def _ids(items: object) -> list[str]:
    return [item.nodeid for item in items]  # type: ignore[attr-defined]


def test_partition_keeps_groups_whole_and_every_node_once() -> None:
    grouped = [_item(f"g{index}", pytest.mark.xdist_group("home")) for index in range(5)]
    singles = [_item(f"s{index:02}") for index in range(30)]
    items = singles[:10] + grouped + singles[10:]

    result = ci_sharding.partition(items, 4)

    assert Counter(_ids(node for shard in result.shards for node in shard)) == Counter(_ids(items))
    owners = [
        index for index, shard in enumerate(result.shards) if set(_ids(grouped)) & set(_ids(shard))
    ]
    assert len(owners) == 1
    assert set(_ids(grouped)) <= set(_ids(result.shards[owners[0]]))
    for shard in result.shards:
        assert list(shard) == [item for item in items if item in shard]


def test_modules_stay_whole_unless_too_heavy_for_one_shard() -> None:
    light = [_item(f"tests/test_light_{m}.py::t{i}") for m in range(6) for i in range(5)]
    heavy = [_item(f"tests/test_heavy.py::t{i}") for i in range(40)]

    result = ci_sharding.partition(light + heavy, 2)

    for module in range(6):
        prefix = f"tests/test_light_{module}.py::"
        owners = [
            shard for shard in result.shards if any(i.nodeid.startswith(prefix) for i in shard)
        ]
        assert len(owners) == 1, prefix
    # 40 heavy tests over an 8.75-test limit: five contiguous chunks of eight.
    runs = 0
    for shard in result.shards:
        indexes = [heavy.index(i) for i in shard if i.nodeid.startswith("tests/test_heavy.py")]
        runs += sum(1 for a, b in itertools.pairwise([-2, *indexes]) if b != a + 1)
    assert runs <= 5
    assert all(any(i.nodeid.startswith("tests/test_heavy.py") for i in s) for s in result.shards)
    sizes = sorted(len(shard) for shard in result.shards)
    assert sum(sizes) == 70 and sizes[1] - sizes[0] <= 8


def test_durations_balance_shards_within_target_ratio() -> None:
    items = [_item(f"t{index:03}") for index in range(200)]
    durations = {f"t{index:03}": float(1 + (index * 37) % 23) for index in range(200)}

    seconds = ci_sharding.partition(items, 6, durations).seconds

    assert max(seconds) / min(seconds) < 1.05


def test_unknown_nodes_weigh_the_mean_known_duration() -> None:
    items = [_item("known-a"), _item("known-b"), _item("new")]

    weights = ci_sharding.item_weights(items, {"known-a": 2.0, "known-b": 4.0})

    assert weights == {"known-a": 2.0, "known-b": 4.0, "new": 3.0}
    assert ci_sharding.item_weights(items, {}) == {"known-a": 1.0, "known-b": 1.0, "new": 1.0}


def test_long_group_is_weighted_by_its_members() -> None:
    grouped = [_item(f"g{index}", pytest.mark.xdist_group("slow")) for index in range(4)]
    singles = [_item(f"s{index}") for index in range(4)]
    durations = {
        **{f"g{index}": 10.0 for index in range(4)},
        **{f"s{index}": 10.0 for index in range(4)},
    }

    result = ci_sharding.partition(grouped + singles, 2, durations)

    assert sorted(result.seconds) == [40.0, 40.0]


@pytest.mark.parametrize("count", [0, -1])
def test_invalid_shard_count_fails_closed(count: int) -> None:
    with pytest.raises(pytest.UsageError, match="at least 1"):
        ci_sharding.partition([_item("a")], count)


def test_more_shards_than_units_fails_closed() -> None:
    items = [_item(node, pytest.mark.xdist_group("one")) for node in "abc"]
    with pytest.raises(pytest.UsageError, match="nonempty"):
        ci_sharding.partition(items, 2)


def test_duplicate_node_ids_fail_closed() -> None:
    with pytest.raises(pytest.UsageError, match="Duplicate"):
        ci_sharding.partition([_item("a"), _item("a")], 1)


def test_plan_picks_smallest_count_meeting_target() -> None:
    items = [_item(f"t{index:03}") for index in range(100)]
    durations = {f"t{index:03}": 24.0 for index in range(100)}

    plan = ci_sharding.plan_shard_count(
        items, durations, target_seconds=150.0, workers=4, max_shards=8
    )

    # 2400 s of work / (150 s x 4 workers) = 4 shards of 600 s, i.e. 150 s wall.
    assert plan["shard_count"] == 4
    assert plan["shards"] == [1, 2, 3, 4]
    assert plan["known_durations"] == 100


def test_plan_grows_past_a_long_unit_and_respects_the_cap() -> None:
    grouped = [_item(f"g{index}", pytest.mark.xdist_group("long")) for index in range(3)]
    singles = [_item(f"s{index:02}") for index in range(40)]
    durations = {
        **{f"g{index}": 100.0 for index in range(3)},
        **{f"s{index:02}": 30.0 for index in range(40)},
    }

    capped = ci_sharding.plan_shard_count(
        grouped + singles, durations, target_seconds=200.0, workers=4, max_shards=3
    )

    assert capped["shard_count"] == 3
    assert capped["largest_unit_seconds"] == 300.0


def test_load_durations_ignores_missing_and_malformed_files(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"a": 1, "b": "x", "c": -1, "d": 2.5}), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")

    assert ci_sharding.load_durations(good) == {"a": 1.0, "d": 2.5}
    assert ci_sharding.load_durations(bad) == {}
    assert ci_sharding.load_durations(tmp_path / "missing.json") == {}
    assert ci_sharding.load_durations(None) == {}


def _config(**options: object) -> MagicMock:
    config = MagicMock(
        spec=pytest.Config,
        hook=MagicMock(spec=["pytest_deselected"]),
        pluginmanager=MagicMock(spec=["get_plugin"]),
    )
    config.getoption.side_effect = lambda name, **kwargs: options.get(name, kwargs.get("default"))
    config.pluginmanager.get_plugin.return_value = None
    return config


@pytest.mark.parametrize("index", [1, 2, 3])
def test_hook_selects_one_shard_and_reports_the_complement(index: int) -> None:
    items = [_item(node) for node in "abcdefg"]
    expected = ci_sharding.partition(list(items), 3).shards[index - 1]
    config = _config(shard_count=3, shard_index=index)

    ci_sharding.pytest_collection_modifyitems(config, items)

    assert items == list(expected)
    (call,) = config.hook.pytest_deselected.call_args_list
    assert set(_ids(call.kwargs["items"])) == set("abcdefg") - set(_ids(expected))


def test_hook_writes_a_plan_without_deselecting(tmp_path: Path) -> None:
    items = [_item(node) for node in "abcd"]
    out = tmp_path / "plan.json"
    config = _config(
        shard_plan_out=str(out), shard_target_seconds=1.0, shard_workers=1, shard_max=8
    )

    ci_sharding.pytest_collection_modifyitems(config, items)

    assert json.loads(out.read_text(encoding="utf-8"))["shard_count"] == 4
    assert _ids(items) == list("abcd")
    config.hook.pytest_deselected.assert_not_called()


def test_hook_is_inert_without_shard_options() -> None:
    items = [_item("only")]
    config = _config()

    ci_sharding.pytest_collection_modifyitems(config, items)

    assert _ids(items) == ["only"]
    config.hook.pytest_deselected.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [
        {"shard_count": 2},
        {"shard_index": 1},
        {"shard_count": 2, "shard_index": 3},
        {"shard_count": 2, "shard_index": 0},
        {"shard_count": 2, "shard_index": 1, "splits": 2},
    ],
)
def test_hook_rejects_incomplete_or_conflicting_selectors(options: dict[str, int]) -> None:
    with pytest.raises(pytest.UsageError):
        ci_sharding.pytest_collection_modifyitems(_config(**options), [_item("a"), _item("b")])


def _report(nodeid: str, duration: float) -> MagicMock:
    return MagicMock(spec=pytest.TestReport, nodeid=nodeid, duration=duration)


def test_duration_recorder_sums_phases_and_writes_sorted_json(tmp_path: Path) -> None:
    out = tmp_path / "durations.json"
    recorder = ci_sharding.DurationRecorder(str(out))
    for nodeid, duration in (("b", 1.0), ("a", 0.25), ("b", 0.5), ("a", -1.0)):
        recorder.pytest_runtest_logreport(_report(nodeid, duration))

    recorder.pytest_sessionfinish()

    assert json.loads(out.read_text(encoding="utf-8")) == {"a": 0.25, "b": 1.5}
    assert ci_sharding.load_durations(out) == {"a": 0.25, "b": 1.5}


@pytest.mark.parametrize(("is_worker", "registered"), [(False, True), (True, False)])
def test_duration_recorder_registers_only_on_the_controller(
    is_worker: bool, registered: bool, tmp_path: Path
) -> None:
    config = _config(shard_store_durations=str(tmp_path / "d.json"))
    config.pluginmanager = MagicMock(spec=["register"])
    if is_worker:
        config.workerinput = {}
    else:
        del config.workerinput

    ci_sharding.pytest_configure(config)

    assert config.pluginmanager.register.called is registered


def test_module_totals_spread_over_tests_without_own_entries() -> None:
    items = [_item("tests/a.py::slow"), _item("tests/a.py::x"), _item("tests/a.py::y")]
    items.append(_item("tests/b.py::z"))

    weights = ci_sharding.item_weights(items, {"tests/a.py::slow": 5.0, "tests/a.py": 1.0})

    assert weights["tests/a.py::slow"] == 5.0
    assert weights["tests/a.py::x"] == weights["tests/a.py::y"] == 0.5
    assert weights["tests/b.py::z"] == pytest.approx(6.0 / 3)


def test_compaction_keeps_slow_tests_and_folds_the_rest(tmp_path: Path) -> None:
    durations = {"tests/a.py::slow": 2.0, "tests/a.py::x": 0.1, "tests/a.py::y": 0.2}
    assert ci_sharding.compact_durations(durations) == {"tests/a.py": 0.3, "tests/a.py::slow": 2.0}

    first, second, out = tmp_path / "1.json", tmp_path / "2.json", tmp_path / "out.json"
    first.write_text(json.dumps({"tests/a.py::slow": 2.0}), encoding="utf-8")
    second.write_text(json.dumps({"tests/b.py::q": 0.1}), encoding="utf-8")
    assert ci_sharding.main(["compact", str(out), str(first), str(second)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "tests/a.py::slow": 2.0,
        "tests/b.py": 0.1,
    }
    assert ci_sharding.main(["merge"]) == 2
