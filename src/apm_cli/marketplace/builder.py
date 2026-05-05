"""MarketplaceBuilder -- load, resolve, compose, and write marketplace.json.

This module implements the full build pipeline:

1. **Load** -- parse ``marketplace.yml`` via ``yml_schema.load_marketplace_yml``.
2. **Resolve** -- for every package entry, call ``git ls-remote`` (via
   ``RefResolver``) and determine the concrete tag + SHA.
3. **Compose** -- produce an Anthropic-compliant ``marketplace.json`` dict
   with all APM-only fields stripped.
4. **Write** -- atomically write the JSON to disk (or skip on dry-run)
   and produce a ``BuildReport`` with diff statistics.

Hard rule: the output ``marketplace.json`` conforms byte-for-byte to
Anthropic's schema.  No APM-specific keys, no extensions, no renamed
fields.  ``packages`` in yml becomes ``plugins`` in json.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple  # noqa: F401, UP035

if TYPE_CHECKING:
    from ..core.auth import HostInfo

import yaml

from ..utils.github_host import default_host
from ..utils.path_security import ensure_path_within
from ._io import atomic_write
from .errors import (
    BuildError,
    HeadNotAllowedError,
    NoMatchingVersionError,
    OfflineMissError,  # noqa: F401
    RefNotFoundError,
)
from .models import parse_marketplace_json
from .ref_resolver import RefResolver, RemoteRef  # noqa: F401
from .semver import SemVer, parse_semver, satisfies_range
from .tag_pattern import build_tag_regex, render_tag  # noqa: F401
from .upstream_cache import UpstreamCache
from .upstream_resolver import (
    ResolvedUpstreamPackage,
    UpstreamResolver,
    UpstreamResolverDiagnostic,
)
from .yml_schema import (
    MarketplaceYml,
    PackageEntry,
    UpstreamPackageEntry,
    load_marketplace_yml,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BuildDiagnostic",
    "BuildOptions",
    "BuildReport",
    "MarketplaceBuilder",
    "ResolveResult",
    "ResolvedPackage",
]

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildDiagnostic:
    """Structured diagnostic emitted during marketplace.json composition.

    Levels: ``"verbose"`` (info-only), ``"warning"`` (non-fatal),
    ``"error"`` (fatal -- ``build()`` will raise BuildError before
    writing). Optional ``code`` is a stable identifier for upstream
    diagnostics so callers can match without parsing message text.
    """

    level: str  # "verbose" | "warning" | "error"
    message: str
    code: str = ""


@dataclass(frozen=True)
class ResolvedPackage:
    """A package entry after ref resolution."""

    name: str
    source_repo: str  # "owner/repo" only
    subdir: str | None  # APM-only (used to compose the output ``source`` object)
    ref: str  # resolved tag name, e.g. "v1.2.0"
    sha: str  # 40-char git SHA
    requested_version: str | None  # original APM-only range (for diagnostics)
    tags: tuple[str, ...]
    is_prerelease: bool  # True if the resolved ref was a prerelease semver


@dataclass(frozen=True)
class ResolveResult:
    """Result of resolving package refs in a marketplace build."""

    entries: tuple[ResolvedPackage, ...]
    errors: tuple[tuple[str, str], ...]  # (package name, error message) pairs
    upstream_entries: tuple[ResolvedUpstreamPackage, ...] = ()
    upstream_diagnostics: tuple[UpstreamResolverDiagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        """True when every package resolved without error."""
        return len(self.errors) == 0


@dataclass(frozen=True)
class BuildReport:
    """Summary of a build run."""

    resolved: tuple[ResolvedPackage, ...]
    errors: tuple[tuple[str, str], ...]  # (package name, error message) pairs
    warnings: tuple[str, ...]  # non-fatal diagnostic messages
    diagnostics: tuple[BuildDiagnostic, ...] = ()  # structured diagnostics
    unchanged_count: int = 0
    added_count: int = 0
    updated_count: int = 0
    removed_count: int = 0
    output_path: Path = field(default_factory=lambda: Path("."))
    dry_run: bool = False
    upstream_resolved: tuple[ResolvedUpstreamPackage, ...] = ()
    upstream_diagnostics: tuple[UpstreamResolverDiagnostic, ...] = ()


@dataclass
class BuildOptions:
    """Configuration knobs for MarketplaceBuilder."""

    concurrency: int = 8
    timeout_seconds: float = 10.0
    include_prerelease: bool = False
    allow_head: bool = False
    continue_on_error: bool = False
    offline: bool = False
    output_override: Path | None = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

# 40-char hex SHA pattern -- shared across marketplace modules; see
# ``marketplace/ref_resolver.FULL_SHA_RE``.
from .ref_resolver import FULL_SHA_RE as _SHA40_RE  # noqa: E402

# Version range indicators -- if a version string starts with any of these
# or contains spaces, it's a resolution constraint, not a display override.
_VERSION_RANGE_CHARS = ("^", "~", ">", "<", "=")


def _is_display_version(version: str | None) -> bool:
    """Return True if *version* looks like a fixed display version, not a range."""
    if not version:
        return False
    v = version.strip()
    if any(v.startswith(c) for c in _VERSION_RANGE_CHARS):
        return False
    return not (" " in v or "*" in v or "x" in v.lower().split(".")[-1:])


def _subtract_plugin_root(source: str, plugin_root: str) -> str:
    """Remove pluginRoot prefix from a local source path for emit.

    Uses PurePosixPath.relative_to() for robust normalization.
    Returns the relative path prefixed with ``./``.

    Raises
    ------
    ValueError
        If *source* does not start with *plugin_root*.
    BuildError
        If subtraction yields an empty or invalid path (S2 guard).
    """
    from pathlib import PurePosixPath

    # Normalize: strip leading "./" for comparison
    norm_source = source.lstrip("./") if source.startswith("./") else source
    norm_root = plugin_root.lstrip("./") if plugin_root.startswith("./") else plugin_root
    # Strip trailing slashes
    norm_root = norm_root.rstrip("/")
    norm_source = norm_source.rstrip("/")

    src_path = PurePosixPath(norm_source)
    root_path = PurePosixPath(norm_root)

    # relative_to raises ValueError if not a prefix
    relative = src_path.relative_to(root_path)
    result = str(relative)

    # X1: empty result means source == pluginRoot exactly
    if not result or result == ".":
        raise BuildError(
            f"subtracting pluginRoot '{plugin_root}' from source '{source}' yields empty path"
        )

    # S2: post-subtraction guard -- no absolute paths, no traversal
    if result.startswith("/"):
        raise BuildError(f"pluginRoot subtraction produced absolute path: '{result}'")
    if ".." in result.split("/"):
        raise BuildError(f"pluginRoot subtraction produced path with traversal: '{result}'")

    return "./" + result


class _UpstreamResolverFactory:
    """Build an :class:`UpstreamResolver` wired to a builder context.

    Extracted from :meth:`MarketplaceBuilder._build_upstream_resolver`
    to flatten what was previously a 100+ line closure stack. Owns:

    - the per-host :class:`RefResolver` cache (lazy, never inherits the
      curator's auth context across hosts);
    - the ``ref_to_sha`` resolution path (SHA short-circuit, offline
      guard, ls-remote-backed tag/branch resolution);
    - the ``version_range`` semver-pattern resolution path.

    Lifetime is one build invocation. Holding the cache as instance
    state (rather than a closure cell) makes the dependencies between
    the three resolver functions explicit.
    """

    def __init__(self, builder: MarketplaceBuilder, yml: MarketplaceYml) -> None:
        self._builder = builder
        self._yml = yml
        self._host_resolvers: dict[str, RefResolver] = {}
        # Populated by _build_upstream_resolver from the existing lockfile.
        # Maps ``owner/repo`` to the previously resolved manifest SHA so that
        # offline rebuilds (``BuildOptions.offline=True``) can replay the
        # cached SHA without a network round-trip to re-resolve a tag/branch.
        self._lockfile_shas_by_repo: dict[str, str] = {}

    # -- internal: per-host RefResolver cache -----------------------------------

    def _resolver_for_host(self, host: str) -> RefResolver:
        if host not in self._host_resolvers:
            if host == self._builder._host:
                self._host_resolvers[host] = self._builder._get_resolver()
            else:
                # v1: only github.com is supported. Construct an
                # unauthenticated RefResolver so we never leak the
                # curator's PAT to a foreign host.
                self._host_resolvers[host] = RefResolver(
                    timeout_seconds=self._builder._options.timeout_seconds,
                    offline=self._builder._options.offline,
                    host=host,
                    token=None,
                )
        return self._host_resolvers[host]

    # -- ref resolution ---------------------------------------------------------

    def ref_to_sha(self, host: str, owner: str, repo: str, ref_or_branch: str) -> str:
        # SHA short-circuit -- offline-safe, no network.
        if _SHA40_RE.match(ref_or_branch):
            return ref_or_branch
        owner_repo = f"{owner}/{repo}"
        # Lockfile replay: when offline and we have a previously resolved SHA
        # for this repo (loaded at build start), return it without a network
        # call. Enables ``--offline`` rebuilds for tag/branch refs.
        if self._builder._options.offline:
            known = self._lockfile_shas_by_repo.get(owner_repo)
            if known:
                return known
            raise BuildError(f"cannot resolve ref '{ref_or_branch}' offline for {owner}/{repo}")
        resolver = self._resolver_for_host(host)
        refs = resolver.list_remote_refs(owner_repo)
        for remote in refs:
            if remote.name == f"refs/tags/{ref_or_branch}":
                return remote.sha
            if remote.name == f"refs/heads/{ref_or_branch}":
                # B1: branch refs are mutable. Reject unless allow_head is
                # enabled at the build level; callers that need to track HEAD
                # explicitly set BuildOptions.allow_head=True.
                if not self._builder._options.allow_head:
                    raise BuildError(
                        f"ref '{ref_or_branch}' resolves to a mutable branch HEAD; "
                        f"set allow_head: true or pin a SHA/tag for reproducible builds."
                    )
                return remote.sha
        raise BuildError(f"ref '{ref_or_branch}' not found in {owner}/{repo}")

    # -- version-range resolution ----------------------------------------------

    def version_range(
        self,
        host: str,
        owner: str,
        repo: str,
        semver_range: str,
        *,
        tag_pattern: str | None = None,
        include_prerelease: bool = False,
    ) -> tuple[str, str]:
        resolver = self._resolver_for_host(host)
        owner_repo = f"{owner}/{repo}"
        pattern = tag_pattern or self._yml.build.tag_pattern
        tag_rx = build_tag_regex(pattern)
        refs = resolver.list_remote_refs(owner_repo)
        candidates: list[tuple[SemVer, str, str]] = []
        for remote in refs:
            if not remote.name.startswith("refs/tags/"):
                continue
            tag_name = remote.name[len("refs/tags/") :]
            m = tag_rx.match(tag_name)
            if not m:
                continue
            version_str = m.group("version")
            sv = parse_semver(version_str)
            if sv is None:
                continue
            if sv.is_prerelease and not (
                include_prerelease or self._builder._options.include_prerelease
            ):
                continue
            if satisfies_range(sv, semver_range):
                candidates.append((sv, tag_name, remote.sha))
        if not candidates:
            raise BuildError(
                f"no version of {owner}/{repo} matches '{semver_range}' (pattern='{pattern}')"
            )
        candidates.sort(key=lambda c: c[0], reverse=True)
        _, best_tag, best_sha = candidates[0]
        return best_tag, best_sha

    # -- assembly ---------------------------------------------------------------

    def build(self) -> UpstreamResolver:
        upstreams_by_alias = {u.alias: u for u in self._yml.upstreams}
        # ``canonical_full_name`` is intentionally ``None`` in v1.
        # The rename-guard (checking that the fetched repo full_name still
        # matches the lockfile's recorded name) requires a GitHub Contents API
        # call for which no existing v1 helper exists. The lockfile already
        # records the field for forward-compatibility; the guard fires in v2
        # once the helper is wired. This deferral is documented in the trust
        # table in docs/guides/marketplace-upstreams.md.
        return UpstreamResolver(
            upstreams=upstreams_by_alias,
            cache=UpstreamCache(),
            ref_to_sha=self.ref_to_sha,
            canonical_full_name=None,
            version_range_resolver=self.version_range,
            auth_resolver=self._builder._auth_resolver,
            offline=self._builder._options.offline,
        )


class MarketplaceBuilder:
    """Load marketplace.yml, resolve refs, compose and write marketplace.json.

    Parameters
    ----------
    marketplace_yml_path:
        Path to the ``marketplace.yml`` file.
    options:
        Build options.  Defaults to ``BuildOptions()`` if not provided.
    auth_resolver:
        Optional ``AuthResolver`` for authenticating requests to private
        GitHub repositories.  When ``None`` (default) a fresh resolver is
        created lazily the first time a token is needed.
    """

    def __init__(
        self,
        marketplace_yml_path: Path,
        options: BuildOptions | None = None,
        auth_resolver: object | None = None,
    ) -> None:
        self._yml_path = marketplace_yml_path
        self._project_root = marketplace_yml_path.parent
        self._options = options or BuildOptions()
        self._yml: MarketplaceYml | None = None
        self._resolver: RefResolver | None = None
        self._auth_resolver = auth_resolver
        # Resolved once per build, used by worker threads (read-only).
        self._github_token: str | None = None
        self._host: str = default_host() or "github.com"
        self._host_info: HostInfo | None = None
        self._auth_resolved: bool = False

    @classmethod
    def from_config(
        cls,
        config: MarketplaceYml,
        project_root: Path,
        options: BuildOptions | None = None,
        auth_resolver: object | None = None,
    ) -> MarketplaceBuilder:
        """Construct a builder from an already-loaded MarketplaceConfig.

        Use this when the caller has already chosen between apm.yml and
        the legacy ``marketplace.yml`` (typically via
        ``migration.load_marketplace_config``).  ``project_root`` is the
        directory output paths are resolved against.
        """
        # Use a synthetic path so legacy code paths that consult
        # ``self._yml_path.parent`` still resolve to the project root.
        synthetic_path = project_root / (
            config.source_path.name if config.source_path is not None else "apm.yml"
        )
        instance = cls(synthetic_path, options=options, auth_resolver=auth_resolver)
        instance._project_root = project_root
        instance._yml = config
        return instance

    # -- lazy loaders -------------------------------------------------------

    def _load_yml(self) -> MarketplaceYml:
        if self._yml is None:
            # Shape-aware load: when the configured path is an apm.yml
            # file, use the apm.yml loader; otherwise default to the
            # legacy marketplace.yml loader.  Callers that have already
            # loaded a config should use ``from_config`` to bypass this.
            from .yml_schema import load_marketplace_from_apm_yml

            if self._yml_path.name == "apm.yml":
                self._yml = load_marketplace_from_apm_yml(self._yml_path)
            else:
                self._yml = load_marketplace_yml(self._yml_path)
        return self._yml

    def _get_resolver(self) -> RefResolver:
        if self._resolver is None:
            self._ensure_auth()
            self._resolver = RefResolver(
                timeout_seconds=self._options.timeout_seconds,
                offline=self._options.offline,
                host=self._host,
                token=self._github_token,
            )
        return self._resolver

    def _ensure_auth(self) -> None:
        """Lazily resolve host classification and GitHub token.

        Short-circuits when already resolved (even if no token was found)
        or when running in offline mode.  Offline mode is still marked as
        resolved so repeated calls remain idempotent.  Called by
        ``_get_resolver()`` so both ``resolve()`` and ``build()`` benefit
        from authenticated ``git ls-remote`` when available.
        """
        if self._auth_resolved:
            return
        if self._options.offline:
            self._auth_resolved = True
            return
        self._github_token = self._resolve_github_token()
        self._auth_resolved = True

    # -- output path --------------------------------------------------------

    def _output_path(self) -> Path:
        if self._options.output_override is not None:
            return self._options.output_override
        yml = self._load_yml()
        output_path = self._project_root / yml.output
        # Containment guard -- reject output paths that escape the project root.
        ensure_path_within(output_path, self._project_root)
        return output_path

    # -- single-entry resolution --------------------------------------------

    def _resolve_entry(self, entry: PackageEntry) -> ResolvedPackage:
        """Resolve a single package entry to a concrete tag + SHA."""
        # Local-path packages skip git resolution entirely.
        if entry.is_local:
            return ResolvedPackage(
                name=entry.name,
                source_repo="",
                subdir=entry.source,
                ref="",
                sha="",
                requested_version=entry.version,
                tags=tuple(entry.tags),
                is_prerelease=False,
            )
        yml = self._load_yml()
        resolver = self._get_resolver()
        owner_repo = entry.source

        if entry.ref is not None:
            return self._resolve_explicit_ref(entry, resolver, owner_repo)
        # version range resolution
        return self._resolve_version_range(entry, resolver, owner_repo, yml)

    def _resolve_explicit_ref(
        self,
        entry: PackageEntry,
        resolver: RefResolver,
        owner_repo: str,
    ) -> ResolvedPackage:
        """Resolve an entry with an explicit ``ref:`` field."""
        ref_text = entry.ref
        assert ref_text is not None  # noqa: S101

        # If it looks like a 40-char SHA, accept it directly
        if _SHA40_RE.match(ref_text):
            sv = parse_semver(ref_text.lstrip("vV"))
            return ResolvedPackage(
                name=entry.name,
                source_repo=owner_repo,
                subdir=entry.subdir,
                ref=ref_text,
                sha=ref_text,
                requested_version=entry.version,
                tags=entry.tags,
                is_prerelease=sv.is_prerelease if sv else False,
            )

        refs = resolver.list_remote_refs(owner_repo)

        # Try as tag first (only check tag refs)
        for remote_ref in refs:
            if not remote_ref.name.startswith("refs/tags/"):
                continue
            tag_name = _strip_ref_prefix(remote_ref.name)
            if tag_name == ref_text:
                sv = parse_semver(tag_name.lstrip("vV"))
                return ResolvedPackage(
                    name=entry.name,
                    source_repo=owner_repo,
                    subdir=entry.subdir,
                    ref=tag_name,
                    sha=remote_ref.sha,
                    requested_version=entry.version,
                    tags=entry.tags,
                    is_prerelease=sv.is_prerelease if sv else False,
                )

        # Try as full refname
        for remote_ref in refs:
            if remote_ref.name == ref_text:
                short = _strip_ref_prefix(remote_ref.name)
                is_branch = remote_ref.name.startswith("refs/heads/")
                if is_branch and not self._options.allow_head:
                    raise HeadNotAllowedError(entry.name, short)
                sv = parse_semver(short.lstrip("vV"))
                return ResolvedPackage(
                    name=entry.name,
                    source_repo=owner_repo,
                    subdir=entry.subdir,
                    ref=short,
                    sha=remote_ref.sha,
                    requested_version=entry.version,
                    tags=entry.tags,
                    is_prerelease=sv.is_prerelease if sv else False,
                )

        # Try as branch name
        for remote_ref in refs:
            if remote_ref.name == f"refs/heads/{ref_text}":
                if not self._options.allow_head:
                    raise HeadNotAllowedError(entry.name, ref_text)
                return ResolvedPackage(
                    name=entry.name,
                    source_repo=owner_repo,
                    subdir=entry.subdir,
                    ref=ref_text,
                    sha=remote_ref.sha,
                    requested_version=entry.version,
                    tags=entry.tags,
                    is_prerelease=False,
                )

        # HEAD special case
        if ref_text.upper() == "HEAD":
            if not self._options.allow_head:
                raise HeadNotAllowedError(entry.name, "HEAD")

        raise RefNotFoundError(entry.name, ref_text, owner_repo)

    def _resolve_version_range(
        self,
        entry: PackageEntry,
        resolver: RefResolver,
        owner_repo: str,
        yml: MarketplaceYml,
    ) -> ResolvedPackage:
        """Resolve an entry using its ``version:`` semver range."""
        version_range = entry.version
        assert version_range is not None  # noqa: S101

        # Determine tag pattern: entry > build > default
        pattern = entry.tag_pattern or yml.build.tag_pattern

        tag_rx = build_tag_regex(pattern)
        refs = resolver.list_remote_refs(owner_repo)

        # Filter tags matching the pattern and extract versions
        candidates: list[tuple[SemVer, str, str]] = []  # (semver, tag_name, sha)
        for remote_ref in refs:
            if not remote_ref.name.startswith("refs/tags/"):
                continue
            tag_name = remote_ref.name[len("refs/tags/") :]
            m = tag_rx.match(tag_name)
            if not m:
                continue
            version_str = m.group("version")
            sv = parse_semver(version_str)
            if sv is None:
                continue

            # Prerelease filter
            include_pre = entry.include_prerelease or self._options.include_prerelease
            if sv.is_prerelease and not include_pre:
                continue

            # Range filter
            if satisfies_range(sv, version_range):
                candidates.append((sv, tag_name, remote_ref.sha))

        if not candidates:
            raise NoMatchingVersionError(
                entry.name,
                version_range,
                detail=f"pattern='{pattern}', remote='{owner_repo}'",
            )

        # Pick highest
        candidates.sort(key=lambda c: c[0], reverse=True)
        best_sv, best_tag, best_sha = candidates[0]

        return ResolvedPackage(
            name=entry.name,
            source_repo=owner_repo,
            subdir=entry.subdir,
            ref=best_tag,
            sha=best_sha,
            requested_version=version_range,
            tags=entry.tags,
            is_prerelease=best_sv.is_prerelease,
        )

    # -- concurrent resolution ----------------------------------------------

    def resolve(self) -> ResolveResult:
        """Resolve every entry concurrently.

        Direct (:class:`PackageEntry`) and upstream-sourced
        (:class:`UpstreamPackageEntry`) entries are partitioned by
        ``isinstance``. Direct entries follow the existing concurrent
        ``git ls-remote`` path; upstream entries are resolved through
        :class:`UpstreamResolver`, which owns its own cache, atomic-fetch
        invariant, and rename guard.

        Returns
        -------
        ResolveResult
            Contains resolved direct entries, resolved upstream entries,
            any per-package errors, and any structured upstream
            diagnostics.

        Raises
        ------
        BuildError
            On any direct-resolution failure (unless ``continue_on_error``).
            Upstream failures are always collected as diagnostics and
            error rows; ``build()`` raises ``BuildError`` before writing
            output when any error diagnostics are present.
        """
        yml = self._load_yml()
        all_entries = yml.packages
        if not all_entries:
            return ResolveResult(entries=(), errors=())

        direct_entries: list[tuple[int, PackageEntry]] = []
        upstream_entries: list[tuple[int, UpstreamPackageEntry]] = []
        for idx, entry in enumerate(all_entries):
            if isinstance(entry, UpstreamPackageEntry):
                upstream_entries.append((idx, entry))
            else:
                direct_entries.append((idx, entry))

        results: dict[int, ResolvedPackage] = {}
        errors: list[tuple[str, str]] = []

        # -- direct path (existing concurrent ls-remote flow) ---------------
        if direct_entries:
            # Eagerly resolve auth + create the shared RefResolver before
            # spawning workers -- avoids a race on _ensure_auth() and
            # matches the pattern used in _prefetch_metadata().
            self._get_resolver()

            with ThreadPoolExecutor(
                max_workers=min(self._options.concurrency, len(direct_entries))
            ) as pool:
                future_to_index = {
                    pool.submit(self._resolve_entry, entry): idx for idx, entry in direct_entries
                }
                for future in as_completed(future_to_index):
                    idx = future_to_index[future]
                    entry = all_entries[idx]
                    try:
                        resolved = future.result(timeout=self._options.timeout_seconds)
                        results[idx] = resolved
                    except BuildError as exc:
                        if self._options.continue_on_error:
                            errors.append((entry.name, str(exc)))
                        else:
                            raise
                    except Exception as exc:
                        logger.debug("Unexpected error resolving '%s'", entry.name, exc_info=True)
                        if self._options.continue_on_error:
                            errors.append((entry.name, str(exc)))
                        else:
                            raise BuildError(
                                f"Unexpected error resolving '{entry.name}': {exc}",
                                package=entry.name,
                            ) from exc

        # -- direct results ordered by yml index ---------------------------
        ordered_direct: list[ResolvedPackage] = []
        for idx in range(len(all_entries)):
            if idx in results:
                ordered_direct.append(results[idx])

        # -- upstream path -------------------------------------------------
        upstream_resolved: list[ResolvedUpstreamPackage] = []
        upstream_diagnostics: list[UpstreamResolverDiagnostic] = []
        if upstream_entries:
            resolver = self._build_upstream_resolver(yml)
            entries_only = [e for _, e in upstream_entries]
            upstream_resolved, upstream_diagnostics = resolver.resolve_all(entries_only)
            # Lift error-level upstream diagnostics into the errors list
            # so the orchestrator and ``build()`` can react uniformly.
            for diag in upstream_diagnostics:
                if diag.level == "error":
                    label = diag.plugin_name or diag.upstream_alias or "upstream"
                    errors.append((label, diag.message))

        return ResolveResult(
            entries=tuple(ordered_direct),
            errors=tuple(errors),
            upstream_entries=tuple(upstream_resolved),
            upstream_diagnostics=tuple(upstream_diagnostics),
        )

    # -- upstream resolver wiring -------------------------------------------

    def _build_upstream_resolver(self, yml: MarketplaceYml) -> UpstreamResolver:
        """Construct an :class:`UpstreamResolver` for this build.

        v1 constraint: only ``github.com`` upstreams are wired; entries
        targeting any other host yield an error diagnostic from
        :meth:`UpstreamResolver.resolve_all`. Cross-host fan-out is
        slated for v2.

        Implementation note: the per-host ``RefResolver`` cache and the
        ref/version resolver functions used to live inside this method
        as a 100+ line closure stack. They are now factored into
        :class:`_UpstreamResolverFactory` for readability and
        testability; this method remains the single assembly point.
        """
        factory = _UpstreamResolverFactory(self, yml)
        # Seed the factory with previously resolved manifest SHAs from the
        # lockfile. This allows offline rebuilds to replay known SHAs for
        # tag/branch refs without making network calls.
        factory._lockfile_shas_by_repo.update(self._load_lockfile_upstream_shas())
        return factory.build()

    def _load_lockfile_upstream_shas(self) -> dict[str, str]:
        """Return ``{owner/repo: manifest_sha}`` from the current lockfile.

        Best-effort: any read/parse error returns an empty dict, falling
        back to online resolution. Only 40-char SHAs are returned (tags and
        branch names stored in old lockfiles are skipped).
        """
        try:
            from ..deps.lockfile import LockFile, get_lockfile_path

            lockfile_path = get_lockfile_path(self._project_root)
            if not lockfile_path.exists():
                return {}
            lock = LockFile.load_or_create(lockfile_path)
            shas: dict[str, str] = {}
            for lu in lock.upstreams.values():
                owner_repo = f"{lu.owner}/{lu.repo}"
                if lu.manifest_sha and _SHA40_RE.match(lu.manifest_sha):
                    shas[owner_repo] = lu.manifest_sha
            return shas
        except Exception:
            return {}

    # -- remote description fetcher -----------------------------------------

    def _fetch_remote_metadata(self, pkg: ResolvedPackage) -> dict[str, str] | None:
        """Best-effort: fetch ``description`` and ``version`` from the
        package's remote ``apm.yml``.

        Returns a dict with ``description`` and/or ``version`` keys, or
        ``None`` on any error.  This is purely cosmetic enrichment --
        failures are silently logged at debug level and never propagate.

        When a GitHub token is available (via ``self._github_token``), it
        is included as an ``Authorization`` header so private repos can be
        accessed.

        For non-github.com GitHub-family hosts (GHES, GHE Cloud), uses the
        GitHub REST API instead of raw.githubusercontent.com (which is only
        available for github.com).  For non-GitHub hosts, metadata
        enrichment is skipped.
        """
        try:
            path_prefix = f"{pkg.subdir}/" if pkg.subdir else ""
            file_path = f"{path_prefix}apm.yml"

            # Determine URL strategy based on host kind
            host_kind = self._host_info.kind if self._host_info else "github"

            if host_kind not in ("github", "ghe_cloud", "ghes"):
                # Non-GitHub hosts -- skip metadata enrichment
                logger.debug(
                    "Skipping metadata fetch for %s (non-GitHub host: %s)",
                    pkg.name,
                    self._host,
                )
                return None

            if host_kind == "ghe_cloud" and not self._github_token:
                logger.debug(
                    "Skipping metadata fetch for %s (GHE Cloud requires auth)",
                    pkg.name,
                )
                return None

            if self._host == "github.com":
                # github.com -- use fast raw.githubusercontent.com CDN
                url = f"https://raw.githubusercontent.com/{pkg.source_repo}/{pkg.sha}/{file_path}"
                req = urllib.request.Request(url)  # noqa: S310
                if self._github_token:
                    req.add_header("Authorization", f"token {self._github_token}")
            else:
                # GHES / GHE Cloud -- use REST API
                api_base = (
                    self._host_info.api_base if self._host_info else None
                ) or f"https://{self._host}/api/v3"
                url = f"{api_base}/repos/{pkg.source_repo}/contents/{file_path}?ref={pkg.sha}"
                req = urllib.request.Request(url)  # noqa: S310
                req.add_header("Accept", "application/vnd.github.raw")
                if self._github_token:
                    req.add_header("Authorization", f"token {self._github_token}")

            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8")
            data = yaml.safe_load(raw)
            if not isinstance(data, dict):
                return None
            result: dict[str, str] = {}
            desc = data.get("description")
            if isinstance(desc, str) and desc:
                result["description"] = desc
            ver = data.get("version")
            if ver is not None:
                ver_str = str(ver).strip()
                if ver_str:
                    result["version"] = ver_str
            if result:
                logger.debug(
                    "Fetched metadata for %s from remote apm.yml: %s",
                    pkg.name,
                    ", ".join(result.keys()),
                )
                return result
        except Exception:
            logger.debug(
                "Could not fetch remote metadata for %s",
                pkg.name,
                exc_info=True,
            )
        return None

    def _resolve_github_token(self) -> str | None:
        """Resolve a GitHub token using ``AuthResolver``.

        Called once before concurrent fetches.  Returns the token string
        or ``None`` if no credentials are available.  Never raises --
        auth failures are logged at debug and silently ignored.
        """
        try:
            from ..core.auth import AuthResolver  # lazy import

            resolver = self._auth_resolver
            if resolver is None:
                resolver = AuthResolver()
                self._auth_resolver = resolver
            # Always classify the host, regardless of token availability,
            # so _fetch_remote_metadata() can branch on host kind.
            if self._host_info is None:
                self._host_info = AuthResolver.classify_host(self._host)
            ctx = resolver.resolve(self._host)  # type: ignore[union-attr]
            if ctx.token:
                logger.debug("Resolved GitHub token for metadata fetch (source=%s)", ctx.source)
                return ctx.token
        except Exception:
            logger.debug("Could not resolve GitHub token for metadata fetch", exc_info=True)
        return None

    def _prefetch_metadata(self, resolved: list[ResolvedPackage]) -> dict[str, dict[str, str]]:
        """Concurrently fetch remote metadata for all packages.

        Returns a mapping of ``{package_name: {"description": ..., "version": ...}}``
        for successful fetches.  Skipped entirely when ``--offline`` is set.
        Local-path packages are skipped (they carry their own metadata).

        A GitHub token is resolved once before spawning worker threads and
        stored on ``self._github_token`` for the workers to read.
        """
        if self._options.offline:
            return {}

        # Filter out local-path entries -- they don't have a remote to fetch from.
        remote = [pkg for pkg in resolved if pkg.source_repo]
        if not remote:
            return {}

        # Resolve token once -- threads read self._github_token (immutable).
        self._ensure_auth()

        results: dict[str, dict[str, str]] = {}
        workers = min(self._options.concurrency, len(remote))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_name = {
                pool.submit(self._fetch_remote_metadata, pkg): pkg.name for pkg in remote
            }
            for future in as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    meta = future.result()
                    if meta:
                        results[name] = meta
                except Exception:
                    pass
        return results

    # -- composition --------------------------------------------------------

    def compose_marketplace_json(
        self,
        resolved: list[ResolvedPackage],
        *,
        upstream_resolved: list[ResolvedUpstreamPackage] | None = None,
    ) -> dict[str, Any]:
        """Produce an Anthropic-compliant marketplace.json dict.

        All APM-only fields are stripped.  Key order follows the Anthropic
        schema exactly.

        Parameters
        ----------
        resolved:
            List of resolved direct packages (from ``resolve()``).
        upstream_resolved:
            Optional list of resolved upstream-sourced packages. Emitted
            after ``resolved`` in the output ``plugins`` array. v1 does
            not interleave upstream and direct entries; consumers see
            direct emissions first, then upstream pass-throughs.

        Returns
        -------
        dict
            An ``OrderedDict``-style dict ready to be serialised as JSON.
        """
        yml = self._load_yml()

        # Pre-fetch metadata (description + version) from remote apm.yml
        remote_metadata = self._prefetch_metadata(resolved)

        # Build a name -> entry map so we can reach back for local-package
        # description / homepage that came from the yml itself.
        entry_by_name: dict[str, PackageEntry] = {
            e.name: e for e in yml.packages if not isinstance(e, UpstreamPackageEntry)
        }

        doc: dict[str, Any] = OrderedDict()
        doc["name"] = yml.name
        # Top-level description / version are emitted only when explicitly
        # set in the marketplace block (or in a legacy marketplace.yml).
        # apm.yml-sourced configs that inherit these from the project skip
        # them so the marketplace.json doesn't drift on unrelated bumps.
        if yml.description_overridden and yml.description:
            doc["description"] = yml.description
        if yml.version_overridden and yml.version:
            doc["version"] = yml.version

        # Owner -- omit empty optional sub-fields
        owner_dict: dict[str, Any] = OrderedDict()
        owner_dict["name"] = yml.owner.name
        if yml.owner.email:
            owner_dict["email"] = yml.owner.email
        if yml.owner.url:
            owner_dict["url"] = yml.owner.url
        doc["owner"] = owner_dict

        # Metadata -- pass-through verbatim (only if present)
        if yml.metadata:
            doc["metadata"] = yml.metadata

        # Plugins (packages -> plugins)
        plugins: list[dict[str, Any]] = []
        diagnostics: list[BuildDiagnostic] = []
        plugin_root = yml.metadata.get("pluginRoot", "")
        strip_count = 0
        override_count = 0

        for pkg in resolved:
            plugin: dict[str, Any] = OrderedDict()
            plugin["name"] = pkg.name

            entry = entry_by_name.get(pkg.name)
            is_local = entry is not None and entry.is_local

            # -- description / version (with curator-wins override for remote) --
            if is_local:
                if entry.description:
                    plugin["description"] = entry.description
                if entry.version:
                    plugin["version"] = entry.version
            else:
                meta = remote_metadata.get(pkg.name, {})
                # Curator-wins: entry-level value overrides remote-fetched
                if entry and entry.description:
                    plugin["description"] = entry.description
                    remote_desc = meta.get("description", "")
                    if remote_desc and remote_desc != entry.description:
                        override_count += 1
                        diagnostics.append(
                            BuildDiagnostic(
                                level="verbose",
                                message=(
                                    f"[i] Package '{pkg.name}': using curator "
                                    f"description (remote: "
                                    f"'{remote_desc[:40]}')"
                                ),
                            )
                        )
                elif meta.get("description"):
                    plugin["description"] = meta["description"]

                if entry and _is_display_version(entry.version):
                    plugin["version"] = entry.version
                    remote_ver = meta.get("version", "")
                    if remote_ver and remote_ver != entry.version:
                        override_count += 1
                        diagnostics.append(
                            BuildDiagnostic(
                                level="verbose",
                                message=(
                                    f"[i] Package '{pkg.name}': using curator "
                                    f"version '{entry.version}' "
                                    f"(remote: '{remote_ver}')"
                                ),
                            )
                        )
                elif meta.get("version"):
                    plugin["version"] = meta["version"]

            # -- author / license / repository (curator-only pass-through) --
            # ``author`` is normalized to an object by the loader, so we can
            # serialize it as-is into the JSON. dict() drops the read-only
            # Mapping wrapper while preserving insertion order (3.7+).
            if entry and entry.author:
                plugin["author"] = dict(entry.author)
            if entry and entry.license:
                plugin["license"] = entry.license
            if entry and entry.repository:
                plugin["repository"] = entry.repository

            # -- tags --
            if pkg.tags:
                plugin["tags"] = list(pkg.tags)

            # -- homepage (local only) --
            if is_local and entry.homepage:
                plugin["homepage"] = entry.homepage

            # -- source --
            if is_local:
                source_value = entry.source
                if plugin_root:
                    try:
                        source_value = _subtract_plugin_root(entry.source, plugin_root)
                        strip_count += 1
                        diagnostics.append(
                            BuildDiagnostic(
                                level="verbose",
                                message=(
                                    f"[i] Package '{pkg.name}': stripped "
                                    f"pluginRoot -- '{entry.source}' -> "
                                    f"'{source_value}'"
                                ),
                            )
                        )
                    except ValueError:
                        # W1: source outside pluginRoot -- emit as-is
                        source_value = entry.source
                        diagnostics.append(
                            BuildDiagnostic(
                                level="warning",
                                message=(
                                    f"[!] Package '{pkg.name}': source "
                                    f"'{entry.source}' is outside pluginRoot "
                                    f"'{plugin_root}' -- emitted as-is"
                                ),
                            )
                        )
                plugin["source"] = source_value
            else:
                # Remote source: emit per the official Claude Code marketplace
                # schema (json.schemastore.org/claude-code-marketplace.json).
                # Subdirs use the ``git-subdir`` form; everything else uses
                # ``github`` shorthand. Field names: ``source``/``repo``/``sha``
                # (NOT ``type``/``repository``/``commit``).
                source_obj: dict[str, Any] = OrderedDict()
                if pkg.subdir:
                    source_obj["source"] = "git-subdir"
                    source_obj["url"] = pkg.source_repo
                    source_obj["path"] = pkg.subdir
                else:
                    source_obj["source"] = "github"
                    source_obj["repo"] = pkg.source_repo
                if pkg.ref:
                    source_obj["ref"] = pkg.ref
                if pkg.sha:
                    source_obj["sha"] = pkg.sha
                plugin["source"] = source_obj

            plugins.append(plugin)

        # -- upstream-sourced plugins --
        if upstream_resolved:
            for rup in upstream_resolved:
                plugins.append(self._emit_upstream_plugin(rup))

        # Verbose summary line
        summary_parts: list[str] = []
        if plugin_root and strip_count > 0:
            summary_parts.append(f"stripped from {strip_count} local source(s)")
        if override_count > 0:
            summary_parts.append(
                f"{override_count} remote entry(ies) used curator-supplied overrides"
            )
        if summary_parts:
            diagnostics.append(
                BuildDiagnostic(
                    level="verbose",
                    message="pluginRoot: " + "; ".join(summary_parts),
                )
            )

        # Defence-in-depth: detect duplicate plugin names and record
        # warnings so the command layer can alert the maintainer.
        seen_names: dict[str, str] = {}
        build_warnings: list[str] = []
        for p in plugins:
            pname = p["name"]
            src = p.get("source", {})
            if isinstance(src, str):
                src_label = src
            else:
                # Prefer ``path`` (git-subdir form) for disambiguation, then
                # fall back to ``repo`` (github form, post-1061) or
                # ``repository`` (legacy emit shape, kept for back-compat).
                src_label = src.get("path") or src.get("repo") or src.get("repository", "?")
            if pname in seen_names:
                build_warnings.append(
                    f"Duplicate package name '{pname}': "
                    f"'{seen_names[pname]}' and '{src_label}'. "
                    f"Consumers will see duplicate entries in browse."
                )
            else:
                seen_names[pname] = src_label
        self._compose_warnings = tuple(build_warnings)
        self._compose_diagnostics = tuple(diagnostics)

        doc["plugins"] = plugins
        return doc

    # -- upstream emission --------------------------------------------------

    def _emit_upstream_plugin(self, rup: ResolvedUpstreamPackage) -> dict[str, Any]:
        """Emit a single upstream-sourced plugin entry.

        Hard rules:

        * Output is byte-for-byte Anthropic-conformant; **no APM-specific
          keys** (no ``metadata.apm.*``). Provenance lives in
          ``apm.lock.yaml`` only.
        * Curator overrides on :class:`UpstreamPackageEntry` win over
          upstream values for ``description``, ``version``, and ``tags``.
          ``author``/``license``/``repository``/``homepage`` are
          curator-only (the strict upstream parser does not surface
          these fields, so there is nothing to fall back to).
        * Source emission shape matches the direct-package emit shape so
          ``parse_marketplace_json`` round-trips cleanly: outer
          ``source`` key with inner ``source`` discriminator
          (``"github"`` or ``"git-subdir"``), ``url`` for git-subdir.
        """
        entry = rup.entry
        plugin: dict[str, Any] = OrderedDict()
        plugin["name"] = entry.name

        # description: curator override > upstream plugin's value
        description = entry.description if entry.description else rup.plugin.description
        if description:
            plugin["description"] = description

        # version: curator override (if a display version) > upstream value
        if entry.version and _is_display_version(entry.version):
            plugin["version"] = entry.version
        elif rup.plugin.version:
            plugin["version"] = rup.plugin.version

        # author / license / repository (curator-only -- StrictPlugin
        # does not carry these). Mirrors direct-emit behaviour.
        if entry.author:
            plugin["author"] = dict(entry.author)
        if entry.license:
            plugin["license"] = entry.license
        if entry.repository:
            plugin["repository"] = entry.repository

        # tags: curator override > upstream plugin tags
        tags = entry.tags or rup.plugin.tags
        if tags:
            plugin["tags"] = list(tags)

        if entry.homepage:
            plugin["homepage"] = entry.homepage

        # source: shape matches direct emission so consumers using
        # ``parse_marketplace_json`` see no difference.
        source_obj: dict[str, Any] = OrderedDict()
        if rup.plugin_subdir:
            source_obj["source"] = "git-subdir"
            source_obj["url"] = rup.plugin_repo
            source_obj["path"] = rup.plugin_subdir
        else:
            source_obj["source"] = "github"
            source_obj["repo"] = rup.plugin_repo
        if rup.plugin_ref:
            source_obj["ref"] = rup.plugin_ref
        if rup.plugin_sha:
            source_obj["sha"] = rup.plugin_sha
        plugin["source"] = source_obj

        return plugin

    # -- diff ---------------------------------------------------------------

    @staticmethod
    def _compute_diff(
        old_json: dict[str, Any] | None,
        new_json: dict[str, Any],
    ) -> tuple[int, int, int, int]:
        """Compare old vs new marketplace.json and classify each plugin.

        Returns (unchanged, added, updated, removed) counts.
        """
        if old_json is None:
            return (0, len(new_json.get("plugins", [])), 0, 0)

        old_plugins: dict[str, str] = {}
        for p in old_json.get("plugins", []):
            name = p.get("name", "")
            sha = ""
            src = p.get("source", {})
            if isinstance(src, dict):
                # Accept both the new ``sha`` field (Claude-spec compliant)
                # and the legacy ``commit`` field for backward-compatibility
                # with marketplace.json files written before this PR.
                sha = src.get("sha") or src.get("commit", "")
            elif isinstance(src, str):
                sha = src  # local-path packages: use the path string itself
            old_plugins[name] = sha

        new_plugins: dict[str, str] = {}
        for p in new_json.get("plugins", []):
            name = p.get("name", "")
            sha = ""
            src = p.get("source", {})
            if isinstance(src, dict):
                sha = src.get("sha") or src.get("commit", "")
            elif isinstance(src, str):
                sha = src
            new_plugins[name] = sha

        unchanged = 0
        updated = 0
        added = 0
        removed = 0

        for name, sha in new_plugins.items():
            if name not in old_plugins:
                added += 1
            elif old_plugins[name] == sha:
                unchanged += 1
            else:
                updated += 1

        for name in old_plugins:
            if name not in new_plugins:
                removed += 1

        return (unchanged, added, updated, removed)

    # -- atomic write -------------------------------------------------------

    @staticmethod
    def _serialize_json(data: dict[str, Any]) -> str:
        """Serialize to JSON with 2-space indent, LF endings, trailing newline."""
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write *content* to *path* atomically via tmp + rename."""
        atomic_write(path, content)

    def _load_existing_json(self, path: Path) -> dict[str, Any] | None:
        """Load existing marketplace.json for diff, or None."""
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
            return json.loads(text)
        except (json.JSONDecodeError, OSError):
            return None

    # -- full pipeline ------------------------------------------------------

    def build(self) -> BuildReport:
        """Full pipeline: load -> resolve -> compose -> validate -> write.

        Returns
        -------
        BuildReport
            Summary including diff statistics and (when present) upstream
            resolution diagnostics.

        Raises
        ------
        BuildError
            When any error-level diagnostic is present (resolution
            failure, upstream rejection, round-trip mismatch). No
            ``marketplace.json`` is written when an error is raised --
            ``continue_on_error`` only governs whether multiple errors
            are collected before raising; it never permits writing
            broken output.
        """
        result = self.resolve()
        resolved = list(result.entries)
        upstream_resolved = list(result.upstream_entries)
        errors = result.errors

        new_json = self.compose_marketplace_json(
            resolved,
            upstream_resolved=upstream_resolved,
        )
        build_warnings = getattr(self, "_compose_warnings", ())
        build_diagnostics = list(getattr(self, "_compose_diagnostics", ()))

        # Lift upstream resolver diagnostics into the structured
        # diagnostics so existing CLI rendering picks them up. Error and
        # warning levels survive; codes are preserved. Warning-level
        # entries are also lifted into ``build_warnings`` so the existing
        # ``logger.warning()`` rendering path in ``pack.py`` surfaces
        # them. Status symbols are NOT baked into the message; the
        # rendering layer adds them via ``CommandLogger`` based on level.
        warning_messages: list[str] = list(build_warnings)
        for diag in result.upstream_diagnostics:
            label_parts: list[str] = []
            if diag.upstream_alias:
                label_parts.append(f"upstream '{diag.upstream_alias}'")
            if diag.plugin_name:
                label_parts.append(f"plugin '{diag.plugin_name}'")
            label = " / ".join(label_parts)
            message = f"{label}: {diag.message}" if label else diag.message
            build_diagnostics.append(
                BuildDiagnostic(level=diag.level, message=message, code=diag.code)
            )
            if diag.level == "warning":
                warning_messages.append(message)
        build_warnings = warning_messages

        output_path = self._output_path()

        # -- fail-closed gate before writing -------------------------------
        # Upstream resolution failures must NEVER produce a published
        # marketplace.json (they signal supply-chain or governance
        # breaks: missing alias, rename detected, unsupported source
        # shape). Direct-package errors continue to honour the existing
        # ``continue_on_error`` semantics (skip the failed entry, emit
        # the rest).
        upstream_errors = [d for d in build_diagnostics if d.level == "error"]
        if upstream_errors:
            if self._resolver is not None:
                self._resolver.close()
            headline = upstream_errors[0].message
            extra = len(upstream_errors) - 1
            summary = headline if extra == 0 else f"{headline} (and {extra} more)"
            raise BuildError(f"Build failed: {summary}")

        # -- round-trip validation invariant -------------------------------
        # Every emitted plugin must survive the lenient consumer parser
        # used by ``apm browse``/``apm install`` resolution. Catches
        # silent-skip discrepancies between the strict emission path and
        # the consumer parser before consumers ever see the output.
        try:
            roundtrip = parse_marketplace_json(new_json)
        except Exception as exc:
            if self._resolver is not None:
                self._resolver.close()
            raise BuildError(f"Round-trip parse of emitted marketplace.json failed: {exc}") from exc
        emitted_plugin_count = len(new_json.get("plugins", []))
        roundtrip_count = len(roundtrip.plugins)
        if roundtrip_count != emitted_plugin_count:
            if self._resolver is not None:
                self._resolver.close()
            raise BuildError(
                f"Round-trip parse dropped plugins: emitted "
                f"{emitted_plugin_count}, parsed {roundtrip_count}"
            )

        # Load existing for diff
        old_json = self._load_existing_json(output_path)
        unchanged, added, updated, removed = self._compute_diff(old_json, new_json)

        # Write (unless dry-run)
        if not self._options.dry_run:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            content = self._serialize_json(new_json)
            self._atomic_write(output_path, content)
            self._write_upstream_lockfile(upstream_resolved)

        # Cleanup resolver
        if self._resolver is not None:
            self._resolver.close()

        return BuildReport(
            resolved=tuple(resolved),
            errors=tuple(errors),
            warnings=tuple(build_warnings),
            diagnostics=tuple(build_diagnostics),
            unchanged_count=unchanged,
            added_count=added,
            updated_count=updated,
            removed_count=removed,
            output_path=output_path,
            dry_run=self._options.dry_run,
            upstream_resolved=tuple(upstream_resolved),
            upstream_diagnostics=result.upstream_diagnostics,
        )

    def _write_upstream_lockfile(self, upstream_resolved: list[ResolvedUpstreamPackage]) -> None:
        """Persist upstream provenance into ``apm.lock.yaml``.

        For every successfully resolved upstream package, record the
        upstream marketplace coordinates (host/owner/repo/path/manifest
        SHA) and the per-plugin pin (resolved SHA + emitted display
        name + resolved source dict). The lockfile is the **only**
        place provenance lives -- ``marketplace.json`` stays
        Anthropic-conformant with no APM-specific keys.

        Reads the existing lockfile (if any), preserves all unrelated
        sections, replaces the ``upstreams`` block, and writes back.
        """
        from datetime import datetime, timezone

        from ..deps.lockfile import (
            LockedUpstream,
            LockedUpstreamPlugin,
            LockFile,
            get_lockfile_path,
        )

        lockfile_path = get_lockfile_path(self._project_root)
        lock = LockFile.load_or_create(lockfile_path)

        if not upstream_resolved:
            # No upstreams in this build; if the existing lockfile has
            # an ``upstreams`` block from a previous build, preserve it
            # only when the apm.yml still declares those upstreams.
            # For v1 simplicity, leave the existing block untouched
            # when there's nothing new to write.
            return

        refreshed_at = datetime.now(timezone.utc).isoformat()
        new_upstreams: dict[str, LockedUpstream] = {}
        for rup in upstream_resolved:
            alias = rup.upstream.alias
            if alias not in new_upstreams:
                new_upstreams[alias] = LockedUpstream(
                    alias=alias,
                    host=rup.upstream.host,
                    owner=rup.upstream.repo.split("/", 1)[0]
                    if "/" in rup.upstream.repo
                    else rup.upstream.repo,
                    repo=rup.upstream.repo.split("/", 1)[1] if "/" in rup.upstream.repo else "",
                    path=rup.upstream.path,
                    manifest_sha=rup.upstream_manifest_sha,
                    canonical_full_name=rup.upstream_canonical_full_name,
                    refreshed_at=refreshed_at,
                )
            resolved_source = {
                "host": rup.plugin_host,
                "repo": rup.plugin_repo,
                "sha": rup.plugin_sha,
            }
            if rup.plugin_subdir:
                resolved_source["path"] = rup.plugin_subdir
            if rup.plugin_ref:
                resolved_source["ref"] = rup.plugin_ref
            new_upstreams[alias].plugins[rup.plugin.name] = LockedUpstreamPlugin(
                upstream_name=rup.plugin.name,
                emitted_as=rup.entry.name,
                resolved_sha=rup.plugin_sha,
                resolved_source=resolved_source,
            )

        lock.upstreams = new_upstreams
        lockfile_path.parent.mkdir(parents=True, exist_ok=True)
        lock.write(lockfile_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_ref_prefix(refname: str) -> str:
    """Strip ``refs/tags/`` or ``refs/heads/`` prefix."""
    if refname.startswith("refs/tags/"):
        return refname[len("refs/tags/") :]
    if refname.startswith("refs/heads/"):
        return refname[len("refs/heads/") :]
    return refname
