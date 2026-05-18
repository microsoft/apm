"""Unit tests for ``apm_cli.deps.tiered_ref_resolver`` (#1369).

Each tier is tested in isolation by mocking the dependency it reaches
into. The orchestrator is tested for cache hit, coalesce-lock, fall-
through, and feature-flag behaviour.
"""

from __future__ import annotations

import os
import sys
import threading
import types
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from apm_cli.deps.tiered_ref_resolver import (
    L0PerRunCache,
    L1CommitsAPI,
    L2BareRevParse,
    L3LegacyClone,
    PerRunRefCache,
    TieredRefResolver,
    build_tiered_ref_resolver,
    is_tiered_resolver_enabled,
)
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _dep(repo: str = "owner/repo", ref: str = "main") -> DependencyReference:
    return DependencyReference(repo_url=repo, reference=ref)


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, True),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("", False),
    ],
)
def test_is_tiered_resolver_enabled(value, expected, monkeypatch):
    if value is None:
        monkeypatch.delenv("APM_TIERED_RESOLVER", raising=False)
    else:
        monkeypatch.setenv("APM_TIERED_RESOLVER", value)
    assert is_tiered_resolver_enabled() is expected


# ---------------------------------------------------------------------------
# PerRunRefCache + L0
# ---------------------------------------------------------------------------


def test_per_run_cache_roundtrip():
    cache = PerRunRefCache()
    assert cache.get("owner/repo", "main") is None
    cache.put("owner/repo", "main", SHA_A)
    assert cache.get("owner/repo", "main") == SHA_A
    assert cache.size() == 1


def test_l0_per_run_cache_tier_hits_cache():
    cache = PerRunRefCache()
    cache.put("owner/repo", "main", SHA_A)
    tier = L0PerRunCache(cache=cache)
    assert tier.try_resolve(_dep(), "main") == SHA_A


def test_l0_per_run_cache_tier_misses_when_cold():
    tier = L0PerRunCache(cache=PerRunRefCache())
    assert tier.try_resolve(_dep(), "main") is None


# ---------------------------------------------------------------------------
# L1 CommitsAPI
# ---------------------------------------------------------------------------


def test_l1_returns_sha_directly_when_ref_is_already_a_sha():
    host = MagicMock()
    tier = L1CommitsAPI(host=host)
    assert tier.try_resolve(_dep(ref=SHA_A), SHA_A) == SHA_A


def test_l1_delegates_to_legacy_resolve_commit_sha_for_ref():
    legacy = MagicMock()
    legacy.resolve_commit_sha_for_ref.return_value = SHA_B
    host = MagicMock()
    host._refs = legacy
    tier = L1CommitsAPI(host=host)
    assert tier.try_resolve(_dep(), "main") == SHA_B
    legacy.resolve_commit_sha_for_ref.assert_called_once()


def test_l1_returns_none_for_artifactory():
    dep = DependencyReference(
        repo_url="owner/repo", reference="main", artifactory_prefix="artifactory/github"
    )
    legacy = MagicMock()
    host = MagicMock()
    host._refs = legacy
    tier = L1CommitsAPI(host=host)
    assert tier.try_resolve(dep, "main") is None
    legacy.resolve_commit_sha_for_ref.assert_not_called()


def test_l1_returns_none_when_legacy_raises():
    legacy = MagicMock()
    legacy.resolve_commit_sha_for_ref.side_effect = RuntimeError("boom")
    host = MagicMock()
    host._refs = legacy
    tier = L1CommitsAPI(host=host)
    assert tier.try_resolve(_dep(), "main") is None


def test_l1_returns_none_when_host_has_no_refs():
    host = types.SimpleNamespace()  # no _refs attribute
    tier = L1CommitsAPI(host=host)
    assert tier.try_resolve(_dep(), "main") is None


# ---------------------------------------------------------------------------
# L2 BareRevParse
# ---------------------------------------------------------------------------


def test_l2_returns_none_when_no_git_cache():
    tier = L2BareRevParse(git_cache=None)
    assert tier.try_resolve(_dep(), "main") is None


def test_l2_returns_none_when_bare_dir_missing(tmp_path):
    fake_cache = types.SimpleNamespace(_db_root=tmp_path / "nonexistent")
    tier = L2BareRevParse(git_cache=fake_cache)
    assert tier.try_resolve(_dep(), "main") is None


def test_l2_short_circuits_on_sha_input():
    fake_cache = types.SimpleNamespace(_db_root=None)
    tier = L2BareRevParse(git_cache=fake_cache)
    assert tier.try_resolve(_dep(ref=SHA_A), SHA_A) == SHA_A


def test_l2_rev_parse_returns_sha_on_branch_match(tmp_path):
    bare = tmp_path / "some_shard"
    bare.mkdir()
    fake_cache = types.SimpleNamespace(_db_root=tmp_path)
    tier = L2BareRevParse(git_cache=fake_cache)

    with (
        patch("apm_cli.cache.url_normalize.cache_shard_key", return_value="some_shard"),
        patch.object(L2BareRevParse, "_rev_parse", return_value=SHA_A) as rp,
    ):
        result = tier.try_resolve(_dep(), "main")
    assert result == SHA_A
    rp.assert_called_once()


# ---------------------------------------------------------------------------
# L3 LegacyClone
# ---------------------------------------------------------------------------


