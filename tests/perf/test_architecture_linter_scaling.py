"""Scaling guards for the architecture linter, fact-cache and real-runner.

Three independent scenarios cover fact extraction, runner mechanics, and the
real six-group catalog:

Section A (fact cache) synthesizes two benign, uniform Python corpora -- N
files and 10N files -- and drives each straight through `FactsProvider`
(bypassing `runner.run` entirely: no inventory walk, no owner registry,
nothing but the read/parse/AST-visit pipeline under test). Asserts:

* every file is read exactly once and parsed exactly once at both scales
  (the same `SourceCache`/`ParseCache` "at most once" invariant unit-tested
  elsewhere, reconfirmed here at a larger scale), and
* the total AST-visit count -- a deterministic node-count metric, not a
  wall-clock duration -- grows ~linearly (~10x) rather than ~quadratically
  (~100x) with a 10x corpus-size increase.

Section A deliberately does not assert on `time.perf_counter()` deltas: wall
time is sensitive to whatever else is running on the host, while the visit
count for a fixed, uniform synthetic corpus is exactly reproducible, so the
scaling assertion never flakes on a slow or loaded CI runner.

Section B (real runner path) is the opposite trade: it exercises the whole
`runner.run()` pipeline -- inventory build, six-group catalog collection,
bidirectional owner-registry validation, one synthetic guard rule executing
against every corpus file via `provider.file_facts`, diagnostic aggregation
and sorting, and `RunMetrics` -- against 0, N, and 10N synthetic files, for
both a benign corpus (zero violations) and a violation-heavy corpus (one
violation per synthetic file). Because this section genuinely measures
`runner.metrics.total_seconds` wall time, each size takes the median of
several fresh runs and compares raw and baseline-adjusted 10N/N ratios
against a loose ceiling -- loose enough that ordinary host noise never
fails it, tight enough that a real quadratic regression still would.

Section C copies the real repository, adds 0, N, and 10N benign integration
modules, and runs the actual six-group catalog without monkeypatching. A
bounded CI case counts every production ``TreeIndex.walk`` result and applies
fixed-cost subtraction to deterministic work units. The richer benchmark uses
three fresh samples per size and applies the same subtraction to median wall
time.

Run explicitly with ``PYTEST_PERF=1 pytest tests/perf -v -s`` (see
`tests/perf/conftest.py`); also gated by the repo-wide `benchmark` marker,
which is deselected by the default addopts (`-m 'not benchmark and not
live'`) the same way every other perf/benchmark test in this repo is.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import statistics
import types
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.architecture_linter import runner
from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.inventory import EXCLUDED_ROOTS
from scripts.architecture_linter.models import Rule, Violation

from .conftest import PERF_ENABLED

pytestmark = [
    pytest.mark.benchmark,
    pytest.mark.skipif(
        not PERF_ENABLED, reason="Perf scaling scenarios are opt-in (PYTEST_PERF=1)"
    ),
]

_SMALL_N = 300
_LARGE_N = _SMALL_N * 10
_MAX_AST_VISIT_RATIO = 15
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class _CorpusResult:
    file_count: int
    read_attempts: int
    parse_attempts: int
    max_reads_per_file: int
    max_parses_per_file: int
    ast_visits: int


def _write_benign_corpus(root: Path, count: int) -> tuple[str, ...]:
    """Write `count` small, uniform, syntactically valid Python modules."""
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        relative_path = f"mod_{index}.py"
        (root / relative_path).write_text(
            f"def f_{index}(x):\n    y = x + 1\n    z = y * 2\n    return z\n",
            encoding="utf-8",
        )
        paths.append(relative_path)
    return tuple(paths)


def _process_corpus_once_through_facts_provider(
    root: Path, paths: tuple[str, ...]
) -> _CorpusResult:
    """Request every file's facts exactly once from a fresh `FactsProvider`."""
    provider = FactsProvider(root, paths, registry=None)
    for path in paths:
        provider.file_facts(path)
    return _CorpusResult(
        file_count=len(paths),
        read_attempts=provider.source_cache.read_attempts,
        parse_attempts=provider.parse_cache.parse_attempts,
        max_reads_per_file=provider.source_cache.max_reads_per_file,
        max_parses_per_file=provider.parse_cache.max_parses_per_file,
        ast_visits=provider.ast_visits,
    )


