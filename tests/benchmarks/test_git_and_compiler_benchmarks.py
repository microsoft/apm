"""Performance benchmarks for iteration-2 P1 hot paths.

Covers three CPU-bound code-paths:

1. ``_parse_ls_remote_output()`` / ``_sort_remote_refs()`` -- git ref parsing
2. ``DistributedAgentsCompiler.analyze_directory_structure()`` -- directory analysis
3. ``MCPIntegrator.collect_transitive()`` -- transitive MCP dependency collection

Run with: uv run pytest tests/benchmarks/test_git_and_compiler_benchmarks.py -v -m benchmark
"""

import hashlib
import time
from dataclasses import dataclass, field  # noqa: F401
from pathlib import Path
from typing import Dict, List, Optional  # noqa: F401, UP035

import pytest

from apm_cli.compilation.distributed_compiler import (
    DirectoryMap,
    DistributedAgentsCompiler,
)
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef
from apm_cli.primitives.models import Instruction

# ---------------------------------------------------------------------------
# Helpers -- synthetic git ls-remote output
# ---------------------------------------------------------------------------


def _make_sha(index: int) -> str:
    """Generate a deterministic 40-hex-char SHA for a given index."""
    return hashlib.sha1(f"ref-{index}".encode()).hexdigest()  # noqa: S324


def _generate_ls_remote_output(ref_count: int) -> str:
    """Generate synthetic ``git ls-remote --tags --heads`` output.

    Produces a mix of tag refs and branch refs:
    - ~60% tags (semver: vX.Y.Z)
    - ~40% branches (feature-N, main, develop)

    Every 3rd tag includes an annotated tag pair (tag object + deref ``^{}``).
    """
    lines: list[str] = []
    tag_count = int(ref_count * 0.6)
    branch_count = ref_count - tag_count

    for i in range(tag_count):
        major = i // 100
        minor = (i // 10) % 10
        patch = i % 10
        tag_name = f"v{major}.{minor}.{patch}"
        sha = _make_sha(i)

        if i % 3 == 0:
            # Annotated tag: emit tag-object line then deref line
            tag_obj_sha = _make_sha(i + 10000)
            lines.append(f"{tag_obj_sha}\trefs/tags/{tag_name}")
            lines.append(f"{sha}\trefs/tags/{tag_name}^{{}}")
        else:
            lines.append(f"{sha}\trefs/tags/{tag_name}")

    for i in range(branch_count):
        sha = _make_sha(i + 5000)
        if i == 0:
            branch_name = "main"
        elif i == 1:
            branch_name = "develop"
        else:
            branch_name = f"feature-{i}"
        lines.append(f"{sha}\trefs/heads/{branch_name}")

    return "\n".join(lines) + "\n"


def _make_instruction(name: str, apply_to: str, tmp_path: Path) -> Instruction:
    """Build a minimal Instruction dataclass for benchmarking."""
    return Instruction(
        name=name,
        file_path=tmp_path / f"{name}.instructions.md",
        description=f"Benchmark instruction {name}",
        apply_to=apply_to,
        content=f"# {name}\nBenchmark content.",
    )


def _create_directory_tree(base: Path, dir_count: int) -> None:
    """Create a directory tree with ``dir_count`` subdirectories.

    Each directory gets 3 dummy files to simulate a realistic project.
    """
    for i in range(dir_count):
        # Distribute across a 2-level hierarchy
        group = f"group-{i // 10}"
        subdir = base / group / f"module-{i}"
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / "main.py").write_text(f"# module {i}\n")
        (subdir / "utils.py").write_text(f"# utils {i}\n")
        (subdir / "README.md").write_text(f"# Module {i}\n")


