"""Upstream resolution layer: turn ``UpstreamPackageEntry`` into a
fully-resolved, immutable plugin source ready for emission.

This module sits between the schema layer (which only validates shape)
and the :class:`MarketplaceBuilder` (which assembles the final
``marketplace.json`` and lockfile). It owns three invariants the
builder cannot reasonably express on its own:

1. **Atomic-fetch invariant.** Each registered :class:`Upstream` is
   fetched and strict-parsed AT MOST ONCE per build, even when many
   packages reference the same upstream. Multiple fetches would let a
   poisoned mid-build response slip past the SHA pin recorded in the
   first fetch.

2. **Repo-rename guard.** GitHub's transparent rename redirect would
   otherwise let a renamed repo silently change identity between
   builds. We compare the API-reported ``full_name`` against the
   configured ``upstream.repo`` and fail closed on mismatch
   (supply-chain panel item 4).

3. **Precedence ladder.** Curator-supplied ``ref`` > curator-supplied
   ``version`` (semver range) > upstream plugin's pinned ``ref`` >
   upstream registration ``ref`` > ``branch`` HEAD when ``allow_head``
   is opted in. Anything else is an unpinned-build error so the
   builder fails loudly rather than emitting a non-reproducible
   ``marketplace.json``.

The resolver is wired into the builder via dependency injection: cache,
ref-resolution, repo-identity, and version-range resolution are all
callables passed at construction time so unit tests never touch the
network.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .upstream_cache import UpstreamCache, UpstreamCacheError, compute_cache_key
from .upstream_parser import (
    StrictManifest,
    StrictPlugin,
    StrictRejection,
    parse_marketplace_strict,
)
from .yml_schema import Upstream, UpstreamPackageEntry

logger = logging.getLogger(__name__)


__all__ = [
    "RepoRenameError",
    "ResolvedUpstreamPackage",
    "UpstreamResolutionError",
    "UpstreamResolver",
    "UpstreamResolverDiagnostic",
]


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# Shared 40-char SHA pattern.
from .ref_resolver import FULL_SHA_RE as _FULL_SHA_RE  # noqa: E402

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UpstreamResolutionError(Exception):
    """Raised when an upstream-sourced package cannot be resolved.

    Carries a stable ``code`` so the builder can map resolver failures
    to ``BuildDiagnostic`` rows without parsing message text.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RepoRenameError(UpstreamResolutionError):
    """The upstream's reported ``full_name`` does not match the registration."""

    def __init__(self, configured: str, reported: str) -> None:
        super().__init__(
            "repo-rename-detected",
            (
                f"upstream repo identity mismatch: configured "
                f"{configured!r} but GitHub reports {reported!r}. "
                f"Refusing to fetch -- update upstream registration to "
                f"the canonical name or remove the upstream."
            ),
        )
        self.configured = configured
        self.reported = reported


# ---------------------------------------------------------------------------
# Diagnostic + result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpstreamResolverDiagnostic:
    """A non-fatal diagnostic emitted during resolution.

    The builder lifts these into :class:`BuildDiagnostic` rows. Levels
    parallel the builder's vocabulary: ``"warning"`` for unpinned-but-
    allowed entries; ``"error"`` for the rejections collected during
    strict parsing.
    """

    level: str  # "warning" | "error"
    code: str
    message: str
    upstream_alias: str = ""
    plugin_name: str = ""


