"""Unit tests for ``seed_ref_resolver_from_lockfile`` (resolve phase).

Verifies the lockfile-seeding step that pre-populates the tiered ref
resolver's L0 cache so branch-pinned / tagless locked deps honour the lock
with zero network round-trips. Companion to the resolver-level tests in
``tests/unit/deps/test_tiered_ref_resolver.py``.
"""

from __future__ import annotations

import os
import re
import sys
import types
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "src"))

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.tiered_ref_resolver import (
    RefFreshnessPolicy,
    build_tiered_ref_resolver,
)
from apm_cli.drift import detect_ref_change
from apm_cli.install.helpers.ref_seed import seed_ref_resolver_from_lockfile
from apm_cli.models.dependency.reference import DependencyReference

_FAKE_SHA_RE = re.compile(r"^[a-f0-9]{40}$", re.IGNORECASE)


class _FakeResolver:
    """Records seed() calls the way TieredRefResolver.seed behaves."""

    def __init__(self):
        self.seeded: list[tuple[str, str, str]] = []

    def seed(self, repo_url, ref, sha):
        # Mirror the real guard: full 40-char hex SHA + non-empty ref.
        if not ref or not sha or not _FAKE_SHA_RE.match(sha):
            return False
        self.seeded.append((repo_url.repo_url, repo_url.host, ref, sha))
        return True


def _locked(repo_url, ref, sha, *, host="github.com"):
    return types.SimpleNamespace(
        repo_url=repo_url,
        resolved_ref=ref,
        resolved_commit=sha,
        to_dependency_ref=lambda: DependencyReference(repo_url=repo_url, host=host, reference=ref),
    )


class _FakeLockfile:
    def __init__(self, deps):
        self._deps = deps

    def get_all_dependencies(self):
        return self._deps


def _ctx(*, resolver, lockfile, update_refs=False, refresh=False):
    return types.SimpleNamespace(
        ref_resolver=resolver,
        ref_freshness_policy=RefFreshnessPolicy.for_install_intent(
            update_refs=update_refs,
            refresh=refresh,
        ),
        existing_lockfile=lockfile,
        update_refs=update_refs,
        refresh=refresh,
        logger=None,
    )


SHA = "a" * 40


@pytest.mark.parametrize("stale_repo", ["owner/repo", "removed/package"])
def test_unclassifiable_historical_seed_allows_current_resolution(
    monkeypatch: pytest.MonkeyPatch, stale_repo: str
) -> None:
    """Both matching and removed provider hints leave valid current work resolvable."""
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    historical = LockedDependency(
        repo_url=stale_repo,
        host="code.example.com",
        host_type="gitlab",
        resolved_ref="main",
        resolved_commit=SHA,
    )
    historical_ref = historical.to_dependency_ref()
    monkeypatch.setenv("GITHUB_HOST", "code.example.com")
    declaration = {"git": "https://code.example.com/owner/repo", "ref": "main"}
    current = DependencyReference.parse_from_dict(declaration)
    assert detect_ref_change(current, historical) is True
    valid_locked = LockedDependency(
        repo_url="still/locked", host="code.example.com", resolved_ref="main", resolved_commit=SHA
    )
    downloader = MagicMock()
    downloader._refs.resolve_commit_sha_for_ref.return_value = "b" * 40
    resolver = build_tiered_ref_resolver(
        downloader=downloader, freshness_policy=RefFreshnessPolicy.LOCKED_OR_CURRENT
    )
    assert resolver is not None
    context = _ctx(
        resolver=resolver,
        lockfile=LockFile(
            dependencies={
                historical_ref.get_unique_key(): historical,
                valid_locked.get_unique_key(): valid_locked,
            }
        ),
    )
    context.logger = MagicMock()
    seed_ref_resolver_from_lockfile(context)
    context.logger.verbose_detail.assert_called_once_with(
        "[i] Seeded ref resolver from lockfile: 1 ref(s) (0 round-trips)"
    )
    assert len(resolver._lock_seeds) == 1
    assert resolver._cache.size() == 0
    assert resolver.resolve(current).resolved_commit == "b" * 40
    downloader._refs.resolve_commit_sha_for_ref.assert_called_once_with(current, "main")
    assert resolver.resolve(valid_locked.to_dependency_ref()).resolved_commit == SHA
    downloader._refs.resolve.assert_not_called()
    assert resolver.remotely_resolved(current, "b" * 40) is False

    # Historical rejection must not turn invalid current declarations into success,
    # even after a fresh repository answer is cached.
    invalid_current = DependencyReference.parse_from_dict({**declaration, "type": "gitlab"})
    with pytest.raises(ValueError, match="conflicts with recognized 'ghes'"):
        resolver.resolve(invalid_current)
    downloader._refs.resolve_commit_sha_for_ref.assert_called_once_with(current, "main")


def test_seeds_branch_and_tag_refs():
    resolver = _FakeResolver()
    lockfile = _FakeLockfile(
        [
            _locked("owner/repo", "main", SHA),  # branch pin
            _locked("owner/repo", "pkg--v1.2.3", "b" * 40),  # tag pin
        ]
    )
    seed_ref_resolver_from_lockfile(_ctx(resolver=resolver, lockfile=lockfile))
    assert resolver.seeded == [
        ("owner/repo", "github.com", "main", SHA),
        ("owner/repo", "github.com", "pkg--v1.2.3", "b" * 40),
    ]


def test_skips_when_update_refs():
    resolver = _FakeResolver()
    lockfile = _FakeLockfile([_locked("owner/repo", "main", SHA)])
    seed_ref_resolver_from_lockfile(_ctx(resolver=resolver, lockfile=lockfile, update_refs=True))
    assert resolver.seeded == []


def test_skips_when_refresh():
    resolver = _FakeResolver()
    lockfile = _FakeLockfile([_locked("owner/repo", "main", SHA)])
    seed_ref_resolver_from_lockfile(_ctx(resolver=resolver, lockfile=lockfile, refresh=True))
    assert resolver.seeded == []


def test_noop_without_lockfile_or_resolver():
    resolver = _FakeResolver()
    # No lockfile
    seed_ref_resolver_from_lockfile(_ctx(resolver=resolver, lockfile=None))
    assert resolver.seeded == []
    # No resolver
    lockfile = _FakeLockfile([_locked("owner/repo", "main", SHA)])
    seed_ref_resolver_from_lockfile(_ctx(resolver=None, lockfile=lockfile))  # must not raise


def test_skips_entries_missing_ref_or_commit():
    resolver = _FakeResolver()
    lockfile = _FakeLockfile(
        [
            _locked("owner/repo", None, SHA),  # no ref
            _locked("owner/repo", "main", None),  # no commit
            _locked(None, "main", SHA),  # no repo
            _locked("owner/repo", "main", SHA),  # valid
        ]
    )
    seed_ref_resolver_from_lockfile(_ctx(resolver=resolver, lockfile=lockfile))
    assert resolver.seeded == [("owner/repo", "github.com", "main", SHA)]


def test_skips_non_hex_commit_of_correct_length():
    """A 40-char but non-hex resolved_commit is rejected by the guard."""
    resolver = _FakeResolver()
    not_hex = "z" * 40  # correct length, not a hex SHA
    lockfile = _FakeLockfile(
        [
            _locked("owner/repo", "main", not_hex),
            _locked("owner/repo", "release", SHA),  # valid
        ]
    )
    seed_ref_resolver_from_lockfile(_ctx(resolver=resolver, lockfile=lockfile))
    assert resolver.seeded == [("owner/repo", "github.com", "release", SHA)]