def test_facts_provider_reads_and_parses_each_file_at_most_once_at_both_scales(
    tmp_path: Path,
) -> None:
    """The read-once/parse-once cache invariant holds for N and for 10N."""
    small_paths = _write_benign_corpus(tmp_path / "corpus_n", _SMALL_N)
    large_paths = _write_benign_corpus(tmp_path / "corpus_ten_n", _LARGE_N)

    small = _process_corpus_once_through_facts_provider(tmp_path / "corpus_n", small_paths)
    large = _process_corpus_once_through_facts_provider(tmp_path / "corpus_ten_n", large_paths)

    assert small.read_attempts == _SMALL_N
    assert small.parse_attempts == _SMALL_N
    assert small.max_reads_per_file <= 1
    assert small.max_parses_per_file <= 1

    assert large.read_attempts == _LARGE_N
    assert large.parse_attempts == _LARGE_N
    assert large.max_reads_per_file <= 1
    assert large.max_parses_per_file <= 1


def test_ast_visit_count_scales_linearly_not_quadratically_with_corpus_size(
    tmp_path: Path,
) -> None:
    """A 10x corpus-size increase gives a far-below-100x AST-visit increase.

    An O(n) fact-extraction pipeline gives a visit-count ratio near 10 for
    a 10x input increase; an O(n^2) regression would give a ratio near
    100. The threshold below is deliberately loose (well above the ~10
    this uniform corpus actually measures) so ordinary constant-factor
    noise never fails the test, while a quadratic regression still would.
    """
    small_paths = _write_benign_corpus(tmp_path / "corpus_n", _SMALL_N)
    large_paths = _write_benign_corpus(tmp_path / "corpus_ten_n", _LARGE_N)

    small = _process_corpus_once_through_facts_provider(tmp_path / "corpus_n", small_paths)
    large = _process_corpus_once_through_facts_provider(tmp_path / "corpus_ten_n", large_paths)

    assert small.ast_visits > 0
    ratio = large.ast_visits / small.ast_visits
    assert ratio < _MAX_AST_VISIT_RATIO


# ---------------------------------------------------------------------------
# Section B: `runner.run()` scaling at 0/N/10N, benign and violation-heavy.
#
# Unlike Section A, this drives the real orchestrator end to end: repository
# inventory (`build_inventory`), the fixed six-group catalog (monkeypatched
# `runner._GROUP_IMPORTS`), bidirectional owner-registry validation, rule
# execution through `provider.inventory`/`provider.file_facts`, diagnostic
# aggregation and sorting, and the emitted `RunMetrics`. It genuinely times
# `runner.run()` wall-clock cost, so every size is measured across several
# fresh runs and summarized by the median before any ratio is computed.
# ---------------------------------------------------------------------------

_RUNS_PER_SIZE = 3
_MAX_TOTAL_SECONDS_RATIO = 15
_MEASURABLE_SECONDS_FLOOR = 0.001

_PERF_RULE_ID = "perf-synthetic-guard"
_PERF_GUARD_ID = "perf-synthetic-guard-owner"
_PERF_RULE_GROUP = "registry_delegation"
_PERF_VIOLATION_MARKER_MODULE = "synthetic_perf_violation_marker"
_PERF_SENTINEL_PATH = "src/sentinel.py"
_PERF_SYNTH_PREFIX = "src/synth/"
_PERF_FIXED_FILE_COUNT = 4  # pyproject.toml, sentinel.py, index.json, owner shard.
_PERF_REGISTRY_READ_COUNT = 2  # registry loading reads index.json + one shard.


def _synthetic_guard_check(provider: FactsProvider) -> list[Violation]:
    """One synthetic guard rule: inventory-driven scan, fact-cache-backed read.

    Walks `provider.inventory` (never the filesystem directly) for synthetic
    corpus files, pulls each file's facts through `provider.file_facts`
    (never a second, ad hoc read/parse), and reports exactly one ASCII
    violation per file that imports the synthetic violation marker module --
    zero violations for a benign corpus, one per file for a violation-heavy
    one.
    """
    violations: list[Violation] = []
    for path in provider.inventory:
        if not path.startswith(_PERF_SYNTH_PREFIX) or not path.endswith(".py"):
            continue
        facts = provider.file_facts(path)
        if any(_PERF_VIOLATION_MARKER_MODULE in imp.names for imp in facts.imports):
            violations.append(
                Violation(
                    rule_id=_PERF_RULE_ID,
                    path=path,
                    line=1,
                    column=1,
                    message="synthetic perf guard: forbidden marker import present",
                )
            )
    return violations


