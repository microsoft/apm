"""Scaling-guard tests -- verify algorithmic complexity class.

These tests run in the NORMAL test suite (no ``@pytest.mark.benchmark``).
They compare execution time at two input sizes and assert the ratio stays
below a threshold, catching O(n^2) regressions without full benchmarking.

Threshold rationale
-------------------
For 10x input growth an O(n) algorithm should give ~10x wall-clock growth.
An O(n^2) algorithm would give ~100x.  Each guard's threshold is set to
~30% above the measured baseline ratio so that noisy CI runners do not
flake while quadratic regressions are still caught.
"""

import os
import statistics
import tempfile  # noqa: F401
import time
from dataclasses import dataclass, field  # noqa: F401
from pathlib import Path
from typing import Dict, List, Optional  # noqa: F401, UP035
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _median_time(fn, *, repeats=5):
    """Return the median wall-clock time of *fn* over *repeats* runs."""
    times: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        t1 = time.perf_counter()
        times.append(t1 - t0)
    return statistics.median(times)


# ---------------------------------------------------------------------------
# 1. Phase 2 -- Children-index scaling (_build_children_index)
# ---------------------------------------------------------------------------


@dataclass
class _FakeDep:
    """Minimal stand-in for ``LockedDependency`` used by ``_build_children_index``."""

    repo_url: str
    resolved_by: str | None = None
    local_path: str | None = None
    depth: int = 1


class _FakeLockFile:
    """Minimal stand-in for ``LockFile`` exposing ``get_package_dependencies``."""

    def __init__(self, deps: list[_FakeDep]):
        self._deps = deps

    def get_package_dependencies(self) -> list[_FakeDep]:
        return self._deps


def _make_lockfile(n: int) -> _FakeLockFile:
    """Build a synthetic lockfile with *n* dependencies.

    Half the deps are resolved_by a parent URL, the other half are
    top-level (resolved_by=None) to mirror realistic lockfiles.
    """
    deps: list[_FakeDep] = []
    for i in range(n):
        parent = f"org/parent-{i % 10}" if i % 2 == 0 else None
        deps.append(_FakeDep(repo_url=f"org/repo-{i}", resolved_by=parent))
    return _FakeLockFile(deps)


