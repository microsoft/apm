"""Tiered ref resolution against on-disk bare caches and install locks."""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from apm_cli.cache.url_normalize import cache_shard_key
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.tiered_ref_resolver import (
    L2BareRevParse,
    RefFreshnessPolicy,
    TieredRefResolver,
    build_tiered_ref_resolver,
    ref_freshness_policy_for_install,
)
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

pytestmark = pytest.mark.component

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _dep(repo: str = "owner/repo", ref: str = "main") -> DependencyReference:
    return DependencyReference(repo_url=repo, reference=ref)


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PerRunRefCache + L0
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# L1 CommitsAPI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# L2 BareRevParse
# ---------------------------------------------------------------------------


def test_l2_returns_none_when_bare_dir_missing(tmp_path):
    fake_cache = types.SimpleNamespace(
        _db_root=tmp_path / "nonexistent", read_resolved_ref=lambda *_: (False, None)
    )
    tier = L2BareRevParse(git_cache=fake_cache)
    assert tier.try_resolve(_dep(), "main") is None


@pytest.mark.parametrize(
    "dependency",
    [
        DependencyReference(repo_url="owner/repo", host="github.com", reference="main"),
        DependencyReference(
            repo_url="acme/platform/team/repo",
            host="gitlab.com",
            reference="main",
        ),
    ],
)
def test_l2_rev_parse_uses_git_cache_repository_identity(tmp_path, dependency):
    bare = tmp_path / cache_shard_key(dependency.to_github_url())
    bare.mkdir(parents=True)
    fake_cache = types.SimpleNamespace(
        _db_root=tmp_path, read_resolved_ref=lambda *_: (False, None)
    )
    tier = L2BareRevParse(git_cache=fake_cache)

    with patch.object(L2BareRevParse, "_rev_parse", return_value=SHA_A) as rp:
        result = tier.try_resolve(dependency, "main")

    assert result == SHA_A
    rp.assert_called_once_with(bare, "main")


# ---------------------------------------------------------------------------
# L3 LegacyClone
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TieredRefResolver orchestrator
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Factory -- update_refs behaviour (#2342)
# ---------------------------------------------------------------------------


def test_stale_bare_bypassed_on_update(monkeypatch, tmp_path):
    """With update_refs=True, a stale local bare SHA is never returned (#2342).

    Scenario:
    - L2BareRevParse would return SHA_A (stale cached value).
    - L1 CommitsAPI returns SHA_B (fresh upstream value).
    - When update_refs=True, L2 is excluded so the resolver returns SHA_B.
    - When update_refs=False, L2 is in the stack but L1 fires first anyway,
      so SHA_B is returned and the stale path is never reached in normal flow.
      The important invariant is that in update mode, L2's stale answer can
      never surface even if L1 were somehow bypassed.
    """
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")

    bare = tmp_path / cache_shard_key(_dep().to_github_url())
    bare.mkdir(parents=True)
    git_cache = types.SimpleNamespace(_db_root=tmp_path, read_resolved_ref=lambda *_: (False, None))
    downloader = MagicMock()
    fake_refs = MagicMock()
    fake_refs.resolve_commit_sha_for_ref.return_value = None
    fake_refs.resolve.return_value = ResolvedReference(
        original_ref="owner/repo#main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=SHA_B,
        ref_name="main",
    )
    downloader._refs = fake_refs

    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        git_cache=git_cache,
        freshness_policy=RefFreshnessPolicy.CURRENT_REMOTE,
    )
    assert isinstance(resolver, TieredRefResolver)

    with patch.object(L2BareRevParse, "_rev_parse", return_value=SHA_A) as stale_l2:
        result = resolver.resolve(_dep(repo="owner/repo", ref="main"))

    assert result.resolved_commit == SHA_B
    stale_l2.assert_not_called()
    fake_refs.resolve_commit_sha_for_ref.assert_called_once()
    fake_refs.resolve.assert_called_once()
    assert resolver.stats["legacy_clone"] == 1
    assert "bare_rev_parse" not in resolver.stats


def test_compatibility_policy_uses_l2_when_api_unavailable_without_clone(monkeypatch, tmp_path):
    """Explicit compatibility policy retains legacy bare-cache behavior."""
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    bare = tmp_path / cache_shard_key(_dep().to_github_url())
    bare.mkdir(parents=True)
    git_cache = types.SimpleNamespace(_db_root=tmp_path, read_resolved_ref=lambda *_: (False, None))
    downloader = MagicMock()
    fake_refs = MagicMock()
    fake_refs.resolve_commit_sha_for_ref.return_value = None
    downloader._refs = fake_refs
    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        git_cache=git_cache,
        freshness_policy=RefFreshnessPolicy.REPRODUCIBLE,
    )
    assert isinstance(resolver, TieredRefResolver)

    with patch.object(L2BareRevParse, "_rev_parse", return_value=SHA_A) as cached_l2:
        result = resolver.resolve(_dep())

    assert result.resolved_commit == SHA_A
    cached_l2.assert_called_once_with(bare, "main")
    fake_refs.resolve_commit_sha_for_ref.assert_called_once()
    fake_refs.resolve.assert_not_called()
    assert resolver.stats["bare_rev_parse"] == 1
    assert resolver.stats["legacy_clone"] == 0


