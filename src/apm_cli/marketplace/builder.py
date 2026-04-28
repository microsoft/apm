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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .errors import (
    BuildError,
    HeadNotAllowedError,
    NoMatchingVersionError,
    OfflineMissError,
    RefNotFoundError,
)
from ._io import atomic_write
from .ref_resolver import RefResolver, RemoteRef
from .semver import SemVer, parse_semver, satisfies_range
from .tag_pattern import build_tag_regex, render_tag
from ..utils.path_security import ensure_path_within
from .yml_schema import MarketplaceYml, PackageEntry, load_marketplace_yml

logger = logging.getLogger(__name__)

__all__ = [
    "ResolvedPackage",
    "ResolveResult",
    "BuildReport",
    "BuildOptions",
    "MarketplaceBuilder",
]

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedPackage:
    """A package entry after ref resolution."""

    name: str
    source_repo: str          # "owner/repo" only
    subdir: Optional[str]     # APM-only (used to compose the output ``source`` object)
    ref: str                  # resolved tag name, e.g. "v1.2.0"
    sha: str                  # 40-char git SHA
    requested_version: Optional[str]  # original APM-only range (for diagnostics)
    tags: Tuple[str, ...]
    is_prerelease: bool       # True if the resolved ref was a prerelease semver


@dataclass(frozen=True)
class ResolveResult:
    """Result of resolving package refs in a marketplace build."""

    entries: Tuple[ResolvedPackage, ...]
    errors: Tuple[Tuple[str, str], ...]  # (package name, error message) pairs

    @property
    def ok(self) -> bool:
        """True when every package resolved without error."""
        return len(self.errors) == 0