def test_l3_returns_sha_from_legacy_resolve():
    legacy = MagicMock()
    legacy.resolve.return_value = ResolvedReference(
        original_ref="owner/repo#main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=SHA_C,
        ref_name="main",
    )
    tier = L3LegacyClone(legacy_resolver=legacy)
    assert tier.try_resolve(_dep(), "main") == SHA_C


def test_l3_returns_none_when_legacy_raises():
    legacy = MagicMock()
    legacy.resolve.side_effect = RuntimeError("network down")
    tier = L3LegacyClone(legacy_resolver=legacy)
    assert tier.try_resolve(_dep(), "main") is None


def test_l3_resolve_full_passes_through():
    legacy = MagicMock()
    rr = ResolvedReference(
        original_ref="owner/repo#main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=SHA_C,
        ref_name="main",
    )
    legacy.resolve.return_value = rr
    tier = L3LegacyClone(legacy_resolver=legacy)
    assert tier.resolve_full(_dep()) is rr


# ---------------------------------------------------------------------------
# TieredRefResolver orchestrator
# ---------------------------------------------------------------------------


def _make_legacy_with(sha: str) -> L3LegacyClone:
    inner = MagicMock()
    inner.resolve.return_value = ResolvedReference(
        original_ref="owner/repo#main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=sha,
        ref_name="main",
    )
    return L3LegacyClone(legacy_resolver=inner)


def test_orchestrator_caches_after_first_resolve():
    cache = PerRunRefCache()
    counting_tier = MagicMock()
    counting_tier.name = "counting"
    counting_tier.try_resolve.return_value = SHA_A
    legacy = _make_legacy_with(SHA_A)
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), counting_tier, legacy],
        cache=cache,
        legacy=legacy,
    )

    r1 = resolver.resolve(_dep())
    r2 = resolver.resolve(_dep())
    r3 = resolver.resolve(_dep())

    assert r1.resolved_commit == SHA_A
    assert r2.resolved_commit == SHA_A
    assert r3.resolved_commit == SHA_A
    # First call: cold L0 miss -> counting tier. Subsequent calls: L0 hit.
    counting_tier.try_resolve.assert_called_once()
    assert resolver.stats["counting"] == 1
    assert resolver.stats["per_run_cache"] == 2


def test_orchestrator_collapses_concurrent_resolves():
    cache = PerRunRefCache()
    legacy = _make_legacy_with(SHA_A)

    in_flight = threading.Event()
    can_continue = threading.Event()
    call_count = [0]

    def slow_tier(dep_ref, ref):
        call_count[0] += 1
        in_flight.set()
        can_continue.wait(timeout=2)
        return SHA_A

    slow = MagicMock()
    slow.name = "slow"
    slow.try_resolve.side_effect = slow_tier

    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), slow, legacy],
        cache=cache,
        legacy=legacy,
    )

    results = []

    def run():
        results.append(resolver.resolve(_dep()))

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start()
    in_flight.wait(timeout=2)
    t2.start()
    # Give T2 a moment to enter resolve() and queue on the event.
    threading.Event().wait(0.05)
    can_continue.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(results) == 2
    assert all(r.resolved_commit == SHA_A for r in results)
    # Critical assertion: even though two threads raced, only ONE
    # underlying tier call happened.
    assert call_count[0] == 1
    assert resolver.stats["coalesced"] >= 1


def test_orchestrator_falls_through_when_all_tiers_return_none():
    cache = PerRunRefCache()
    miss = MagicMock()
    miss.name = "miss"
    miss.try_resolve.return_value = None
    legacy = _make_legacy_with(SHA_B)
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), miss, legacy],
        cache=cache,
        legacy=legacy,
    )
    result = resolver.resolve(_dep())
    assert result.resolved_commit == SHA_B


def test_orchestrator_handles_string_input():
    cache = PerRunRefCache()
    cache.put("owner/repo", "main", SHA_A)
    legacy = _make_legacy_with(SHA_A)
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), legacy],
        cache=cache,
        legacy=legacy,
    )
    result = resolver.resolve("owner/repo#main")
    assert result.resolved_commit == SHA_A


def test_orchestrator_routes_no_ref_to_legacy():
    cache = PerRunRefCache()
    legacy = _make_legacy_with(SHA_C)
    miss = MagicMock()
    miss.name = "miss"
    miss.try_resolve.return_value = None
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), miss, legacy],
        cache=cache,
        legacy=legacy,
    )
    dep = DependencyReference(repo_url="owner/repo", reference=None)
    result = resolver.resolve(dep)
    assert result.resolved_commit == SHA_C
    # No-ref path skips the tier dispatch entirely.
    miss.try_resolve.assert_not_called()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_returns_none_when_feature_flag_disabled(monkeypatch):
    monkeypatch.setenv("APM_TIERED_RESOLVER", "0")
    downloader = MagicMock()
    downloader._refs = MagicMock()
    assert build_tiered_ref_resolver(downloader=downloader) is None


def test_factory_returns_none_when_downloader_has_no_refs(monkeypatch):
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    downloader = types.SimpleNamespace()  # no _refs
    assert build_tiered_ref_resolver(downloader=downloader) is None


def test_factory_builds_full_stack_when_enabled(monkeypatch):
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    downloader = MagicMock()
    downloader._refs = MagicMock()
    resolver = build_tiered_ref_resolver(downloader=downloader)
    assert isinstance(resolver, TieredRefResolver)
    # 4 tiers: L0, L1, L2, L3
    assert len(resolver._tiers) == 4
    tier_names = [t.name for t in resolver._tiers]
    assert tier_names == ["per_run_cache", "commits_api", "bare_rev_parse", "legacy_clone"]
