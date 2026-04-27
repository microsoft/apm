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
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field  # noqa: F401
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
from .ref_resolver import RefResolver, RemoteRef  # noqa: F401
from .semver import SemVer, parse_semver, satisfies_range
from .tag_pattern import build_tag_regex, render_tag  # noqa: F401
from .yml_schema import MarketplaceYml, PackageEntry, load_marketplace_yml

logger = logging.getLogger(__name__)

__all__ = [
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
class BuildReport:
    """Summary of a build run."""

    resolved: tuple[ResolvedPackage, ...]
    errors: tuple[tuple[str, str], ...]  # (package name, error message) pairs
    warnings: tuple[str, ...]  # non-fatal diagnostic messages
    unchanged_count: int
    added_count: int
    updated_count: int
    removed_count: int
    output_path: Path
    dry_run: bool


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

# 40-char hex SHA pattern
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


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
        yml = self._load_yml()

        # Pre-fetch metadata (description + version) from remote apm.yml
        remote_metadata = self._prefetch_metadata(resolved)

        # Build a name -> entry map so we can reach back for local-package
        # description / homepage that came from the yml itself.
        entry_by_name: dict[str, PackageEntry] = {e.name: e for e in yml.packages}

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
        for pkg in resolved:
            plugin: dict[str, Any] = OrderedDict()
            plugin["name"] = pkg.name

            entry = entry_by_name.get(pkg.name)
            is_local = entry is not None and entry.is_local

            if is_local:
                # Local packages: description/version come from the yml
                # entry itself (not a remote fetch).
                if entry.description:
                    plugin["description"] = entry.description
                if entry.version:
                    plugin["version"] = entry.version
            else:
                meta = remote_metadata.get(pkg.name, {})
                if meta.get("description"):
                    plugin["description"] = meta["description"]
                if meta.get("version"):
                    plugin["version"] = meta["version"]

            if pkg.tags:
                plugin["tags"] = list(pkg.tags)

            if is_local:
                # Anthropic spec: local sources are emitted as plain
                # strings (the path), not the {type, repository, ref,
                # commit} object used for remote sources.
                plugin["source"] = entry.source
                if entry.homepage:
                    plugin["homepage"] = entry.homepage
            else:
                source: dict[str, Any] = OrderedDict()
                source["type"] = "github"
                source["repository"] = pkg.source_repo
                if pkg.subdir:
                    source["path"] = pkg.subdir
                source["ref"] = pkg.ref
                source["commit"] = pkg.sha
                plugin["source"] = source

            plugins.append(plugin)

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
                src_label = src.get("path") or src.get("repository", "?")
            if pname in seen_names:
                build_warnings.append(
                    f"Duplicate package name '{pname}': "
                    f"'{seen_names[pname]}' and '{src_label}'. "
                    f"Consumers will see duplicate entries in browse."
                )
            else:
                seen_names[pname] = src_label
        self._compose_warnings = tuple(build_warnings)

        doc["plugins"] = plugins
        return doc

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
                sha = src.get("commit", "")
            elif isinstance(src, str):
                sha = src  # local-path packages: use the path string itself
            old_plugins[name] = sha

        new_plugins: dict[str, str] = {}
        for p in new_json.get("plugins", []):
            name = p.get("name", "")
            sha = ""
            src = p.get("source", {})
            if isinstance(src, dict):
                sha = src.get("commit", "")
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
        resolved = list(result.entries)
        errors = result.errors

        new_json = self.compose_marketplace_json(resolved)
        build_warnings = getattr(self, "_compose_warnings", ())
        output_path = self._output_path()

        # Load existing for diff
        old_json = self._load_existing_json(output_path)
        unchanged, added, updated, removed = self._compute_diff(old_json, new_json)

        # Write (unless dry-run)
        if not self._options.dry_run:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            content = self._serialize_json(new_json)
            self._atomic_write(output_path, content)

        # Cleanup resolver
        if self._resolver is not None:
            self._resolver.close()

        return BuildReport(
            resolved=tuple(resolved),
            errors=tuple(errors),
            warnings=tuple(build_warnings),
            unchanged_count=unchanged,
            added_count=added,
            updated_count=updated,
            removed_count=removed,
            output_path=output_path,
            dry_run=self._options.dry_run,
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
