"""Performance benchmarks for the ``apm install`` critical path.

Covers the five hottest code-paths identified in profiling:

1. ``compute_package_hash()`` -- file-tree hashing (rglob + sort + read_bytes)
2. ``LockFile.get_all_dependencies()`` -- repeated sort on every call
3. ``LockFile.is_semantically_equivalent()`` -- to_dict() per dep pair
4. ``flatten_dependencies()`` -- linear conflict scan in FlatDependencyMap
5. ``LockFile.to_yaml()`` -- sort + to_dict() + YAML dump

Plus correctness and cache-opportunity checks.

Run with: uv run pytest tests/benchmarks/test_install_hot_paths.py -v -m benchmark
"""

import os
import time
from pathlib import Path
from typing import List  # noqa: F401, UP035

import pytest

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.dependency_graph import (
    DependencyNode,
    DependencyTree,
    FlatDependencyMap,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.content_hash import compute_package_hash

# ---------------------------------------------------------------------------
# Helpers to build synthetic data
# ---------------------------------------------------------------------------


def _populate_dir(base: Path, file_count: int) -> None:
    """Create *file_count* files (~1 KB each) under *base*."""
    base.mkdir(parents=True, exist_ok=True)
    for i in range(file_count):
        subdir = base / f"sub-{i // 20}"
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / f"file-{i}.dat").write_bytes(os.urandom(1024))


def _make_lockfile(n: int) -> LockFile:
    """Build a synthetic LockFile with *n* LockedDependency entries."""
    lf = LockFile()
    for i in range(n):
        dep = LockedDependency(
            repo_url=f"https://github.com/org/pkg-{i}",
            depth=(i % 5) + 1,
        )
        lf.add_dependency(dep)
    return lf


def _make_lockfile_with_files(n: int, files_per_dep: int = 10) -> LockFile:
    """Build a LockFile where each dep carries *files_per_dep* deployed files."""
    lf = LockFile()
    for i in range(n):
        dep = LockedDependency(
            repo_url=f"https://github.com/org/pkg-{i}",
            depth=(i % 5) + 1,
            deployed_files=[f".github/agents/agent-{i}-{j}.agent.md" for j in range(files_per_dep)],
            deployed_file_hashes={
                f".github/agents/agent-{i}-{j}.agent.md": f"sha256:{'ab' * 32}"
                for j in range(files_per_dep)
            },
        )
        lf.add_dependency(dep)
    return lf


def _make_tree(n: int, conflict_pct: float = 0.0) -> DependencyTree:
    """Build a DependencyTree with *n* nodes.

    *conflict_pct*: fraction of nodes that reuse an earlier repo_url,
    producing conflicts during flattening.
    """
    root = APMPackage(name="root", version="1.0.0")
    tree = DependencyTree(root_package=root)
    conflict_threshold = max(int(n * (1 - conflict_pct)), 1)

    for i in range(n):
        if i >= conflict_threshold:
            repo_url = f"org/pkg-{i % conflict_threshold}"
        else:
            repo_url = f"org/pkg-{i}"

        dep_ref = DependencyReference(repo_url=repo_url)
        pkg = APMPackage(name=f"pkg-{i}", version="1.0.0")
        depth = (i % 3) + 1
        node = DependencyNode(
            package=pkg,
            dependency_ref=dep_ref,
            depth=depth,
        )
        tree.add_node(node)

    return tree


# ---------------------------------------------------------------------------
# Benchmark 1: compute_package_hash() scaling
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestComputePackageHashPerf:
    """Benchmark compute_package_hash() across directory sizes."""

    @pytest.mark.parametrize("file_count", [10, 50, 200, 500])
    def test_hash_scaling(self, tmp_path: Path, file_count: int):
        """Hashing N x 1 KB files should stay well under 2s."""
        pkg_dir = tmp_path / "pkg"
        _populate_dir(pkg_dir, file_count)

        start = time.perf_counter()
        h = compute_package_hash(pkg_dir)
        elapsed = time.perf_counter() - start

        assert h.startswith("sha256:")
        assert len(h) > len("sha256:")
        thresholds = {10: 0.5, 50: 1.0, 200: 2.0, 500: 5.0}
        limit = thresholds[file_count]
        assert elapsed < limit, f"Hashing {file_count} files took {elapsed:.3f}s (limit {limit}s)"


