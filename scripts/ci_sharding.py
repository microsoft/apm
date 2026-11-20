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

Five modes:

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
* ``--shard-inventory-out PATH`` writes every selected node ID with its marker
  names, so the drift guard can evaluate each job's ``-m`` from one collect.
* ``python -m scripts.ci_sharding files`` (no pytest collection) writes the
  test modules of one shard to a pytest ``@argfile``. Each sharded CI job
  passes that file list instead of whole directories, so every shard imports
  only its own modules. Modules stay whole, modules naming one xdist group
  stay together, and the shards' file lists are disjoint and cover every
  test module under the targets that can hold a test selected by ``-m``.
  tests/quality/test_ci_topology.py compares the lists with a full collect.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import math
import re
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
# pytest's default python_files and norecursedirs, which this repo keeps.
TEST_MODULE_PATTERNS = ("test_*.py", "*_test.py")
_SKIPPED_DIRS = frozenset({"_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}"})
_GROUP_NAME = re.compile(r"""xdist_group\(\s*(?:name\s*=\s*)?['"]([^'"]+)['"]""")
_EXPRESSION_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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
    their usual neighbours. A module heavier than ``split_over`` is cut into
    the fewest contiguous chunks under that weight, so one large file cannot
    become the long pole.
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
        if kind != "module" or weights is None or not split_over:
            result.append(unit)
            continue
        result.extend(_contiguous_chunks(unit, weights, split_over))
    return result


def _contiguous_chunks(
    unit: list[pytest.Item], weights: Mapping[str, float], limit: float
) -> list[list[pytest.Item]]:
    """Split a module into as few in-order chunks as keep each under limit.

    Per-process warm-up (module fixtures, caches) is paid once per worker
    that runs a module, so a heavy module is cut into a few large chunks
    rather than scattered test by test.
    """
    total = sum(weights[item.nodeid] for item in unit)
    count = max(1, math.ceil(total / limit))
    if count == 1:
        return [unit]
    share = total / count
    chunks: list[list[pytest.Item]] = [[]]
    filled = 0.0
    for item in unit:
        if chunks[-1] and filled >= share and len(chunks) < count:
            chunks.append([])
            filled = 0.0
        chunks[-1].append(item)
        filled += weights[item.nodeid]
    return chunks


def _measured_seconds(item: pytest.Item, durations: Mapping[str, float]) -> float | None:
    """Look up an item's duration under either node ID form.

    Under ``--dist loadgroup`` xdist appends ``@<group>`` to grouped node IDs,
    so recorded durations carry the suffix while a plain collection (the
    planner) does not, and vice versa.
    """
    nodeid = item.nodeid
    candidates = [nodeid]
    group = _group_key(item)
    if group:
        suffix = f"@{group}"
        candidates.append(nodeid[: -len(suffix)] if nodeid.endswith(suffix) else nodeid + suffix)
    for candidate in candidates:
        if candidate in durations:
            return durations[candidate]
    return None


def item_weights(items: Sequence[pytest.Item], durations: Mapping[str, float]) -> dict[str, float]:
    """Weight every item by its measured duration.

    A key without ``::`` is a module total (see compact_durations): it is
    shared equally by that module's selected tests that have no own entry.
    Anything still unknown weighs the mean known duration (1.0 with no data).
    """
    weights: dict[str, float] = {}
    for item in items:
        measured = _measured_seconds(item, durations)
        if measured is not None:
            weights[item.nodeid] = measured
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
    # A module bigger than a quarter of one shard's share is cut into chunks.
    split_over = sum(weights.values()) / (shard_count * 4)
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
    units = affinity_units(items, weights, sum(weights.values()) / (max_shards * 4))
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


@dataclass(frozen=True)
class FilePartition:
    """Test modules per shard and the estimated serial seconds of each."""

    shards: tuple[tuple[str, ...], ...]
    seconds: tuple[float, ...]


def _skipped_dir(part: str) -> bool:
    return part.startswith(".") or part.endswith(".egg") or part in _SKIPPED_DIRS


