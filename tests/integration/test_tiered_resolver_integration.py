"""Integration test for the TieredRefResolver fix to #1369.

Asserts the empirical claim: a 9-dep manifest pointing at 3 unique
(repo, ref) tuples should produce **at most 3** underlying resolve
calls when routed through the tiered resolver -- not 9.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from apm_cli.deps.tiered_ref_resolver import (
    L0PerRunCache,
    L3LegacyClone,
    PerRunRefCache,
    TieredRefResolver,
)
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

SHA_FMT = "{:040x}"


def _make_legacy_counter():
    """Return a (legacy_tier, counter) pair tracking underlying clones."""
    counter = {"count": 0, "by_key": {}}
    inner = MagicMock()

    def fake_resolve(repo_ref):
        dep = DependencyReference.parse(repo_ref) if isinstance(repo_ref, str) else repo_ref
        key = (dep.repo_url, dep.reference)
        counter["count"] += 1
        counter["by_key"][key] = counter["by_key"].get(key, 0) + 1
        idx = abs(hash(key)) % 10000
        return ResolvedReference(
            original_ref=str(dep),
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=SHA_FMT.format(idx),
            ref_name=dep.reference or "main",
        )

    inner.resolve.side_effect = fake_resolve
    legacy = L3LegacyClone(legacy_resolver=inner)
    return legacy, counter


def test_nine_deps_three_unique_collapses_to_three_clones():
    """The #1369 workload: integrate.py serial loop, 9 deps, 3 unique tuples.

    Before the fix: 9 underlying shallow clones (one per dep).
    After the fix: 3 underlying clones (one per UNIQUE (repo, ref)).
    Subsequent in-run lookups hit the L0 per-run cache.
    """
    cache = PerRunRefCache()
    legacy, counter = _make_legacy_counter()
    # Pure L0 + L3 stack -- the cheap-API and bare-rev-parse tiers
    # aren't relevant to the dedup claim. The claim is about per-run
    # memoization, which the L0 tier owns.
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), legacy],
        cache=cache,
        legacy=legacy,
    )

    # The #1369 manifest shape: 9 deps, 3 unique (repo, ref) tuples.
    deps = [
        DependencyReference(repo_url="awesome/copilot", reference="main"),
        DependencyReference(repo_url="awesome/copilot", reference="main"),
        DependencyReference(repo_url="awesome/copilot", reference="main"),
        DependencyReference(repo_url="awesome/copilot", reference="main"),
        DependencyReference(repo_url="awesome/copilot", reference="main"),
        DependencyReference(repo_url="org/lib-a", reference="v1.0.0"),
        DependencyReference(repo_url="org/lib-a", reference="v1.0.0"),
        DependencyReference(repo_url="org/lib-b", reference="main"),
        DependencyReference(repo_url="org/lib-b", reference="main"),
    ]

    results = [resolver.resolve(d) for d in deps]

    # All 9 resolve correctly.
    assert len(results) == 9
    assert all(r.resolved_commit is not None for r in results)

    # Critical assertion: ONLY 3 underlying resolves happened.
    assert counter["count"] == 3, (
        f"Expected 3 underlying resolves (one per unique tuple), got {counter['count']}. "
        f"Per-key counts: {counter['by_key']}"
    )

    # Each unique tuple resolved exactly once.
    assert counter["by_key"][("awesome/copilot", "main")] == 1
    assert counter["by_key"][("org/lib-a", "v1.0.0")] == 1
    assert counter["by_key"][("org/lib-b", "main")] == 1

    # Per-run cache holds exactly 3 entries.
    assert cache.size() == 3

    # Resolver stats: 6 cache hits + 3 misses fall-through.
    assert resolver.stats["per_run_cache"] == 6
    assert resolver.stats["legacy_clone"] == 3


def test_tiered_resolver_attached_via_downloader_facade():
    """When ``downloader._tiered_resolver`` is set, ``resolve_git_reference`` uses it."""
    from apm_cli.deps.github_downloader import GitHubPackageDownloader

    downloader = GitHubPackageDownloader.__new__(GitHubPackageDownloader)
    downloader._refs = MagicMock()
    downloader._refs.resolve = MagicMock(side_effect=AssertionError("legacy should not run"))

    cache = PerRunRefCache()
    legacy, counter = _make_legacy_counter()
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), legacy],
        cache=cache,
        legacy=legacy,
    )
    downloader._tiered_resolver = resolver

    dep = DependencyReference(repo_url="awesome/copilot", reference="main")
    r1 = downloader.resolve_git_reference(dep)
    r2 = downloader.resolve_git_reference(dep)

    assert r1.resolved_commit == r2.resolved_commit
    assert counter["count"] == 1, "Downloader facade must route through tiered resolver"


def test_tiered_resolver_disabled_via_env(monkeypatch):
    """Feature flag forces the downloader facade to bypass the tiered stack."""
    from apm_cli.deps.github_downloader import GitHubPackageDownloader
    from apm_cli.deps.tiered_ref_resolver import build_tiered_ref_resolver

    monkeypatch.setenv("APM_TIERED_RESOLVER", "0")
    downloader = GitHubPackageDownloader.__new__(GitHubPackageDownloader)
    downloader._refs = MagicMock()
    assert build_tiered_ref_resolver(downloader=downloader) is None