def _write_apm_yml_with_mcp(path: Path, pkg_name: str, mcp_servers: list[str]) -> Path:
    """Write an apm.yml with MCP dependencies and return its path."""
    lines = [
        f"name: {pkg_name}",
        "version: 1.0.0",
    ]
    if mcp_servers:
        lines.append("dependencies:")
        lines.append("  mcp:")
        for server in mcp_servers:
            lines.append(f"    - {server}")
    apm_yml = path / "apm.yml"
    apm_yml.write_text("\n".join(lines) + "\n")
    return apm_yml


def _setup_mcp_modules(tmp_path: Path, pkg_count: int, servers_per_pkg: int = 2) -> Path:
    """Create an apm_modules layout with ``pkg_count`` packages.

    Each package has ``servers_per_pkg`` MCP server entries.  A minimal
    apm.lock.yaml is written so ``collect_transitive`` can resolve the
    packages via the lock-derived fast path.

    Returns the apm_modules directory.
    """
    apm_modules = tmp_path / "apm_modules"

    for i in range(pkg_count):
        owner = "bench-org"
        repo = f"pkg-{i}"
        pkg_dir = apm_modules / owner / repo
        pkg_dir.mkdir(parents=True, exist_ok=True)
        servers = [f"io.bench/server-{i}-{j}" for j in range(servers_per_pkg)]
        _write_apm_yml_with_mcp(pkg_dir, f"pkg-{i}", servers)

    # Write a minimal apm.lock.yaml so collect_transitive uses lock-derived paths.
    # The on-disk format uses a *list* of dependency dicts under "dependencies:".
    lock_lines = [
        "lockfile_version: '1'",
        "generated_at: '2025-01-01T00:00:00+00:00'",
        "dependencies:",
    ]
    for i in range(pkg_count):
        owner = "bench-org"
        repo = f"pkg-{i}"
        lock_lines.append(f"  - repo_url: {owner}/{repo}")
        lock_lines.append(f"    resolved_commit: {_make_sha(i)}")
    lock_path = tmp_path / "apm.lock.yaml"
    lock_path.write_text("\n".join(lock_lines) + "\n")

    return apm_modules


# ---------------------------------------------------------------------------
# P1 #1: _parse_ls_remote_output() + _sort_remote_refs()
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestParseLsRemoteThroughput:
    """Benchmark GitHubPackageDownloader._parse_ls_remote_output() at various scales."""

    @pytest.mark.parametrize(
        "ref_count, ceiling",
        [
            (50, 2.5),
            (200, 5.0),
            (500, 10.0),
        ],
    )
    def test_parse_throughput(self, ref_count: int, ceiling: float):
        """Parsing ls-remote output with N refs should stay within ceiling."""
        output = _generate_ls_remote_output(ref_count)

        start = time.perf_counter()
        refs = GitHubPackageDownloader._parse_ls_remote_output(output)
        elapsed = time.perf_counter() - start

        # Tag count is ~60% of ref_count, but annotated tags produce one
        # RemoteRef per unique tag name (not per line).  Branch count ~40%.
        assert len(refs) > 0
        tag_refs = [r for r in refs if r.ref_type == GitReferenceType.TAG]
        branch_refs = [r for r in refs if r.ref_type == GitReferenceType.BRANCH]
        assert len(tag_refs) + len(branch_refs) == len(refs)
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        assert elapsed < ceiling, (
            f"Parsing {ref_count} refs took {elapsed:.3f}s, "
            f"expected < {ceiling}s (generous ceiling)"
        )


