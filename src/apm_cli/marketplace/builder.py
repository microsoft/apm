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

Internal implementation is split across sibling leaf modules:

* ``._builder_reports`` -- frozen result/report dataclasses + BuildOptions
* ``._builder_resolve`` -- ``_BuilderResolveMixin`` (resolve + metadata fetch)
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request  # noqa: F401 -- patchable at apm_cli.marketplace.builder.urllib.request.urlopen
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.auth import HostInfo

from ..utils.github_host import default_host
from ..utils.path_security import ensure_path_within
from ._builder_reports import (
    BuildOptions as BuildOptions,
)
from ._builder_reports import (
    BuildReport as BuildReport,
)
from ._builder_reports import (
    MarketplaceOutputReport as MarketplaceOutputReport,
)
from ._builder_reports import (
    ResolvedPackage as ResolvedPackage,
)
from ._builder_reports import (
    ResolveResult as ResolveResult,
)
from ._builder_resolve import _BuilderResolveMixin
from ._builder_resolve import _strip_ref_prefix as _strip_ref_prefix
from ._io import atomic_write
from .diagnostics import BuildDiagnostic as BuildDiagnostic
from .errors import BuildError
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
from .ref_resolver import RefResolver
from .yml_schema import (
    MarketplaceYml,
    load_marketplace_yml,
    split_source_base,
)

logger = logging.getLogger(__name__)

_LOCAL_METADATA_MAX_BYTES = 64 * 1024


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


__all__ = [
    "BuildDiagnostic",
    "BuildOptions",
    "BuildReport",
    "MarketplaceBuilder",
    "ResolveResult",
    "ResolvedPackage",
]


def _is_display_version(version: str | None) -> bool:
    """Return True if *version* looks like a fixed display version, not a range."""
    return _mapper_is_display_version(version)


def _subtract_plugin_root(source: str, plugin_root: str) -> str:
    """Remove pluginRoot prefix from a local source path for emit."""
    return _mapper_subtract_plugin_root(source, plugin_root)


class MarketplaceBuilder(_BuilderResolveMixin):
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

    @classmethod
    def from_config(
        cls,
        config: MarketplaceYml,
        project_root: Path,
        options: BuildOptions | None = None,
        auth_resolver: object | None = None,
    ) -> MarketplaceBuilder:
        """Construct a builder from an already-loaded MarketplaceConfig."""
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
            self._resolver = RefResolver(
                timeout_seconds=self._options.timeout_seconds,
                offline=self._options.offline,
                host=self._host,
                token=self._github_token,
            )
        return self._resolver

    def _effective_host(self, host: str | None) -> str | None:
        """Normalize ``host`` for marketplace.json emission.

        Returns ``None`` when ``host`` matches the active default host.
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
            token = self._resolve_token_for_host(resolved_host, org=org)
            logger.debug(
                "Creating per-host RefResolver for %s (org=%s, token=%s)",
                resolved_host,
                org or "none",
                "set" if token else "unset",
            )
            resolver = RefResolver(
                timeout_seconds=self._options.timeout_seconds,
                offline=self._options.offline,
                host=resolved_host,
                token=token,
            )
            self._host_resolvers[key] = resolver
            return resolver

    def _resolve_token_for_host(self, host: str, *, org: str | None = None) -> str | None:
        """Resolve an auth token for *host* via ``AuthResolver``.

        Returns ``None`` -- letting ``git`` fall back to ambient credentials
        -- when offline, when no token is configured for the host, or when
        ``AuthResolver`` raises.  Never raises.
        """
        if self._options.offline:
            return None
        try:
            from ..core.auth import AuthResolver  # lazy import

            resolver = self._auth_resolver
            if resolver is None:
                resolver = AuthResolver()
                self._auth_resolver = resolver
            ctx = resolver.resolve(host) if org is None else resolver.resolve(host, org=org)
            if ctx.token:
                logger.debug("Resolved token for host %s (source=%s)", host, ctx.source)
                return ctx.token
        except Exception:
            logger.debug("Could not resolve token for host %s", host, exc_info=True)
        return None

    def _ensure_auth(self) -> None:
        """Lazily resolve host classification and GitHub token."""
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
        output_path = self._project_root / yml.claude.output
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

    # -- auth + metadata prefetch -------------------------------------------

    def _resolve_github_token(self) -> str | None:
        """Resolve a GitHub token using ``AuthResolver``."""
        try:
            from ..core.auth import AuthResolver  # lazy import

            resolver = self._auth_resolver
            if resolver is None:
                resolver = AuthResolver()
                self._auth_resolver = resolver
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
        """Fetch ``description``/``version`` metadata for resolved packages.

        Returns a mapping of ``{package_name: {"description": ..., "version": ...}}``
        for successful fetches.  Both local-path and remote packages are
        read from each package's own ``apm.yml`` so the output mapper can
        apply one fallback rule regardless of source kind.

        Local reads always run (filesystem only).  Remote fetches are
        skipped when ``--offline`` is set.  A GitHub token is resolved
        once before spawning worker threads and stored on
        ``self._github_token`` for the workers to read.
        """
        results: dict[str, dict[str, str]] = {}

        # Local-path packages: read each apm.yml directly from disk.
        # Cheap and serial -- no network, no thread pool needed.
        for pkg in resolved:
            if pkg.source_repo:
                continue
            meta = self._fetch_local_metadata(pkg)
            if meta:
                results[pkg.name] = meta

        if self._options.offline:
            return results

        remote = [pkg for pkg in resolved if pkg.source_repo]
        if not remote:
            return results

        # Resolve token once -- threads read self._github_token (immutable).
        self._ensure_auth()

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
        """Produce an Anthropic-compliant marketplace.json dict."""
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

        old_plugins = _extract_plugin_shas(old_json)
        new_plugins = _extract_plugin_shas(new_json)

        unchanged = updated = added = removed = 0
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
        """Full pipeline: load -> resolve -> compose -> write."""
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

        if self._resolver is not None:
            self._resolver.close()
        with self._host_resolvers_lock:
            for host_resolver in self._host_resolvers.values():
                try:
                    host_resolver.close()
                except Exception:  # pragma: no cover
                    logger.debug("Failed to close per-host RefResolver", exc_info=True)
            self._host_resolvers.clear()

        return BuildReport(outputs=report.outputs)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _extract_plugin_shas(doc: dict[str, Any]) -> dict[str, str]:
    """Return {plugin_name: sha_or_path} from a marketplace.json document."""
    plugins: dict[str, str] = {}
    for p in doc.get("plugins", []):
        name = p.get("name", "")
        sha = ""
        src = p.get("source", {})
        if isinstance(src, dict):
            sha = src.get("sha") or src.get("commit", "")
        elif isinstance(src, str):
            sha = src
        plugins[name] = sha
    return plugins