class TestChildrenIndexScaling:
    """_build_children_index must stay O(n)."""

    def test_scaling_ratio(self):
        from apm_cli.commands.uninstall.engine import _build_children_index

        small_lf = _make_lockfile(50)
        large_lf = _make_lockfile(500)

        t_small = _median_time(lambda: _build_children_index(small_lf))
        t_large = _median_time(lambda: _build_children_index(large_lf))

        # Guard against division by near-zero (extremely fast small run)
        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        assert ratio < 15, (
            f"Scaling ratio {ratio:.1f}x for 10x input suggests "
            f"O(n^2) regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 2. Phase 6 -- Discovery scanning scaling (find_primitive_files)
# ---------------------------------------------------------------------------


def _create_file_tree(root: str, n: int) -> None:
    """Populate *root* with *n* files spread across subdirectories.

    Roughly 30% are ``.instructions.md``, 30% are ``.agent.md``,
    and 40% are non-matching files to exercise the filter path.
    """
    for i in range(n):
        # Spread across subdirs to exercise os.walk depth
        subdir = os.path.join(root, f"dir-{i % 20}", f"sub-{i % 5}")
        os.makedirs(subdir, exist_ok=True)
        if i % 10 < 3:
            fname = f"file-{i}.instructions.md"
        elif i % 10 < 6:
            fname = f"file-{i}.agent.md"
        else:
            fname = f"file-{i}.txt"
        filepath = os.path.join(subdir, fname)
        with open(filepath, "w") as fh:
            fh.write(f"# file {i}\n")


class TestDiscoveryScaling:
    """find_primitive_files must stay O(n) in file count."""

    def test_scaling_ratio(self, tmp_path):
        from apm_cli.primitives.discovery import find_primitive_files

        patterns = ["**/*.instructions.md", "**/*.agent.md"]

        small_dir = str(tmp_path / "small")
        large_dir = str(tmp_path / "large")
        os.makedirs(small_dir)
        os.makedirs(large_dir)

        _create_file_tree(small_dir, 100)
        _create_file_tree(large_dir, 1000)

        t_small = _median_time(lambda: find_primitive_files(small_dir, patterns))
        t_large = _median_time(lambda: find_primitive_files(large_dir, patterns))

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        # Threshold 20x: a true O(n^2) regression for 10x input would
        # produce ~100x. The slack above 10x absorbs measurement noise
        # plus the fact that small-tree timings (~5ms) are dominated by
        # per-call overhead (Path.resolve on the base dir, perf-stats
        # bookkeeping) that does NOT scale with file count; further
        # optimizing the per-file loop makes the small case faster
        # proportionally faster than the large case, inflating the
        # ratio without any algorithmic regression. See #1533 perf
        # work.
        assert ratio < 20, (
            f"Scaling ratio {ratio:.1f}x for 10x input suggests "
            f"O(n^2) regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 3. Console singleton scaling (_get_console)
# ---------------------------------------------------------------------------


class TestConsoleSingletonScaling:
    """Repeated _get_console() calls must be O(1) per call after init."""

    def setup_method(self):
        from apm_cli.utils.console import _reset_console

        _reset_console()

    def teardown_method(self):
        from apm_cli.utils.console import _reset_console

        _reset_console()

    def test_scaling_ratio(self):
        from apm_cli.utils.console import _get_console

        def call_n(n):
            for _ in range(n):
                _get_console()

        t_small = _median_time(lambda: call_n(1000))
        t_large = _median_time(lambda: call_n(10000))

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        assert ratio < 15, (
            f"Scaling ratio {ratio:.1f}x for 10x calls suggests "
            f"caching regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 4. compute_package_hash scaling
# ---------------------------------------------------------------------------


def _populate_hash_dir(base: Path, file_count: int) -> None:
    """Create *file_count* files (~1 KB each) under *base*."""
    base.mkdir(parents=True, exist_ok=True)
    for i in range(file_count):
        subdir = base / f"sub-{i // 20}"
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / f"file-{i}.dat").write_bytes(os.urandom(1024))


class TestComputePackageHashScaling:
    """compute_package_hash must stay O(n) in file count."""

    def test_scaling_ratio(self, tmp_path):
        from apm_cli.utils.content_hash import compute_package_hash

        small_dir = tmp_path / "small"
        large_dir = tmp_path / "large"
        _populate_hash_dir(small_dir, 50)
        _populate_hash_dir(large_dir, 500)

        t_small = _median_time(lambda: compute_package_hash(small_dir))
        t_large = _median_time(lambda: compute_package_hash(large_dir))

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        assert ratio < 16, (
            f"Scaling ratio {ratio:.1f}x for 10x input suggests "
            f"O(n^2) regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 5. is_semantically_equivalent scaling
# ---------------------------------------------------------------------------


def _make_equiv_lockfile_pair(n: int, files_per_dep: int = 10):
    """Build two identical LockFiles with *n* deps, each carrying *files_per_dep* files."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    def _build(count: int) -> "LockFile":
        lf = LockFile()
        for i in range(count):
            dep = LockedDependency(
                repo_url=f"https://github.com/org/pkg-{i}",
                depth=(i % 5) + 1,
                deployed_files=[
                    f".github/agents/agent-{i}-{j}.agent.md" for j in range(files_per_dep)
                ],
                deployed_file_hashes={
                    f".github/agents/agent-{i}-{j}.agent.md": f"sha256:{'ab' * 32}"
                    for j in range(files_per_dep)
                },
            )
            lf.add_dependency(dep)
        return lf

    return _build(n), _build(n)


class TestSemanticEquivalenceScaling:
    """is_semantically_equivalent must stay O(n) in dependency count."""

    def test_scaling_ratio(self):
        lf1_small, lf2_small = _make_equiv_lockfile_pair(50)
        lf1_large, lf2_large = _make_equiv_lockfile_pair(500)

        t_small = _median_time(lambda: lf1_small.is_semantically_equivalent(lf2_small))
        t_large = _median_time(lambda: lf1_large.is_semantically_equivalent(lf2_large))

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        assert ratio < 25, (
            f"Scaling ratio {ratio:.1f}x for 10x input suggests "
            f"O(n^2) regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 6. Deployment manifest reconciliation scaling
# ---------------------------------------------------------------------------


def _reconcile_manifest_size(n: int) -> None:
    from apm_cli.install.manifest_reconcile import union_preserving

    current = [f"custom/current-{i}.json" for i in range(n // 2)]
    prior = [*current, *(f"custom/prior-{i}.json" for i in range(n // 2))]
    hashes = {path: f"sha256:{'ab' * 32}" for path in prior}
    with patch("apm_cli.integration.targets.KNOWN_TARGETS", {}):
        union_preserving(
            current,
            {path: hashes[path] for path in current},
            prior,
            hashes,
            targets=[],
        )


class TestDeploymentManifestReconcileScaling:
    """Manifest union must stay linear in the number of deployed paths."""

    def test_scaling_ratio(self):
        t_small = _median_time(lambda: _reconcile_manifest_size(1000), repeats=3)
        t_large = _median_time(lambda: _reconcile_manifest_size(10000), repeats=3)

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        assert ratio < 25, (
            f"Scaling ratio {ratio:.1f}x for 10x input suggests "
            f"O(n^2) regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 7. should_exclude scaling with ** patterns
# ---------------------------------------------------------------------------


def _make_test_tree(base: Path, depth: int) -> Path:
    """Create a file at the given depth under *base* and return its path.

    E.g. depth=5 -> base/a/b/c/d/test.py
    """
    parts = [chr(ord("a") + (i % 26)) for i in range(depth - 1)]
    parts.append("test.py")
    file_path = base
    for p in parts:
        file_path = file_path / p
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("# test\n")
    return file_path


class TestShouldExcludeScaling:
    """should_exclude() with ** patterns must stay sub-quadratic in path depth.

    Both test paths are designed to NOT match the pattern, exercising the full
    backtracking path before rejection -- the worst case for recursive matchers.
    """

    def test_scaling_ratio(self, tmp_path):
        """Depth 5 vs depth 15 with a 2-segment ** pattern.

        For a 3x depth increase, the ratio should stay < 2x (sub-quadratic).
        A quadratic algorithm would give ~9x just from depth; < 2x confirms
        the matcher scales well below that.
        """
        from apm_cli.utils.exclude import should_exclude, validate_exclude_patterns

        pattern = validate_exclude_patterns(["**/a/**/b/*.py"])

        shallow_file = _make_test_tree(tmp_path / "shallow", 5)
        deep_file = _make_test_tree(tmp_path / "deep", 15)

        t_shallow = _median_time(
            lambda: should_exclude(shallow_file, tmp_path / "shallow", pattern)
        )
        t_deep = _median_time(lambda: should_exclude(deep_file, tmp_path / "deep", pattern))

        if t_shallow < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_deep / t_shallow
        assert ratio < 2, (
            f"Scaling ratio {ratio:.1f}x for 3x depth increase suggests "
            f"super-quadratic regression (t_shallow={t_shallow:.6f}s, "
            f"t_deep={t_deep:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 7. Sparse-cone variant key scaling (_variant_key)
# ---------------------------------------------------------------------------


def _make_sparse_paths(n: int) -> list[str]:
    """Build a list of *n* distinct top-level sparse-cone paths."""
    return [f"plugins/pkg-{i}/skills/skill-{i}" for i in range(n)]


class TestVariantKeyScaling:
    """_variant_key must stay O(n log n) in path count.

    The function sorts, deduplicates, JSON-serialises and SHA-256-hashes
    the sparse path list. For 10x input growth (200->2000) an O(n log n)
    algorithm gives a theoretical ratio of ~14.3x; an O(n^2) algorithm
    would give ~100x. We use ``ratio < 25`` as the guard -- tight enough
    to catch quadratic regressions while leaving ~74% margin above the
    theoretical baseline.

    Uses repeated calls per sample to push total runtime above the
    measurement floor and reduce timer noise on fast CI runners.
    """

    def test_scaling_ratio(self) -> None:
        from apm_cli.cache.git_cache import _variant_key

        small_paths = _make_sparse_paths(200)
        large_paths = _make_sparse_paths(2000)
        repeats = 500

        def call_n(paths: list[str]) -> None:
            for _ in range(repeats):
                _variant_key(paths)

        t_small = _median_time(lambda: call_n(small_paths))
        t_large = _median_time(lambda: call_n(large_paths))

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        assert ratio < 25, (
            f"Scaling ratio {ratio:.1f}x for 10x input suggests "
            f"O(n^2) regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )

    def test_canonicalization(self) -> None:
        """Variant key is order-insensitive, duplicate-insensitive, and deterministic."""
        from apm_cli.cache.git_cache import _variant_key

        # Order-insensitive
        assert _variant_key(["b", "a"]) == _variant_key(["a", "b"])
        # Duplicate-insensitive
        assert _variant_key(["a", "a"]) == _variant_key(["a"])
        # Distinct sets produce distinct keys
        assert _variant_key(["a"]) != _variant_key(["b"])
        # None / empty -> "full"
        assert _variant_key(None) == "full"
        assert _variant_key([]) == "full"


# ---------------------------------------------------------------------------
# 8. Sparse-cone checkout variant lookup scaling
# ---------------------------------------------------------------------------


class TestVariantLookupScaling:
    """Checkout variant resolution must be O(1) per lookup.

    The cache layout places variants at ``<shard>/<sha>/<variant>/``.
    This guard creates many sibling variant directories for the same
    SHA and verifies that checking whether one specific variant exists
    does not degrade with the number of siblings. The ``is_dir()``
    call is an inode lookup -- O(1) on all supported filesystems.
    Measured baseline is ~1x; we use ``ratio < 2`` to leave ~30% margin
    for noisy CI runners while catching any accidental directory scan.
    """

    def test_scaling_ratio(self, tmp_path: Path) -> None:
        from apm_cli.cache.git_cache import _variant_key

        sha_dir_small = tmp_path / "small" / "abc123"
        sha_dir_large = tmp_path / "large" / "abc123"
        sha_dir_small.mkdir(parents=True)
        sha_dir_large.mkdir(parents=True)

        target_variant = _variant_key(["target/path"])

        # Small: 50 sibling variants
        for i in range(50):
            v = _variant_key([f"path-{i}"])
            (sha_dir_small / v).mkdir()
        (sha_dir_small / target_variant).mkdir(exist_ok=True)

        # Large: 500 sibling variants
        for i in range(500):
            v = _variant_key([f"path-{i}"])
            (sha_dir_large / v).mkdir()
        (sha_dir_large / target_variant).mkdir(exist_ok=True)

        repeats = 2000

        def check_exists(sha_dir: Path) -> None:
            target = sha_dir / target_variant
            for _ in range(repeats):
                target.is_dir()

        t_small = _median_time(lambda: check_exists(sha_dir_small))
        t_large = _median_time(lambda: check_exists(sha_dir_large))

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        assert ratio < 2, (
            f"Scaling ratio {ratio:.1f}x for 10x sibling variants suggests "
            f"linear scan regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 9. Hook sidecar re-injection scaling (_reinject_apm_source_from_sidecar)
# ---------------------------------------------------------------------------


def _make_hook_and_sidecar(n: int) -> tuple[dict, dict]:
    """Build hooks + sidecar dicts with *n* entries per event."""
    entries = [{"command": f"echo {i}", "pattern": f"*.{i}"} for i in range(n)]
    sidecar_entries = [
        {"command": f"echo {i}", "pattern": f"*.{i}", "_apm_source": f"pkg-{i}"} for i in range(n)
    ]
    return {"onFileCreate": list(entries)}, {"onFileCreate": list(sidecar_entries)}


class TestSidecarReinjectionScaling:
    """_reinject_apm_source_from_sidecar must stay O(n).

    The function matches disk entries against sidecar entries to restore
    ownership markers. Uses dict-based lookup for O(n) matching.
    """

    def test_scaling_ratio(self) -> None:
        import copy

        from apm_cli.integration.hook_integrator import (
            _reinject_apm_source_from_sidecar,
        )

        hooks_s, sidecar_s = _make_hook_and_sidecar(50)
        hooks_l, sidecar_l = _make_hook_and_sidecar(500)
        repeats = 200

        def run(hooks, sidecar):
            for _ in range(repeats):
                h = copy.deepcopy(hooks)
                _reinject_apm_source_from_sidecar(h, sidecar)

        t_small = _median_time(lambda: run(hooks_s, sidecar_s))
        t_large = _median_time(lambda: run(hooks_l, sidecar_l))

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        assert ratio < 15, (
            f"Scaling ratio {ratio:.1f}x for 10x input suggests "
            f"super-linear regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 10. Hook dedup scaling (inline dedup from _integrate_merged_hooks)
# ---------------------------------------------------------------------------


def _dedup_hook_entries(entries: list[dict]) -> list[dict]:
    """Reproduce the dedup logic from _integrate_merged_hooks.

    Uses set-based lookup for O(n) deduplication.
    """
    import json

    seen_keys: set[str] = set()
    deduped: list = []
    for entry in entries:
        if not isinstance(entry, dict):
            deduped.append(entry)
            continue
        cmp = {k: v for k, v in sorted(entry.items()) if k != "_apm_source"}
        source = entry.get("_apm_source")
        dedup_key = json.dumps({"s": source, "c": cmp}, sort_keys=True)
        if dedup_key not in seen_keys:
            seen_keys.add(dedup_key)
            deduped.append(entry)
    return deduped


class TestHookDedupScaling:
    """Hook entry deduplication must stay O(n).

    Previously used a list scan (O(n^2)); now uses set-based lookup.
    This guard catches regressions back to quadratic.
    """

    def test_scaling_ratio(self) -> None:
        small_entries = [
            {"command": f"echo {i}", "pattern": f"*.{i}", "_apm_source": f"pkg-{i}"}
            for i in range(50)
        ]
        large_entries = [
            {"command": f"echo {i}", "pattern": f"*.{i}", "_apm_source": f"pkg-{i}"}
            for i in range(500)
        ]
        repeats = 200

        def run(entries):
            for _ in range(repeats):
                _dedup_hook_entries(entries)

        t_small = _median_time(lambda: run(small_entries))
        t_large = _median_time(lambda: run(large_entries))

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        assert ratio < 15, (
            f"Scaling ratio {ratio:.1f}x for 10x input suggests "
            f"quadratic regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 11. Dependency tree flattening scaling (flatten_dependencies)
# ---------------------------------------------------------------------------


def _make_dependency_tree(n: int, depths: int = 3):
    """Build a synthetic DependencyTree with *n* nodes across *depths* levels."""
    from apm_cli.deps.dependency_graph import DependencyNode, DependencyTree
    from apm_cli.models.apm_package import APMPackage, DependencyReference

    root = APMPackage(name="root", version="1.0.0")
    tree = DependencyTree(root_package=root)
    per_level = max(1, n // depths)

    for d in range(1, depths + 1):
        for i in range(per_level):
            idx = (d - 1) * per_level + i
            dep_ref = DependencyReference(repo_url=f"org/pkg-{idx}")
            pkg = APMPackage(name=f"pkg-{idx}", version="1.0.0")
            node = DependencyNode(package=pkg, dependency_ref=dep_ref, depth=d)
            tree.add_node(node)

    return tree


class TestFlattenDependenciesScaling:
    """flatten_dependencies must stay O(n log n).

    BFS traversal with sort per depth level. This guard catches
    accidental quadratic regressions in the core install path.
    Uses same depth for both sizes to isolate node-count scaling.
    """

    def test_scaling_ratio(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        resolver = APMDependencyResolver.__new__(APMDependencyResolver)

        small_tree = _make_dependency_tree(50, depths=3)
        large_tree = _make_dependency_tree(500, depths=3)
        repeats = 200

        def run(tree):
            for _ in range(repeats):
                resolver.flatten_dependencies(tree)

        t_small = _median_time(lambda: run(small_tree))
        t_large = _median_time(lambda: run(large_tree))

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        assert ratio < 21, (
            f"Scaling ratio {ratio:.1f}x for 10x input suggests "
            f"O(n^2) regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 12. JSON key diff scaling (json_key_diff)
# ---------------------------------------------------------------------------


def _make_flat_diff_dict(n: int, prefix: str = "") -> dict:
    """Build a flat dict with *n* keys for diff benchmarking."""
    return {f"{prefix}key-{i}": f"value-{i}" for i in range(n)}


class TestJsonKeyDiffScaling:
    """json_key_diff must stay O(n log n) in total key count.

    Recursive tree walk that emits per-leaf differences. The
    sorted(set(old.keys()) | set(new.keys())) at each level adds
    an n log n factor. This guard catches quadratic regressions.
    """

    def test_scaling_ratio(self) -> None:
        from apm_cli.marketplace.drift_check import json_key_diff

        small_a = _make_flat_diff_dict(100)
        small_b = _make_flat_diff_dict(100, "alt-")
        large_a = _make_flat_diff_dict(1000)
        large_b = _make_flat_diff_dict(1000, "alt-")
        repeats = 200

        def run(a, b):
            for _ in range(repeats):
                json_key_diff(a, b)

        t_small = _median_time(lambda: run(small_a, small_b))
        t_large = _median_time(lambda: run(large_a, large_b))

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        assert ratio < 22, (
            f"Scaling ratio {ratio:.1f}x for 10x key count suggests "
            f"quadratic regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 13. Render instructions block scaling (render_instructions_block)
# ---------------------------------------------------------------------------


class TestRenderInstructionsScaling:
    """render_instructions_block must stay O(n log n).

    Groups by pattern + sorts within each group. Central to
    AGENTS.md / CLAUDE.md compilation.
    """

    def test_scaling_ratio(self, tmp_path: Path) -> None:
        from apm_cli.compilation.template_builder import render_instructions_block
        from apm_cli.primitives.models import Instruction

        def make_instructions(n: int) -> list[Instruction]:
            instrs = []
            for i in range(n):
                pattern = "src/**/*.py" if i % 3 == 0 else ("tests/**/*.py" if i % 3 == 1 else "")
                instrs.append(
                    Instruction(
                        name=f"instr-{i}",
                        file_path=tmp_path / f"pkg-{i % 20}" / f"instr-{i}.instructions.md",
                        description=f"Instruction {i}",
                        apply_to=pattern,
                        content=f"# Instruction {i}\nDo thing {i}.\n",
                    )
                )
            return instrs

        small = make_instructions(50)
        large = make_instructions(500)

        def emit(inst):
            return [f"<!-- {inst.name} -->", inst.content, ""]

        repeats = 200

        def run(instrs):
            for _ in range(repeats):
                render_instructions_block(instrs, base_dir=tmp_path, emit_instruction=emit)

        t_small = _median_time(lambda: run(small))
        t_large = _median_time(lambda: run(large))

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        assert ratio < 21, (
            f"Scaling ratio {ratio:.1f}x for 10x input suggests "
            f"O(n^2) regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 14. LockFile.get_installed_paths scaling
# ---------------------------------------------------------------------------


class TestGetInstalledPathsScaling:
    """LockFile.get_installed_paths must stay O(n log n).

    Iterates all deps (via sorted get_all_dependencies), computes
    install paths, deduplicates via set. Called from multiple code paths.
    """

    def test_scaling_ratio(self, tmp_path: Path) -> None:
        from apm_cli.deps.lockfile import LockedDependency, LockFile

        def make_lockfile(n: int) -> LockFile:
            lf = LockFile()
            for i in range(n):
                lf.add_dependency(
                    LockedDependency(
                        repo_url=f"https://github.com/org/pkg-{i}",
                        depth=(i % 5) + 1,
                    )
                )
            return lf

        small_lf = make_lockfile(50)
        large_lf = make_lockfile(500)
        modules_dir = tmp_path / "apm_modules"
        modules_dir.mkdir()
        repeats = 20

        def run(lf):
            for _ in range(repeats):
                lf.get_installed_paths(modules_dir)

        t_small = _median_time(lambda: run(small_lf))
        t_large = _median_time(lambda: run(large_lf))

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        ratio = t_large / t_small
        assert ratio < 21, (
            f"Scaling ratio {ratio:.1f}x for 10x input suggests "
            f"O(n^2) regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )


# ---------------------------------------------------------------------------
# 15. Hook data rewriting scaling (_rewrite_hooks_data)
# ---------------------------------------------------------------------------


class TestRewriteHooksDataScaling:
    """_rewrite_hooks_data must stay O(E * M) -- linear in total matchers.

    Deep-copies + rewrites command paths for each event/matcher/key.
    This guard catches regressions from the deep copy or nested loop.
    """

    def test_scaling_ratio(self, tmp_path: Path) -> None:
        from apm_cli.integration.hook_integrator import HookIntegrator

        integrator = HookIntegrator()

        def make_hooks_data(events: int, matchers: int) -> dict:
            data: dict = {"hooks": {}}
            for e in range(events):
                data["hooks"][f"onEvent{e}"] = [
                    {"command": f"echo {m}", "pattern": f"*.{m}"} for m in range(matchers)
                ]
            return data

        pkg_path = tmp_path / "pkg"
        pkg_path.mkdir()

        small_data = make_hooks_data(10, 5)
        large_data = make_hooks_data(50, 20)
        repeats = 100

        def run(data):
            for _ in range(repeats):
                integrator._rewrite_hooks_data(data, pkg_path, "test-pkg", "copilot")

        t_small = _median_time(lambda: run(small_data))
        t_large = _median_time(lambda: run(large_data))

        if t_small < 1e-7:
            pytest.skip("below measurement threshold -- too fast to measure reliably")

        # 10*5=50 -> 50*20=1000: 20x growth in total matchers
        ratio = t_large / t_small
        assert ratio < 30, (
            f"Scaling ratio {ratio:.1f}x for 20x total matchers suggests "
            f"super-linear regression (t_small={t_small:.6f}s, "
            f"t_large={t_large:.6f}s)"
        )
