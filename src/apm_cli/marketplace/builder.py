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
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ..core.auth import AuthContext, HostInfo

from ..utils.github_host import default_host, is_azure_devops_hostname
from ..utils.path_security import ensure_path_within
from ..utils.yaml_io import load_yaml_str
from ._io import atomic_write
from ._shared import iter_semver_tags
from .auth_helpers import resolve_auth_for_host
from .diagnostics import BuildDiagnostic
from .errors import (
    BuildError,
    HeadNotAllowedError,
    NoMatchingVersionError,
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
    resolve_effective_output_path,
)
from .ref_resolver import RefResolver
from .semver import SemVer, parse_semver, satisfies_range
from .tag_pattern import build_tag_regex
from .yml_schema import (
    MarketplaceYml,
    PackageEntry,
    load_marketplace_yml,
    split_source_base,
)

logger = logging.getLogger(__name__)

_LOCAL_METADATA_MAX_BYTES = 64 * 1024
_METADATA_STATUSES = frozenset({"fetched", "empty", "failed", "offline", "local", "explicit"})
_CERTIFIABLE_METADATA_STATUSES = frozenset({"fetched", "empty", "local", "explicit"})
_CERTIFYING_METADATA_FIELDS = frozenset({"description", "version"})
MetadataEnrichmentStatus = Literal["fetched", "empty", "failed", "offline", "local", "explicit"]


def _read_capped_text(resp: Any) -> str:
    """Read a remote metadata body bounded to ``_LOCAL_METADATA_MAX_BYTES``.

    Cosmetic metadata enrichment must never buffer an unbounded remote
    ``apm.yml``; a body over the cap raises ``ValueError``, which the caller's
    ``except Exception`` turns into a fail-closed ``None`` (no metadata) -- the
    same byte ceiling the local on-disk reader applies.
    """
    raw = resp.read(_LOCAL_METADATA_MAX_BYTES + 1)
    if len(raw) > _LOCAL_METADATA_MAX_BYTES:
        raise ValueError("remote metadata exceeds byte cap")
    return raw.decode("utf-8")


def _safe_metadata_failure_cause(exc: BaseException) -> str:
    """Return a stable failure category without propagating exception secrets."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, TimeoutError):
        return "request timed out"
    if isinstance(exc, urllib.error.URLError):
        return "network request failed"
    if isinstance(exc, PermissionError):
        return "authentication required"
    return type(exc).__name__


__all__ = [
    "BuildDiagnostic",
    "BuildOptions",
    "BuildReport",
    "MarketplaceBuilder",
    "MetadataEnrichmentOutcome",
    "MetadataEnrichmentResult",
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
    source_repo: str  # repository path without an optional host
    subdir: str | None  # APM-only (used to compose the output ``source`` object)
    ref: str  # resolved tag name, e.g. "v1.2.0"
    sha: str  # 40-char git SHA
    requested_version: str | None  # original APM-only range (for diagnostics)
    tags: tuple[str, ...]
    is_prerelease: bool  # True if the resolved ref was a prerelease semver
    host: str | None = None  # non-default git host parsed from apm.yml source
    source_url: str | None = None  # canonical URL for sourceBase-composed entries
    # Propagated to marketplace.json so consumer range resolution and
    # diagnostics use the producer's convention without re-reading apm.yml.
    effective_tag_pattern: str = ""
    curator_metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class MetadataEnrichmentOutcome:
    """The metadata-fetch result for one resolved marketplace package."""

    package: str
    status: MetadataEnrichmentStatus
    values: tuple[tuple[str, str], ...] = ()
    cause: str | None = None

    def __post_init__(self) -> None:
        """Reject unknown states so certification always fails closed."""
        if self.status not in _METADATA_STATUSES:
            raise ValueError(f"unknown metadata enrichment status: {self.status!r}")

    @property
    def metadata(self) -> dict[str, str]:
        """Return this outcome's mapper-ready description/version values."""
        return dict(self.values)

    def to_json_dict(self) -> dict[str, Any]:
        """Serialize the durable outcome without exposing source document contents."""
        payload: dict[str, Any] = {"package": self.package, "status": self.status}
        if self.cause is not None:
            payload["cause"] = self.cause
        return payload