@pytest.mark.benchmark
class TestSortRemoteRefsThroughput:
    """Benchmark GitHubPackageDownloader._sort_remote_refs() with semver ordering."""

    @pytest.mark.parametrize(
        "ref_count, ceiling",
        [
            (50, 2.5),
            (200, 5.0),
            (500, 10.0),
        ],
    )
    def test_sort_throughput(self, ref_count: int, ceiling: float):
        """Sorting N pre-parsed refs with semver key within ceiling."""
        output = _generate_ls_remote_output(ref_count)
        refs = GitHubPackageDownloader._parse_ls_remote_output(output)

        start = time.perf_counter()
        sorted_refs = GitHubPackageDownloader._sort_remote_refs(refs)
        elapsed = time.perf_counter() - start

        assert len(sorted_refs) == len(refs)
        # Tags should come before branches in sorted output
        first_branch_idx = None
        for idx, r in enumerate(sorted_refs):
            if r.ref_type == GitReferenceType.BRANCH:
                first_branch_idx = idx
                break
        if first_branch_idx is not None:
            # All refs after first branch should also be branches
            for r in sorted_refs[first_branch_idx:]:
                assert r.ref_type == GitReferenceType.BRANCH
        else:
            # All-tags input: verify all entries are tags
            assert all(r.ref_type == GitReferenceType.TAG for r in sorted_refs), (
                "Expected all-tags output when no branches present"
            )
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        assert elapsed < ceiling, (
            f"Sorting {ref_count} refs took {elapsed:.3f}s, "
            f"expected < {ceiling}s (generous ceiling)"
        )

    def test_sort_semver_order(self):
        """Sorted tags should be in descending semver order."""
        refs = [
            RemoteRef(name="v1.0.0", ref_type=GitReferenceType.TAG, commit_sha="a" * 40),
            RemoteRef(name="v2.0.0", ref_type=GitReferenceType.TAG, commit_sha="b" * 40),
            RemoteRef(name="v1.1.0", ref_type=GitReferenceType.TAG, commit_sha="c" * 40),
            RemoteRef(name="v0.9.0", ref_type=GitReferenceType.TAG, commit_sha="d" * 40),
        ]

        sorted_refs = GitHubPackageDownloader._sort_remote_refs(refs)
        tag_names = [r.name for r in sorted_refs]
        # Descending semver: v2.0.0, v1.1.0, v1.0.0, v0.9.0
        assert tag_names == ["v2.0.0", "v1.1.0", "v1.0.0", "v0.9.0"]


@pytest.mark.benchmark
class TestParseLsRemoteCorrectness:
    """Verify _parse_ls_remote_output handles edge cases correctly."""

    def test_annotated_tags_use_deref_sha(self):
        """Annotated tag ^{} line overrides the tag-object SHA."""
        output = (
            "aaaa000000000000000000000000000000000000\trefs/tags/v1.0.0\n"
            "bbbb000000000000000000000000000000000000\trefs/tags/v1.0.0^{}\n"
        )
        refs = GitHubPackageDownloader._parse_ls_remote_output(output)
        assert len(refs) == 1
        assert refs[0].name == "v1.0.0"
        assert refs[0].ref_type == GitReferenceType.TAG
        # Should use the deref (commit) SHA, not the tag-object SHA
        assert refs[0].commit_sha == "bbbb000000000000000000000000000000000000"

    def test_head_ref_ignored(self):
        """HEAD ref line (no refs/tags/ or refs/heads/ prefix) is ignored."""
        output = (
            "cccc000000000000000000000000000000000000\tHEAD\n"
            "dddd000000000000000000000000000000000000\trefs/heads/main\n"
        )
        refs = GitHubPackageDownloader._parse_ls_remote_output(output)
        assert len(refs) == 1
        assert refs[0].name == "main"
        assert refs[0].ref_type == GitReferenceType.BRANCH

    def test_non_semver_branches(self):
        """Non-semver branch names are parsed as BRANCH type."""
        output = (
            "eeee000000000000000000000000000000000000\trefs/heads/feature/my-branch\n"
            "ffff000000000000000000000000000000000000\trefs/heads/fix-123\n"
        )
        refs = GitHubPackageDownloader._parse_ls_remote_output(output)
        assert len(refs) == 2
        names = {r.name for r in refs}
        assert "feature/my-branch" in names
        assert "fix-123" in names
        for r in refs:
            assert r.ref_type == GitReferenceType.BRANCH

    def test_empty_output_returns_empty(self):
        """Empty string produces an empty ref list."""
        assert GitHubPackageDownloader._parse_ls_remote_output("") == []

    def test_blank_lines_skipped(self):
        """Blank lines and whitespace-only lines are ignored."""
        output = "\n   \naaaa000000000000000000000000000000000000\trefs/tags/v1.0.0\n\n"
        refs = GitHubPackageDownloader._parse_ls_remote_output(output)
        assert len(refs) == 1

    def test_mixed_tags_and_branches(self):
        """Output with both tags and branches parses both correctly."""
        output = (
            "1111000000000000000000000000000000000000\trefs/tags/v1.0.0\n"
            "2222000000000000000000000000000000000000\trefs/heads/main\n"
            "3333000000000000000000000000000000000000\trefs/tags/v2.0.0\n"
            "4444000000000000000000000000000000000000\trefs/heads/develop\n"
        )
        refs = GitHubPackageDownloader._parse_ls_remote_output(output)
        tags = [r for r in refs if r.ref_type == GitReferenceType.TAG]
        branches = [r for r in refs if r.ref_type == GitReferenceType.BRANCH]
        assert len(tags) == 2
        assert len(branches) == 2