# ---------------------------------------------------------------------------
# Benchmark 2: LockFile.get_all_dependencies() sort cost
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestGetAllDependenciesPerf:
    """Benchmark get_all_dependencies() -- re-sorts on every call."""

    @pytest.mark.parametrize("dep_count", [20, 100, 500])
    def test_sort_latency(self, dep_count: int):
        """Sorting N deps by (depth, repo_url) should be fast."""
        lf = _make_lockfile(dep_count)

        start = time.perf_counter()
        deps = lf.get_all_dependencies()
        elapsed = time.perf_counter() - start

        assert len(deps) == dep_count
        # Verify sort order
        for a, b in zip(deps, deps[1:]):  # noqa: B905, RUF007
            assert (a.depth, a.repo_url) <= (b.depth, b.repo_url)
        assert elapsed < 0.5, f"Sorting {dep_count} deps took {elapsed:.3f}s (limit 0.5s)"


# ---------------------------------------------------------------------------
# Benchmark 3: LockFile.is_semantically_equivalent()
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestSemanticEquivalencePerf:
    """Benchmark is_semantically_equivalent() -- to_dict() per dep pair."""

    @pytest.mark.parametrize("dep_count", [50, 200, 500])
    def test_identical_lockfiles(self, dep_count: int):
        """Comparing two identical lockfiles (worst case -- must check all)."""
        lf1 = _make_lockfile_with_files(dep_count)
        lf2 = _make_lockfile_with_files(dep_count)

        start = time.perf_counter()
        result = lf1.is_semantically_equivalent(lf2)
        elapsed = time.perf_counter() - start

        assert result is True
        thresholds = {50: 0.5, 200: 1.0, 500: 2.0}
        limit = thresholds[dep_count]
        assert elapsed < limit, f"Comparing {dep_count} deps took {elapsed:.3f}s (limit {limit}s)"


# ---------------------------------------------------------------------------
# Benchmark 4: flatten_dependencies() with conflict rate
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestFlattenDependenciesPerf:
    """Benchmark flatten_dependencies() -- linear conflict scan cost."""

    @pytest.mark.parametrize(
        "node_count, conflict_pct",
        [
            (50, 0.0),
            (50, 0.5),
            (200, 0.0),
            (200, 0.5),
        ],
    )
    def test_flatten_latency(self, node_count: int, conflict_pct: float):
        """Flattening N nodes with X% conflict rate."""
        tree = _make_tree(node_count, conflict_pct)
        resolver = APMDependencyResolver()

        start = time.perf_counter()
        flat_map = resolver.flatten_dependencies(tree)
        elapsed = time.perf_counter() - start

        assert isinstance(flat_map, FlatDependencyMap)
        # With 0% conflicts all deps are unique; with 50% the conflict
        # threshold halves the unique count.
        if conflict_pct == 0.0:
            assert flat_map.total_dependencies() == node_count
        elif conflict_pct == 0.5:
            # With 50% conflicts, some deps should be resolved/merged
            assert flat_map.total_dependencies() <= node_count
        assert elapsed < 1.0, (
            f"Flattening {node_count} nodes ({conflict_pct * 100:.0f}% "
            f"conflicts) took {elapsed:.3f}s (limit 1.0s)"
        )


# ---------------------------------------------------------------------------
# Benchmark 5: LockFile.to_yaml() serialization
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestToYamlPerf:
    """Benchmark to_yaml() -- sort + to_dict() + YAML dump."""

    @pytest.mark.parametrize("dep_count", [50, 200, 500])
    def test_to_yaml_latency(self, dep_count: int):
        """Serializing lockfile with N deps + MCP config to YAML."""
        lf = _make_lockfile_with_files(dep_count)
        # Add MCP metadata
        lf.mcp_servers = [f"server-{i}" for i in range(10)]
        lf.mcp_configs = {f"config-{i}": {"key": f"val-{i}"} for i in range(5)}
        lf.local_deployed_files = [f"local-{i}.md" for i in range(20)]
        lf.local_deployed_file_hashes = {f"local-{i}.md": f"sha256:{'cd' * 32}" for i in range(20)}

        start = time.perf_counter()
        yaml_str = lf.to_yaml()
        elapsed = time.perf_counter() - start

        assert isinstance(yaml_str, str)
        assert "lockfile_version" in yaml_str
        thresholds = {50: 1.0, 200: 2.0, 500: 5.0}
        limit = thresholds[dep_count]
        assert elapsed < limit, (
            f"to_yaml() for {dep_count} deps took {elapsed:.3f}s (limit {limit}s)"
        )


