"""Tiered git reference resolver -- collapses redundant clones (#1369).

Issue #1369: ``apm update -y -v`` ran the integrate loop serially, calling
``ctx.downloader.resolve_git_reference(dep_ref)`` per-dep. Each call did
``mkdtemp`` + a shallow ``git clone --depth=1`` with ZERO memoization.
A 9-dep manifest pointing at 3 unique (repo, ref) tuples produced 9
clones. On Windows + Defender + a slow ADO endpoint that scaled to 1583s.

This module collapses that work via a four-tier waterfall executed by
:class:`TieredRefResolver`:

* **L0 PerRunCache** -- in-memory ``{(url, ref): sha}``. Zero I/O.
  Catches the duplicate-within-run case (9 deps -> 3 underlying resolves).
* **L1 CommitsAPI** -- cheap ``GET /repos/.../commits/{ref}`` against the
  GitHub-family host_backend, with ``Accept: application/vnd.github.sha``
  + optional ``HttpCache`` ETag. ~1 RTT.
* **L2 BareRevParse** -- if the cross-run :class:`GitCache` already has a
  bare clone of the URL, ``git rev-parse refs/heads/REF`` against it.
  Zero network. Catches the second-run case.
* **L3 LegacyClone** -- delegates to the legacy
  :meth:`GitReferenceResolver.resolve` (shallow clone + introspect).
  Behaviourally identical to the pre-#1369 path; always succeeds or
  raises.

The resolver reuses every existing performance primitive APM already
ships -- ``HttpCache`` (ETag), ``GitCache`` (bare rev-parse),
``host_backends`` (commits API URL), ``_resilient_get`` (HTTP + retries),
``AuthResolver`` (token resolution) -- and adds only the orchestrator
plus the per-run cache. No new infrastructure.

Concurrency: a coalesce lock around ``(url, ref)`` ensures that
concurrent ``resolve()`` calls for the same key block on a single
in-flight resolution rather than racing. Makes the resolver safe for
``apm outdated``'s ThreadPoolExecutor and for any future parallel
integrate phase.

Feature flag: setting ``APM_TIERED_RESOLVER=0`` (or any value other than
``1``/``true``/``yes``) disables the tiered stack entirely; callers fall
through to the legacy clone path. Default ON.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..models.apm_package import (
    DependencyReference,
    GitReferenceType,
    ResolvedReference,
)
from ..utils.github_host import default_host

if TYPE_CHECKING:
    from ..cache.git_cache import GitCache
    from ..deps.git_reference_resolver import GitReferenceResolver
    from ..deps.github_downloader import GitHubPackageDownloader

_log = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[a-f0-9]{40}$", re.IGNORECASE)


def is_tiered_resolver_enabled() -> bool:
    """Read the ``APM_TIERED_RESOLVER`` env flag. Default ON.

    Set to ``0``/``false``/``no`` to disable the tiered stack and force
    every resolution through the legacy clone path. Useful as an
    emergency rollback without redeploying.
    """
    val = os.environ.get("APM_TIERED_RESOLVER", "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# Per-run cache
# ---------------------------------------------------------------------------


class PerRunRefCache:
    """Thread-safe in-memory ``{(url, ref): sha}`` cache.

    Lives for the duration of one install/update/outdated run. Cleared
    by simply dropping the surrounding :class:`TieredRefResolver`.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def get(self, url: str, ref: str) -> str | None:
        with self._lock:
            return self._store.get((url, ref))

    def put(self, url: str, ref: str, sha: str) -> None:
        with self._lock:
            self._store[(url, ref)] = sha

    def size(self) -> int:
        with self._lock:
            return len(self._store)


# ---------------------------------------------------------------------------
# Tier Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RefResolutionTier(Protocol):
    """Single resolution strategy. Returns the SHA or ``None`` to fall through."""

    name: str

    def try_resolve(self, dep_ref: DependencyReference, ref: str) -> str | None: ...


# ---------------------------------------------------------------------------
# L0: in-memory cache lookup
# ---------------------------------------------------------------------------


@dataclass
class L0PerRunCache:
    """Read-only view over :class:`PerRunRefCache` for tier dispatch."""

    cache: PerRunRefCache
    name: str = "per_run_cache"

    def try_resolve(self, dep_ref: DependencyReference, ref: str) -> str | None:
        return self.cache.get(dep_ref.repo_url, ref)


# ---------------------------------------------------------------------------
# L1: cheap commits API
# ---------------------------------------------------------------------------