_PERF_RULE = Rule(
    id=_PERF_RULE_ID,
    group=_PERF_RULE_GROUP,
    guard_ids=(_PERF_GUARD_ID,),
    description="Synthetic benchmark guard rule exercised by the perf scaling test.",
    check=_synthetic_guard_check,
)


def _make_group_imports() -> tuple[tuple[str, types.ModuleType, None], ...]:
    """Build exactly six fake group modules holding the one synthetic rule.

    Five of the six modules are empty (`RULES = ()`); the sixth (matching
    `_PERF_RULE.group`) holds the single synthetic guard rule. This mirrors
    the real `runner._GROUP_IMPORTS` shape -- six literal group slots -- so
    `runner.run()` runs its real catalog-collection and cross-group
    validation logic, not a shortcut.
    """
    entries: list[tuple[str, types.ModuleType, None]] = []
    for name in runner.GROUP_MODULE_NAMES:
        module = types.ModuleType(f"fake-perf-{name}")
        module.RULES = (_PERF_RULE,) if name == _PERF_RULE_GROUP else ()
        module.COLLECTORS = ()
        entries.append((name, module, None))
    return tuple(entries)


def _write_repo_skeleton(root: Path) -> None:
    """Write the smallest valid repository root plus a one-owner registry.

    The owner's selector targets a fixed sentinel file that exists at every
    corpus size (including zero), so registry validity never depends on how
    many synthetic corpus files exist -- only the rule's own inventory scan
    does.
    """
    for name in ("src", "scripts", "tests", ".apm"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("", encoding="ascii")
    (root / _PERF_SENTINEL_PATH).write_text("x = 1\n", encoding="ascii")

    owners_dir = root / ".apm" / "architecture" / "owners"
    owners_dir.mkdir(parents=True, exist_ok=True)
    (owners_dir / "index.json").write_text(
        json.dumps({"version": 1, "shards": ["perf.json"]}), encoding="ascii"
    )
    (owners_dir / "perf.json").write_text(
        json.dumps(
            {
                "version": 1,
                "owners": [
                    {
                        "id": "perf-owner",
                        "decision": "Synthetic perf benchmark ownership decision",
                        "owner": "core/sentinel.py (PerfOwner)",
                        "selectors": [_PERF_SENTINEL_PATH],
                        "guards": [_PERF_GUARD_ID],
                    }
                ],
            }
        ),
        encoding="ascii",
    )


def _write_synth_corpus(root: Path, count: int, *, violation: bool) -> tuple[str, ...]:
    """Write `count` synthetic corpus files under `src/synth/`, if any.

    Benign files never import the marker module; violation-heavy files
    always do, so the synthetic rule reports zero or `count` violations.
    """
    if count <= 0:
        return ()
    synth_dir = root / "src" / "synth"
    synth_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        if violation:
            body = (
                f"import {_PERF_VIOLATION_MARKER_MODULE}\n\n\n"
                f"def f_{index}(x):\n    y = x + 1\n    return y\n"
            )
        else:
            body = f"def f_{index}(x):\n    y = x + 1\n    z = y * 2\n    return z\n"
        relative_path = f"{_PERF_SYNTH_PREFIX}mod_{index}.py"
        (root / relative_path).write_text(body, encoding="ascii")
        paths.append(relative_path)
    return tuple(paths)


def _build_perf_repo(root: Path, count: int, *, violation: bool) -> Path:
    """Build one complete synthetic repository root for one (size, kind) cell."""
    root.mkdir(parents=True, exist_ok=True)
    _write_repo_skeleton(root)
    _write_synth_corpus(root, count, violation=violation)
    return root


def _median_total_seconds(reports: Sequence[runner.RunReport]) -> float:
    return statistics.median(report.metrics.total_seconds for report in reports)


@pytest.mark.parametrize("violation", [False, True], ids=["benign", "violation_heavy"])
def test_runner_real_path_scales_subquadratically_at_zero_n_and_ten_n(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, violation: bool
) -> None:
    """`runner.run()` end to end at 0/N/10N stays sub-quadratic and correct.

    Exercises the real inventory build, six-group catalog collection,
    bidirectional owner-registry validation, the one synthetic guard rule
    (inventory-driven, fact-cache-backed), diagnostic aggregation/sorting,
    and `RunMetrics`, for both a benign corpus (zero violations at every
    size) and a violation-heavy corpus (one violation per synthetic file).
    Every size is measured across `_RUNS_PER_SIZE` fresh `runner.run()`
    calls and summarized by the median before either ratio is computed, so
    a single slow run on a loaded host cannot flip the assertion.
    """
    monkeypatch.setattr(runner, "_GROUP_IMPORTS", _make_group_imports())

    sizes = {"zero": 0, "n": _SMALL_N, "ten_n": _LARGE_N}
    roots = {
        label: _build_perf_repo(tmp_path / f"repo_{label}", count, violation=violation)
        for label, count in sizes.items()
    }

    reports = {
        label: tuple(runner.run(roots[label]) for _ in range(_RUNS_PER_SIZE)) for label in sizes
    }

    for label, count in sizes.items():
        expected_violation_count = count if violation else 0
        expected_read_attempts = count + _PERF_REGISTRY_READ_COUNT
        expected_synth_paths = {f"{_PERF_SYNTH_PREFIX}mod_{i}.py" for i in range(count)}

        parse_attempts_seen = set()
        read_attempts_seen = set()
        violation_counts_seen = set()
        for report in reports[label]:
            metrics = report.metrics
            assert metrics.child_process_count == 0
            assert metrics.max_reads_per_file <= 1
            assert metrics.max_parses_per_file <= 1
            assert metrics.parse_attempts == count
            assert metrics.read_attempts == expected_read_attempts
            assert metrics.inventory_file_count == count + _PERF_FIXED_FILE_COUNT
            assert len(report.violations) == expected_violation_count
            assert all(v.rule_id == _PERF_RULE_ID for v in report.violations)
            assert all(v.message.isascii() and v.message for v in report.violations)
            assert {v.path for v in report.violations} == (
                expected_synth_paths if violation else set()
            )
            sorted_paths = [v.path for v in report.violations]
            assert sorted_paths == sorted(sorted_paths)
            parse_attempts_seen.add(metrics.parse_attempts)
            read_attempts_seen.add(metrics.read_attempts)
            violation_counts_seen.add(len(report.violations))

        # Deterministic work: repeated fresh runs against the same corpus
        # produce identical counters every time.
        assert len(parse_attempts_seen) == 1
        assert len(read_attempts_seen) == 1
        assert len(violation_counts_seen) == 1

    medians = {label: _median_total_seconds(reports[label]) for label in sizes}
    baseline = medians["zero"]
    n_time = medians["n"]
    ten_n_time = medians["ten_n"]

    if n_time > _MEASURABLE_SECONDS_FLOOR:
        raw_ratio = ten_n_time / n_time
        assert raw_ratio < _MAX_TOTAL_SECONDS_RATIO

    adjusted_n_time = n_time - baseline
    adjusted_ten_n_time = ten_n_time - baseline
    if adjusted_n_time > _MEASURABLE_SECONDS_FLOOR:
        adjusted_ratio = adjusted_ten_n_time / adjusted_n_time
        assert adjusted_ratio < _MAX_TOTAL_SECONDS_RATIO


# ---------------------------------------------------------------------------
# Section C: actual six-group catalog over 0/N/10N repository copies. No fake
# Rule, group module, or selected-rule shortcut is involved.
# ---------------------------------------------------------------------------
_CI_REAL_SMALL_N = 6
_CI_REAL_LARGE_N = _CI_REAL_SMALL_N * 10
_REAL_SMALL_N = 80
_REAL_LARGE_N = _REAL_SMALL_N * 10
_MAX_REAL_CATALOG_RATIO = 15
_REAL_RUNS_PER_SIZE = 3
_REAL_MEASURABLE_SECONDS = 0.05


@dataclass(frozen=True)
class _CatalogSample:
    report: runner.RunReport
    walk_work_units: int


def _build_real_catalog_repo(root: Path, count: int) -> Path:
    shutil.copytree(
        ROOT,
        root,
        ignore=shutil.ignore_patterns(*EXCLUDED_ROOTS),
    )
    directory = root / "tests/integration/perf_catalog"
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"test_module_{index}.py").write_text(
            "import pytest\n\n"
            "pytestmark = pytest.mark.integration\n\n\n"
            f"def helper_{index}(value):\n"
            "    if value:\n"
            "        return value\n"
            "    return None\n",
            encoding="ascii",
        )
    return root


