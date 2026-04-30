#!/usr/bin/env python3
"""Standalone baseline benchmark — works against both original and optimized code.

Measures raw performance of the bottleneck paths identified in #171.
No dependency on cache-clearing functions (those only exist post-optimization).

Usage: uv run python tests/benchmarks/run_baseline.py
"""

import importlib  # noqa: F401
import statistics
import sys
import time
from pathlib import Path

# Ensure the project is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.dependency_graph import DependencyNode, DependencyTree
from apm_cli.models.apm_package import APMPackage, DependencyReference
from apm_cli.primitives.models import Instruction, PrimitiveCollection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_instruction(name: str, source: str = "local") -> Instruction:
    return Instruction(
        name=name,
        file_path=Path(f"/fake/{name}.instructions.md"),
        description=f"Test instruction {name}",
        apply_to="**",
        content=f"Content for {name}",
        source=source,
    )


def _make_node(owner: str, repo: str, depth: int) -> DependencyNode:
    dep_ref = DependencyReference.parse(f"{owner}/{repo}#main")
    pkg = APMPackage(name=repo, version="1.0.0", source=f"{owner}/{repo}")
    return DependencyNode(package=pkg, dependency_ref=dep_ref, depth=depth)


def _build_tree(n: int, max_depth: int) -> DependencyTree:
    root = APMPackage(name="root", version="1.0.0")
    tree = DependencyTree(root_package=root)
    for i in range(n):
        depth = (i % max_depth) + 1
        node = _make_node("owner", f"pkg-{i}", depth)
        tree.add_node(node)
    return tree


def bench(fn, *, runs: int = 5, label: str = ""):
    """Run fn `runs` times, return (median_ms, min_ms, max_ms)."""
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        times.append((time.perf_counter() - start) * 1000)
    med = statistics.median(times)
    lo = min(times)
    hi = max(times)
    return med, lo, hi


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def bench_primitive_conflict_detection(count: int):
    """Add `count` unique primitives — measures conflict-check cost."""

    def run():
        coll = PrimitiveCollection()
        for i in range(count):
            coll.add_primitive(_make_instruction(f"instr-{i}"))
        assert coll.count() == count

    return bench(run, label=f"primitive_add_{count}")


def bench_primitive_conflict_50pct(count: int):
    """Add `count` primitives with 50% name collisions."""
    half = count // 2

    def run():
        coll = PrimitiveCollection()
        for i in range(count):
            coll.add_primitive(_make_instruction(f"instr-{i % half}", source="dep:pkg"))
        assert coll.count() == half

    return bench(run, label=f"primitive_conflict_50pct_{count}")


def bench_depth_lookup(n_packages: int, max_depth: int):
    """Build tree then query every depth level."""
    tree = _build_tree(n_packages, max_depth)

    def run():
        total = 0
        for d in range(1, max_depth + 1):
            total += len(tree.get_nodes_at_depth(d))
        assert total == n_packages

    return bench(run, runs=20, label=f"depth_lookup_{n_packages}x{max_depth}")


def bench_cycle_detection_chain(length: int):
    """Detect cycles in a linear chain of `length` nodes."""
    root = APMPackage(name="root", version="1.0.0")
    tree = DependencyTree(root_package=root)
    prev = None
    for i in range(length):
        node = _make_node("owner", f"chain-{i}", depth=1)
        tree.add_node(node)
        if prev:
            prev.children.append(node)
        prev = node
    resolver = APMDependencyResolver()

    def run():
        cycles = resolver.detect_circular_dependencies(tree)
        assert len(cycles) == 0

    return bench(run, label=f"cycle_detect_chain_{length}")


def bench_flatten(n_packages: int, max_depth: int):
    """Flatten a tree of n packages across max_depth levels."""
    tree = _build_tree(n_packages, max_depth)
    resolver = APMDependencyResolver()

    def run():
        flat = resolver.flatten_dependencies(tree)
        assert flat.total_dependencies() == n_packages

    return bench(run, label=f"flatten_{n_packages}x{max_depth}")