class L1CommitsAPI:
    """Resolve via the GitHub-family commits API.

    Delegates to :meth:`GitReferenceResolver.resolve_commit_sha_for_ref` --
    the cheap-path helper that already dispatches through
    ``host_backends.build_commits_api_url`` and ``host._resilient_get``,
    inheriting that helper's auth + retry behavior. Returns ``None`` for
    hosts whose backend has no cheap commits endpoint (e.g. ADO today);
    the caller then falls through to L2/L3.

    Future: an explicit :class:`HttpCache` ETag pass could be added here
    for unauthenticated requests (the underlying helper does not yet
    integrate with the on-disk HTTP cache). Out of scope for #1369.
    """

    name = "commits_api"

    def __init__(self, host: object) -> None:
        self._host = host

    def try_resolve(self, dep_ref: DependencyReference, ref: str) -> str | None:
        if _SHA_RE.match(ref or ""):
            return ref.lower()

        try:
            if dep_ref.is_artifactory() or dep_ref.is_azure_devops():
                return None
        except Exception:
            return None

        # Fast path: delegate to the existing cheap-API helper. It
        # already handles host backend dispatch, token resolution,
        # _resilient_get, and the GitHub sha-accept Content-Type. We do
        # NOT duplicate that code here; tier-1's value-add over the
        # raw helper is the orchestrator-level caching + coalescing.
        try:
            resolver = getattr(self._host, "_refs", None)
            if resolver is None:
                return None
            sha = resolver.resolve_commit_sha_for_ref(dep_ref, ref)
            if sha and _SHA_RE.match(sha):
                return sha.lower()
            return None
        except Exception as exc:
            _log.debug("L1 commits API failed for %s@%s: %s", dep_ref.repo_url, ref, exc)
            return None


# ---------------------------------------------------------------------------
# L2: bare-repo rev-parse (zero network)
# ---------------------------------------------------------------------------


