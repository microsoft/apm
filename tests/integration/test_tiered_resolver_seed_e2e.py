"""End-to-end proof that a lockfile-seeded warm resolve makes ZERO commits-API calls.

Companion to the resolver-unit tests in
``tests/unit/deps/test_tiered_ref_resolver.py`` and the seed-phase unit tests in
``tests/unit/install/phases/test_resolve_seed_from_lockfile.py``. Those exercise
``TieredRefResolver.seed`` and ``seed_ref_resolver_from_lockfile`` in isolation
against hand-built tier mocks / fake resolvers.

This module instead drives the claim through the REAL production wiring:

* the real :func:`seed_ref_resolver_from_lockfile` install-phase step,
* a real :class:`LockFile` / :class:`LockedDependency`,
* a real tier stack from :func:`build_tiered_ref_resolver` (L0 per-run cache ->
  L1 commits API -> L2 bare rev-parse -> L3 legacy clone), and
* the real :meth:`GitHubPackageDownloader.resolve_git_reference` facade that the
  install/update/outdated code paths call.

Only the innermost network seam -- the legacy
:class:`GitReferenceResolver` reached via ``downloader._refs`` -- is faked, so no
live GitHub commits API call or git clone ever runs. The fake counts the two
network entry points (``resolve_commit_sha_for_ref`` = commits API tier,
``resolve`` = legacy clone tier) so the test can assert ``commits_api == 0`` on
the warm, seeded path and prove the same resolve WOULD hit the commits API
without the seed (regression trap).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import types

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.tiered_ref_resolver import build_tiered_ref_resolver
from apm_cli.install.helpers.ref_seed import seed_ref_resolver_from_lockfile
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

REPO = "owner/repo"
BRANCH = "main"
# The commit the lockfile already recorded for owner/repo#main.
LOCK_SHA = "a" * 40
# A DIFFERENT sha the faked network would return -- lets us prove the seeded
# value (not a fresh network round-trip) is what the warm resolve returns.
NETWORK_SHA = "b" * 40


class _FakeLegacyRefs:
    """Stand-in for ``GitReferenceResolver`` (downloader._refs).

    Counts the two network entry points the tier stack can reach:

    * ``resolve_commit_sha_for_ref`` -- the cheap commits API used by the L1
      ``commits_api`` tier. Every call here is one GitHub commits-API round-trip
      in production.
    * ``resolve`` -- the shallow-clone legacy path used by the L3 tier.

    A warm, lockfile-seeded resolve must touch NEITHER.
    """

    def __init__(self) -> None:
        self.commits_api_calls = 0
        self.legacy_clone_calls = 0

    def resolve_commit_sha_for_ref(self, dep_ref, ref):
        self.commits_api_calls += 1
        return NETWORK_SHA

    def resolve(self, repo_ref):
        self.legacy_clone_calls += 1
        dep = DependencyReference.parse(repo_ref) if isinstance(repo_ref, str) else repo_ref
        return ResolvedReference(
            original_ref=str(dep),
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=NETWORK_SHA,
            ref_name=dep.reference or BRANCH,
        )


def _downloader_with_real_tiered_resolver():
    """Build a real downloader + real tiered resolver over a faked network seam.

    Mirrors what the install resolve phase's ``_setup_downloader`` does: attach a
    per-run :class:`TieredRefResolver` to ``downloader._tiered_resolver`` so
    ``resolve_git_reference`` routes through it. ``persistent_git_cache`` is left
    unset (git_cache=None) so the L2 bare-rev-parse tier is inert -- no disk, no
    network.
    """
    downloader = GitHubPackageDownloader.__new__(GitHubPackageDownloader)
    fake_refs = _FakeLegacyRefs()
    downloader._refs = fake_refs
    resolver = build_tiered_ref_resolver(downloader=downloader, git_cache=None)
    assert resolver is not None, "tiered resolver must be enabled by default"
    downloader._tiered_resolver = resolver
    return downloader, resolver, fake_refs


def _lockfile_with_branch_pin():
    """A real lockfile recording owner/repo#main -> LOCK_SHA (branch pin).

    Branch-pinned deps are exactly the gap the seed step closes: the semver
    lockfile-replay path does not cover them, so pre-PR they re-resolve ``main``
    over the commits API on every warm install even though the lock holds the
    exact commit.
    """
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url=REPO,
            resolved_ref=BRANCH,
            resolved_commit=LOCK_SHA,
        )
    )
    return lockfile


def _ctx(*, resolver, lockfile, update_refs=False, refresh=False):
    return types.SimpleNamespace(
        ref_resolver=resolver,
        existing_lockfile=lockfile,
        update_refs=update_refs,
        refresh=refresh,
        logger=None,
    )


def test_warm_seeded_resolve_makes_zero_commits_api_calls():
    """The end-to-end claim: a warm, lockfile-seeded resolve of a branch pin
    goes through the real ``resolve_git_reference`` facade with ZERO commits-API
    round-trips and returns the lockfile's own commit.

    Regression trap: if the seed step were removed (or the seed did not populate
    the exact ``(repo_url, ref)`` key the facade looks up), the branch ref would
    fall through L0 into the L1 commits-API tier -- ``commits_api`` would be 1 and
    the resolved commit would be ``NETWORK_SHA`` instead of ``LOCK_SHA``, failing
    both assertions below. See ``test_unseeded_resolve_would_hit_commits_api`` for
    the explicit before/after contrast.
    """
    downloader, resolver, fake_refs = _downloader_with_real_tiered_resolver()
    lockfile = _lockfile_with_branch_pin()

    # Real production seed step (what resolve.run() calls at phase start).
    seed_ref_resolver_from_lockfile(_ctx(resolver=resolver, lockfile=lockfile))

    # Real facade used by install/update/outdated to resolve a ref.
    result = downloader.resolve_git_reference(DependencyReference(repo_url=REPO, reference=BRANCH))

    # Warm resolve returns the lockfile's own commit -- not a fresh network sha.
    assert result.resolved_commit == LOCK_SHA
    # No network was touched: neither commits API nor legacy clone fired.
    assert fake_refs.commits_api_calls == 0
    assert fake_refs.legacy_clone_calls == 0
    # And the resolver's own diagnostics agree: L0 hit, commits-API tier silent.
    assert resolver.stats["commits_api"] == 0
    assert resolver.stats["per_run_cache"] == 1


def test_unseeded_resolve_would_hit_commits_api():
    """Control: the SAME real facade + tier stack, WITHOUT the seed step, does
    fire the commits-API tier for the branch ref. This is the behaviour the seed
    optimization eliminates -- so a regression that drops the seed would make the
    warm path look like this again.
    """
    downloader, resolver, fake_refs = _downloader_with_real_tiered_resolver()

    # No seed_ref_resolver_from_lockfile() call here.
    result = downloader.resolve_git_reference(DependencyReference(repo_url=REPO, reference=BRANCH))

    assert result.resolved_commit == NETWORK_SHA
    assert fake_refs.commits_api_calls == 1
    assert resolver.stats["commits_api"] == 1
    assert resolver.stats["per_run_cache"] == 0


def test_update_refs_skips_seed_and_reresolves_over_network():
    """``--update`` must preserve drift re-resolution: the seed step is skipped,
    so the warm resolve deliberately falls through to the commits-API tier and
    re-resolves the branch from the network. Proves the optimization does NOT
    degrade the intentional re-resolve paths end-to-end.
    """
    downloader, resolver, fake_refs = _downloader_with_real_tiered_resolver()
    lockfile = _lockfile_with_branch_pin()

    seed_ref_resolver_from_lockfile(_ctx(resolver=resolver, lockfile=lockfile, update_refs=True))

    result = downloader.resolve_git_reference(DependencyReference(repo_url=REPO, reference=BRANCH))

    # Seed was skipped -> commits-API tier re-resolves the ref.
    assert result.resolved_commit == NETWORK_SHA
    assert fake_refs.commits_api_calls == 1
    assert resolver.stats["commits_api"] == 1
