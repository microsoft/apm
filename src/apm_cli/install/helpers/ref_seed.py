"""Seed the tiered ref resolver's L0 cache from the lockfile.

Extracted from :mod:`apm_cli.install.phases.resolve` to keep that phase
module within its LOC budget (see ``tests/unit/install/test_architecture_invariants.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def seed_ref_resolver_from_lockfile(ctx: InstallContext) -> None:
    """Seed the tiered ref resolver's L0 cache from the lockfile.

    For every locked dep that records a concrete ``resolved_commit`` for a
    named ``resolved_ref`` (a branch or tag), inject ``ref -> commit`` into
    the per-run cache BEFORE resolution runs. Any later
    ``resolve_git_reference()`` for that (repo, ref) then hits L0 and never
    fires the commits-API tier (L1) or a clone.

    This closes a gap the semver lockfile-replay path
    (``_maybe_resolve_git_semver``) does not cover: branch-pinned deps
    (e.g. ``#main``) and any dep whose lockfile entry lacks a
    ``resolved_tag`` re-resolve their ref over the network on every install
    even though the lockfile already holds the exact commit. Seeding makes
    those installs honour the lock with zero round-trips.

    Skipped when ``--update`` / ``--refresh`` is active (those modes
    intentionally re-resolve refs) or when no resolver / lockfile is present.
    Safe: the seeded SHA is the lockfile's own trust anchor; drift detection
    in ``download_callback`` still runs against the manifest ref, so a
    changed pin is still caught and re-resolved.

    Cache-key invariant: correctness relies on ``locked.repo_url`` and
    ``locked.resolved_ref`` being identical to the ``(repo_url, ref)`` pair
    that ``resolve()`` will look up at download time. Both originate from the
    same ``DependencyReference`` that was serialised into the lockfile, so
    the invariant holds today. Any future URL canonicalisation (e.g. ``.git``
    suffix stripping, case folding, or host-qualification) must be applied
    symmetrically at lockfile-write and lockfile-read time; an asymmetric
    change silently degrades the seed to a no-op (graceful fallback to the
    pre-PR resolve path, no incorrectness).
    """
    if ctx.update_refs or ctx.refresh:
        return
    resolver = getattr(ctx, "ref_resolver", None)
    lockfile = ctx.existing_lockfile
    if resolver is None or lockfile is None:
        return
    seed = getattr(resolver, "seed", None)
    if not callable(seed):
        return
    seeded = 0
    for locked in lockfile.get_all_dependencies():
        ref = getattr(locked, "resolved_ref", None)
        sha = getattr(locked, "resolved_commit", None)
        repo = getattr(locked, "repo_url", None)
        if repo and ref and sha and seed(repo, ref, sha):
            seeded += 1
    if seeded and ctx.logger:
        ctx.logger.verbose_detail(
            f"[i] Seeded ref resolver from lockfile: {seeded} ref(s) (0 round-trips)"
        )