@dataclass(frozen=True)
class MetadataEnrichmentResult(Mapping[str, dict[str, str]]):
    """Canonical metadata enrichment state for one marketplace build.

    The mapping interface preserves mapper compatibility while this object owns
    the distinction between an empty source manifest and an unavailable source.
    Consumers must use ``certifiable`` rather than inferring it from omitted
    mapped fields.
    """

    outcomes: tuple[MetadataEnrichmentOutcome, ...] = ()
    _metadata_by_package: dict[str, dict[str, str]] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Index mapper-ready metadata once for every output composition."""
        object.__setattr__(
            self,
            "_metadata_by_package",
            {outcome.package: dict(outcome.values) for outcome in self.outcomes if outcome.values},
        )

    def __getitem__(self, package: str) -> dict[str, str]:
        return dict(self._metadata_by_package[package])

    def __iter__(self) -> Iterator[str]:
        return iter(self._metadata_by_package)

    def __len__(self) -> int:
        return len(self._metadata_by_package)

    @property
    def certifiable(self) -> bool:
        """Whether output based on this enrichment may certify clean drift."""
        return all(outcome.status in _CERTIFIABLE_METADATA_STATUSES for outcome in self.outcomes)

    @property
    def warnings(self) -> tuple[str, ...]:
        """Return user-actionable warnings for incomplete remote enrichment."""
        warnings: list[str] = []
        for outcome in self.outcomes:
            if outcome.status == "offline":
                warnings.append(
                    f"Package '{outcome.package}': metadata enrichment skipped by "
                    "--offline. Rerun without --offline when certification is required."
                )
            elif outcome.status == "failed":
                cause = outcome.cause or "unknown failure"
                if cause.startswith("metadata fetch is unsupported for host"):
                    warnings.append(
                        f"Package '{outcome.package}': metadata enrichment failed "
                        f"({cause}). Add fixed description and version fields to certify "
                        "this package."
                    )
                else:
                    warnings.append(
                        f"Package '{outcome.package}': metadata enrichment failed "
                        f"({cause}). Restore repository, network, or credential access and retry."
                    )
        return tuple(warnings)

    def to_json_dict(self) -> dict[str, Any]:
        """Serialize metadata completeness for machine-readable pack output."""
        return {
            "certifiable": self.certifiable,
            "outcomes": [outcome.to_json_dict() for outcome in self.outcomes],
        }


@dataclass(frozen=True)
class _SourceBaseCoords:
    """Parsed sourceBase coordinates cached for one marketplace build."""

    host: str
    path_prefix: str
    source_base: str

    @property
    def org_hint(self) -> str:
        """Return the leading path segment used for per-org auth lookup."""
        return self.path_prefix.split("/", 1)[0]


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
    metadata_enrichment: MetadataEnrichmentResult | None = None


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
        warnings = [warn for output in self.outputs for warn in output.warnings]
        warnings.extend(self.metadata_enrichment.warnings)
        return tuple(dict.fromkeys(warnings))

    @property
    def metadata_enrichment(self) -> MetadataEnrichmentResult:
        """Return the canonical result shared by metadata-aware outputs."""
        for output in self.outputs:
            if output.metadata_enrichment is not None:
                return output.metadata_enrichment
        return MetadataEnrichmentResult()

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
        """Serialize build report as the Section 4 JSON contract.

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
            "metadata_enrichment": self.metadata_enrichment.to_json_dict(),
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
        metadata_enrichment: MetadataEnrichmentResult | None = None,
    ) -> dict[str, Any]:
        """Produce the Section 4 JSON shape for a pre-build failure.

        Used when the build cannot even start (e.g., config parse error,
        unknown format filter).
        """
        return {
            "ok": False,
            "dry_run": dry_run,
            "warnings": warnings or [],
            "errors": errors,
            "metadata_enrichment": (
                metadata_enrichment
                if metadata_enrichment is not None
                else MetadataEnrichmentResult()
            ).to_json_dict(),
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
        # Per-host RefResolver cache, keyed by host and optional org hint.
        # Pre-warmed on the main thread before workers spawn; lock guards
        # against future refactors that allow worker-side cache misses.
        self._host_resolvers: dict[tuple[str, str | None], RefResolver] = {}
        self._host_resolvers_lock = threading.Lock()
        self._source_base_parts: _SourceBaseCoords | None = None
        self._source_base_parts_loaded = False
        self._metadata_enrichment: MetadataEnrichmentResult | None = None
        self._metadata_fetch_outcomes: dict[str, MetadataEnrichmentOutcome] = {}
        self._metadata_fetch_outcomes_lock = threading.Lock()

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

    def _get_source_base_parts(self) -> _SourceBaseCoords | None:
        """Return cached sourceBase coordinates for this builder."""
        if not self._source_base_parts_loaded:
            yml = self._load_yml()
            source_base = getattr(yml, "source_base", None)
            if isinstance(source_base, str) and source_base:
                base_host, base_path = split_source_base(source_base)
                self._source_base_parts = _SourceBaseCoords(
                    host=base_host,
                    path_prefix=base_path,
                    source_base=source_base,
                )
            self._source_base_parts_loaded = True
        return self._source_base_parts

    def _get_resolver(self) -> RefResolver:
        if self._resolver is None:
            self._ensure_auth()
            auth = self._resolve_auth_for_host(self._host)
            harden = getattr(
                self._auth_resolver,
                "hardened_git_env_for_context",
                None,
            )
            self._resolver = RefResolver(
                timeout_seconds=self._options.timeout_seconds,
                offline=self._options.offline,
                host=self._host,
                token=auth.token if auth else self._github_token,
                auth_scheme=getattr(auth, "auth_scheme", "basic"),
                git_env=(harden(auth) if auth is not None and callable(harden) else None),
                auth_resolver=self._auth_resolver,
                auth_target=self._host,
            )
        return self._resolver

    def _effective_host(self, host: str | None) -> str | None:
        """Normalize ``host`` for marketplace.json emission.

        Returns ``None`` when ``host`` matches the active default host so
        an explicit ``github.com/owner/repo`` source in apm.yml emits the
        same shorthand (``source: github``, ``repo: owner/repo``) shape as
        the bare ``owner/repo`` form.  Non-default hosts pass through
        unchanged and downstream mappers emit ``source: url`` /
        ``source: git-subdir`` with the full HTTPS URL.
        """
        if host is None or host == self._host:
            return None
        return host

    def _get_resolver_for_host(self, host: str | None, *, org: str | None = None) -> RefResolver:
        """Return a RefResolver bound to *host* and optional auth org hint.

        Non-default hosts and sourceBase-derived org hints go through
        ``AuthResolver.resolve(host, org=org)`` so per-org variables are
        honored before ambient git credentials.  Existing default-host calls
        without an org hint keep the legacy resolver path.
        """
        if org is None and (host is None or host == self._host):
            return self._get_resolver()
        resolved_host = host or self._host
        key = (resolved_host, org)
        with self._host_resolvers_lock:
            cached = self._host_resolvers.get(key)
            if cached is not None:
                return cached
            auth = self._resolve_auth_for_host(resolved_host, org=org)
            harden = getattr(
                self._auth_resolver,
                "hardened_git_env_for_context",
                None,
            )
            logger.debug(
                "Creating per-host RefResolver for %s (org=%s, token=%s)",
                resolved_host,
                org or "none",
                "set" if auth else "unset",
            )
            resolver = RefResolver(
                timeout_seconds=self._options.timeout_seconds,
                offline=self._options.offline,
                host=resolved_host,
                token=auth.token if auth else None,
                auth_scheme=getattr(auth, "auth_scheme", "basic"),
                git_env=(harden(auth) if auth is not None and callable(harden) else None),
                auth_resolver=self._auth_resolver,
                auth_target=resolved_host,
            )
            self._host_resolvers[key] = resolver
            return resolver

    def _resolve_auth_for_host(self, host: str, *, org: str | None = None) -> AuthContext | None:
        """Resolve the complete auth context for marketplace git operations."""
        if self._options.offline:
            return None
        from ..core.auth import AuthResolver  # lazy import

        resolver = self._auth_resolver
        if resolver is None:
            resolver = AuthResolver()
            self._auth_resolver = resolver
        return resolve_auth_for_host(
            host,
            offline=self._options.offline,
            org=org,
            auth_resolver=resolver,
        )

    def _resolve_token_for_host(self, host: str, *, org: str | None = None) -> str | None:
        """Resolve an auth token for *host* via the shared marketplace helper."""
        auth = self._resolve_auth_for_host(host, org=org)
        return auth.token if auth else None

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
        yml = self._load_yml()
        overrides = (
            {DEFAULT_MARKETPLACE_OUTPUT.name: self._options.output_override}
            if self._options.output_override is not None
            else None
        )
        return resolve_effective_output_path(
            yml,
            DEFAULT_MARKETPLACE_OUTPUT,
            self._project_root,
            overrides,
        )

    def _mapper_for_profile(self, profile: MarketplaceOutputProfile):
        mapper = MARKETPLACE_OUTPUT_MAPPERS.get(profile.mapper)
        if mapper is None:
            raise BuildError(f"Unknown marketplace output mapper: {profile.mapper}")
        return mapper

    def remote_metadata_for_profile(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
    ) -> MetadataEnrichmentResult | None:
        """Return the canonical enrichment result needed to compose *profile*."""
        mapper = self._mapper_for_profile(profile)
        if not mapper.uses_remote_metadata:
            return None
        result = self._prefetch_metadata(resolved)
        if isinstance(result, MetadataEnrichmentResult):
            return result
        # Compatibility for tests and third-party callers that substituted the
        # old mapping return value while the builder remained the owner.
        return MetadataEnrichmentResult(
            outcomes=tuple(
                MetadataEnrichmentOutcome(
                    package=pkg.name,
                    status="fetched" if pkg.name in result else "empty",
                    values=tuple(sorted(result.get(pkg.name, {}).items())),
                )
                for pkg in resolved
            )
        )

    def _map_output(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
        remote_metadata: Mapping[str, dict[str, str]] | None = None,
    ) -> MapperResult:
        """Map resolved packages into one marketplace output format."""
        mapper = self._mapper_for_profile(profile)
        return mapper.compose(
            config=self._load_yml(),
            resolved=resolved,
            remote_metadata=remote_metadata,
        )

    # -- single-entry resolution --------------------------------------------

    def _remote_source_coordinates(
        self,
        entry: PackageEntry,
    ) -> tuple[str | None, str, str | None, str | None]:
        """Return ``(host, repo_path, source_url, org_hint)`` for a remote entry."""
        if entry.host:
            if entry.source_url is not None:
                from ..models.dependency.reference import DependencyReference

                dependency = DependencyReference.parse(entry.source_url)
                return (
                    dependency.host,
                    dependency.repo_url,
                    entry.source_url,
                    dependency.ado_organization,
                )
            return entry.host, entry.source, None, None
        source_base_parts = self._get_source_base_parts()
        if source_base_parts is not None:
            repo_path = f"{source_base_parts.path_prefix}/{entry.source}"
            source_url = f"{source_base_parts.source_base}/{entry.source}"
            if is_azure_devops_hostname(source_base_parts.host):
                from ..models.dependency.reference import DependencyReference

                dependency = DependencyReference.parse(source_url)
                return (
                    dependency.host,
                    dependency.repo_url,
                    source_url,
                    dependency.ado_organization,
                )
            logger.debug(
                "Composed marketplace source %r onto sourceBase %r as %r",
                entry.source,
                source_base_parts.source_base,
                repo_path,
            )
            return source_base_parts.host, repo_path, source_url, source_base_parts.org_hint
        return None, entry.source, None, None

    def _resolved_output_host(
        self,
        *,
        source_host: str | None,
        source_url: str | None,
    ) -> str | None:
        """Return the host marker mappers should use for the resolved package."""
        if source_url is not None:
            return source_host
        return self._effective_host(source_host)

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
                curator_metadata=self._curator_metadata(entry),
            )
        yml = self._load_yml()
        source_host, owner_repo, source_url, source_org = self._remote_source_coordinates(entry)
        if source_org is None:
            resolver = self._get_resolver_for_host(source_host)
        else:
            resolver = self._get_resolver_for_host(source_host, org=source_org)

        if entry.ref is not None:
            return self._resolve_explicit_ref(
                entry,
                resolver,
                owner_repo,
                source_host=source_host,
                source_url=source_url,
                effective_tag_pattern=entry.tag_pattern or yml.build.tag_pattern,
            )
        # version range resolution
        return self._resolve_version_range(
            entry,
            resolver,
            owner_repo,
            yml,
            source_host=source_host,
            source_url=source_url,
        )

    @staticmethod
    def _curator_metadata(entry: PackageEntry) -> tuple[tuple[str, str], ...]:
        """Return fixed curator fields that can certify without a remote fetch."""
        metadata: dict[str, str] = {}
        if entry.description:
            metadata["description"] = entry.description
        if _is_display_version(entry.version):
            assert entry.version is not None  # noqa: S101
            metadata["version"] = entry.version
        return tuple(sorted(metadata.items()))

    def _resolve_explicit_ref(
        self,
        entry: PackageEntry,
        resolver: RefResolver,
        owner_repo: str,
        *,
        source_host: str | None = None,
        source_url: str | None = None,
        effective_tag_pattern: str = "",
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
                host=self._resolved_output_host(source_host=source_host, source_url=source_url),
                source_url=source_url,
                effective_tag_pattern=effective_tag_pattern,
                curator_metadata=self._curator_metadata(entry),
            )

        refs = resolver.list_remote_refs(owner_repo)

        # Single-pass index for O(1) lookup by tag name, full refname, and branch
        tags_by_name: dict[str, Any] = {}
        refs_by_name: dict[str, Any] = {}
        branches_by_name: dict[str, Any] = {}
        for remote_ref in refs:
            refs_by_name[remote_ref.name] = remote_ref
            if remote_ref.name.startswith("refs/tags/"):
                tag_name = _strip_ref_prefix(remote_ref.name)
                tags_by_name[tag_name] = remote_ref
            elif remote_ref.name.startswith("refs/heads/"):
                branch_name = remote_ref.name[len("refs/heads/") :]
                branches_by_name[branch_name] = remote_ref

        # Try as tag first
        if ref_text in tags_by_name:
            remote_ref = tags_by_name[ref_text]
            tag_name = _strip_ref_prefix(remote_ref.name)
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
                host=self._resolved_output_host(source_host=source_host, source_url=source_url),
                source_url=source_url,
                effective_tag_pattern=effective_tag_pattern,
                curator_metadata=self._curator_metadata(entry),
            )

        # Try as full refname
        if ref_text in refs_by_name:
            remote_ref = refs_by_name[ref_text]
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
                host=self._resolved_output_host(source_host=source_host, source_url=source_url),
                source_url=source_url,
                effective_tag_pattern=effective_tag_pattern,
                curator_metadata=self._curator_metadata(entry),
            )

        # Try as branch name
        if ref_text in branches_by_name:
            remote_ref = branches_by_name[ref_text]
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
                host=self._resolved_output_host(source_host=source_host, source_url=source_url),
                source_url=source_url,
                effective_tag_pattern=effective_tag_pattern,
                curator_metadata=self._curator_metadata(entry),
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
        *,
        source_host: str | None = None,
        source_url: str | None = None,
    ) -> ResolvedPackage:
        """Resolve an entry using its ``version:`` semver range."""
        version_range = entry.version
        assert version_range is not None  # noqa: S101

        # Determine tag pattern: entry > build > default
        pattern = entry.tag_pattern or yml.build.tag_pattern

        tag_rx = build_tag_regex(pattern, name=entry.name)
        refs = resolver.list_remote_refs(owner_repo)

        # Filter tags matching the pattern and extract versions
        candidates: list[tuple[SemVer, str, str]] = []  # (semver, tag_name, sha)
        for sv, tag_name, sha in iter_semver_tags(refs, tag_rx):
            # Prerelease filter
            include_pre = entry.include_prerelease or self._options.include_prerelease
            if sv.is_prerelease and not include_pre:
                continue

            # Range filter
            if satisfies_range(sv, version_range):
                candidates.append((sv, tag_name, sha))

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
            host=self._resolved_output_host(source_host=source_host, source_url=source_url),
            source_url=source_url,
            effective_tag_pattern=pattern,
            curator_metadata=self._curator_metadata(entry),
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
        # Pre-warm per-host resolvers on the main thread so workers never race
        # to create the same resolver. Include the sourceBase host because
        # base-relative entries derive their host during composition.
        source_base_parts = self._get_source_base_parts()
        if source_base_parts is not None:
            self._get_resolver_for_host(source_base_parts.host, org=source_base_parts.org_hint)
        for entry in entries:
            if entry.host:
                self._get_resolver_for_host(entry.host)

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

    # -- description/version metadata fetchers ------------------------------

    def _fetch_local_metadata(self, pkg: ResolvedPackage) -> dict[str, str] | None:
        """Best-effort: read ``description`` and ``version`` from a
        local-path package's ``apm.yml`` on disk.

        Local-path packages (``source: ./...``) record the curator's
        ``source`` value on ``ResolvedPackage.subdir``; the package's
        own ``apm.yml`` lives at ``<project_root>/<subdir>/apm.yml``.
        Returns a dict with ``description`` and/or ``version`` keys, or
        ``None`` when the file is missing or unreadable.  Mirrors
        ``_fetch_remote_metadata``: cosmetic enrichment only, failures
        are logged at debug level and never propagate.

        The resolved path is constrained to ``self._project_root`` so a
        curator entry pointing outside the tree is skipped.  A source
        that resolves to the project root itself is also skipped -- that
        file is the marketplace's own ``apm.yml``, not a package
        manifest.
        """
        if not pkg.subdir:
            return None
        try:
            project_root = ensure_path_within(self._project_root, self._project_root)
            package_root = ensure_path_within(project_root / pkg.subdir, project_root)
            if package_root == project_root:
                return None
            file_path = package_root / "apm.yml"
            if not file_path.is_file():
                return None
            metadata_path = ensure_path_within(file_path, project_root)
            with metadata_path.open("rb") as handle:
                raw = handle.read(_LOCAL_METADATA_MAX_BYTES + 1)
            if len(raw) > _LOCAL_METADATA_MAX_BYTES:
                logger.debug(
                    "Skipping local metadata for %s: apm.yml exceeds %d bytes",
                    pkg.name,
                    _LOCAL_METADATA_MAX_BYTES,
                )
                return None
            data = load_yaml_str(raw.decode("utf-8"))
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
                    "Read local metadata for %s from %s: %s",
                    pkg.name,
                    file_path,
                    ", ".join(result.keys()),
                )
                return result
        except Exception:
            logger.debug(
                "Could not read local metadata for %s",
                pkg.name,
                exc_info=True,
            )
        return None

    def _fetch_remote_metadata_outcome(
        self,
        pkg: ResolvedPackage,
    ) -> MetadataEnrichmentOutcome:
        """Fetch remote metadata while preserving complete failure state.

        When a token is available for the package's host, it is included
        as an ``Authorization`` header so private repos can be accessed.
        A token resolved for the builder's default host is never sent to
        another host.

        github.com packages use the fast raw.githubusercontent.com CDN
        first, then fall back to the GitHub REST Contents endpoint when
        raw returns 404 (the private / INTERNAL repository symptom).
        GHES and GHE Cloud packages use the GitHub REST API on the
        package's host.  For non-GitHub-class hosts, metadata enrichment
        is skipped.
        """
        try:
            from ..core.auth import AuthResolver

            path_prefix = f"{pkg.subdir}/" if pkg.subdir else ""
            file_path = f"{path_prefix}apm.yml"

            # Resolve the effective host for this package and its
            # classification.  Falls back to the builder default when the
            # package did not carry an explicit host override.
            effective_host = pkg.host or self._host
            if pkg.host is None or pkg.host == self._host:
                host_info = self._host_info
            else:
                try:
                    host_info = AuthResolver.classify_host(effective_host)
                except Exception:
                    host_info = None

            host_kind = host_info.kind if host_info else "github"

            if host_kind not in ("github", "ghe_cloud", "ghes"):
                # Non-GitHub hosts cannot provide this GitHub-specific enrichment.
                logger.debug(
                    "Metadata fetch unsupported for %s (non-GitHub host: %s)",
                    pkg.name,
                    effective_host,
                )
                return MetadataEnrichmentOutcome(
                    pkg.name,
                    "failed",
                    cause=f"metadata fetch is unsupported for host '{effective_host}'",
                )

            auth_resolver = self._auth_resolver

            def _open_metadata(token: str | None, _git_env: dict[str, str]) -> str:
                """Fetch the manifest with the credential selected for this attempt."""
                if host_kind == "ghe_cloud" and token is None:
                    raise PermissionError(
                        f"metadata fetch for '{effective_host}' requires a credential"
                    )
                if effective_host == "github.com":
                    raw_url = (
                        f"https://raw.githubusercontent.com/{pkg.source_repo}/{pkg.sha}/{file_path}"
                    )
                    req = urllib.request.Request(raw_url)  # noqa: S310
                    if token:
                        req.add_header("Authorization", f"token {token}")
                    try:
                        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                            return _read_capped_text(resp)
                    except urllib.error.HTTPError as exc:
                        if exc.code != 404:
                            raise
                        if token is None:
                            raise
                        api_base = (
                            host_info.api_base if host_info else None
                        ) or "https://api.github.com"
                        rest_url = (
                            f"{api_base}/repos/{pkg.source_repo}/contents/{file_path}?ref={pkg.sha}"
                        )
                        req = urllib.request.Request(rest_url)  # noqa: S310
                        req.add_header("Accept", "application/vnd.github.raw")
                        if token:
                            req.add_header("Authorization", f"token {token}")
                        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                            return _read_capped_text(resp)

                api_base = (
                    host_info.api_base if host_info else None
                ) or f"https://{effective_host}/api/v3"
                url = f"{api_base}/repos/{pkg.source_repo}/contents/{file_path}?ref={pkg.sha}"
                req = urllib.request.Request(url)  # noqa: S310
                req.add_header("Accept", "application/vnd.github.raw")
                if token:
                    req.add_header("Authorization", f"token {token}")
                with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                    return _read_capped_text(resp)

            org = pkg.source_repo.split("/", 1)[0] if pkg.source_repo else None
            if auth_resolver is None:
                # Legacy direct callers do not enter the concurrent prefetch
                # path, which initializes AuthResolver before worker threads.
                raw = _open_metadata(self._github_token, {})
            else:
                unauth_first = auth_resolver.uses_public_github_anonymous_first(effective_host)
                raw = auth_resolver.try_with_fallback(
                    effective_host,
                    _open_metadata,
                    org=org,
                    path=pkg.source_repo,
                    unauth_first=unauth_first,
                )
            data = load_yaml_str(raw)
            if not isinstance(data, dict):
                return MetadataEnrichmentOutcome(pkg.name, "empty")
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
                return MetadataEnrichmentOutcome(
                    pkg.name,
                    "fetched",
                    values=tuple(sorted(result.items())),
                )
            return MetadataEnrichmentOutcome(pkg.name, "empty")
        except Exception as exc:
            logger.debug(
                "Could not fetch remote metadata for %s",
                pkg.name,
                exc_info=True,
            )
            cause = _safe_metadata_failure_cause(exc)
            return MetadataEnrichmentOutcome(pkg.name, "failed", cause=cause)

    def _fetch_remote_metadata(self, pkg: ResolvedPackage) -> dict[str, str] | None:
        """Return remote metadata for legacy direct callers.

        The full outcome remains owned by
        :meth:`_fetch_remote_metadata_outcome`; this compatibility adapter must
        not be used to decide whether an output is certifiable.
        """
        outcome = self._fetch_remote_metadata_outcome(pkg)
        with self._metadata_fetch_outcomes_lock:
            self._metadata_fetch_outcomes[pkg.name] = outcome
        return outcome.metadata or None

    def _fetch_local_metadata_outcome(self, pkg: ResolvedPackage) -> MetadataEnrichmentOutcome:
        """Return local metadata as an intentional, certifiable enrichment state."""
        metadata = self._fetch_local_metadata(pkg)
        return MetadataEnrichmentOutcome(
            pkg.name,
            "local",
            values=tuple(sorted((metadata or {}).items())),
        )

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

    def _prefetch_metadata(
        self,
        resolved: tuple[ResolvedPackage, ...],
    ) -> MetadataEnrichmentResult:
        """Fetch package metadata and retain every per-package outcome.

        This is the single authority for deciding whether metadata-dependent
        output is certifiable.  A reachable manifest without description or
        version is ``empty``; an unavailable remote source is ``failed`` or
        intentionally ``offline`` and cannot certify a clean regeneration.
        """
        if self._metadata_enrichment is not None:
            return self._metadata_enrichment

        outcomes_by_package: dict[str, MetadataEnrichmentOutcome] = {}
        remote: list[ResolvedPackage] = []
        for pkg in resolved:
            curator_metadata = dict(pkg.curator_metadata)
            if pkg.source_repo and curator_metadata.keys() >= _CERTIFYING_METADATA_FIELDS:
                outcomes_by_package[pkg.name] = MetadataEnrichmentOutcome(
                    pkg.name,
                    "explicit",
                    values=pkg.curator_metadata,
                )
            elif pkg.source_repo:
                remote.append(pkg)
            else:
                outcomes_by_package[pkg.name] = self._fetch_local_metadata_outcome(pkg)

        if self._options.offline:
            for pkg in remote:
                outcomes_by_package[pkg.name] = MetadataEnrichmentOutcome(
                    pkg.name,
                    "offline",
                    cause="metadata fetch skipped by --offline",
                )
        elif remote:
            from ..core.auth import AuthResolver

            if self._auth_resolver is None:
                self._auth_resolver = AuthResolver()
            if self._host_info is None:
                self._host_info = AuthResolver.classify_host(self._host)
            workers = min(self._options.concurrency, len(remote))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_package = {
                    pool.submit(self._fetch_remote_metadata, pkg): pkg for pkg in remote
                }
                for future in as_completed(future_to_package):
                    pkg = future_to_package[future]
                    try:
                        metadata = future.result()
                        with self._metadata_fetch_outcomes_lock:
                            outcome = self._metadata_fetch_outcomes.pop(pkg.name, None)
                        if (
                            isinstance(outcome, MetadataEnrichmentOutcome)
                            and outcome.package == pkg.name
                        ):
                            outcomes_by_package[pkg.name] = outcome
                        else:
                            # Preserve the legacy extension seam: callers that
                            # override _fetch_remote_metadata still feed the
                            # canonical outcome owner without reimplementing
                            # certifiability logic.
                            outcomes_by_package[pkg.name] = MetadataEnrichmentOutcome(
                                pkg.name,
                                "fetched" if metadata else "empty",
                                values=tuple(sorted((metadata or {}).items())),
                            )
                    except Exception as exc:
                        cause = _safe_metadata_failure_cause(exc)
                        outcomes_by_package[pkg.name] = MetadataEnrichmentOutcome(
                            pkg.name,
                            "failed",
                            cause=cause,
                        )

        self._metadata_enrichment = MetadataEnrichmentResult(
            outcomes=tuple(outcomes_by_package[pkg.name] for pkg in resolved)
        )
        return self._metadata_enrichment

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
        output_path = resolve_effective_output_path(
            yml,
            CODEX_MARKETPLACE_OUTPUT,
            self._project_root,
        )
        output = self.write_output(CODEX_MARKETPLACE_OUTPUT, resolved, output_path)
        return output.output_path, output.warnings

    def compose_output(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
        remote_metadata: Mapping[str, dict[str, str]] | None = None,
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
        remote_metadata: MetadataEnrichmentResult | None = None,
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
            metadata_enrichment=remote_metadata,
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

        # Cleanup default + per-host resolvers so long-lived builder
        # instances do not leak caches or thread locks across builds.
        if self._resolver is not None:
            self._resolver.close()
        with self._host_resolvers_lock:
            for host_resolver in self._host_resolvers.values():
                try:
                    host_resolver.close()
                except Exception:  # pragma: no cover - close is best-effort
                    logger.debug("Failed to close per-host RefResolver", exc_info=True)
            self._host_resolvers.clear()

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