# ---------------------------------------------------------------------------
# P1 #2: DistributedAgentsCompiler.analyze_directory_structure()
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestAnalyzeDirectoryStructureThroughput:
    """Benchmark analyze_directory_structure() with varying project sizes."""

    @pytest.mark.parametrize(
        "dir_count, ceiling",
        [
            (10, 5.0),
            (50, 10.0),
            (200, 25.0),
        ],
    )
    def test_throughput_by_project_size(self, tmp_path: Path, dir_count: int, ceiling: float):
        """analyze_directory_structure with N directories within ceiling."""
        _create_directory_tree(tmp_path, dir_count)

        # Build instructions with applyTo patterns spanning the tree
        instructions = []
        for i in range(min(dir_count, 20)):
            group = f"group-{i // 10}"
            pattern = f"{group}/module-{i}/**/*.py"
            instructions.append(_make_instruction(f"instr-{i}", pattern, tmp_path))
        # Add a few global patterns
        instructions.append(_make_instruction("global-md", "**/*.md", tmp_path))
        instructions.append(_make_instruction("root-py", "*.py", tmp_path))

        compiler = DistributedAgentsCompiler(base_dir=str(tmp_path))

        start = time.perf_counter()
        result = compiler.analyze_directory_structure(instructions)
        elapsed = time.perf_counter() - start

        assert isinstance(result, DirectoryMap)
        assert len(result.directories) > 0
        assert len(result.depth_map) > 0
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        assert elapsed < ceiling, (
            f"analyze_directory_structure({dir_count} dirs) took "
            f"{elapsed:.3f}s, expected < {ceiling}s (generous ceiling)"
        )


