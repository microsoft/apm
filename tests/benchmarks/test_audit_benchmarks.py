"""Performance benchmarks for APM audit hot paths.

Covers the bottlenecks identified in the complexity audit:
- Phase 0: Dependency parsing deduplication (APMPackage.from_apm_yml)
- Phase 2: Uninstall engine children index (_build_children_index)
- Phase 6: Primitive discovery scanning (find_primitive_files)
- Registry config cache (MCPServerOperations._get_installed_server_ids)
- Console singleton (_get_console thread-safe singleton)

Run with: uv run pytest tests/benchmarks/test_audit_benchmarks.py -v -m benchmark
"""

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import patch, MagicMock

import pytest

from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.commands.uninstall.engine import _build_children_index
from apm_cli.primitives.discovery import find_primitive_files
from apm_cli.utils.console import _get_console, _reset_console


# ---------------------------------------------------------------------------
# Helpers to build synthetic data
# ---------------------------------------------------------------------------

def _write_apm_yml_with_deps(path: Path, dep_count: int) -> Path:
    """Write an apm.yml with N APM dependencies."""
    lines = [
        "name: bench-pkg",
        "version: 1.0.0",
        "dependencies:",
        "  apm:",
    ]
    for i in range(dep_count):
        lines.append(f"    - owner-{i}/repo-{i}")
    apm_yml = path / "apm.yml"
    apm_yml.write_text("\n".join(lines) + "\n")
    return apm_yml


@dataclass
class _FakeDep:
    """Minimal stand-in for LockedDependency used by _build_children_index."""
    repo_url: str
    resolved_by: Optional[str] = None
    virtual_path: Optional[str] = None
    is_virtual: bool = False
    source: Optional[str] = None
    local_path: Optional[str] = None

    def get_unique_key(self) -> str:
        if self.source == "local" and self.local_path:
            return self.local_path
        if self.is_virtual and self.virtual_path:
            return f"{self.repo_url}/{self.virtual_path}"
        return self.repo_url


@dataclass
class _FakeLockFile:
    """Minimal stand-in for LockFile used by _build_children_index."""
    dependencies: Dict[str, "_FakeDep"] = field(default_factory=dict)

    def get_package_dependencies(self) -> List["_FakeDep"]:
        return sorted(self.dependencies.values(), key=lambda d: d.repo_url)


def _build_fake_lockfile(dep_count: int) -> _FakeLockFile:
    """Build a synthetic lockfile with parent -> child relationships.

    Creates ``dep_count`` dependencies where each dep (except the first)
    has a ``resolved_by`` pointing to the previous dep, forming a chain.
    """
    lockfile = _FakeLockFile()
    for i in range(dep_count):
        parent_url = f"owner/parent-{i - 1}" if i > 0 else None
        dep = _FakeDep(
            repo_url=f"owner/child-{i}",
            resolved_by=parent_url,
        )
        lockfile.dependencies[dep.get_unique_key()] = dep
    return lockfile


def _create_file_tree(base: Path, file_count: int) -> None:
    """Create a directory tree with a mix of matching and non-matching files.

    Distributes files across subdirectories (10 files per subdir).
    Roughly 20% are .instructions.md, 20% are .agent.md, and 60% are
    plain .md or .txt files that should NOT match discovery patterns.
    """
    for i in range(file_count):
        subdir = base / f"sub-{i // 10}"
        subdir.mkdir(parents=True, exist_ok=True)
        remainder = i % 5
        if remainder == 0:
            (subdir / f"file-{i}.instructions.md").write_text(f"instr {i}\n")
        elif remainder == 1:
            (subdir / f"file-{i}.agent.md").write_text(f"agent {i}\n")
        else:
            (subdir / f"file-{i}.txt").write_text(f"other {i}\n")


# ---------------------------------------------------------------------------
# Benchmark: Phase 0 -- Dependency parsing deduplication
# ---------------------------------------------------------------------------

