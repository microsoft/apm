"""Scaling-guard tests -- verify algorithmic complexity class.

These tests run in the NORMAL test suite (no ``@pytest.mark.benchmark``).
They compare execution time at two input sizes and assert the ratio stays
below a threshold, catching O(n^2) regressions without full benchmarking.

Threshold rationale
-------------------
For 10x input growth an O(n) algorithm should give ~10x wall-clock growth.
An O(n^2) algorithm would give ~100x.  We use ``ratio < 25`` as the guard
so that noisy CI runners do not flake while quadratic regressions are still
caught.
"""

import os
import statistics
import tempfile  # noqa: F401
import time
from dataclasses import dataclass, field  # noqa: F401
from pathlib import Path
from typing import Dict, List, Optional  # noqa: F401, UP035

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
        assert ratio < 25, (
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
        assert ratio < 25, (
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

        t_small = _median_time(lambda: call_n(100))
        t_large = _median_time(lambda: call_n(1000))

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
        assert ratio < 25, (
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
# 6. should_exclude scaling with ** patterns
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

        For a 3x depth increase, the ratio should be < 25x (sub-quadratic).
        A quadratic algorithm would give ~9x just from depth, but with
        2 ** segments the branching factor can compound.  25x is our
        generous guard against super-quadratic blowup.
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
        assert ratio < 25, (
            f"Scaling ratio {ratio:.1f}x for 3x depth increase suggests "
            f"super-quadratic regression (t_shallow={t_shallow:.6f}s, "
            f"t_deep={t_deep:.6f}s)"
        )