# ---------------------------------------------------------------------------
# Benchmark 6: compute_package_hash() correctness
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestComputePackageHashCorrectness:
    """Sanity: hash is deterministic and content-sensitive."""

    def test_deterministic_hash(self, tmp_path: Path):
        """Same directory hashed twice must return the same value."""
        pkg = tmp_path / "det"
        pkg.mkdir()
        (pkg / "a.txt").write_text("alpha\n")
        (pkg / "b.txt").write_text("bravo\n")
        (pkg / "c.txt").write_text("charlie\n")

        h1 = compute_package_hash(pkg)
        h2 = compute_package_hash(pkg)

        assert h1 == h2
        assert h1.startswith("sha256:")
        assert len(h1) == len("sha256:") + 64  # SHA-256 hex

    def test_content_change_changes_hash(self, tmp_path: Path):
        """Modifying a file must change the hash."""
        pkg = tmp_path / "mut"
        pkg.mkdir()
        f = pkg / "data.txt"
        f.write_text("version-1\n")

        h1 = compute_package_hash(pkg)
        f.write_text("version-2\n")
        h2 = compute_package_hash(pkg)

        assert h1 != h2


# ---------------------------------------------------------------------------
# Benchmark 7: get_all_dependencies() cache opportunity
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestGetAllDependenciesCacheOpportunity:
    """Demonstrate repeated-sort cost: 10 calls on the same lockfile."""

    def test_repeated_calls(self):
        """10 calls to get_all_dependencies() -- no caching today."""
        lf = _make_lockfile(500)
        call_count = 10

        start = time.perf_counter()
        for _ in range(call_count):
            deps = lf.get_all_dependencies()
        elapsed = time.perf_counter() - start

        assert len(deps) == 500
        # Total time for 10 sorts of 500 deps should be well under 1s
        assert elapsed < 1.0, (
            f"{call_count} calls took {elapsed:.3f}s total ({elapsed / call_count:.4f}s per call)"
        )


# ---------------------------------------------------------------------------
# Benchmark 8: is_semantically_equivalent() with diff
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestSemanticEquivalenceWithDiff:
    """Measure early-exit vs full-scan cost when lockfiles differ."""

    def test_key_set_mismatch(self):
        """Key-set mismatch should short-circuit before per-dep comparison."""
        lf1 = _make_lockfile_with_files(200)
        lf2 = _make_lockfile_with_files(200)
        # Add an extra dep to lf2 so key sets differ
        lf2.add_dependency(
            LockedDependency(
                repo_url="https://github.com/org/extra-pkg",
                depth=1,
            )
        )

        start = time.perf_counter()
        result = lf1.is_semantically_equivalent(lf2)
        elapsed_key_mismatch = time.perf_counter() - start

        assert result is False
        assert elapsed_key_mismatch < 0.5, (
            f"Key-set mismatch took {elapsed_key_mismatch:.3f}s (limit 0.5s)"
        )

        # Full-scan: identical lockfiles (must iterate all deps)
        lf3 = _make_lockfile_with_files(200)
        lf4 = _make_lockfile_with_files(200)

        start = time.perf_counter()
        result2 = lf3.is_semantically_equivalent(lf4)
        elapsed_full = time.perf_counter() - start

        assert result2 is True
        assert elapsed_full < 0.5, f"Full scan took {elapsed_full:.3f}s (limit 0.5s)"

    def test_last_dep_value_diff(self):
        """When only the last dep differs, must scan all deps."""
        lf1 = _make_lockfile_with_files(200)
        lf2 = _make_lockfile_with_files(200)

        # Mutate the last dep in lf2 by adding an extra deployed file
        last_key = list(lf2.dependencies.keys())[-1]
        lf2.dependencies[last_key].deployed_files.append(".github/agents/extra-agent.agent.md")

        start = time.perf_counter()
        result = lf1.is_semantically_equivalent(lf2)
        elapsed = time.perf_counter() - start

        assert result is False
        assert elapsed < 1.0, f"Full scan with last-dep diff took {elapsed:.3f}s (limit 1.0s)"