@dataclass(frozen=True)
class ResolvedUpstreamPackage:
    """A fully-resolved upstream-sourced package, ready for emission.

    Attributes mirror the data the builder needs to:
    1. Emit a vanilla Anthropic-conformant entry in the curator's
       ``marketplace.json`` (no ``metadata.apm.*`` keys).
    2. Record full provenance in the lockfile's ``upstreams:`` block.
    """

    entry: UpstreamPackageEntry
    upstream: Upstream
    plugin: StrictPlugin
    # Final resolved coordinates of the *plugin*. ``ref`` is the user-
    # facing pin (sha or tag); ``sha`` is the immutable commit SHA
    # the lockfile records. ``ref`` and ``sha`` MAY be equal (when the
    # curator pinned a SHA directly).
    plugin_host: str
    plugin_repo: str
    plugin_subdir: str | None
    plugin_ref: str | None  # may be None for HEAD-tracking entries
    plugin_sha: str | None  # may be None when ref-resolution deferred
    # The upstream-registration provenance we need in the lockfile.
    upstream_manifest_sha: str
    upstream_canonical_full_name: str
    # Source of the resolved ref, for diagnostics + lockfile metadata.
    # Values: "curator-ref" | "curator-version" | "upstream-pin" |
    # "upstream-registration-ref" | "branch-head".
    pin_source: str


# ---------------------------------------------------------------------------
# Callable types for dependency injection
# ---------------------------------------------------------------------------

# (host, owner, repo, ref-or-branch) -> 40-char SHA. Used to collapse
# branches/tags to immutable SHAs before they reach the cache key.
RefToShaResolver = Callable[[str, str, str, str], str]

# (host, owner, repo) -> canonical "owner/repo" as reported by the
# git host's API. Empty string means "unknown -- skip rename check".
CanonicalFullNameResolver = Callable[[str, str, str], str]

# (host, owner, repo, semver_range, *, tag_pattern, include_prerelease)
# -> (resolved_ref, resolved_sha). Used for curator-supplied
# ``version:`` semver ranges on UpstreamPackageEntry.
VersionRangeResolver = Callable[..., tuple[str, str]]


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


@dataclass
class _UpstreamFetchRecord:
    """Internal cache of a per-upstream fetch result.

    Keyed by upstream alias to enforce the atomic-fetch invariant.
    """

    manifest: StrictManifest
    manifest_sha: str  # the resolved SHA used as the cache key
    canonical_full_name: str
    fetched: bool = True