def discover_test_modules(targets: Sequence[str], root: Path) -> list[str]:
    """Every test module pytest would collect under targets, as sorted relative paths."""
    found: set[str] = set()
    for target in targets:
        path = root / target
        if path.is_file():
            found.add(path.relative_to(root).as_posix())
            continue
        if not path.is_dir():
            raise pytest.UsageError(f"Shard target does not exist: {target}")
        for candidate in path.rglob("*.py"):
            relative = candidate.relative_to(root)
            if any(_skipped_dir(part) for part in relative.parts[:-1]):
                continue
            if any(fnmatch.fnmatchcase(candidate.name, glob) for glob in TEST_MODULE_PATTERNS):
                found.add(relative.as_posix())
    return sorted(found)


def required_markers(expression: str | None) -> tuple[str, ...]:
    """Markers every test selected by a ``-m`` expression must carry.

    Only a plain conjunction promises a marker; ``or``, parentheses or marker
    arguments return none, so the caller keeps every module.
    """
    if not expression or re.search(r"\bor\b|[()]", expression):
        return ()
    required: set[str] = set()
    negated = False
    for word in _EXPRESSION_WORD.findall(expression):
        if word == "not":
            negated = not negated
        elif word != "and":
            if not negated:
                required.add(word)
            negated = False
    return tuple(sorted(required))


def select_test_modules(
    targets: Sequence[str], expression: str | None, root: Path
) -> dict[str, str]:
    """Map each module that can hold a selected test to its source text.

    A module whose source never names a marker the expression requires
    cannot hold a selected test, so it is not imported at all.
    """
    patterns = [re.compile(rf"\b{re.escape(name)}\b") for name in required_markers(expression)]
    selected: dict[str, str] = {}
    for module in discover_test_modules(targets, root):
        text = (root / module).read_text(encoding="utf-8", errors="replace")
        if all(pattern.search(text) for pattern in patterns):
            selected[module] = text
    return selected


def module_units(sources: Mapping[str, str]) -> list[tuple[str, ...]]:
    """Keep modules that name the same xdist group on one shard."""
    parent = {module: module for module in sources}

    def root_of(module: str) -> str:
        while parent[module] != module:
            parent[module] = parent[parent[module]]
            module = parent[module]
        return module

    first_by_group: dict[str, str] = {}
    for module, text in sorted(sources.items()):
        for name in sorted(set(_GROUP_NAME.findall(text))):
            first = first_by_group.setdefault(name, module)
            parent[root_of(module)] = root_of(first)
    units: dict[str, list[str]] = defaultdict(list)
    for module in sorted(sources):
        units[root_of(module)].append(module)
    return sorted(tuple(members) for members in units.values())


def _entry_of(node: str) -> tuple[str, str | None]:
    """Split a duration key into its module and top-level test (class or function)."""
    module, _, rest = node.partition("::")
    if not rest:
        return module, None
    return module, re.split(r"::|\[|@", rest, maxsplit=1)[0]


def entry_weights(durations: Mapping[str, float]) -> dict[str, float]:
    """Sum every duration under each module and under each module's top-level test."""
    totals: dict[str, float] = defaultdict(float)
    for node, seconds in durations.items():
        module, test = _entry_of(node)
        totals[module] += seconds
        if test is not None:
            totals[f"{module}::{test}"] += seconds
    return totals


def module_weights(modules: Sequence[str], durations: Mapping[str, float]) -> dict[str, float]:
    """Sum every duration entry of a module; unknown modules weigh the mean."""
    totals = entry_weights(durations)
    known = [totals[module] for module in modules if module in totals]
    default = sum(known) / len(known) if known else 1.0
    return {module: totals.get(module, default) for module in modules}


def top_level_tests(text: str) -> tuple[str, ...]:
    """Top-level test functions and classes, or none when splitting is unsafe.

    A module that imports test names, names an xdist group or does not parse
    is never split, so every test it holds stays reachable from its file.
    """
    if _GROUP_NAME.search(text):
        return ()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ()
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
            (alias.asname is None and alias.name.split(".")[-1].startswith(("test", "Test")))
            or (alias.asname or "").startswith(("test", "Test"))
            for alias in node.names
        ):
            return ()
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
        ) or (isinstance(node, ast.ClassDef) and node.name.startswith("Test")):
            names.append(node.name)
    return tuple(dict.fromkeys(names))


