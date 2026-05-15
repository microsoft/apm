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
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple  # noqa: F401, UP035

import yaml

if TYPE_CHECKING:
    from ..core.auth import HostInfo

from ..utils.github_host import default_host
from ..utils.path_security import ensure_path_within
from ._io import atomic_write
from .diagnostics import BuildDiagnostic
from .errors import (
    BuildError,
    HeadNotAllowedError,
    NoMatchingVersionError,
    OfflineMissError,  # noqa: F401
    RefNotFoundError,
)
from .output_mappers import (
    MARKETPLACE_OUTPUT_MAPPERS,
    MapperResult,
)
from .output_mappers import (
    _is_display_version as _mapper_is_display_version,
)
from .output_mappers import (
    _subtract_plugin_root as _mapper_subtract_plugin_root,
)
from .output_profiles import (
    CODEX_MARKETPLACE_OUTPUT,
    DEFAULT_MARKETPLACE_OUTPUT,
    MarketplaceOutputProfile,
)
from .ref_resolver import RefResolver, RemoteRef  # noqa: F401
from .semver import SemVer, parse_semver, satisfies_range
from .tag_pattern import build_tag_regex, render_tag  # noqa: F401
from .yml_schema import MarketplaceYml, PackageEntry, load_marketplace_yml

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

    @property
    def ok(self) -> bool:
        """True when every package resolved without error."""
        return len(self.errors) == 0


@dataclass(frozen=True)
class MarketplaceOutputReport:
    """Summary for one generated marketplace output profile."""

    profile: str
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