def bench_from_apm_yml_repeated(tmp_dir: Path, repeats: int):
    """Parse the same apm.yml file `repeats` times."""
    apm_yml = tmp_dir / "apm.yml"
    apm_yml.write_text("name: bench-pkg\nversion: 1.0.0\n")

    def run():
        for _ in range(repeats):
            # Reload module-level cache state differs between baseline/optimized,
            # but we just measure wall-clock for `repeats` calls.
            APMPackage.from_apm_yml(apm_yml)

    return bench(run, runs=3, label=f"from_apm_yml_x{repeats}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    import tempfile

    results = {}

    print("=" * 70)
    print("APM Performance Baseline Benchmark")
    print("=" * 70)

    # 1. Primitive conflict detection
    for count in [100, 500, 1000]:
        med, lo, hi = bench_primitive_conflict_detection(count)
        key = f"primitive_add_unique_{count}"
        results[key] = (med, lo, hi)
        print(f"  {key:45s}  median={med:8.2f}ms  min={lo:8.2f}ms  max={hi:8.2f}ms")

    for count in [100, 500, 1000]:
        med, lo, hi = bench_primitive_conflict_50pct(count)
        key = f"primitive_conflict_50pct_{count}"
        results[key] = (med, lo, hi)
        print(f"  {key:45s}  median={med:8.2f}ms  min={lo:8.2f}ms  max={hi:8.2f}ms")

    # 2. Depth lookups
    for n, d in [(50, 5), (100, 10), (500, 10)]:
        med, lo, hi = bench_depth_lookup(n, d)
        key = f"depth_lookup_{n}x{d}"
        results[key] = (med, lo, hi)
        print(f"  {key:45s}  median={med:8.2f}ms  min={lo:8.2f}ms  max={hi:8.2f}ms")

    # 3. Cycle detection
    for length in [20, 50, 100]:
        med, lo, hi = bench_cycle_detection_chain(length)
        key = f"cycle_detect_chain_{length}"
        results[key] = (med, lo, hi)
        print(f"  {key:45s}  median={med:8.2f}ms  min={lo:8.2f}ms  max={hi:8.2f}ms")

    # 4. Flatten
    for n, d in [(50, 5), (100, 10), (500, 10)]:
        med, lo, hi = bench_flatten(n, d)
        key = f"flatten_{n}x{d}"
        results[key] = (med, lo, hi)
        print(f"  {key:45s}  median={med:8.2f}ms  min={lo:8.2f}ms  max={hi:8.2f}ms")

    # 5. from_apm_yml repeated parsing
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for repeats in [10, 50]:
            med, lo, hi = bench_from_apm_yml_repeated(tmp_path, repeats)
            key = f"from_apm_yml_x{repeats}"
            results[key] = (med, lo, hi)
            print(f"  {key:45s}  median={med:8.2f}ms  min={lo:8.2f}ms  max={hi:8.2f}ms")

    # 6. Phase 4: Parallel execution overhead (ThreadPoolExecutor vs sequential)
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _simulated_work(ms: float):
        """Simulate I/O-bound work by sleeping."""
        _time.sleep(ms / 1000)
        return True

    # Sequential: 10 tasks × 50ms each = ~500ms
    def _seq_10():
        for _ in range(10):
            _simulated_work(50)

    med, lo, hi = bench(_seq_10, runs=3, label="sequential_10x50ms")
    key = "sequential_10x50ms"
    results[key] = (med, lo, hi)
    print(f"  {key:45s}  median={med:8.2f}ms  min={lo:8.2f}ms  max={hi:8.2f}ms")

    # Parallel: 10 tasks × 50ms, 4 workers → ~150ms (ceil(10/4) × 50ms)
    def _par_10():
        with ThreadPoolExecutor(max_workers=4) as executor:
            futs = [executor.submit(_simulated_work, 50) for _ in range(10)]
            for f in as_completed(futs):
                f.result()

    med, lo, hi = bench(_par_10, runs=3, label="parallel_4w_10x50ms")
    key = "parallel_4w_10x50ms"
    results[key] = (med, lo, hi)
    print(f"  {key:45s}  median={med:8.2f}ms  min={lo:8.2f}ms  max={hi:8.2f}ms")

    print("=" * 70)
    print(f"Total benchmarks: {len(results)}")
    print("=" * 70)

    return results


if __name__ == "__main__":
    main()
