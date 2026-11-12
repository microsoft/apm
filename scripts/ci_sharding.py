"""Duration-weighted, xdist-group-aware CI sharding (pytest plugin).

Every CI lane that splits one pytest selection across runners loads this
plugin. It keeps each ``xdist_group`` together (``--dist loadgroup`` needs
the whole group on one runner) and each test module together (so files run
next to their usual neighbours), splits only modules too heavy for one
shard, and balances the resulting units by measured duration with
pytest-split's LeastDuration algorithm.

Durations come from a JSON file ``{node_id: seconds}``. The nightly release
run publishes a fresh one as an artifact, and a committed seed keeps the lanes
balanced when no artifact is available. Node IDs missing from the file are
weighted with the mean known duration. Without any file every test weighs 1.

Three modes:

* ``--shard-plan-out PATH`` (with ``--collect-only``) estimates the work of the
  selection and writes ``{"shard_count": N, "shards": [1..N], ...}``. N is the
  smallest count whose estimated per-shard wall stays within
  ``--shard-target-seconds`` on ``--shard-workers`` xdist workers, capped by
  ``--shard-max``.
* ``--shard-count N --shard-index I`` runs shard I of N. Every shard computes
  the same deterministic partition from the same collection and durations.
* ``--shard-store-durations PATH`` records the measured setup + call +
  teardown seconds of every test that ran (on the xdist controller) so the
  nightly run can publish fresh durations.
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from pytest_split.algorithms import LeastDurationAlgorithm

DEFAULT_TARGET_SECONDS = 480.0
DEFAULT_WORKERS = 4
DEFAULT_MAX_SHARDS = 8


@dataclass(frozen=True)
class Partition:
    """Selected items per shard and the estimated serial seconds of each."""

    shards: tuple[tuple[pytest.Item, ...], ...]
    seconds: tuple[float, ...]


def load_durations(path: str | Path | None) -> dict[str, float]:
    """Read ``{node_id: seconds}``; a missing or unreadable file means no data."""
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(node): float(seconds)
        for node, seconds in raw.items()
        if isinstance(seconds, (int, float)) and seconds >= 0
    }


def _group_key(item: pytest.Item) -> str | None:
    names = {
        str(mark.args[0] if mark.args else mark.kwargs.get("name", "default"))
        for mark in item.iter_markers("xdist_group")
    }
    # Match the composite labels used by xdist's loadgroup scheduler.
    return "_".join(sorted(names)) if names else None


def affinity_units(
    items: Sequence[pytest.Item],
    weights: Mapping[str, float] | None = None,
    split_over: float | None = None,
) -> list[list[pytest.Item]]:
    """Keep each xdist group, and each module, together; fail on duplicates.

    Modules stay whole so a shard runs files in their usual order next to
    their usual neighbours. A module heavier than ``split_over`` is split
    into single tests so one large file cannot become the long pole.
    """
    node_ids = {item.nodeid for item in items}
    if len(node_ids) != len(items):
        raise pytest.UsageError("Duplicate node IDs; remove duplicate collection inputs.")
    units: dict[tuple[str, str], list[pytest.Item]] = defaultdict(list)
    for item in items:
        group = _group_key(item)
        key = ("group", group) if group else ("module", item.nodeid.split("::", 1)[0])
        units[key].append(item)
    result: list[list[pytest.Item]] = []
    for (kind, _name), unit in units.items():
        too_heavy = (
            kind == "module"
            and weights is not None
            and split_over is not None
            and sum(weights[item.nodeid] for item in unit) > split_over
        )
        result.extend([[item] for item in unit] if too_heavy else [unit])
    return result


def item_weights(items: Sequence[pytest.Item], durations: Mapping[str, float]) -> dict[str, float]:
    """Weight every item by its measured duration.

    A key without ``::`` is a module total (see compact_durations): it is
    shared equally by that module's selected tests that have no own entry.
    Anything still unknown weighs the mean known duration (1.0 with no data).
    """
    weights: dict[str, float] = {
        item.nodeid: durations[item.nodeid] for item in items if item.nodeid in durations
    }
    residual: dict[str, list[pytest.Item]] = defaultdict(list)
    for item in items:
        module = item.nodeid.split("::", 1)[0]
        if item.nodeid not in weights and module in durations:
            residual[module].append(item)
    for module, members in residual.items():
        for item in members:
            weights[item.nodeid] = durations[module] / len(members)
    default = sum(weights.values()) / len(weights) if weights else 1.0
    return {item.nodeid: weights.get(item.nodeid, default) for item in items}


def compact_durations(durations: Mapping[str, float], min_seconds: float = 0.5) -> dict[str, float]:
    """Keep tests slower than min_seconds; fold the rest into module totals."""
    compact: dict[str, float] = defaultdict(float)
    for node, seconds in durations.items():
        if "::" not in node or seconds >= min_seconds:
            compact[node] += seconds
        else:
            compact[node.split("::", 1)[0]] += seconds
    return {node: round(value, 2) for node, value in sorted(compact.items())}


def partition(
    items: Sequence[pytest.Item],
    shard_count: int,
    durations: Mapping[str, float] | None = None,
) -> Partition:
    """Split items into shard_count nonempty shards without breaking groups."""
    if shard_count < 1:
        raise pytest.UsageError("Shard count must be at least 1.")
    weights = item_weights(items, durations or {})
    # A module bigger than half of one shard's share is split per test.
    split_over = sum(weights.values()) / (shard_count * 2)
    units = affinity_units(items, weights, split_over)
    representatives = [unit[0] for unit in units]
    members = {unit[0].nodeid: unit for unit in units}
    unit_weights = {
        node: sum(weights[item.nodeid] for item in unit) for node, unit in members.items()
    }
    groups = LeastDurationAlgorithm()(shard_count, representatives, unit_weights)
    selected = [
        {item.nodeid for rep in group.selected for item in members[rep.nodeid]} for group in groups
    ]
    union: set[str] = set()
    for chosen in selected:
        if not chosen or union & chosen:
            raise pytest.UsageError(
                "Shards must be nonempty and partition every selected node exactly once; "
                "check collection inputs, xdist groups and the shard count."
            )
        union |= chosen
    if len(selected) != shard_count or union != {item.nodeid for item in items}:
        raise pytest.UsageError("Shards must partition every selected node exactly once.")
    return Partition(
        shards=tuple(tuple(item for item in items if item.nodeid in chosen) for chosen in selected),
        seconds=tuple(sum(weights[node] for node in chosen) for chosen in selected),
    )


def estimated_wall(seconds: float, largest_unit: float, workers: int) -> float:
    """A shard finishes no sooner than its work over its workers or its longest serial unit."""
    return max(seconds / max(workers, 1), largest_unit)


def plan_shard_count(
    items: Sequence[pytest.Item],
    durations: Mapping[str, float],
    *,
    target_seconds: float,
    workers: int,
    max_shards: int,
) -> dict[str, object]:
    """Pick the smallest shard count whose estimated per-shard wall meets the target."""
    if not items:
        raise pytest.UsageError("Cannot plan shards for an empty selection.")
    weights = item_weights(items, durations)
    units = affinity_units(items, weights, sum(weights.values()) / (max_shards * 2))
    # Only an xdist group (or one test) runs serially on one worker; a module's
    # tests spread across the shard's workers.
    serial: dict[str, float] = defaultdict(float)
    for item in items:
        group = _group_key(item)
        serial[f"group:{group}" if group else item.nodeid] += weights[item.nodeid]
    largest = max(serial.values())
    total = sum(weights.values())
    upper = max(1, min(max_shards, len(units)))
    count = min(upper, max(1, math.ceil(total / (target_seconds * max(workers, 1)))))
    chosen = partition(items, count, durations)
    while (
        count < upper
        and max(estimated_wall(seconds, largest, workers) for seconds in chosen.seconds)
        > target_seconds
    ):
        count += 1
        chosen = partition(items, count, durations)
    known = sum(1 for item in items if item.nodeid in durations)
    return {
        "shard_count": count,
        "shards": list(range(1, count + 1)),
        "selected": len(items),
        "known_durations": known,
        "total_seconds": round(total, 1),
        "largest_unit_seconds": round(largest, 1),
        "shard_seconds": [round(seconds, 1) for seconds in chosen.seconds],
    }


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ci-sharding")
    group.addoption("--shard-count", type=int, default=None, help="Total shards.")
    group.addoption("--shard-index", type=int, default=None, help="1-based shard to run.")
    group.addoption("--shard-durations", default=None, help="JSON {node_id: seconds}.")
    group.addoption("--shard-plan-out", default=None, help="Write a shard plan JSON here.")
    group.addoption(
        "--shard-target-seconds",
        type=float,
        default=DEFAULT_TARGET_SECONDS,
        help="Target estimated pytest wall per shard.",
    )
    group.addoption("--shard-workers", type=int, default=DEFAULT_WORKERS)
    group.addoption("--shard-max", type=int, default=DEFAULT_MAX_SHARDS)
    group.addoption(
        "--shard-store-durations",
        default=None,
        help="Write measured {node_id: seconds} for every test that ran.",
    )


class DurationRecorder:
    """Sum setup, call and teardown seconds per node on the reporting process."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.seconds: dict[str, float] = defaultdict(float)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.duration >= 0:
            self.seconds[report.nodeid] += report.duration

    def pytest_sessionfinish(self) -> None:
        rounded = {node: round(value, 3) for node, value in sorted(self.seconds.items())}
        self.path.write_text(json.dumps(rounded, indent=0, sort_keys=True) + "\n", encoding="utf-8")


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("shard_store_durations", default=None)
    # xdist workers forward their reports to the controller, which records them.
    if path and not hasattr(config, "workerinput"):
        config.pluginmanager.register(DurationRecorder(path), "ci-sharding-durations")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Plan or select one shard after pytest's marker selection."""
    plan_out = config.getoption("shard_plan_out")
    count = config.getoption("shard_count")
    index = config.getoption("shard_index")
    if plan_out is None and count is None and index is None:
        return
    if config.getoption("splits", default=None) or config.getoption("group", default=None):
        raise pytest.UsageError("Do not combine --shard-* with --splits/--group.")
    durations = load_durations(config.getoption("shard_durations"))
    if plan_out is not None:
        plan = plan_shard_count(
            items,
            durations,
            target_seconds=config.getoption("shard_target_seconds"),
            workers=config.getoption("shard_workers"),
            max_shards=config.getoption("shard_max"),
        )
        Path(plan_out).write_text(json.dumps(plan, sort_keys=True) + "\n", encoding="utf-8")
        return
    if count is None or index is None or not 1 <= index <= count:
        raise pytest.UsageError("Pass --shard-count N and --shard-index between 1 and N.")
    chosen = partition(items, count, durations)
    keep = {item.nodeid for item in chosen.shards[index - 1]}
    deselected = [item for item in items if item.nodeid not in keep]
    items[:] = list(chosen.shards[index - 1])
    config.hook.pytest_deselected(items=deselected)
    seconds = ", ".join(f"{value:.0f}" for value in chosen.seconds)
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            f"[ci-sharding] shard {index}/{count}: {len(items)} tests; "
            f"estimated serial seconds per shard: {seconds}"
        )


def main(argv: Sequence[str]) -> int:
    """``python -m scripts.ci_sharding compact OUT IN...``: merge, then compact."""
    if len(argv) < 3 or argv[0] != "compact":
        print("usage: python -m scripts.ci_sharding compact OUT IN...", file=sys.stderr)
        return 2
    merged: dict[str, float] = {}
    for path in argv[2:]:
        merged.update(load_durations(path))
    compact = compact_durations(merged)
    Path(argv[1]).write_text(json.dumps(compact, indent=0) + "\n", encoding="utf-8")
    print(f"[i] {len(merged)} test durations -> {len(compact)} entries in {argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