@dataclass(frozen=True)
class BuildReport:
    """Summary of a build run."""

    resolved: Tuple[ResolvedPackage, ...]
    errors: Tuple[Tuple[str, str], ...]  # (package name, error message) pairs
    warnings: Tuple[str, ...]  # non-fatal diagnostic messages
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
    output_override: Optional[Path] = None
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
        options: Optional[BuildOptions] = None,
        auth_resolver: Optional[object] = None,
    ) -> None:
        self._yml_path = marketplace_yml_path
        self._options = options or BuildOptions()
        self._yml: Optional[MarketplaceYml] = None
        self._resolver: Optional[RefResolver] = None
        self._auth_resolver = auth_resolver
        # Resolved once per build, used by worker threads (read-only).
        self._github_token: Optional[str] = None

    # -- lazy loaders -------------------------------------------------------

    def _load_yml(self) -> MarketplaceYml:
        if self._yml is None:
            self._yml = load_marketplace_yml(self._yml_path)
        return self._yml

    def _get_resolver(self) -> RefResolver:
        if self._resolver is None:
            self._resolver = RefResolver(
                timeout_seconds=self._options.timeout_seconds,
                offline=self._options.offline,
            )
        return self._resolver

    # -- output path --------------------------------------------------------

    def _output_path(self) -> Path:
        if self._options.output_override is not None:
            return self._options.output_override
        yml = self._load_yml()
        output_path = self._yml_path.parent / yml.output
        # Containment guard -- reject output paths that escape the project root.
        project_root = self._yml_path.parent
        ensure_path_within(output_path, project_root)
        return output_path

    # -- single-entry resolution --------------------------------------------

    def _resolve_entry(self, entry: PackageEntry) -> ResolvedPackage:
        """Resolve a single package entry to a concrete tag + SHA."""
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
        assert ref_text is not None

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
        assert version_range is not None

        # Determine tag pattern: entry > build > default
        pattern = entry.tag_pattern or yml.build.tag_pattern

        tag_rx = build_tag_regex(pattern)
        refs = resolver.list_remote_refs(owner_repo)

        # Filter tags matching the pattern and extract versions
        candidates: list[tuple[SemVer, str, str]] = []  # (semver, tag_name, sha)
        for remote_ref in refs:
            if not remote_ref.name.startswith("refs/tags/"):
                continue
            tag_name = remote_ref.name[len("refs/tags/"):]
            m = tag_rx.match(tag_name)
            if not m:
                continue
            version_str = m.group("version")
            sv = parse_semver(version_str)
            if sv is None:
                continue

            # Prerelease filter
            include_pre = (
                entry.include_prerelease or self._options.include_prerelease
            )
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

        results: Dict[int, ResolvedPackage] = {}
        errors: List[Tuple[str, str]] = []

        with ThreadPoolExecutor(
            max_workers=min(self._options.concurrency, len(entries))
        ) as pool:
            future_to_index = {
                pool.submit(self._resolve_entry, entry): idx
                for idx, entry in enumerate(entries)
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
                except Exception as exc:  # noqa: BLE001 -- thread-pool catch-all wraps to BuildError
                    logger.debug("Unexpected error resolving '%s'", entry.name, exc_info=True)
                    if self._options.continue_on_error:
                        errors.append((entry.name, str(exc)))
                    else:
                        raise BuildError(
                            f"Unexpected error resolving '{entry.name}': {exc}",
                            package=entry.name,
                        ) from exc

        # Return in yml order
        ordered: List[ResolvedPackage] = []
        for idx in range(len(entries)):
            if idx in results:
                ordered.append(results[idx])
        return ResolveResult(entries=tuple(ordered), errors=tuple(errors))

    # -- remote description fetcher -----------------------------------------

    def _fetch_remote_metadata(self, pkg: ResolvedPackage) -> Optional[Dict[str, str]]:
        """Best-effort: fetch ``description`` and ``version`` from the
        package's remote ``apm.yml``.

        Returns a dict with ``description`` and/or ``version`` keys, or
        ``None`` on any error.  This is purely cosmetic enrichment --
        failures are silently logged at debug level and never propagate.

        When a GitHub token is available (via ``self._github_token``), it
        is included as an ``Authorization`` header so private repos can be
        accessed.
        """
        try:
            path_prefix = f"{pkg.subdir}/" if pkg.subdir else ""
            url = (
                f"https://raw.githubusercontent.com/"
                f"{pkg.source_repo}/{pkg.sha}/{path_prefix}apm.yml"
            )
            req = urllib.request.Request(url)
            if self._github_token:
                req.add_header("Authorization", f"token {self._github_token}")
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8")
            data = yaml.safe_load(raw)
            if not isinstance(data, dict):
                return None
            result: Dict[str, str] = {}
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
        except Exception:  # noqa: BLE001 -- best-effort enrichment
            logger.debug(
                "Could not fetch remote metadata for %s",
                pkg.name,
                exc_info=True,
            )
        return None

    def _resolve_github_token(self) -> Optional[str]:
        """Resolve a GitHub token using ``AuthResolver``.

        Called once before concurrent fetches.  Returns the token string
        or ``None`` if no credentials are available.  Never raises --
        auth failures are logged at debug and silently ignored.
        """
        try:
            resolver = self._auth_resolver
            if resolver is None:
                from ..core.auth import AuthResolver  # lazy import

                resolver = AuthResolver()
                self._auth_resolver = resolver
            ctx = resolver.resolve("github.com")  # type: ignore[union-attr]
            if ctx.token:
                logger.debug("Resolved GitHub token for metadata fetch (source=%s)", ctx.source)
                return ctx.token
        except Exception:  # noqa: BLE001 -- best-effort
            logger.debug("Could not resolve GitHub token for metadata fetch", exc_info=True)
        return None

    def _prefetch_metadata(
        self, resolved: List[ResolvedPackage]
    ) -> Dict[str, Dict[str, str]]:
        """Concurrently fetch remote metadata for all packages.

        Returns a mapping of ``{package_name: {"description": ..., "version": ...}}``
        for successful fetches.  Skipped entirely when ``--offline`` is set.

        A GitHub token is resolved once before spawning worker threads and
        stored on ``self._github_token`` for the workers to read.
        """
        if self._options.offline:
            return {}

        if not resolved:
            return {}

        # Resolve token once -- threads read self._github_token (immutable).
        self._github_token = self._resolve_github_token()

        results: Dict[str, Dict[str, str]] = {}
        workers = min(self._options.concurrency, len(resolved))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_name = {
                pool.submit(self._fetch_remote_metadata, pkg): pkg.name
                for pkg in resolved
            }
            for future in as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    meta = future.result()
                    if meta:
                        results[name] = meta
                except Exception:  # noqa: BLE001 -- best-effort
                    pass
        return results

    # -- composition --------------------------------------------------------

    def compose_marketplace_json(
        self, resolved: List[ResolvedPackage]
    ) -> Dict[str, Any]:
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

        doc: Dict[str, Any] = OrderedDict()
        doc["name"] = yml.name
        doc["description"] = yml.description
        doc["version"] = yml.version

        # Owner -- omit empty optional sub-fields
        owner_dict: Dict[str, Any] = OrderedDict()
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
        plugins: List[Dict[str, Any]] = []
        for pkg in resolved:
            plugin: Dict[str, Any] = OrderedDict()
            plugin["name"] = pkg.name
            meta = remote_metadata.get(pkg.name, {})
            if meta.get("description"):
                plugin["description"] = meta["description"]
            if meta.get("version"):
                plugin["version"] = meta["version"]
            plugin["tags"] = list(pkg.tags)

            source: Dict[str, Any] = OrderedDict()
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
        seen_names: Dict[str, str] = {}
        build_warnings: list[str] = []
        for p in plugins:
            pname = p["name"]
            src = p.get("source", {})
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
        old_json: Optional[Dict[str, Any]],
        new_json: Dict[str, Any],
    ) -> Tuple[int, int, int, int]:
        """Compare old vs new marketplace.json and classify each plugin.

        Returns (unchanged, added, updated, removed) counts.
        """
        if old_json is None:
            return (0, len(new_json.get("plugins", [])), 0, 0)

        old_plugins: Dict[str, str] = {}
        for p in old_json.get("plugins", []):
            name = p.get("name", "")
            sha = ""
            src = p.get("source", {})
            if isinstance(src, dict):
                sha = src.get("commit", "")
            old_plugins[name] = sha

        new_plugins: Dict[str, str] = {}
        for p in new_json.get("plugins", []):
            name = p.get("name", "")
            sha = ""
            src = p.get("source", {})
            if isinstance(src, dict):
                sha = src.get("commit", "")
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
    def _serialize_json(data: Dict[str, Any]) -> str:
        """Serialize to JSON with 2-space indent, LF endings, trailing newline."""
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write *content* to *path* atomically via tmp + rename."""
        atomic_write(path, content)

    def _load_existing_json(self, path: Path) -> Optional[Dict[str, Any]]:
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
        return refname[len("refs/tags/"):]
    if refname.startswith("refs/heads/"):
        return refname[len("refs/heads/"):]
    return refname
