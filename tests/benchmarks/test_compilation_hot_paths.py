"""Performance benchmarks for APM compilation and integration hot paths.

Covers the key bottlenecks in the compilation / integration lifecycle:

1. ``compute_deployed_hashes()`` -- per-file content hashing at scale
2. ``ContextOptimizer.optimize_instruction_placement()`` -- glob matching + dir walk
3. ``UnifiedLinkResolver._rewrite_markdown_links()`` -- regex rewrite throughput
4. ``BaseIntegrator.partition_managed_files()`` -- trie-based file routing
5. ``LockFile`` round-trip -- to_yaml() + from_yaml() serialization
6. ``UnifiedLinkResolver.register_contexts()`` -- context registry index build
7. ``compute_deployed_hashes()`` correctness -- sanity check on hash format

Run with: uv run pytest tests/benchmarks/test_compilation_hot_paths.py -v -m benchmark
"""

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set  # noqa: F401, UP035

import pytest

from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.compilation.link_resolver import (
    LinkResolutionContext,
    UnifiedLinkResolver,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.phases.lockfile import compute_deployed_hashes
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.primitives.models import Context, Instruction  # noqa: F401
from apm_cli.utils.content_hash import compute_file_hash  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers to build synthetic data
# ---------------------------------------------------------------------------


def _populate_flat_files(base: Path, file_count: int) -> list[str]:
    """Create *file_count* ~1 KB files under *base* and return relative paths."""
    base.mkdir(parents=True, exist_ok=True)
    rel_paths: list[str] = []
    for i in range(file_count):
        subdir = base / f"sub-{i // 20}"
        subdir.mkdir(parents=True, exist_ok=True)
        fname = f"file-{i}.dat"
        fpath = subdir / fname
        fpath.write_bytes(os.urandom(1024))
        # Relative path from *base*
        rel_paths.append(str(fpath.relative_to(base)))
    return rel_paths


def _create_dir_tree(base: Path, dir_count: int, files_per_dir: int = 3) -> None:
    """Create a directory tree with *dir_count* directories under *base*.

    Each directory gets *files_per_dir* small files so that os.walk and
    glob have content to traverse.
    """
    for d in range(dir_count):
        subdir = base / f"src/module-{d}"
        subdir.mkdir(parents=True, exist_ok=True)
        for f in range(files_per_dir):
            (subdir / f"file-{f}.py").write_text(f"# module {d} file {f}\n")


def _build_instructions(count: int) -> list[Instruction]:
    """Build *count* synthetic Instruction objects with varied apply_to patterns."""
    instructions: list[Instruction] = []
    patterns = [
        "src/**/*.py",
        "tests/**/*.py",
        "src/module-*/*.py",
        "**/*.md",
        "docs/**/*",
    ]
    for i in range(count):
        instructions.append(
            Instruction(
                name=f"instruction-{i}",
                file_path=Path(f"test-{i}.instructions.md"),
                description=f"Test instruction {i}",
                apply_to=patterns[i % len(patterns)],
                content=f"Instruction content for rule {i}. Follow this guideline.",
                source="local",
            )
        )
    return instructions


def _generate_managed_paths(count: int) -> set[str]:
    """Generate *count* realistic managed-file paths across targets."""
    prefixes = [
        ".github/prompts/p{i}.prompt.md",
        ".github/agents/a{i}.agent.md",
        ".github/instructions/i{i}.instructions.md",
        ".cursor/rules/r{i}.mdc",
        ".github/skills/s{i}/SKILL.md",
        ".github/hooks/h{i}.hook.md",
    ]
    paths: set[str] = set()
    for i in range(count):
        template = prefixes[i % len(prefixes)]
        paths.add(template.format(i=i))
    return paths


def _make_rich_lockfile(dep_count: int) -> LockFile:
    """Build a LockFile with *dep_count* deps, each carrying deployed files and hashes."""
    lf = LockFile()
    for i in range(dep_count):
        dep = LockedDependency(
            repo_url=f"https://github.com/org/pkg-{i}",
            depth=(i % 5) + 1,
            deployed_files=[f".github/agents/agent-{i}-{j}.agent.md" for j in range(10)],
            deployed_file_hashes={
                f".github/agents/agent-{i}-{j}.agent.md": f"sha256:{'ab' * 32}" for j in range(10)
            },
        )
        lf.add_dependency(dep)
    # Attach MCP and local metadata
    lf.mcp_servers = [f"server-{s}" for s in range(10)]
    lf.mcp_configs = {f"config-{c}": {"key": f"val-{c}"} for c in range(5)}
    lf.local_deployed_files = [f"local-{n}.md" for n in range(20)]
    lf.local_deployed_file_hashes = {f"local-{n}.md": f"sha256:{'cd' * 32}" for n in range(20)}
    return lf


@dataclass
class _FakeContext:
    """Minimal stand-in for a context object used by register_contexts."""

    file_path: Path
    source: str | None = None


@dataclass
class _FakePrimitiveCollection:
    """Minimal stand-in for PrimitiveCollection."""

    contexts: list[_FakeContext]


# ---------------------------------------------------------------------------
# Benchmark 1: compute_deployed_hashes() throughput
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestComputeDeployedHashesPerf:
    """Benchmark compute_deployed_hashes() across file counts."""

    @pytest.mark.parametrize("file_count", [100, 500, 2000])
    def test_hash_throughput(self, tmp_path: Path, file_count: int):
        """Hashing N x 1 KB deployed files should scale linearly."""
        rel_paths = _populate_flat_files(tmp_path, file_count)

        start = time.perf_counter()
        result = compute_deployed_hashes(rel_paths, tmp_path)
        elapsed = time.perf_counter() - start

        assert len(result) == file_count
        # Spot-check format
        first_hash = next(iter(result.values()))
        assert first_hash.startswith("sha256:")
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        thresholds = {100: 5.0, 500: 15.0, 2000: 50.0}
        limit = thresholds[file_count]
        assert elapsed < limit, (
            f"Hashing {file_count} files took {elapsed:.3f}s, "
            f"expected < {limit}s (generous ceiling)"
        )


# ---------------------------------------------------------------------------
# Benchmark 2: ContextOptimizer.optimize_instruction_placement()
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestOptimizeInstructionPlacementPerf:
    """Benchmark optimize_instruction_placement() with varying scale."""

    @pytest.mark.parametrize(
        "instr_count, dir_count",
        [
            (10, 20),
            (50, 100),
            (200, 200),
        ],
    )
    def test_placement_latency(self, tmp_path: Path, instr_count: int, dir_count: int):
        """Optimizing N instructions over M directories should finish in time."""
        _create_dir_tree(tmp_path, dir_count)
        instructions = _build_instructions(instr_count)
        optimizer = ContextOptimizer(base_dir=str(tmp_path), exclude_patterns=None)

        start = time.perf_counter()
        placement = optimizer.optimize_instruction_placement(instructions)
        elapsed = time.perf_counter() - start

        assert isinstance(placement, dict)
        # Every instruction should appear in at least one directory
        placed_instructions = set()
        for instrs in placement.values():
            for instr in instrs:
                placed_instructions.add(instr.name)
        assert len(placed_instructions) == instr_count

        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        thresholds = {(10, 20): 10.0, (50, 100): 25.0, (200, 200): 20.0}
        limit = thresholds[(instr_count, dir_count)]
        assert elapsed < limit, (
            f"Optimizing {instr_count} instructions over {dir_count} dirs "
            f"took {elapsed:.3f}s, expected < {limit}s (generous ceiling)"
        )


# ---------------------------------------------------------------------------
# Benchmark 3: UnifiedLinkResolver._rewrite_markdown_links()
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestRewriteMarkdownLinksPerf:
    """Benchmark _rewrite_markdown_links() for context link rewriting."""

    @pytest.mark.parametrize("link_count", [5, 20, 50])
    def test_rewrite_latency(self, tmp_path: Path, link_count: int):
        """Rewriting N context links in markdown content should be fast."""
        resolver = UnifiedLinkResolver(base_dir=tmp_path)

        # Pre-populate context registry with 50 entries
        context_dir = tmp_path / ".apm" / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        for i in range(50):
            ctx_file = context_dir / f"ctx-{i}.context.md"
            ctx_file.write_text(f"# Context {i}\nContent for context {i}.\n")
            resolver.context_registry[ctx_file.name] = ctx_file

        # Build markdown content with link_count context links
        lines = ["# Test Document\n\n"]
        for i in range(link_count):
            ctx_name = f"ctx-{i % 50}.context.md"
            lines.append(f"See [{ctx_name}]({ctx_name}) for details on item {i}.\n\n")
        lines.append("End of document.\n")
        content = "".join(lines)

        source_file = tmp_path / "test.agent.md"
        source_file.write_text(content)

        ctx = LinkResolutionContext(
            source_file=source_file,
            source_location=source_file.parent,
            target_location=tmp_path,
            base_dir=tmp_path,
            available_contexts=dict(resolver.context_registry),
        )

        start = time.perf_counter()
        result = resolver._rewrite_markdown_links(content, ctx)
        elapsed = time.perf_counter() - start

        assert isinstance(result, str)
        assert len(result) > 0
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        thresholds = {5: 2.5, 20: 5.0, 50: 10.0}
        limit = thresholds[link_count]
        assert elapsed < limit, (
            f"Rewriting {link_count} links took {elapsed:.3f}s, "
            f"expected < {limit}s (generous ceiling)"
        )

    def test_no_context_links_passthrough(self, tmp_path: Path):
        """Content without context links should pass through unchanged."""
        resolver = UnifiedLinkResolver(base_dir=tmp_path)
        content = (
            "# Plain Document\n\n"
            "No context links here.\n"
            "[External](https://example.com)\n"
            "[Internal](readme.md)\n"
        )
        source_file = tmp_path / "test.md"
        source_file.write_text(content)

        ctx = LinkResolutionContext(
            source_file=source_file,
            source_location=source_file.parent,
            target_location=tmp_path,
            base_dir=tmp_path,
            available_contexts={},
        )

        start = time.perf_counter()
        result = resolver._rewrite_markdown_links(content, ctx)
        elapsed = time.perf_counter() - start

        # Non-context links should remain unchanged
        assert "[External](https://example.com)" in result
        # Generous ceiling -- catches catastrophic regressions only.
        assert elapsed < 2.0, f"Passthrough took {elapsed:.3f}s, expected < 2.0s (generous ceiling)"


# ---------------------------------------------------------------------------
# Benchmark 4: partition_managed_files() at scale
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestPartitionManagedFilesPerf:
    """Benchmark BaseIntegrator.partition_managed_files() routing."""

    @pytest.mark.parametrize("file_count", [100, 1000, 5000])
    def test_partition_latency(self, file_count: int):
        """Routing N managed files to buckets via trie should be fast."""
        managed = _generate_managed_paths(file_count)

        start = time.perf_counter()
        buckets = BaseIntegrator.partition_managed_files(managed)
        elapsed = time.perf_counter() - start

        assert isinstance(buckets, dict)
        # Every path should land in exactly one bucket
        total_routed = sum(len(v) for v in buckets.values())
        assert total_routed == file_count, f"Expected {file_count} routed files, got {total_routed}"
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        thresholds = {100: 2.5, 1000: 5.0, 5000: 15.0}
        limit = thresholds[file_count]
        assert elapsed < limit, (
            f"Partitioning {file_count} files took {elapsed:.3f}s, "
            f"expected < {limit}s (generous ceiling)"
        )

    def test_partition_correctness(self):
        """Known paths land in the expected buckets."""
        managed = {
            ".github/prompts/p1.prompt.md",
            ".github/agents/a1.agent.md",
            ".github/skills/s1/SKILL.md",
            ".github/hooks/h1.hook.md",
        }
        buckets = BaseIntegrator.partition_managed_files(managed)

        # Skills and hooks have their own cross-target buckets
        assert ".github/skills/s1/SKILL.md" in buckets.get("skills", set())
        assert ".github/hooks/h1.hook.md" in buckets.get("hooks", set())


# ---------------------------------------------------------------------------
# Benchmark 5: LockFile round-trip (to_yaml + from_yaml)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestLockFileRoundTripPerf:
    """Benchmark LockFile serialization + deserialization round-trip."""

    @pytest.mark.parametrize("dep_count", [50, 200, 500])
    def test_round_trip_latency(self, dep_count: int):
        """Round-tripping a lockfile with N deps should stay bounded."""
        lf = _make_rich_lockfile(dep_count)

        start = time.perf_counter()
        yaml_str = lf.to_yaml()
        lf2 = LockFile.from_yaml(yaml_str)
        elapsed = time.perf_counter() - start

        assert isinstance(yaml_str, str)
        assert "lockfile_version" in yaml_str
        # The deserialized lockfile should have the same dep count
        # (from_yaml may add a synthetic "." entry for local_deployed_files)
        real_deps = {k: v for k, v in lf2.dependencies.items() if k != "."}
        assert len(real_deps) == dep_count
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        thresholds = {50: 10.0, 200: 25.0, 500: 50.0}
        limit = thresholds[dep_count]
        assert elapsed < limit, (
            f"Round-trip for {dep_count} deps took {elapsed:.3f}s, "
            f"expected < {limit}s (generous ceiling)"
        )

    def test_round_trip_preserves_data(self):
        """Key fields survive the round-trip without data loss."""
        lf = _make_rich_lockfile(10)
        yaml_str = lf.to_yaml()
        lf2 = LockFile.from_yaml(yaml_str)

        assert lf2.lockfile_version == lf.lockfile_version
        assert lf2.mcp_servers == sorted(lf.mcp_servers)
        assert len(lf2.local_deployed_files) == len(lf.local_deployed_files)

        # Spot-check a dependency
        real_deps_orig = {k: v for k, v in lf.dependencies.items() if k != "."}
        real_deps_rt = {k: v for k, v in lf2.dependencies.items() if k != "."}
        orig_key = next(iter(real_deps_orig))
        assert orig_key in real_deps_rt
        assert real_deps_rt[orig_key].repo_url == real_deps_orig[orig_key].repo_url


# ---------------------------------------------------------------------------
# Benchmark 6: register_contexts() index building
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestRegisterContextsPerf:
    """Benchmark UnifiedLinkResolver.register_contexts() index build."""

    @pytest.mark.parametrize("context_count", [100, 500])
    def test_register_latency(self, tmp_path: Path, context_count: int):
        """Registering N contexts into the lookup index should be fast."""
        resolver = UnifiedLinkResolver(base_dir=tmp_path)

        contexts: list[_FakeContext] = []
        for i in range(context_count):
            source = f"dependency:org/repo-{i}" if i % 2 == 0 else "local"
            contexts.append(
                _FakeContext(
                    file_path=Path(f".apm/context/ctx-{i}.context.md"),
                    source=source,
                )
            )
        primitives = _FakePrimitiveCollection(contexts=contexts)

        start = time.perf_counter()
        resolver.register_contexts(primitives)
        elapsed = time.perf_counter() - start

        # Every context should be registered by simple filename
        assert len(resolver.context_registry) >= context_count
        # Dependency contexts get a second qualified-name entry
        dep_count = sum(1 for c in contexts if c.source.startswith("dependency:"))
        assert len(resolver.context_registry) >= context_count + dep_count

        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        thresholds = {100: 2.5, 500: 5.0}
        limit = thresholds[context_count]
        assert elapsed < limit, (
            f"Registering {context_count} contexts took {elapsed:.3f}s, "
            f"expected < {limit}s (generous ceiling)"
        )

    def test_registry_lookup_correctness(self, tmp_path: Path):
        """Registered contexts should be findable by filename and qualified name."""
        resolver = UnifiedLinkResolver(base_dir=tmp_path)
        contexts = [
            _FakeContext(
                file_path=Path(".apm/context/api-standards.context.md"),
                source="dependency:company/standards",
            ),
            _FakeContext(
                file_path=Path(".apm/context/local-rules.context.md"),
                source="local",
            ),
        ]
        primitives = _FakePrimitiveCollection(contexts=contexts)
        resolver.register_contexts(primitives)

        # Simple filename lookup
        assert "api-standards.context.md" in resolver.context_registry
        assert "local-rules.context.md" in resolver.context_registry
        # Qualified name lookup for dependency
        assert "company/standards:api-standards.context.md" in resolver.context_registry


# ---------------------------------------------------------------------------
# Benchmark 7: compute_deployed_hashes() correctness
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestComputeDeployedHashesCorrectness:
    """Sanity: deployed hashes have correct format and are content-sensitive."""

    def test_hash_format(self, tmp_path: Path):
        """Each hash should start with 'sha256:' and be 71 chars total."""
        for i in range(5):
            (tmp_path / f"file-{i}.md").write_text(f"content {i}\n")

        rel_paths = [f"file-{i}.md" for i in range(5)]
        result = compute_deployed_hashes(rel_paths, tmp_path)

        assert len(result) == 5
        for rp, h in result.items():
            assert h.startswith("sha256:"), f"Hash for {rp} missing prefix"
            # sha256: (7 chars) + 64 hex chars = 71
            assert len(h) == 71, f"Hash for {rp} has unexpected length {len(h)}"

    def test_content_sensitivity(self, tmp_path: Path):
        """Changing file content must change the hash."""
        f = tmp_path / "data.md"
        f.write_text("version-1\n")
        h1 = compute_deployed_hashes(["data.md"], tmp_path)

        f.write_text("version-2\n")
        h2 = compute_deployed_hashes(["data.md"], tmp_path)

        assert h1["data.md"] != h2["data.md"]

    def test_missing_file_omitted(self, tmp_path: Path):
        """Non-existent paths should be silently omitted from output."""
        (tmp_path / "exists.md").write_text("present\n")
        result = compute_deployed_hashes(["exists.md", "missing.md"], tmp_path)
        assert "exists.md" in result
        assert "missing.md" not in result


# ---------------------------------------------------------------------------
# Benchmark 8: ContextOptimizer with empty instructions
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestContextOptimizerEdgeCases:
    """Edge-case benchmarks for ContextOptimizer."""

    def test_empty_instructions(self, tmp_path: Path):
        """Optimizing zero instructions should return empty dict instantly."""
        _create_dir_tree(tmp_path, 10)
        optimizer = ContextOptimizer(base_dir=str(tmp_path), exclude_patterns=None)

        start = time.perf_counter()
        placement = optimizer.optimize_instruction_placement([])
        elapsed = time.perf_counter() - start

        assert placement == {}
        # Generous ceiling -- catches catastrophic regressions only.
        assert elapsed < 5.0, (
            f"Empty instructions took {elapsed:.3f}s, expected < 5.0s (generous ceiling)"
        )

    def test_global_instruction_placement(self, tmp_path: Path):
        """Instructions without apply_to pattern go to root directory."""
        _create_dir_tree(tmp_path, 5)
        instr = Instruction(
            name="global-rule",
            file_path=Path("global.instructions.md"),
            description="Applies everywhere",
            apply_to="",
            content="Follow this global rule.",
            source="local",
        )
        optimizer = ContextOptimizer(base_dir=str(tmp_path), exclude_patterns=None)

        placement = optimizer.optimize_instruction_placement([instr])

        assert len(placement) >= 1
        # The global instruction should be placed at the resolved base_dir
        placed_names = set()
        for instrs in placement.values():
            for i in instrs:
                placed_names.add(i.name)
        assert "global-rule" in placed_names


# ---------------------------------------------------------------------------
# Benchmark 9: link rewriter with mixed link types
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestRewriteMixedLinks:
    """Benchmark rewriter with a mix of context, external, and internal links."""

    def test_mixed_link_content(self, tmp_path: Path):
        """Mixed content should only rewrite context links."""
        resolver = UnifiedLinkResolver(base_dir=tmp_path)
        ctx_dir = tmp_path / ".apm" / "context"
        ctx_dir.mkdir(parents=True, exist_ok=True)
        ctx_file = ctx_dir / "api.context.md"
        ctx_file.write_text("# API Context\n")
        resolver.context_registry["api.context.md"] = ctx_file

        content = (
            "# Mixed Document\n\n"
            "[External Link](https://example.com/page)\n"
            "[API Context](api.context.md)\n"
            "[Readme](README.md)\n"
            "[Another Context](api.context.md)\n"
            "[Image](./logo.png)\n"
        )
        source_file = tmp_path / "test.agent.md"
        source_file.write_text(content)

        ctx = LinkResolutionContext(
            source_file=source_file,
            source_location=source_file.parent,
            target_location=tmp_path,
            base_dir=tmp_path,
            available_contexts=dict(resolver.context_registry),
        )

        start = time.perf_counter()
        result = resolver._rewrite_markdown_links(content, ctx)
        elapsed = time.perf_counter() - start

        # External links should be preserved
        assert "https://example.com/page" in result
        # Generous ceiling -- catches catastrophic regressions only.
        assert elapsed < 2.5, (
            f"Mixed link rewrite took {elapsed:.3f}s, expected < 2.5s (generous ceiling)"
        )


# ---------------------------------------------------------------------------
# Benchmark 10: partition_managed_files() with empty set
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestPartitionEdgeCases:
    """Edge cases for partition_managed_files."""

    def test_empty_set(self):
        """Partitioning an empty set should return quickly."""
        start = time.perf_counter()
        buckets = BaseIntegrator.partition_managed_files(set())
        elapsed = time.perf_counter() - start

        assert isinstance(buckets, dict)
        total = sum(len(v) for v in buckets.values())
        assert total == 0
        # Generous ceiling -- catches catastrophic regressions only.
        assert elapsed < 2.0, (
            f"Empty set partition took {elapsed:.3f}s, expected < 2.0s (generous ceiling)"
        )

    def test_unknown_prefix_not_routed(self):
        """Paths that do not match any known prefix are not routed."""
        managed = {
            "random/path/file.txt",
            "another/unknown.md",
        }
        buckets = BaseIntegrator.partition_managed_files(managed)
        total = sum(len(v) for v in buckets.values())
        # Unknown paths should not appear in any bucket
        assert total == 0


# ---------------------------------------------------------------------------
# Benchmark 11: compute_deployed_hashes() with symlinks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestDeployedHashesSymlinks:
    """Verify symlinks are silently omitted from hash output."""

    def test_symlinks_omitted(self, tmp_path: Path):
        """Symlinks should be excluded from hash results."""
        real_file = tmp_path / "real.md"
        real_file.write_text("real content\n")
        link_file = tmp_path / "link.md"
        try:
            link_file.symlink_to(real_file)
        except OSError:
            pytest.skip("Cannot create symlinks on this platform")

        result = compute_deployed_hashes(["real.md", "link.md"], tmp_path)
        assert "real.md" in result
        assert "link.md" not in result
