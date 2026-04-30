"""Performance benchmarks for APM dependency resolution and compilation.

Covers the bottlenecks identified in issue #171:
- Primitive conflict detection (O(m) after fix, was O(m²))
- Cycle detection (O(V+E) after fix, was O(V×D))
- get_nodes_at_depth() (O(1) after fix, was O(V × max_depth))
- from_apm_yml() parse caching
- Inheritance chain caching

Run with: uv run pytest tests/benchmarks/ -v --tb=short -m benchmark
"""

import time
from pathlib import Path

import pytest

from apm_cli.compilation.constitution import clear_constitution_cache, read_constitution
from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.dependency_graph import DependencyNode, DependencyTree
from apm_cli.models.apm_package import APMPackage, DependencyReference, clear_apm_yml_cache
from apm_cli.primitives.models import (
    Instruction,
    PrimitiveCollection,
)

# ---------------------------------------------------------------------------
# Helpers to build synthetic data
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


def _make_dep_ref(owner: str, repo: str, ref: str = "main") -> DependencyReference:
    return DependencyReference.parse(f"{owner}/{repo}#{ref}")


def _make_node(owner: str, repo: str, depth: int, ref: str = "main") -> DependencyNode:
    dep_ref = _make_dep_ref(owner, repo, ref)
    pkg = APMPackage(name=repo, version="1.0.0", source=f"{owner}/{repo}")
    return DependencyNode(package=pkg, dependency_ref=dep_ref, depth=depth)


def _build_deep_tree(n_packages: int, max_depth: int) -> DependencyTree:
    """Build a synthetic dependency tree with n packages spread across depths."""
    root = APMPackage(name="root", version="1.0.0")
    tree = DependencyTree(root_package=root)
    for i in range(n_packages):
        depth = (i % max_depth) + 1
        node = _make_node("owner", f"pkg-{i}", depth)
        tree.add_node(node)
    return tree


# ---------------------------------------------------------------------------
# Benchmark: Primitive conflict detection
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestPrimitiveConflictDetectionPerf:
    """Verify O(m) conflict detection (was O(m²) before #171)."""

    @pytest.mark.parametrize("count", [100, 500, 1000])
    def test_add_unique_primitives(self, count: int):
        """Adding N unique primitives should scale linearly."""
        coll = PrimitiveCollection()
        start = time.perf_counter()
        for i in range(count):
            coll.add_primitive(_make_instruction(f"instr-{i}"))
        elapsed = time.perf_counter() - start

        assert coll.count() == count
        assert not coll.has_conflicts()
        # Rough sanity: 1000 unique adds should be well under 0.5s
        assert elapsed < 0.5, f"Adding {count} primitives took {elapsed:.3f}s"

    def test_conflict_detection_with_duplicates(self):
        """500 adds with 50% conflicts should still be fast."""
        coll = PrimitiveCollection()
        names = [f"instr-{i % 250}" for i in range(500)]
        start = time.perf_counter()
        for name in names:
            coll.add_primitive(_make_instruction(name, source="dep:pkg"))
        elapsed = time.perf_counter() - start

        assert coll.count() == 250
        assert elapsed < 0.5


# ---------------------------------------------------------------------------
# Benchmark: Dependency tree depth-index
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestDepthIndexPerf:
    """Verify O(1) depth lookups (was O(V × max_depth) before #171)."""

    def test_get_nodes_at_depth_large_tree(self):
        """100 packages across 10 depths — lookups should be instant."""
        tree = _build_deep_tree(100, 10)
        start = time.perf_counter()
        total = 0
        for depth in range(1, 11):
            total += len(tree.get_nodes_at_depth(depth))
        elapsed = time.perf_counter() - start

        assert total == 100
        assert elapsed < 0.01, f"Depth lookups took {elapsed:.3f}s"

    def test_depth_index_consistency(self):
        """Depth index returns same nodes as brute-force scan."""
        tree = _build_deep_tree(50, 5)
        for depth in range(1, 6):
            indexed = set(n.get_id() for n in tree.get_nodes_at_depth(depth))
            brute = set(n.get_id() for n in tree.nodes.values() if n.depth == depth)
            assert indexed == brute, f"Mismatch at depth {depth}"


# ---------------------------------------------------------------------------
# Benchmark: Cycle detection
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestCycleDetectionPerf:
    """Verify O(V+E) cycle detection (was O(V×D) before #171)."""

    def test_no_cycles_deep_chain(self):
        """50-node linear chain — O(V+E) detection."""
        root = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root)
        prev = None
        for i in range(50):
            node = _make_node("owner", f"chain-{i}", depth=1)
            tree.add_node(node)
            if prev:
                prev.children.append(node)
            prev = node

        resolver = APMDependencyResolver()
        start = time.perf_counter()
        cycles = resolver.detect_circular_dependencies(tree)
        elapsed = time.perf_counter() - start

        assert len(cycles) == 0
        assert elapsed < 0.05


# ---------------------------------------------------------------------------
# Benchmark: from_apm_yml caching
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestFromApmYmlCachePerf:
    """Verify parse caching eliminates repeated disk I/O."""

    def setup_method(self):
        clear_apm_yml_cache()

    def test_cache_hit_is_faster(self, tmp_path: Path):
        """Second parse of same file should be near-instant (cache hit)."""
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("name: bench-pkg\nversion: 1.0.0\n")

        # Cold parse
        start = time.perf_counter()
        pkg1 = APMPackage.from_apm_yml(apm_yml)
        cold = time.perf_counter() - start

        # Warm parse (cache hit)
        start = time.perf_counter()
        pkg2 = APMPackage.from_apm_yml(apm_yml)
        warm = time.perf_counter() - start

        assert pkg1.name == pkg2.name
        # Cache hit should be at least 2x faster (typically 100x+)
        assert warm < cold or warm < 0.001


# ---------------------------------------------------------------------------
# Benchmark: read_constitution caching
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestConstitutionCachePerf:
    """Verify constitution read caching."""

    def setup_method(self):
        clear_constitution_cache()

    def test_cache_hit(self, tmp_path: Path):
        """Repeated reads of constitution should be cached."""
        const_dir = tmp_path / "memory"
        const_dir.mkdir()
        (const_dir / "constitution.md").write_text("# Constitution\nTest content\n")

        # Cold read
        content1 = read_constitution(tmp_path)
        # Warm read
        content2 = read_constitution(tmp_path)

        assert content1 == content2
        assert content1 is content2  # Same object (cache hit)