@pytest.mark.parametrize("lock_shape", ["matching", "unrelated", "sibling", "empty"])
def test_install_lock_presence_cannot_authorize_unseeded_bare_refs(
    monkeypatch, tmp_path, lock_shape: str
) -> None:
    """Only a concrete matching lock seed can avoid current upstream resolution."""
    from apm_cli.install.helpers.ref_seed import seed_ref_resolver_from_lockfile

    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    dep = _dep()
    bare = tmp_path / cache_shard_key(dep.to_github_url())
    bare.mkdir(parents=True)
    locked_ref = _dep(repo="other/package") if lock_shape == "unrelated" else _dep()
    if lock_shape == "sibling":
        locked_ref = DependencyReference(
            repo_url="owner/repo", reference="main", virtual_path="skills/sibling", is_virtual=True
        )
    locked = LockedDependency(
        repo_url=locked_ref.repo_url,
        virtual_path=locked_ref.virtual_path,
        is_virtual=locked_ref.is_virtual,
        resolved_ref="main",
        resolved_commit=SHA_A,
    )
    context = types.SimpleNamespace(
        ref_freshness_policy=None,
        update_refs=False,
        refresh=False,
        existing_lockfile=LockFile(
            dependencies={} if lock_shape == "empty" else {locked_ref.get_unique_key(): locked}
        ),
        logger=None,
    )
    downloader = MagicMock()
    downloader._refs.resolve_commit_sha_for_ref.return_value = None
    downloader._refs.resolve.return_value = ResolvedReference(
        original_ref=str(dep),
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=SHA_B,
        ref_name="main",
    )
    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        git_cache=types.SimpleNamespace(
            _db_root=tmp_path, read_resolved_ref=lambda *_: (False, None)
        ),
        freshness_policy=ref_freshness_policy_for_install(context),
    )
    assert resolver is not None
    context.ref_resolver = resolver
    seed_ref_resolver_from_lockfile(context)
    with patch.object(L2BareRevParse, "_rev_parse", return_value=SHA_A) as stale_l2:
        result = resolver.resolve(dep)
    assert result.resolved_commit == (SHA_A if lock_shape == "matching" else SHA_B)
    stale_l2.assert_not_called()
    if lock_shape == "matching":
        downloader._refs.resolve.assert_not_called()
    else:
        downloader._refs.resolve.assert_called_once_with(dep)
    if lock_shape == "sibling":
        assert resolver.resolve(locked_ref).resolved_commit == SHA_A
        other_unlocked = DependencyReference(
            repo_url="owner/repo", reference="main", virtual_path="skills/new", is_virtual=True
        )
        assert resolver.resolve(other_unlocked).resolved_commit == SHA_B
        downloader._refs.resolve.assert_called_once_with(dep)


@pytest.mark.parametrize(
    "policy", [RefFreshnessPolicy.CURRENT_REMOTE, RefFreshnessPolicy.LOCKED_OR_CURRENT]
)
def test_current_policy_fails_closed_when_remote_tiers_fail(monkeypatch, tmp_path, policy):
    """Freshness-required resolution never substitutes a stale L2 answer."""
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    bare = tmp_path / cache_shard_key(_dep().to_github_url())
    bare.mkdir(parents=True)
    downloader = MagicMock()
    fake_refs = MagicMock()
    fake_refs.resolve_commit_sha_for_ref.return_value = None
    fake_refs.resolve.side_effect = RuntimeError("remote unavailable")
    downloader._refs = fake_refs
    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        git_cache=types.SimpleNamespace(
            _db_root=tmp_path, read_resolved_ref=lambda *_: (False, None)
        ),
        freshness_policy=policy,
    )
    assert isinstance(resolver, TieredRefResolver)

    with (
        patch.object(L2BareRevParse, "_rev_parse", return_value=SHA_A) as stale_l2,
        pytest.raises(RuntimeError, match="remote unavailable"),
    ):
        resolver.resolve(_dep())

    stale_l2.assert_not_called()
    assert resolver._cache.size() == 0
    assert "bare_rev_parse" not in resolver.stats