class UpstreamResolver:
    """Resolves :class:`UpstreamPackageEntry` values into emission-ready records.

    Construction parameters
    -----------------------
    upstreams
        The full :class:`Upstream` collection from
        :class:`MarketplaceConfig`, indexed by ``alias`` for O(1)
        lookup.
    cache
        :class:`UpstreamCache` instance used for upstream
        ``marketplace.json`` storage. Tests inject one rooted at a
        ``tmp_path``.
    ref_to_sha
        Callable that resolves a ref/branch to a 40-char SHA against
        the *upstream marketplace's* repo. Required so the cache key
        is always SHA-keyed even when the curator pinned a tag/branch.
    canonical_full_name
        Callable that returns the upstream host's canonical
        ``owner/repo`` for the registration. Used for the rename
        guard. May return empty to skip the check (offline rebuilds).
    version_range_resolver
        Callable used to turn a curator-supplied ``version:`` semver
        range into a concrete ref+SHA against the *upstream plugin's*
        repo (NOT the upstream marketplace's repo). Optional: when
        omitted, version ranges raise ``UpstreamResolutionError``.
    auth_resolver
        Optional :class:`AuthResolver` instance threaded into cache
        fetches.
    offline
        When true, cache misses raise instead of fetching. Maps to
        the builder's ``--offline`` flag (e.g. lockfile rebuild
        reproducibility check).
    """

    def __init__(
        self,
        upstreams: Mapping[str, Upstream],
        *,
        cache: UpstreamCache,
        ref_to_sha: RefToShaResolver,
        canonical_full_name: CanonicalFullNameResolver | None = None,
        version_range_resolver: VersionRangeResolver | None = None,
        auth_resolver: Any = None,
        offline: bool = False,
    ) -> None:
        self._upstreams: dict[str, Upstream] = dict(upstreams)
        self._cache = cache
        self._ref_to_sha = ref_to_sha
        self._canonical_full_name = canonical_full_name
        self._version_range_resolver = version_range_resolver
        self._auth_resolver = auth_resolver
        self._offline = offline

        self._fetched: dict[str, _UpstreamFetchRecord] = {}
        self._diagnostics: list[UpstreamResolverDiagnostic] = []

    # -- Public API ---------------------------------------------------------

    @property
    def diagnostics(self) -> tuple[UpstreamResolverDiagnostic, ...]:
        """Diagnostics collected during resolution (immutable view)."""
        return tuple(self._diagnostics)

    def resolve_all(
        self,
        entries: list[UpstreamPackageEntry],
    ) -> tuple[list[ResolvedUpstreamPackage], list[UpstreamResolverDiagnostic]]:
        """Resolve a batch of upstream-sourced packages.

        Continues on per-package failures so the builder can report
        every error at once instead of bailing on the first. Critical
        upstream-level failures (rename, missing alias, fetch error)
        propagate to the upstream's *every* dependent package as
        per-package diagnostics so the curator sees the cascade.

        Resolution is intentionally sequential. Build-time concurrency
        was considered and deferred: the per-upstream cache layer is
        the bottleneck, manifest fetches dominate wall-clock, and
        most curator marketplaces have a small number of upstreams
        (single digits). Threading would obscure error attribution
        without a measurable speedup at the current scale and is
        revisited only if a curator profile produces evidence
        otherwise.
        """
        resolved: list[ResolvedUpstreamPackage] = []
        for entry in entries:
            try:
                resolved.append(self.resolve_package(entry))
            except UpstreamResolutionError as exc:
                self._diagnostics.append(
                    UpstreamResolverDiagnostic(
                        level="error",
                        code=exc.code,
                        message=str(exc),
                        upstream_alias=entry.upstream_alias,
                        plugin_name=entry.plugin or entry.name,
                    )
                )
        return resolved, list(self._diagnostics)

    def resolve_package(self, entry: UpstreamPackageEntry) -> ResolvedUpstreamPackage:
        """Resolve a single upstream-sourced package."""
        upstream = self._upstreams.get(entry.upstream_alias)
        if upstream is None:
            raise UpstreamResolutionError(
                "unknown-upstream-alias",
                (
                    f"package {entry.name!r} references upstream "
                    f"alias {entry.upstream_alias!r} which is not "
                    f"registered. Run 'apm marketplace upstream add' "
                    f"or fix the alias."
                ),
            )

        record = self._get_or_fetch_upstream(upstream)

        plugin_name = entry.plugin or entry.name
        plugin = record.manifest.find_plugin(plugin_name)
        if plugin is None:
            available = ", ".join(p.name for p in record.manifest.plugins) or "<none>"
            raise UpstreamResolutionError(
                "missing-plugin",
                (
                    f"upstream {upstream.alias!r} does not contain a "
                    f"plugin named {plugin_name!r}. Available: {available}."
                ),
            )

        resolved_ref, resolved_sha, pin_source = self._apply_precedence_ladder(
            entry=entry,
            plugin=plugin,
            upstream=upstream,
        )

        plugin_host = plugin.source.host
        plugin_repo = plugin.source.repo
        plugin_subdir = plugin.source.subdir

        return ResolvedUpstreamPackage(
            entry=entry,
            upstream=upstream,
            plugin=plugin,
            plugin_host=plugin_host,
            plugin_repo=plugin_repo,
            plugin_subdir=plugin_subdir,
            plugin_ref=resolved_ref,
            plugin_sha=resolved_sha,
            upstream_manifest_sha=record.manifest_sha,
            upstream_canonical_full_name=record.canonical_full_name,
            pin_source=pin_source,
        )

    # -- Internal: upstream fetch ------------------------------------------

    def _get_or_fetch_upstream(self, upstream: Upstream) -> _UpstreamFetchRecord:
        """Fetch + strict-parse an upstream once per build (atomic invariant)."""
        cached = self._fetched.get(upstream.alias)
        if cached is not None:
            return cached

        owner, repo = upstream.repo.split("/", 1)

        canonical = ""
        if self._canonical_full_name is not None:
            try:
                canonical = self._canonical_full_name(upstream.host, owner, repo)
            except Exception as exc:
                # Identity check is part of the supply-chain guard; if it
                # raises (network error, 5xx), refuse to proceed rather
                # than silently downgrade to "unknown identity".
                raise UpstreamResolutionError(
                    "canonical-name-unavailable",
                    (
                        f"could not verify identity of upstream "
                        f"{upstream.repo!r}: {exc}. Refusing to fetch."
                    ),
                ) from exc

        if canonical and canonical.lower() != upstream.repo.lower():
            raise RepoRenameError(configured=upstream.repo, reported=canonical)

        # Resolve the registration's ref / branch to an immutable SHA so
        # the cache is always SHA-keyed.
        target_ref = upstream.ref or upstream.branch
        if upstream.ref is None and not upstream.allow_head:
            raise UpstreamResolutionError(
                "upstream-unpinned",
                (
                    f"upstream {upstream.alias!r} has no pinned ref and "
                    f"allow_head=false; declare 'ref:' or set allow_head: true."
                ),
            )
        if upstream.ref is None and upstream.allow_head:
            self._diagnostics.append(
                UpstreamResolverDiagnostic(
                    level="warning",
                    code="upstream-tracks-head",
                    message=(
                        f"upstream {upstream.alias!r} tracks branch "
                        f"{upstream.branch!r} HEAD; lockfile records the "
                        f"resolved SHA but rebuilds may drift."
                    ),
                    upstream_alias=upstream.alias,
                )
            )

        try:
            manifest_sha = self._normalise_to_sha(
                self._ref_to_sha(upstream.host, owner, repo, target_ref)
            )
        except UpstreamResolutionError:
            raise
        except Exception as exc:
            raise UpstreamResolutionError(
                "ref-resolution-failed",
                (
                    f"could not resolve upstream {upstream.alias!r} "
                    f"ref {target_ref!r} to a commit SHA: {exc}."
                ),
            ) from exc

        try:
            key = compute_cache_key(
                host=upstream.host,
                owner=owner,
                repo=repo,
                sha=manifest_sha,
                path=upstream.path,
            )
        except UpstreamCacheError as exc:
            raise UpstreamResolutionError("invalid-cache-key", str(exc)) from exc

        try:
            raw = self._cache.get_or_fetch(
                key,
                auth_resolver=self._auth_resolver,
                offline=self._offline,
            )
        except UpstreamCacheError as exc:
            raise UpstreamResolutionError("upstream-fetch-failed", str(exc)) from exc

        manifest = parse_marketplace_strict(
            raw,
            upstream_owner_repo=upstream.repo,
            upstream_host=upstream.host,
        )

        # Hoist strict-parser rejections into the diagnostic stream as
        # build errors. The builder treats any non-empty error list as
        # a hard failure (exit code 2).
        for rej in manifest.rejections:
            self._record_strict_rejection(upstream.alias, rej)

        record = _UpstreamFetchRecord(
            manifest=manifest,
            manifest_sha=manifest_sha,
            canonical_full_name=canonical or upstream.repo,
        )
        self._fetched[upstream.alias] = record
        return record

    def _record_strict_rejection(self, upstream_alias: str, rejection: StrictRejection) -> None:
        self._diagnostics.append(
            UpstreamResolverDiagnostic(
                level="error",
                code=f"upstream-rejection:{rejection.reason}",
                message=(
                    f"upstream {upstream_alias!r} plugin "
                    f"{rejection.plugin_name!r}: {rejection.detail}"
                ),
                upstream_alias=upstream_alias,
                plugin_name=rejection.plugin_name,
            )
        )

    # -- Internal: precedence ladder ---------------------------------------

    def _apply_precedence_ladder(
        self,
        *,
        entry: UpstreamPackageEntry,
        plugin: StrictPlugin,
        upstream: Upstream,
    ) -> tuple[str | None, str | None, str]:
        """Determine final (ref, sha, pin_source) for the package.

        Order:
          1. Curator ``entry.ref`` (sha or tag).
          2. Curator ``entry.version`` (semver range, requires
             ``version_range_resolver``).
          3. Upstream plugin's pinned ``ref`` / ``sha``.
          4. ``allow_head`` HEAD (deferred resolution).
          5. Otherwise: hard fail (unpinned).
        """
        if entry.ref is not None:
            ref = entry.ref
            sha = self._maybe_sha(ref)
            return ref, sha, "curator-ref"

        if entry.version is not None:
            if self._version_range_resolver is None:
                raise UpstreamResolutionError(
                    "version-resolver-missing",
                    (
                        f"package {entry.name!r} pins a semver range "
                        f"({entry.version!r}) but no version_range_resolver "
                        f"was provided to the UpstreamResolver."
                    ),
                )
            owner, repo = plugin.source.repo.split("/", 1)
            try:
                ref, sha = self._version_range_resolver(
                    plugin.source.host,
                    owner,
                    repo,
                    entry.version,
                    tag_pattern=entry.tag_pattern,
                    include_prerelease=entry.include_prerelease,
                )
            except Exception as exc:
                raise UpstreamResolutionError(
                    "version-resolution-failed",
                    (
                        f"could not resolve semver range "
                        f"{entry.version!r} for {entry.name!r}: {exc}."
                    ),
                ) from exc
            return ref, self._normalise_to_sha(sha), "curator-version"

        # 3. Upstream plugin's pin (sha preferred, else tag-style ref).
        if plugin.source.sha is not None:
            return plugin.source.sha, plugin.source.sha, "upstream-pin"
        if plugin.source.ref is not None:
            ref = plugin.source.ref
            return ref, self._maybe_sha(ref), "upstream-pin"

        # 4. Same-repo fallback (deterministic). When the upstream
        # plugin lives in the same repo as the upstream marketplace
        # itself -- the typical single-repo Claude marketplace shape
        # (e.g. abhigyanpatwari/GitNexus) -- the upstream-registration
        # SHA we just resolved IS the plugin SHA. This branch
        # reproducibly pins without any opt-in.
        fetched = self._fetched.get(upstream.alias)
        if fetched is not None and plugin.source.repo.lower() == upstream.repo.lower():
            return (
                fetched.manifest_sha,
                fetched.manifest_sha,
                "upstream-registration-ref",
            )

        # 5. HEAD-tracking opt-in: defer resolution. Lockfile writer
        # records what the builder ultimately resolved; rebuilds rely
        # on the lockfile SHA, not the network.
        if entry.allow_head or upstream.allow_head:
            self._diagnostics.append(
                UpstreamResolverDiagnostic(
                    level="warning",
                    code="package-tracks-upstream-head",
                    message=(
                        f"package {entry.name!r} tracks upstream "
                        f"{upstream.alias!r} HEAD; rebuilds may drift "
                        f"unless lockfile is honoured."
                    ),
                    upstream_alias=upstream.alias,
                    plugin_name=entry.name,
                )
            )
            return None, None, "branch-head"

        # 6. Hard fail.
        raise UpstreamResolutionError(
            "package-unpinned",
            (
                f"package {entry.name!r} (upstream {upstream.alias!r}) is "
                f"unpinned: no curator ref/version, upstream plugin has "
                f"no ref/sha, and the plugin source repo "
                f"({plugin.source.repo!r}) does not match the upstream "
                f"registration repo ({upstream.repo!r}). Set 'ref:' or "
                f"'version:' on the package."
            ),
        )

    # -- Internal: helpers --------------------------------------------------

    @staticmethod
    def _maybe_sha(ref: str) -> str | None:
        """Return ref if it is already a 40-char SHA, else None."""
        return ref if _FULL_SHA_RE.match(ref) else None

    @staticmethod
    def _normalise_to_sha(value: str) -> str:
        """Validate that *value* is a full 40-char hex SHA."""
        if not isinstance(value, str) or not _FULL_SHA_RE.match(value):
            raise UpstreamResolutionError(
                "invalid-sha",
                f"expected a 40-char hex SHA, got {value!r}",
            )
        return value