@dataclass(frozen=True)
class BuildReport:
    """Summary of a marketplace build run across one or more output profiles."""

    outputs: tuple[MarketplaceOutputReport, ...]

    @property
    def primary_output(self) -> MarketplaceOutputReport:
        """Return the first output report for legacy single-output callers."""
        if not self.outputs:
            return MarketplaceOutputReport(
                profile="",
                resolved=(),
                errors=(),
                warnings=(),
            )
        return self.outputs[0]

    @property
    def resolved(self) -> tuple[ResolvedPackage, ...]:
        return self.primary_output.resolved

    @property
    def errors(self) -> tuple[tuple[str, str], ...]:
        return self.primary_output.errors

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(warn for output in self.outputs for warn in output.warnings)

    @property
    def diagnostics(self) -> tuple[BuildDiagnostic, ...]:
        return tuple(diag for output in self.outputs for diag in output.diagnostics)

    @property
    def unchanged_count(self) -> int:
        return self.primary_output.unchanged_count

    @property
    def added_count(self) -> int:
        return self.primary_output.added_count

    @property
    def updated_count(self) -> int:
        return self.primary_output.updated_count

    @property
    def removed_count(self) -> int:
        return self.primary_output.removed_count

    @property
    def output_path(self) -> Path:
        return self.primary_output.output_path

    @property
    def dry_run(self) -> bool:
        return any(output.dry_run for output in self.outputs)

    def to_json_dict(self) -> dict[str, Any]:
        """Serialize build report as the §4 JSON contract.

        Shape: {ok, dry_run, warnings[], errors[],
                marketplace: {outputs: [{format, path, added, updated,
                unchanged, skipped}]}, bundle: null}
        """
        all_warnings = list(self.warnings)
        all_errors: list[dict[str, str]] = []
        output_entries: list[dict[str, Any]] = []

        for out in self.outputs:
            output_entries.append(
                {
                    "format": out.profile,
                    "path": str(out.output_path),
                    "added": out.added_count,
                    "updated": out.updated_count,
                    "unchanged": out.unchanged_count,
                    "skipped": out.removed_count,
                }
            )
            for pkg_name, err_msg in out.errors:
                all_errors.append({"code": "build_error", "message": f"{pkg_name}: {err_msg}"})

        ok = len(all_errors) == 0
        return {
            "ok": ok,
            "dry_run": self.dry_run,
            "warnings": all_warnings,
            "errors": all_errors,
            "marketplace": {
                "outputs": output_entries,
            },
            "bundle": None,
        }

    @classmethod
    def failure_to_json_dict(
        cls,
        *,
        errors: list[dict[str, str]],
        warnings: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Produce the §4 JSON shape for a pre-build failure.

        Used when the build cannot even start (e.g., config parse error,
        unknown format filter).
        """
        return {
            "ok": False,
            "dry_run": dry_run,
            "warnings": warnings or [],
            "errors": errors,
            "marketplace": {
                "outputs": [],
            },
            "bundle": None,
        }


@dataclass
class BuildOptions:
    """Configuration knobs for MarketplaceBuilder."""

    concurrency: int = 8
    timeout_seconds: float = 10.0
    include_prerelease: bool = False
    allow_head: bool = False
    continue_on_error: bool = False
    offline: bool = False
    marketplace_output: Path | None = None
    # Backwards-compatible spelling for callers that predate ``apm pack``.
    output_override: Path | None = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

# 40-char hex SHA pattern
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def _is_display_version(version: str | None) -> bool:
    """Return True if *version* looks like a fixed display version, not a range."""
    return _mapper_is_display_version(version)


def _subtract_plugin_root(source: str, plugin_root: str) -> str:
    """Remove pluginRoot prefix from a local source path for emit."""
    return _mapper_subtract_plugin_root(source, plugin_root)


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
        if self._options.marketplace_output is not None:
            return self._options.marketplace_output
        if self._options.output_override is not None:
            return self._options.output_override
        yml = self._load_yml()
        output_path = self._project_root / yml.claude.output
        # Containment guard -- reject output paths that escape the project root.
        ensure_path_within(output_path, self._project_root)
        return output_path

    def _mapper_for_profile(self, profile: MarketplaceOutputProfile):
        mapper = MARKETPLACE_OUTPUT_MAPPERS.get(profile.mapper)
        if mapper is None:
            raise BuildError(f"Unknown marketplace output mapper: {profile.mapper}")
        return mapper

    def remote_metadata_for_profile(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
    ) -> dict[str, dict[str, Any]] | None:
        """Return remote metadata needed to compose this output, if any."""
        mapper = self._mapper_for_profile(profile)
        if not mapper.uses_remote_metadata:
            return None
        return self._prefetch_metadata(resolved)

    def _map_output(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
        remote_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> MapperResult:
        """Map resolved packages into one marketplace output format."""
        mapper = self._mapper_for_profile(profile)
        return mapper.compose(
            config=self._load_yml(),
            resolved=resolved,
            remote_metadata=remote_metadata,
        )

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

        Returns
        -------
        ResolveResult
            Contains resolved entries and any errors encountered.

        Raises
        ------
        BuildError
            On any resolution failure (unless ``continue_on_error``).
        """
        yml = self._load_yml()
        entries = yml.packages
        if not entries:
            return ResolveResult(entries=(), errors=())

        results: dict[int, ResolvedPackage] = {}
        errors: list[tuple[str, str]] = []

        # Eagerly resolve auth + create the shared RefResolver before
        # spawning workers -- avoids a race on _ensure_auth() and
        # matches the pattern used in _prefetch_metadata().
        self._get_resolver()

        with ThreadPoolExecutor(max_workers=min(self._options.concurrency, len(entries))) as pool:
            future_to_index = {
                pool.submit(self._resolve_entry, entry): idx for idx, entry in enumerate(entries)
            }
            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                entry = entries[idx]
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

        # Return in yml order
        ordered: list[ResolvedPackage] = []
        for idx in range(len(entries)):
            if idx in results:
                ordered.append(results[idx])
        return ResolveResult(entries=tuple(ordered), errors=tuple(errors))

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

    def compose_marketplace_json(self, resolved: list[ResolvedPackage]) -> dict[str, Any]:
        """Produce an Anthropic-compliant marketplace.json dict.

        All APM-only fields are stripped.  Key order follows the Anthropic
        schema exactly.

        Parameters
        ----------
        resolved:
            List of resolved packages (from ``resolve()``).

        Returns
        -------
        dict
            An ``OrderedDict``-style dict ready to be serialised as JSON.
        """
        resolved_tuple = tuple(resolved)
        mapper_result = self._map_output(
            DEFAULT_MARKETPLACE_OUTPUT,
            resolved_tuple,
            remote_metadata=self._prefetch_metadata(resolved_tuple),
        )
        self._compose_warnings = mapper_result.warnings
        self._compose_diagnostics = mapper_result.diagnostics
        return mapper_result.document

    def compose_codex_marketplace_json(
        self,
        resolved: list[ResolvedPackage],
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Produce a Codex ``.agents/plugins/marketplace.json`` document."""
        mapper_result = self._map_output(CODEX_MARKETPLACE_OUTPUT, tuple(resolved))
        return mapper_result.document, mapper_result.warnings

    def write_codex_marketplace_json(
        self,
        resolved: tuple[ResolvedPackage, ...],
    ) -> tuple[Path, tuple[str, ...]]:
        """Write the configured Codex marketplace output using resolved packages."""
        yml = self._load_yml()
        output_path = self._project_root / yml.codex.output
        ensure_path_within(output_path, self._project_root)
        output = self.write_output(CODEX_MARKETPLACE_OUTPUT, resolved, output_path)
        return output.output_path, output.warnings

    def compose_output(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
        remote_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], tuple[str, ...], tuple[BuildDiagnostic, ...]]:
        """Compose the JSON document for a marketplace output profile."""
        mapper_result = self._map_output(profile, resolved, remote_metadata=remote_metadata)
        return mapper_result.document, mapper_result.warnings, mapper_result.diagnostics

    def write_output(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
        output_path: Path,
        *,
        include_diff: bool = False,
        remote_metadata: dict[str, dict[str, Any]] | None = None,
        errors: tuple[tuple[str, str], ...] = (),
    ) -> BuildReport:
        """Write one marketplace output profile using already resolved packages."""
        ensure_path_within(output_path, self._project_root)
        new_json, warnings, diagnostics = self.compose_output(
            profile,
            resolved,
            remote_metadata=remote_metadata,
        )

        unchanged = added = updated = removed = 0
        if include_diff:
            old_json = self._load_existing_json(output_path)
            unchanged, added, updated, removed = self._compute_diff(old_json, new_json)

        if not self._options.dry_run:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(output_path, self._serialize_json(new_json))

        output_report = MarketplaceOutputReport(
            profile=profile.name,
            resolved=tuple(resolved),
            errors=tuple(errors),
            warnings=tuple(warnings),
            diagnostics=tuple(diagnostics),
            unchanged_count=unchanged,
            added_count=added,
            updated_count=updated,
            removed_count=removed,
            output_path=output_path,
            dry_run=self._options.dry_run,
        )
        return BuildReport(outputs=(output_report,))

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
        """Full pipeline: load -> resolve -> compose -> write.

        Returns
        -------
        BuildReport
            Summary including diff statistics.
        """
        result = self.resolve()
        report = self.write_output(
            DEFAULT_MARKETPLACE_OUTPUT,
            result.entries,
            self._output_path(),
            include_diff=True,
            errors=result.errors,
            remote_metadata=self.remote_metadata_for_profile(
                DEFAULT_MARKETPLACE_OUTPUT,
                result.entries,
            ),
        )

        # Cleanup resolver
        if self._resolver is not None:
            self._resolver.close()

        return BuildReport(
            outputs=report.outputs,
        )


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