@pytest.mark.benchmark
class TestAnalyzeDirectoryStructureCorrectness:
    """Verify pattern-to-directory mapping correctness."""

    def test_src_pattern_maps_to_src(self, tmp_path: Path):
        """Pattern 'src/**/*.py' should map to the src directory."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "main.py").write_text("# main\n")

        instructions = [
            _make_instruction("src-py", "src/**/*.py", tmp_path),
        ]

        compiler = DistributedAgentsCompiler(base_dir=str(tmp_path))
        result = compiler.analyze_directory_structure(instructions)

        assert isinstance(result, DirectoryMap)
        # The pattern should create a mapping for the src directory
        src_abs = compiler.base_dir / "src"
        assert src_abs in result.directories
        assert "src/**/*.py" in result.directories[src_abs]

    def test_global_pattern_maps_to_base(self, tmp_path: Path):
        """Pattern '**/*.md' should map to the base directory."""
        instructions = [
            _make_instruction("all-md", "**/*.md", tmp_path),
        ]

        compiler = DistributedAgentsCompiler(base_dir=str(tmp_path))
        result = compiler.analyze_directory_structure(instructions)

        assert isinstance(result, DirectoryMap)
        # Global pattern (**/*) maps to "." which resolves to base_dir
        assert compiler.base_dir in result.directories
        assert "**/*.md" in result.directories[compiler.base_dir]

    def test_multiple_patterns_accumulate(self, tmp_path: Path):
        """Multiple instructions with different patterns create multiple entries."""
        for d in ["src", "tests", "docs"]:
            (tmp_path / d).mkdir()

        instructions = [
            _make_instruction("src-py", "src/**/*.py", tmp_path),
            _make_instruction("tests-py", "tests/**/*.py", tmp_path),
            _make_instruction("docs-md", "docs/**/*.md", tmp_path),
        ]

        compiler = DistributedAgentsCompiler(base_dir=str(tmp_path))
        result = compiler.analyze_directory_structure(instructions)

        assert isinstance(result, DirectoryMap)
        src_abs = compiler.base_dir / "src"
        tests_abs = compiler.base_dir / "tests"
        docs_abs = compiler.base_dir / "docs"
        assert src_abs in result.directories
        assert tests_abs in result.directories
        assert docs_abs in result.directories

    def test_instruction_without_apply_to_skipped(self, tmp_path: Path):
        """Instructions with empty apply_to should not add pattern-based dirs."""
        instructions = [
            Instruction(
                name="no-pattern",
                file_path=tmp_path / "no-pattern.md",
                description="No pattern",
                apply_to="",
                content="# no pattern",
            ),
        ]

        compiler = DistributedAgentsCompiler(base_dir=str(tmp_path))
        result = compiler.analyze_directory_structure(instructions)

        assert isinstance(result, DirectoryMap)
        # Base dir always present, but no extra pattern-derived dirs
        assert compiler.base_dir in result.directories

    def test_depth_and_parent_populated(self, tmp_path: Path):
        """Depth and parent maps should be populated for pattern directories."""
        (tmp_path / "src").mkdir()

        instructions = [
            _make_instruction("src-py", "src/**/*.py", tmp_path),
        ]

        compiler = DistributedAgentsCompiler(base_dir=str(tmp_path))
        result = compiler.analyze_directory_structure(instructions)

        src_abs = compiler.base_dir / "src"
        assert src_abs in result.depth_map
        assert result.depth_map[src_abs] >= 1
        assert src_abs in result.parent_map


# ---------------------------------------------------------------------------
# P1 #3: MCPIntegrator.collect_transitive()
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestCollectTransitiveThroughput:
    """Benchmark MCPIntegrator.collect_transitive() at various scales.

    Note: setup_method clears the apm.yml cache before each test to ensure
    isolation between parametrised runs.
    """

    def setup_method(self):
        clear_apm_yml_cache()

    @pytest.mark.parametrize(
        "pkg_count, ceiling",
        [
            (5, 5.0),
            (20, 15.0),
            (50, 25.0),
        ],
    )
    def test_throughput_by_dependency_count(self, tmp_path: Path, pkg_count: int, ceiling: float):
        """collect_transitive with N packages (2 MCP servers each) within ceiling."""
        servers_per_pkg = 2
        apm_modules = _setup_mcp_modules(tmp_path, pkg_count, servers_per_pkg)
        lock_path = tmp_path / "apm.lock.yaml"

        start = time.perf_counter()
        collected = MCPIntegrator.collect_transitive(
            apm_modules_dir=apm_modules,
            lock_path=lock_path,
        )
        elapsed = time.perf_counter() - start

        expected_count = pkg_count * servers_per_pkg
        assert len(collected) == expected_count, (
            f"Expected {expected_count} MCP deps, got {len(collected)}"
        )
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        assert elapsed < ceiling, (
            f"collect_transitive({pkg_count} pkgs) took {elapsed:.3f}s, "
            f"expected < {ceiling}s (generous ceiling)"
        )


@pytest.mark.benchmark
class TestCollectTransitiveCorrectness:
    """Verify collect_transitive correctness for MCP dependency collection."""

    def setup_method(self):
        clear_apm_yml_cache()

    def test_empty_modules_returns_empty(self, tmp_path: Path):
        """Non-existent apm_modules dir returns empty list."""
        result = MCPIntegrator.collect_transitive(
            apm_modules_dir=tmp_path / "nonexistent",
        )
        assert result == []

    def test_packages_without_mcp_return_empty(self, tmp_path: Path):
        """Packages with no MCP section produce zero collected deps."""
        clear_apm_yml_cache()
        apm_modules = tmp_path / "apm_modules"
        pkg_dir = apm_modules / "org" / "no-mcp-pkg"
        pkg_dir.mkdir(parents=True)
        # Write apm.yml without MCP deps
        (pkg_dir / "apm.yml").write_text("name: no-mcp-pkg\nversion: 1.0.0\n")

        # No lockfile: falls back to rglob scan
        result = MCPIntegrator.collect_transitive(
            apm_modules_dir=apm_modules,
        )
        assert result == []

    def test_collects_from_all_packages(self, tmp_path: Path):
        """Each package's MCP servers appear in the collected result."""
        clear_apm_yml_cache()
        apm_modules = _setup_mcp_modules(tmp_path, pkg_count=3, servers_per_pkg=2)
        lock_path = tmp_path / "apm.lock.yaml"

        collected = MCPIntegrator.collect_transitive(
            apm_modules_dir=apm_modules,
            lock_path=lock_path,
        )

        names = [dep.name for dep in collected]
        # Each package contributes 2 servers: server-{pkg}-0, server-{pkg}-1
        for i in range(3):
            for j in range(2):
                assert f"io.bench/server-{i}-{j}" in names

    def test_fallback_scan_without_lockfile(self, tmp_path: Path):
        """Without a lockfile, collect_transitive falls back to rglob scan."""
        clear_apm_yml_cache()
        apm_modules = tmp_path / "apm_modules"
        pkg_dir = apm_modules / "org" / "fallback-pkg"
        pkg_dir.mkdir(parents=True)
        _write_apm_yml_with_mcp(pkg_dir, "fallback-pkg", ["io.bench/fb-server"])

        # No lockfile passed
        collected = MCPIntegrator.collect_transitive(
            apm_modules_dir=apm_modules,
        )

        assert len(collected) == 1
        assert collected[0].name == "io.bench/fb-server"

    def test_lock_derived_path_filters_stale(self, tmp_path: Path):
        """Packages NOT in the lockfile are skipped when lock_path is provided."""
        clear_apm_yml_cache()
        apm_modules = tmp_path / "apm_modules"

        # Package in lockfile
        locked_dir = apm_modules / "org" / "locked-pkg"
        locked_dir.mkdir(parents=True)
        _write_apm_yml_with_mcp(locked_dir, "locked-pkg", ["io.bench/locked-server"])

        # Stale package NOT in lockfile
        stale_dir = apm_modules / "org" / "stale-pkg"
        stale_dir.mkdir(parents=True)
        _write_apm_yml_with_mcp(stale_dir, "stale-pkg", ["io.bench/stale-server"])

        # Lockfile only references locked-pkg
        lock_lines = [
            "lockfile_version: '1'",
            "generated_at: '2025-01-01T00:00:00+00:00'",
            "dependencies:",
            "  - repo_url: org/locked-pkg",
            f"    resolved_commit: {_make_sha(0)}",
        ]
        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text("\n".join(lock_lines) + "\n")

        collected = MCPIntegrator.collect_transitive(
            apm_modules_dir=apm_modules,
            lock_path=lock_path,
        )

        names = [dep.name for dep in collected]
        assert "io.bench/locked-server" in names
        assert "io.bench/stale-server" not in names