def _install_walk_counter(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count structural work without adding another AST traversal."""
    work_units = [0]
    original = TreeIndex.walk

    def counted_walk(index: TreeIndex, node: ast.AST) -> Sequence[ast.AST]:
        result = original(index, node)
        work_units[0] += len(result)
        return result

    monkeypatch.setattr(TreeIndex, "walk", counted_walk)
    return work_units


def _catalog_sample(root: Path, work_units: list[int]) -> _CatalogSample:
    before = work_units[0]
    report = runner.run(root)
    return _CatalogSample(report=report, walk_work_units=work_units[0] - before)


def _assert_catalog_sample(sample: _CatalogSample) -> None:
    report = sample.report
    assert report.exit_code == 0
    assert report.metrics.tree_index_builds > 0
    assert report.metrics.max_tree_index_builds_per_file <= 1
    assert report.metrics.max_reads_per_file <= 1
    assert report.metrics.max_parses_per_file <= 1
    assert report.metrics.child_process_count == 0
    assert sample.walk_work_units > 0


def test_ci_real_catalog_work_scales_below_fifteen_x_after_fixed_cost_subtraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded CI gate exercises the production catalog with deterministic work."""
    work_units = _install_walk_counter(monkeypatch)
    roots = {
        "zero": _build_real_catalog_repo(tmp_path / "ci_real_zero", 0),
        "n": _build_real_catalog_repo(tmp_path / "ci_real_n", _CI_REAL_SMALL_N),
        "ten_n": _build_real_catalog_repo(tmp_path / "ci_real_ten_n", _CI_REAL_LARGE_N),
    }
    samples = {label: _catalog_sample(root, work_units) for label, root in roots.items()}
    for sample in samples.values():
        _assert_catalog_sample(sample)

    fixed = samples["zero"].walk_work_units
    incremental_n = samples["n"].walk_work_units - fixed
    incremental_ten_n = samples["ten_n"].walk_work_units - fixed
    assert incremental_n > 0
    assert incremental_ten_n / incremental_n < _MAX_REAL_CATALOG_RATIO


def test_real_six_group_catalog_wall_time_scales_below_fifteen_x(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three-sample production-catalog medians scale subquadratically."""
    work_units = _install_walk_counter(monkeypatch)
    sizes = {"zero": 0, "n": _REAL_SMALL_N, "ten_n": _REAL_LARGE_N}
    roots = {
        label: _build_real_catalog_repo(tmp_path / f"real_{label}", count)
        for label, count in sizes.items()
    }
    samples = {
        label: tuple(_catalog_sample(root, work_units) for _ in range(_REAL_RUNS_PER_SIZE))
        for label, root in roots.items()
    }
    for scale_samples in samples.values():
        for sample in scale_samples:
            _assert_catalog_sample(sample)

    median_seconds = {
        label: statistics.median(sample.report.metrics.total_seconds for sample in values)
        for label, values in samples.items()
    }
    baseline = median_seconds["zero"]
    incremental_n = median_seconds["n"] - baseline
    incremental_ten_n = median_seconds["ten_n"] - baseline
    assert incremental_n > _REAL_MEASURABLE_SECONDS
    assert incremental_ten_n / incremental_n < _MAX_REAL_CATALOG_RATIO

    median_work = {
        label: statistics.median(sample.walk_work_units for sample in values)
        for label, values in samples.items()
    }
    incremental_n_work = median_work["n"] - median_work["zero"]
    incremental_ten_n_work = median_work["ten_n"] - median_work["zero"]
    report_path = os.environ.get("APM_ARCH_SCALING_REPORT")
    if report_path:
        payload = {
            "fixed_cost_subtraction": "0-file production-catalog median",
            "max_ratio": _MAX_REAL_CATALOG_RATIO,
            "run_count_per_size": _REAL_RUNS_PER_SIZE,
            "sizes": sizes,
            "scales": {
                label: {
                    "ast_visits": [sample.report.metrics.ast_visits for sample in values],
                    "inventory_files": [
                        sample.report.metrics.inventory_file_count for sample in values
                    ],
                    "max_parses_per_file": [
                        sample.report.metrics.max_parses_per_file for sample in values
                    ],
                    "max_reads_per_file": [
                        sample.report.metrics.max_reads_per_file for sample in values
                    ],
                    "wall_seconds": [sample.report.metrics.total_seconds for sample in values],
                    "walk_work_units": [sample.walk_work_units for sample in values],
                }
                for label, values in samples.items()
            },
            "statistics": {
                "incremental_ten_n_over_n_wall": incremental_ten_n / incremental_n,
                "incremental_ten_n_over_n_work": (incremental_ten_n_work / incremental_n_work),
                "median_wall_seconds": median_seconds,
                "median_walk_work_units": median_work,
            },
        }
        Path(report_path).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="ascii",
        )
    assert incremental_n_work > 0
    assert incremental_ten_n_work / incremental_n_work < _MAX_REAL_CATALOG_RATIO