@pytest.mark.benchmark
class TestDependencyParsingPerf:
    """Benchmark APMPackage.from_apm_yml() with many dependencies."""

    def setup_method(self):
        clear_apm_yml_cache()

    @pytest.mark.parametrize("dep_count", [50, 100, 200])
    def test_from_apm_yml_parsing(self, tmp_path: Path, dep_count: int):
        """Parsing apm.yml with N dependencies should stay well under 1s."""
        apm_yml = _write_apm_yml_with_deps(tmp_path, dep_count)
        clear_apm_yml_cache()

        start = time.perf_counter()
        pkg = APMPackage.from_apm_yml(apm_yml)
        elapsed = time.perf_counter() - start

        assert pkg.name == "bench-pkg"
        apm_deps = pkg.get_apm_dependencies()
        assert len(apm_deps) == dep_count
        assert elapsed < 1.0, (
            f"Parsing {dep_count} deps took {elapsed:.3f}s (limit 1.0s)"
        )

    def test_cache_hit_after_parse(self, tmp_path: Path):
        """Second parse of same file should be near-instant (cache hit)."""
        apm_yml = _write_apm_yml_with_deps(tmp_path, 100)
        clear_apm_yml_cache()

        # Cold parse
        start = time.perf_counter()
        pkg1 = APMPackage.from_apm_yml(apm_yml)
        cold = time.perf_counter() - start

        # Warm parse (cache hit)
        start = time.perf_counter()
        pkg2 = APMPackage.from_apm_yml(apm_yml)
        warm = time.perf_counter() - start

        assert pkg1.name == pkg2.name
        assert len(pkg1.get_apm_dependencies()) == 100
        # Cache hit should be at least 2x faster (typically 100x+)
        assert warm < cold or warm < 0.001


# ---------------------------------------------------------------------------
# Benchmark: Phase 2 -- Uninstall engine children index
# ---------------------------------------------------------------------------

@pytest.mark.benchmark
class TestChildrenIndexPerf:
    """Benchmark _build_children_index for various lockfile sizes."""

    @pytest.mark.parametrize("dep_count", [50, 200, 500])
    def test_build_children_index(self, dep_count: int):
        """Building children index for N deps should be O(n) and fast."""
        lockfile = _build_fake_lockfile(dep_count)

        start = time.perf_counter()
        index = _build_children_index(lockfile)
        elapsed = time.perf_counter() - start

        # Every dep except the first has a parent, so index should be populated
        assert isinstance(index, dict)
        total_children = sum(len(v) for v in index.values())
        assert total_children == dep_count - 1
        assert elapsed < 0.1, (
            f"Building index for {dep_count} deps took {elapsed:.3f}s (limit 0.1s)"
        )

    def test_children_index_correctness(self):
        """Index maps parent_url -> list of child deps correctly."""
        lockfile = _FakeLockFile()
        parent = _FakeDep(repo_url="owner/parent", resolved_by=None)
        child_a = _FakeDep(repo_url="owner/child-a", resolved_by="owner/parent")
        child_b = _FakeDep(repo_url="owner/child-b", resolved_by="owner/parent")
        orphan = _FakeDep(repo_url="owner/orphan", resolved_by=None)

        for dep in [parent, child_a, child_b, orphan]:
            lockfile.dependencies[dep.get_unique_key()] = dep

        index = _build_children_index(lockfile)

        assert "owner/parent" in index
        child_urls = [d.repo_url for d in index["owner/parent"]]
        assert sorted(child_urls) == ["owner/child-a", "owner/child-b"]
        # orphan and parent have no resolved_by, so they are not children
        assert "owner/orphan" not in index


# ---------------------------------------------------------------------------
# Benchmark: Phase 6 -- Primitive discovery scanning
# ---------------------------------------------------------------------------

@pytest.mark.benchmark
class TestPrimitiveDiscoveryPerf:
    """Benchmark find_primitive_files with large directory trees."""

    @pytest.mark.parametrize("file_count", [100, 500])
    def test_find_primitive_files(self, tmp_path: Path, file_count: int):
        """Scanning N files for primitive patterns should stay fast."""
        _create_file_tree(tmp_path, file_count)
        patterns = ["**/*.instructions.md", "**/*.agent.md"]

        start = time.perf_counter()
        found = find_primitive_files(str(tmp_path), patterns)
        elapsed = time.perf_counter() - start

        # ~20% instructions + ~20% agents = ~40% match rate
        expected_min = file_count // 5  # at least the instructions count
        assert len(found) >= expected_min
        thresholds = {100: 0.5, 500: 2.0}
        limit = thresholds[file_count]
        assert elapsed < limit, (
            f"Scanning {file_count} files took {elapsed:.3f}s (limit {limit}s)"
        )

    def test_no_matches_returns_empty(self, tmp_path: Path):
        """Directory with no matching files returns empty list quickly."""
        for i in range(50):
            (tmp_path / f"readme-{i}.txt").write_text(f"txt {i}\n")

        start = time.perf_counter()
        found = find_primitive_files(str(tmp_path), ["**/*.instructions.md"])
        elapsed = time.perf_counter() - start

        assert found == []
        assert elapsed < 0.1


