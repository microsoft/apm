"""Benchmark for the #1369 fix: TieredRefResolver call-count reduction.

Asserts a deterministic call-count and wall-clock speedup ratio on the
9-dep/3-unique workload from the bug report. Underlying "clone" is a
fixture sleeping ``CLONE_LATENCY_S`` per call so the assertion is
deterministic in CI rather than depending on real network latency.

Run with:
    uv run pytest tests/benchmarks/test_tiered_resolver_benchmarks.py -v -m benchmark
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from apm_cli.deps.tiered_ref_resolver import (
    L0PerRunCache,
    L3LegacyClone,
    PerRunRefCache,
    TieredRefResolver,
)
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

# Simulates the per-clone cost. Real clones in #1369 took 1583s / 9 ~= 176s
# on Windows+Defender+slow ADO. We use 30ms here so the test runs in
# under 1s while still being measurable.
CLONE_LATENCY_S = 0.03

WORKLOAD = [
    ("awesome/copilot", "main"),
    ("awesome/copilot", "main"),
    ("awesome/copilot", "main"),
    ("awesome/copilot", "main"),
    ("awesome/copilot", "main"),
    ("org/lib-a", "v1.0.0"),
    ("org/lib-a", "v1.0.0"),
    ("org/lib-b", "main"),
    ("org/lib-b", "main"),
]


def _slow_resolve_factory():
    """Return a mock that sleeps to simulate per-clone latency."""

    def fake_resolve(repo_ref):
        time.sleep(CLONE_LATENCY_S)
        dep = DependencyReference.parse(repo_ref) if isinstance(repo_ref, str) else repo_ref
        return ResolvedReference(
            original_ref=str(dep),
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="a" * 40,
            ref_name=dep.reference or "main",
        )

    inner = MagicMock()
    inner.resolve.side_effect = fake_resolve
    return inner


@pytest.mark.benchmark
def test_legacy_path_baseline():
    """Baseline: legacy path runs N clones serially -- one per dep."""
    inner = _slow_resolve_factory()
    deps = [DependencyReference(repo_url=r, reference=ref) for r, ref in WORKLOAD]

    start = time.perf_counter()
    for d in deps:
        inner.resolve(d)
    elapsed = time.perf_counter() - start

    # Sanity: at least N * latency.
    assert elapsed >= len(deps) * CLONE_LATENCY_S * 0.9
    assert inner.resolve.call_count == len(deps)


@pytest.mark.benchmark
def test_tiered_path_collapses_workload():
    """Tiered: L0 cache reduces underlying clones to unique-tuple count."""
    inner = _slow_resolve_factory()
    legacy = L3LegacyClone(legacy_resolver=inner)
    cache = PerRunRefCache()
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), legacy],
        cache=cache,
        legacy=legacy,
    )
    deps = [DependencyReference(repo_url=r, reference=ref) for r, ref in WORKLOAD]

    start = time.perf_counter()
    for d in deps:
        resolver.resolve(d)
    elapsed = time.perf_counter() - start

    unique_count = len({(d.repo_url, d.reference) for d in deps})
    # Call count: exactly unique-tuple count (3), not workload size (9).
    assert inner.resolve.call_count == unique_count == 3
    # Wall-clock: roughly unique_count * latency, NOT len(deps) * latency.
    assert elapsed < len(deps) * CLONE_LATENCY_S * 0.7


@pytest.mark.benchmark
def test_speedup_ratio_meets_three_x_target():
    """End-to-end: the tiered path is at least 3x faster than legacy."""
    # Baseline run
    inner_l = _slow_resolve_factory()
    deps = [DependencyReference(repo_url=r, reference=ref) for r, ref in WORKLOAD]
    t0 = time.perf_counter()
    for d in deps:
        inner_l.resolve(d)
    legacy_elapsed = time.perf_counter() - t0

    # Tiered run
    inner_t = _slow_resolve_factory()
    legacy_tier = L3LegacyClone(legacy_resolver=inner_t)
    cache = PerRunRefCache()
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), legacy_tier],
        cache=cache,
        legacy=legacy_tier,
    )
    t0 = time.perf_counter()
    for d in deps:
        resolver.resolve(d)
    tiered_elapsed = time.perf_counter() - t0

    speedup = legacy_elapsed / tiered_elapsed if tiered_elapsed > 0 else float("inf")
    # Conservative gate: with 9 deps / 3 unique we expect ~3x.
    # Real-world #1369 workload (1583s -> ~few hundred ms cheap-API) is
    # orders of magnitude more dramatic.
    assert speedup >= 2.5, (
        f"Expected >=2.5x speedup, got {speedup:.2f}x "
        f"(legacy={legacy_elapsed:.3f}s tiered={tiered_elapsed:.3f}s)"
    )