class L2BareRevParse:
    """Resolve by ``git rev-parse`` against an already-cached bare clone.

    No network. Hits only when :class:`GitCache` has a bare clone of the
    URL from a previous run. Cheap follow-up tier after L0/L1 miss --
    catches the common ``apm install`` -> ``apm update`` second-run case
    where the bare exists but the cheap API is unavailable (e.g. ADO).
    """

    name = "bare_rev_parse"

    def __init__(self, git_cache: GitCache | None) -> None:
        self._git_cache = git_cache

    def try_resolve(self, dep_ref: DependencyReference, ref: str) -> str | None:
        if self._git_cache is None or not ref:
            return None
        if _SHA_RE.match(ref):
            return ref.lower()

        try:
            from ..cache.url_normalize import cache_shard_key
        except Exception:
            return None

        try:
            shard_key = cache_shard_key(dep_ref.to_github_url())
        except Exception:
            return None

        # Reach into GitCache's bare DB dir. We avoid calling
        # GitCache.get_checkout() (which would trigger a fresh clone +
        # ls-remote if missing). Bare path layout is stable per
        # cache/git_cache.py:226.
        try:
            bare_dir = self._git_cache._db_root / shard_key
        except Exception:
            return None

        if not bare_dir.is_dir():
            return None

        return self._rev_parse(bare_dir, ref)

    @staticmethod
    def _rev_parse(bare_dir: Path, ref: str) -> str | None:
        import subprocess

        from ..utils.git_env import get_git_executable, git_subprocess_env

        git_exe = get_git_executable()
        env = git_subprocess_env()

        # Try refs/heads/REF, refs/tags/REF, then REF (covers branch,
        # tag, and SHA prefix). All against the existing bare; no
        # network. ``--verify`` ensures we only accept an exact match.
        candidates = (f"refs/heads/{ref}", f"refs/tags/{ref}", ref)
        for candidate in candidates:
            try:
                result = subprocess.run(
                    [git_exe, "-C", str(bare_dir), "rev-parse", "--verify", candidate],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=env,
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            if result.returncode == 0:
                sha = (result.stdout or "").strip().lower()
                if _SHA_RE.match(sha):
                    return sha
        return None


# ---------------------------------------------------------------------------
# L3: legacy clone path (always-correct fallback)
# ---------------------------------------------------------------------------


class L3LegacyClone:
    """Delegate to the pre-#1369 :meth:`GitReferenceResolver.resolve`.

    Behaviourally identical to today's path. Always returns a SHA on
    success or raises. The whole point of L0-L2 is to AVOID calling
    this tier -- it does the shallow-clone-into-tempdir that #1369 was
    about.
    """

    name = "legacy_clone"

    def __init__(self, legacy_resolver: GitReferenceResolver) -> None:
        self._legacy = legacy_resolver

    def try_resolve(self, dep_ref: DependencyReference, ref: str) -> str | None:
        try:
            resolved = self._legacy.resolve(dep_ref)
        except Exception as exc:
            _log.debug("L3 legacy resolve failed for %s: %s", dep_ref.repo_url, exc)
            return None
        sha = getattr(resolved, "resolved_commit", None)
        if sha and _SHA_RE.match(sha):
            return sha.lower()
        return None

    def resolve_full(self, dep_ref: DependencyReference) -> ResolvedReference:
        """Return the full :class:`ResolvedReference` from the legacy path.

        Used as the final escape hatch when no tier produced a SHA but
        the caller still needs a structured result (e.g. artifactory
        deps which have ``resolved_commit=None`` by design).
        """
        return self._legacy.resolve(dep_ref)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class TieredRefResolver:
    """Run the four-tier waterfall with per-run dedup + coalescing.

    Lifetime: per install/update/outdated run. The factory
    :func:`build_tiered_ref_resolver` constructs an instance during the
    resolve phase; it is assigned to both ``ctx.ref_resolver`` and
    ``downloader._tiered_resolver`` so any subsequent code path that
    calls ``downloader.resolve_git_reference()`` automatically benefits.
    """

    def __init__(
        self,
        tiers: list[RefResolutionTier],
        cache: PerRunRefCache,
        legacy: L3LegacyClone,
    ) -> None:
        self._tiers = tiers
        self._cache = cache
        self._legacy = legacy
        self._coalesce: dict[tuple[str, str], threading.Event] = {}
        self._coalesce_lock = threading.Lock()
        # Diagnostics: counts per tier across the run. Read by tests.
        self.stats: dict[str, int] = {tier.name: 0 for tier in tiers}
        self.stats["coalesced"] = 0
        # Zero-I/O resolves of an already-concrete SHA. Tracked separately
        # so verbose tier stats do not inflate the commits-API count.
        self.stats["sha_passthrough"] = 0

    def seed(self, repo_url: str, ref: str, sha: str) -> bool:
        """Pre-populate the L0 per-run cache with a known ``ref -> sha``.

        Used by the resolve phase to inject a lockfile-recorded commit
        (``resolved_commit``) for a named ``resolved_ref`` -- a branch OR a
        tag name, whichever the lockfile recorded -- BEFORE any download
        runs, so the subsequent ``resolve()`` for that same ref gets an L0
        hit and the commits-API tier (L1) never fires. ``ref`` must be the
        exact ref string ``resolve()`` will look up. Idempotent; a no-op
        unless ``sha`` is a full 40-char hex commit and ``ref`` is
        non-empty. Returns ``True`` when a value was stored.

        Safe because the seeded SHA is the lockfile's own trust anchor --
        the same value ``resolve()`` would otherwise fetch from the network
        and cache. No behavior change beyond eliminating the round-trip.
        """
        if not ref or not sha or not _SHA_RE.match(sha):
            return False
        self._cache.put(repo_url, ref, sha.lower())
        return True

    def resolve(self, repo_ref: str | DependencyReference) -> ResolvedReference:
        """Resolve a git reference, dispatching through the tier waterfall.

        Mirrors :meth:`GitReferenceResolver.resolve` in signature and
        return type so callers swap in without code changes elsewhere.
        """
        dep_ref = self._normalize(repo_ref)
        ref = dep_ref.reference or None

        # No ref or artifactory: fall straight through to legacy so we
        # preserve the existing default-branch + artifactory paths
        # without re-implementing them per-tier.
        if not ref or dep_ref.is_artifactory():
            return self._legacy.resolve_full(dep_ref)

        # Ref is already a concrete commit SHA (e.g. the download ref was
        # rewritten from the lockfile): resolution is a no-op with zero I/O.
        # Count it distinctly so verbose tier stats reflect *real* network
        # round-trips -- previously this fell into the commits-API tier and
        # inflated ``commits_api`` even though no HTTP call was made.
        if _SHA_RE.match(ref):
            self.stats["sha_passthrough"] = self.stats.get("sha_passthrough", 0) + 1
            return self._build_result(dep_ref, ref, ref.lower(), tier_name="sha_passthrough")

        key = (dep_ref.repo_url, ref)

        # Fast path: cache hit avoids both tier dispatch and the
        # coalesce lock entirely.
        cached = self._cache.get(*key)
        if cached:
            self.stats["per_run_cache"] += 1
            return self._build_result(dep_ref, ref, cached, tier_name="per_run_cache")

        # Coalesce concurrent resolves of the same key.
        with self._coalesce_lock:
            existing = self._coalesce.get(key)
            if existing is None:
                self._coalesce[key] = threading.Event()
                leader = True
            else:
                leader = False

        if not leader:
            existing.wait()
            self.stats["coalesced"] += 1
            cached = self._cache.get(*key)
            if cached:
                return self._build_result(dep_ref, ref, cached, tier_name="coalesced")
            # Leader failed -- fall through and try ourselves.

        try:
            sha = self._dispatch(dep_ref, ref)
            if sha:
                self._cache.put(*key, sha)
                return self._build_result(dep_ref, ref, sha, tier_name=self._last_tier)
            # All tiers returned None and L3 included -- this is
            # genuine failure (e.g. ref not found). Re-raise from
            # legacy so callers get the original error message.
            return self._legacy.resolve_full(dep_ref)
        finally:
            if leader:
                with self._coalesce_lock:
                    event = self._coalesce.pop(key, None)
                if event is not None:
                    event.set()

    def _dispatch(self, dep_ref: DependencyReference, ref: str) -> str | None:
        self._last_tier = "none"
        for tier in self._tiers:
            try:
                sha = tier.try_resolve(dep_ref, ref)
            except Exception as exc:
                _log.debug("Tier %s raised: %s", tier.name, exc)
                continue
            if sha and _SHA_RE.match(sha):
                self.stats[tier.name] = self.stats.get(tier.name, 0) + 1
                self._last_tier = tier.name
                return sha.lower()
        return None

    @staticmethod
    def _normalize(repo_ref: str | DependencyReference) -> DependencyReference:
        if isinstance(repo_ref, DependencyReference):
            return repo_ref
        try:
            return DependencyReference.parse(repo_ref)
        except ValueError as exc:
            raise ValueError(f"Invalid repository reference '{repo_ref}': {exc}") from exc

    @staticmethod
    def _build_result(
        dep_ref: DependencyReference,
        ref: str,
        sha: str,
        *,
        tier_name: str,
    ) -> ResolvedReference:
        # Choose ref_type heuristically. The cheap tiers do not
        # distinguish branch vs tag (a single SHA-returning API call
        # cannot tell you which container the ref lives in without a
        # second call). We pick COMMIT when the input *was* a SHA-like
        # string and BRANCH otherwise. Callers that need precise
        # distinction (today: cache eligibility in integrate.py:77)
        # already treat BRANCH and TAG identically when the SHA
        # matches the lockfile, so the heuristic is behaviour-safe.
        ref_type = GitReferenceType.COMMIT if _SHA_RE.match(ref) else GitReferenceType.BRANCH
        _log.debug(
            "TieredRefResolver: %s @ %s -> %s (via %s)",
            dep_ref.repo_url,
            ref,
            sha[:12],
            tier_name,
        )
        return ResolvedReference(
            original_ref=str(dep_ref),
            ref_type=ref_type,
            resolved_commit=sha,
            ref_name=ref,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_tiered_ref_resolver(
    *,
    downloader: GitHubPackageDownloader,
    git_cache: GitCache | None = None,
) -> TieredRefResolver | None:
    """Construct the production tier stack, or ``None`` if disabled.

    Returns ``None`` when ``APM_TIERED_RESOLVER`` is disabled so callers
    can opt out by simply leaving ``downloader._tiered_resolver = None``;
    the downloader facade falls through to the legacy resolver in that
    case.
    """
    _ = default_host  # keep import side-effect for compat with monkeypatched tests
    if not is_tiered_resolver_enabled():
        _log.debug("TieredRefResolver disabled via APM_TIERED_RESOLVER env var")
        return None

    cache = PerRunRefCache()
    legacy_inner = getattr(downloader, "_refs", None)
    if legacy_inner is None:
        # Pathological: downloader without a legacy resolver. Bail out
        # so callers fall through to whatever path they had before.
        return None
    legacy = L3LegacyClone(legacy_inner)

    tiers: list[RefResolutionTier] = [
        L0PerRunCache(cache=cache),
        L1CommitsAPI(host=downloader),
        L2BareRevParse(git_cache=git_cache),
        legacy,
    ]

    return TieredRefResolver(tiers=tiers, cache=cache, legacy=legacy)