# ---------------------------------------------------------------------------
# Benchmark: Registry config cache
# ---------------------------------------------------------------------------

@pytest.mark.benchmark
class TestRegistryConfigCachePerf:
    """Verify that MCPServerOperations pre-loads installed IDs per runtime."""

    def test_installed_ids_preloaded_per_runtime(self):
        """check_servers_needing_installation reads config O(R) times, not O(S*R).

        We mock _get_installed_server_ids and the registry client to count
        how many times the config reader is called for 3 runtimes and
        10 server references.
        """
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations.__new__(MCPServerOperations)
        ops.registry_client = MagicMock()
        ops.registry_client.find_server_by_reference.return_value = {
            "id": "server-uuid-1",
            "name": "test-server",
        }

        call_count = {"n": 0}

        def fake_get_installed(runtimes):
            call_count["n"] += 1
            return {"already-installed-uuid"}

        ops._get_installed_server_ids = fake_get_installed

        runtimes = ["copilot", "vscode", "codex"]
        servers = [f"server-ref-{i}" for i in range(10)]

        start = time.perf_counter()
        result = ops.check_servers_needing_installation(runtimes, servers)
        elapsed = time.perf_counter() - start

        # Config reader should be called exactly len(runtimes) times, not
        # len(runtimes) * len(servers).
        assert call_count["n"] == len(runtimes), (
            f"Expected {len(runtimes)} config reads, got {call_count['n']}"
        )
        # All 10 servers should need installation (uuid mismatch)
        assert len(result) == 10
        assert elapsed < 0.1


# ---------------------------------------------------------------------------
# Benchmark: Console singleton
# ---------------------------------------------------------------------------

@pytest.mark.benchmark
class TestConsoleSingletonPerf:
    """Benchmark _get_console singleton -- repeated calls should be instant."""

    def setup_method(self):
        _reset_console()

    def teardown_method(self):
        _reset_console()

    def test_get_console_1000_calls(self):
        """1000 calls to _get_console() should complete near-instantly."""
        # First call creates the singleton
        console = _get_console()

        start = time.perf_counter()
        for _ in range(1000):
            c = _get_console()
        elapsed = time.perf_counter() - start

        # After the first call, every subsequent call is a simple
        # identity check on the module-level variable.
        assert elapsed < 0.05, (
            f"1000 _get_console() calls took {elapsed:.3f}s (limit 0.05s)"
        )
        # All calls should return the same object
        assert c is console

    def test_concurrent_singleton_identity(self):
        """50 threads calling _get_console() should all get the same object."""
        import threading
        _reset_console()
        results = []
        errors = []
        def worker():
            try:
                results.append(_get_console())
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"Thread errors: {errors}"
        assert len(set(id(c) for c in results)) == 1, "Multiple Console instances created"

    def test_reset_clears_singleton(self):
        """_reset_console() should force re-creation on next call."""
        c1 = _get_console()
        _reset_console()
        c2 = _get_console()
        # After reset, a new instance should be created
        assert c1 is not c2


# ---------------------------------------------------------------------------
# Benchmark: NullCommandLogger dispatch overhead
# ---------------------------------------------------------------------------

@pytest.mark.benchmark
class TestNullCommandLoggerPerf:
    """Verify NullCommandLogger dispatch overhead is negligible."""

    def test_null_logger_dispatch_overhead(self):
        """10,000 calls to NullCommandLogger methods should be near-instant."""
        from apm_cli.core.null_logger import NullCommandLogger
        logger = NullCommandLogger()
        start = time.perf_counter()
        for _ in range(10_000):
            logger.progress("msg")
            logger.verbose_detail("msg")
        elapsed = time.perf_counter() - start
        # 20,000 no-op/minimal method calls should complete in well under 1s
        assert elapsed < 1.0, f"NullCommandLogger dispatch took {elapsed:.3f}s for 20k calls"