def _split_heavy_units(
    units: list[tuple[str, ...]],
    sources: Mapping[str, str],
    weights: dict[str, float],
    totals: Mapping[str, float],
    limit: float,
) -> list[tuple[str, ...]]:
    """Run a module heavier than limit as one node ID per top-level test."""
    result: list[tuple[str, ...]] = []
    for unit in units:
        module = unit[0]
        tests = top_level_tests(sources[module]) if len(unit) == 1 else ()
        if weights[module] <= limit or len(tests) < 2:
            result.append(unit)
            continue
        entries = [f"{module}::{test}" for test in tests]
        measured = sum(totals.get(entry, 0.0) for entry in entries)
        residual = max(weights[module] - measured, 0.0) / len(entries)
        for entry in entries:
            weights[entry] = totals.get(entry, 0.0) + residual
            result.append((entry,))
    return result


def partition_modules(
    sources: Mapping[str, str], shard_count: int, durations: Mapping[str, float]
) -> FilePartition:
    """Split modules into shard_count disjoint, nonempty, balanced shards.

    Modules stay whole unless one outweighs half a shard's share; such a
    module runs as one pytest node ID per top-level test instead.
    """
    weights = module_weights(list(sources), durations)
    units = module_units(sources)
    if shard_count < 1 or len(units) < shard_count:
        raise pytest.UsageError(
            f"Cannot split {len(units)} module units into {shard_count} nonempty shards."
        )
    limit = sum(weights.values()) / (shard_count * 2)
    units = _split_heavy_units(units, sources, weights, entry_weights(durations), limit)
    unit_seconds = {unit: sum(weights[entry] for entry in unit) for unit in units}
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    loads = [0.0] * shard_count
    for unit in sorted(units, key=lambda unit: (-unit_seconds[unit], unit)):
        index = min(range(shard_count), key=lambda shard: (loads[shard], shard))
        shards[index].extend(unit)
        loads[index] += unit_seconds[unit]
    return FilePartition(
        shards=tuple(tuple(sorted(shard)) for shard in shards),
        seconds=tuple(loads),
    )


def shard_test_files(
    targets: Sequence[str],
    expression: str | None,
    shard_count: int,
    durations: Mapping[str, float],
    root: Path,
) -> FilePartition:
    """The canonical per-shard test file lists every sharded CI job runs."""
    return partition_modules(select_test_modules(targets, expression, root), shard_count, durations)


def _write_shard_files(args: argparse.Namespace) -> int:
    if not 1 <= args.shard_index <= args.shard_count:
        print("[x] --shard-index must be between 1 and --shard-count", file=sys.stderr)
        return 2
    try:
        plan = shard_test_files(
            args.targets,
            args.marker_expression,
            args.shard_count,
            load_durations(args.shard_durations),
            Path.cwd(),
        )
    except pytest.UsageError as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 2
    files = plan.shards[args.shard_index - 1]
    Path(args.out).write_text("".join(f"{path}\n" for path in files), encoding="utf-8")
    seconds = ", ".join(f"{value:.0f}" for value in plan.seconds)
    print(
        f"[i] shard {args.shard_index}/{args.shard_count}: {len(files)} of "
        f"{sum(len(shard) for shard in plan.shards)} test files -> {args.out}; "
        f"estimated serial seconds per shard: {seconds}"
    )
    return 0


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
        "--shard-inventory-out",
        default=None,
        help="Write {node_id: [marker names]} of every selected test (drift guard input).",
    )
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
    inventory_out = config.getoption("shard_inventory_out", default=None)
    if inventory_out is not None:
        inventory = {
            item.nodeid: sorted({mark.name for mark in item.iter_markers()}) for item in items
        }
        Path(inventory_out).write_text(json.dumps(inventory, sort_keys=True), encoding="utf-8")
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


def _files_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ci_sharding files",
        description="Write one shard's test modules to a pytest @argfile.",
    )
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-durations", default=None, help="JSON {node_id: seconds}.")
    parser.add_argument("-m", dest="marker_expression", default=None, help="pytest -m expression.")
    parser.add_argument("--out", required=True, help="Write one test file path per line here.")
    parser.add_argument("targets", nargs="+", help="Test directories or files.")
    return parser


def main(argv: Sequence[str]) -> int:
    """``compact OUT IN...`` merges and compacts; ``files ...`` writes a shard file list."""
    if argv and argv[0] == "files":
        return _write_shard_files(_files_parser().parse_args(list(argv[1:])))
    if len(argv) < 3 or argv[0] != "compact":
        print("usage: python -m scripts.ci_sharding compact OUT IN...", file=sys.stderr)
        print("       python -m scripts.ci_sharding files --help", file=sys.stderr)
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
