"""Per-invocation ``git ls-remote`` dedup for ``apm outdated``.

``apm outdated`` checks every locked dependency against its upstream by
calling ``downloader.list_remote_refs`` / ``list_remote_tag_refs``, each of
which issues a fresh ``git ls-remote`` (see
:class:`apm_cli.deps.git_reference_resolver.GitReferenceResolver`). When
several locked deps share one upstream repo -- e.g. multiple virtual
subdirectory packages carved out of the same monorepo -- that is one
redundant ``ls-remote`` per dep for identical output.

This wrapper collapses those redundant round-trips to one per
(repo-identity, ref-family) within a single ``apm outdated`` invocation. It
is the ``outdated``-surface analogue of the install-path RefResolver reuse
in :mod:`apm_cli.install.helpers.ref_reuse`, extending the dedup so the
optimization is pervasive across install and outdated.

Correctness: the cache is built fresh per invocation and never persisted, so
a newer upstream ref pushed between two ``apm outdated`` runs is still seen
on the next run. Within one run every same-repo dep observes one consistent
ls-remote snapshot, which cannot make ``outdated`` miss a newer upstream ref
(the snapshot is the freshest listing of this run). Failures are not cached,
preserving the per-dep ``unknown`` degradation of the un-wrapped path.

Auth-lens: the cache key is the repo *identity* (host, repo_url, port,
insecure-transport, ref-family) -- never a token. Two deps from one repo
resolve the same credential via ``AuthResolver`` and the same upstream refs,
so identity alone is a sufficient and non-secret key.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models.dependency.reference import DependencyReference
    from ..models.dependency.types import RemoteRef


def _identity_key(dep_ref: DependencyReference, include_heads: bool) -> tuple:
    """Return a non-secret cache key for *dep_ref*'s ls-remote listing.

    ``include_heads`` distinguishes the tags+branches listing
    (``list_remote_refs``) from the tags-only listing
    (``list_remote_tag_refs``): the two return different ref sets and must
    not share a bucket. No token is part of the key -- see module docstring.
    """
    return (
        dep_ref.host or "",
        dep_ref.repo_url or "",
        getattr(dep_ref, "port", None),
        bool(getattr(dep_ref, "is_insecure", False)),
        include_heads,
    )


class OutdatedRefCache:
    """Thread-safe, per-invocation ``ls-remote`` dedup around a downloader.

    Wraps a ``GitHubPackageDownloader`` so the two ref-listing methods used
    by ``apm outdated`` memoize their result per repo identity while every
    other attribute/method delegates to the wrapped downloader unchanged.
    Concurrent first-touches for the same key coalesce under a per-key lock
    so the parallel ``-j`` path still issues exactly one ``ls-remote``.
    """

    def __init__(self, downloader: Any) -> None:
        self._downloader = downloader
        self._results: dict[tuple, list[RemoteRef]] = {}
        self._key_locks: dict[tuple, threading.Lock] = {}
        self._master_lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        # Delegate everything not overridden here (registry/marketplace paths,
        # tiered-resolver wiring, etc.) to the wrapped downloader untouched.
        return getattr(self._downloader, name)

    def list_remote_refs(self, dep_ref: DependencyReference) -> list[RemoteRef]:
        return self._cached(dep_ref, include_heads=True)

    def list_remote_tag_refs(self, dep_ref: DependencyReference) -> list[RemoteRef]:
        return self._cached(dep_ref, include_heads=False)

    def _cached(self, dep_ref: DependencyReference, *, include_heads: bool) -> list[RemoteRef]:
        key = _identity_key(dep_ref, include_heads)
        with self._master_lock:
            cached = self._results.get(key)
            if cached is not None:
                return cached
            key_lock = self._key_locks.get(key)
            if key_lock is None:
                key_lock = threading.Lock()
                self._key_locks[key] = key_lock
        # Compute outside the master lock (so distinct repos fetch in
        # parallel) but under a per-key lock (so same-repo first-touches
        # coalesce to one ls-remote). Failures propagate uncached, matching
        # the un-wrapped per-dep 'unknown' degradation.
        with key_lock:
            cached = self._results.get(key)
            if cached is not None:
                return cached
            if include_heads:
                refs = self._downloader.list_remote_refs(dep_ref)
            else:
                refs = self._downloader.list_remote_tag_refs(dep_ref)
            self._results[key] = refs
            return refs
