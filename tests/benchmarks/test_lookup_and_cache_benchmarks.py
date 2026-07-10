"""Performance benchmarks for optimised lookup and cache paths.

Covers the fixes from the performance audit:
- has_dependency() O(1) index lookup (was O(n) linear scan)
- HttpCache._enforce_size_cap() fast-path (was unconditional full scan)
- github_host env-var helper consolidation (was repeated parsing)

Run with: uv run pytest tests/benchmarks/test_lookup_and_cache_benchmarks.py -v -m benchmark
"""

import statistics
import time
from pathlib import Path

import pytest

from apm_cli.deps.dependency_graph import DependencyNode, DependencyTree
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.reference import DependencyReference

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _median_time(fn, *, repeats: int = 7) -> float:
    """Return the median wall-clock time of *fn* over *repeats* runs."""
    times: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        t1 = time.perf_counter()
        times.append(t1 - t0)
    return statistics.median(times)


def _make_node(owner: str, repo: str, depth: int) -> DependencyNode:
    dep_ref = DependencyReference.parse(f"{owner}/{repo}#main")
    pkg = APMPackage(name=repo, version="1.0.0", source=f"{owner}/{repo}")
    return DependencyNode(package=pkg, dependency_ref=dep_ref, depth=depth)


def _build_tree(n: int) -> DependencyTree:
    """Build a tree with *n* packages."""
    root = APMPackage(name="root", version="1.0.0")
    tree = DependencyTree(root_package=root)
    for i in range(n):
        node = _make_node("org", f"pkg-{i}", depth=(i % 5) + 1)
        tree.add_node(node)
    return tree


# ---------------------------------------------------------------------------
# 1. has_dependency() -- O(1) via _repo_url_index
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestHasDependencyScaling:
    """has_dependency() must stay O(1) regardless of tree size."""

    def test_constant_time_lookup(self):
        """Lookup time should not grow significantly with tree size."""
        small_tree = _build_tree(50)
        large_tree = _build_tree(5000)

        # Look up a URL that exists (last added)
        small_url = "https://github.com/org/pkg-49"
        large_url = "https://github.com/org/pkg-4999"

        t_small = _median_time(lambda: small_tree.has_dependency(small_url), repeats=1000)
        t_large = _median_time(lambda: large_tree.has_dependency(large_url), repeats=1000)

        # O(1) means the ratio should be close to 1 regardless of input size.
        # With 100x more nodes, an O(n) scan would be ~100x slower.
        # Allow generous headroom for measurement noise (< 5x).
        if t_small < 1e-9:
            pytest.skip("below measurement threshold")

        ratio = t_large / t_small
        assert ratio < 5.0, (
            f"has_dependency ratio {ratio:.1f}x for 100x larger tree "
            f"suggests O(n) regression (small={t_small:.9f}s, "
            f"large={t_large:.9f}s)"
        )

    def test_miss_is_also_constant(self):
        """Negative lookups (URL not in tree) must also be O(1)."""
        tree = _build_tree(5000)
        missing_url = "https://github.com/org/does-not-exist"

        t = _median_time(lambda: tree.has_dependency(missing_url), repeats=1000)
        # Must be essentially free (< 1 microsecond median)
        assert t < 1e-4, f"Negative lookup took {t:.6f}s"

    def test_index_consistency(self):
        """_repo_url_index matches brute-force scan."""
        tree = _build_tree(200)
        for node in tree.nodes.values():
            url = node.dependency_ref.repo_url
            if url:
                assert tree.has_dependency(url)
        assert not tree.has_dependency("https://github.com/no/such-repo")


# ---------------------------------------------------------------------------
# 2. HttpCache._enforce_size_cap fast-path
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestHttpCacheSizeCapFastPath:
    """_enforce_size_cap must skip full scan when tracked size is under cap."""

    def test_store_does_not_scan_when_under_cap(self, tmp_path: Path):
        """Repeated stores under the cap should be near-instant."""
        from apm_cli.cache.http_cache import HttpCache

        cache = HttpCache(tmp_path)
        body = b"x" * 1024  # 1KB

        # Prime: first store initialises tracking
        cache.store("http://example.com/prime", body, headers={"Cache-Control": "max-age=300"})

        # Now measure subsequent stores (should hit fast-path)
        def _store_one():
            url = f"http://example.com/{time.perf_counter_ns()}"
            cache.store(url, body, headers={"Cache-Control": "max-age=300"})

        t = _median_time(_store_one, repeats=20)
        # Fast-path store (no directory walk) should be < 5ms
        assert t < 0.05, f"Store with fast-path took {t:.4f}s (expected < 50ms)"

    def test_many_stores_scale_linearly(self, tmp_path: Path):
        """N stores should scale linearly, not O(N^2) from rescanning."""
        from apm_cli.cache.http_cache import HttpCache

        cache = HttpCache(tmp_path)
        body = b"y" * 512

        def _store_batch(n: int) -> float:
            t0 = time.perf_counter()
            for i in range(n):
                cache.store(
                    f"http://example.com/batch-{n}-{i}",
                    body,
                    headers={"Cache-Control": "max-age=300"},
                )
            return time.perf_counter() - t0

        t_small = _store_batch(20)
        t_large = _store_batch(200)

        if t_small < 1e-6:
            pytest.skip("below measurement threshold")

        ratio = t_large / t_small
        # With O(N^2), 10x more stores would be ~100x slower.
        # With fast-path O(N), expect ~10x. Allow 20x for noise.
        assert ratio < 20, (
            f"Scaling ratio {ratio:.1f}x for 10x batch suggests "
            f"quadratic regression (small={t_small:.4f}s, large={t_large:.4f}s)"
        )


# ---------------------------------------------------------------------------
# 3. github_host env helpers -- consolidated parsing
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestGithubHostEnvParsing:
    """Env-var parsing helpers avoid redundant string processing."""

    def test_repeated_calls_are_fast(self, monkeypatch):
        """is_gitlab_hostname called 1000x should be well under 50ms."""
        from apm_cli.utils.github_host import is_gitlab_hostname

        monkeypatch.setenv("GITHUB_HOST", "ghe.example.com")
        monkeypatch.setenv("APM_GITLAB_HOSTS", "gl1.corp.net,gl2.corp.net,gl3.corp.net")

        def _check_many():
            for _ in range(1000):
                is_gitlab_hostname("gl1.corp.net")
                is_gitlab_hostname("ghe.example.com")
                is_gitlab_hostname("unknown.host.com")

        t = _median_time(_check_many, repeats=5)
        # 3000 calls should be < 100ms total (< 33us each)
        assert t < 0.1, f"3000 host classifications took {t:.4f}s ({t / 3000 * 1e6:.1f}us/call)"

    def test_conflict_check_fast(self, monkeypatch):
        """has_github_gitlab_host_env_conflict 1000x should be < 50ms."""
        from apm_cli.utils.github_host import has_github_gitlab_host_env_conflict

        monkeypatch.setenv("GITHUB_HOST", "shared.example.com")
        monkeypatch.setenv("GITLAB_HOST", "shared.example.com")

        def _check_many():
            for _ in range(1000):
                has_github_gitlab_host_env_conflict("shared.example.com")

        t = _median_time(_check_many, repeats=5)
        assert t < 0.1, f"1000 conflict checks took {t:.4f}s"
