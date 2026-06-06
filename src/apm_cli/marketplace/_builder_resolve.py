"""Resolve mixin for MarketplaceBuilder.

Provides ``_BuilderResolveMixin`` which is mixed into ``MarketplaceBuilder``
in ``builder.py``.  Keeping these methods separate reduces the line count
of ``builder.py`` without splitting the public class interface.

urllib Rule B
-------------
``_fetch_remote_metadata`` uses ``urllib.request`` but does NOT import it
at module scope here.  Instead it performs a late import::

    from apm_cli.marketplace import builder as _b
    ... _b.urllib.request.urlopen(req, timeout=5) ...

This keeps the patch target ``apm_cli.marketplace.builder.urllib`` valid
for the 20+ test-suite ``patch()`` calls that mock ``urlopen``.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

import yaml

from ._builder_reports import BuildOptions, ResolvedPackage, ResolveResult
from ._shared import iter_semver_tags
from .errors import (
    BuildError,
    HeadNotAllowedError,
    NoMatchingVersionError,
    RefNotFoundError,
)
from .ref_resolver import RefResolver
from .semver import SemVer, parse_semver, satisfies_range
from .tag_pattern import build_tag_regex
from .yml_schema import MarketplaceYml

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# 40-char hex SHA pattern (also used in builder.py -- defined here because
# _resolve_explicit_ref lives here).
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def _strip_ref_prefix(refname: str) -> str:
    """Strip ``refs/tags/`` or ``refs/heads/`` prefix."""
    if refname.startswith("refs/tags/"):
        return refname[len("refs/tags/") :]
    if refname.startswith("refs/heads/"):
        return refname[len("refs/heads/") :]
    return refname


class _BuilderResolveMixin:
    """Resolution methods factored out of MarketplaceBuilder.

    All methods access ``self`` attributes set by ``MarketplaceBuilder.__init__``.
    This class should never be instantiated directly.
    """

    # -- single-entry resolution --------------------------------------------

    def _resolve_entry(self, entry: Any) -> ResolvedPackage:
        """Resolve a single package entry to a concrete tag + SHA."""
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
        yml = self._load_yml()  # type: ignore[attr-defined]
        resolver = self._get_resolver_for_host(entry.host)  # type: ignore[attr-defined]
        owner_repo = entry.source

        if entry.ref is not None:
            return self._resolve_explicit_ref(entry, resolver, owner_repo)
        return self._resolve_version_range(entry, resolver, owner_repo, yml)

    def _resolve_explicit_ref(
        self,
        entry: Any,
        resolver: RefResolver,
        owner_repo: str,
    ) -> ResolvedPackage:
        """Resolve an entry with an explicit ``ref:`` field."""
        ref_text = entry.ref
        assert ref_text is not None  # noqa: S101

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
                host=self._effective_host(entry.host),  # type: ignore[attr-defined]
            )

        refs = resolver.list_remote_refs(owner_repo)

        # Try as tag first
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
                    host=self._effective_host(entry.host),  # type: ignore[attr-defined]
                )

        # Try as full refname
        for remote_ref in refs:
            if remote_ref.name == ref_text:
                short = _strip_ref_prefix(remote_ref.name)
                is_branch = remote_ref.name.startswith("refs/heads/")
                if is_branch and not self._options.allow_head:  # type: ignore[attr-defined]
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
                    host=self._effective_host(entry.host),  # type: ignore[attr-defined]
                )

        # Try as branch name
        for remote_ref in refs:
            if remote_ref.name == f"refs/heads/{ref_text}":
                if not self._options.allow_head:  # type: ignore[attr-defined]
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
                    host=self._effective_host(entry.host),  # type: ignore[attr-defined]
                )

        if ref_text.upper() == "HEAD":
            if not self._options.allow_head:  # type: ignore[attr-defined]
                raise HeadNotAllowedError(entry.name, "HEAD")

        raise RefNotFoundError(entry.name, ref_text, owner_repo)

    def _resolve_version_range(
        self,
        entry: Any,
        resolver: RefResolver,
        owner_repo: str,
        yml: MarketplaceYml,
    ) -> ResolvedPackage:
        """Resolve an entry using its ``version:`` semver range."""
        version_range = entry.version
        assert version_range is not None  # noqa: S101

        pattern = entry.tag_pattern or yml.build.tag_pattern
        tag_rx = build_tag_regex(pattern)
        refs = resolver.list_remote_refs(owner_repo)

        candidates: list[tuple[SemVer, str, str]] = []
        for sv, tag_name, sha in iter_semver_tags(refs, tag_rx):
            include_pre = (
                entry.include_prerelease or self._options.include_prerelease  # type: ignore[attr-defined]
            )
            if sv.is_prerelease and not include_pre:
                continue
            if satisfies_range(sv, version_range):
                candidates.append((sv, tag_name, sha))

        if not candidates:
            raise NoMatchingVersionError(
                entry.name,
                version_range,
                detail=f"pattern='{pattern}', remote='{owner_repo}'",
            )

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
            host=self._effective_host(entry.host),  # type: ignore[attr-defined]
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
        yml = self._load_yml()  # type: ignore[attr-defined]
        entries = yml.packages
        if not entries:
            return ResolveResult(entries=(), errors=())

        results: dict[int, ResolvedPackage] = {}
        errors: list[tuple[str, str]] = []

        self._get_resolver()  # type: ignore[attr-defined]
        for entry in entries:
            if entry.host:
                self._get_resolver_for_host(entry.host)  # type: ignore[attr-defined]

        options: BuildOptions = self._options  # type: ignore[attr-defined]
        with ThreadPoolExecutor(max_workers=min(options.concurrency, len(entries))) as pool:
            future_to_index = {
                pool.submit(self._resolve_entry, entry): idx for idx, entry in enumerate(entries)
            }
            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                entry = entries[idx]
                try:
                    resolved = future.result(timeout=options.timeout_seconds)
                    results[idx] = resolved
                except BuildError as exc:
                    if options.continue_on_error:
                        errors.append((entry.name, str(exc)))
                    else:
                        raise
                except Exception as exc:
                    logger.debug("Unexpected error resolving '%s'", entry.name, exc_info=True)
                    if options.continue_on_error:
                        errors.append((entry.name, str(exc)))
                    else:
                        raise BuildError(
                            f"Unexpected error resolving '{entry.name}': {exc}",
                            package=entry.name,
                        ) from exc

        ordered: list[ResolvedPackage] = []
        for idx in range(len(entries)):
            if idx in results:
                ordered.append(results[idx])
        return ResolveResult(entries=tuple(ordered), errors=tuple(errors))

    # -- remote description fetcher -----------------------------------------

    def _fetch_remote_metadata(self, pkg: ResolvedPackage) -> dict[str, str] | None:
        """Best-effort: fetch ``description`` and ``version`` from the
        package's remote ``apm.yml``.

        urllib Rule B: ``urllib`` is accessed via ``_b.urllib`` (late import of
        ``builder`` module) so that test patches on
        ``apm_cli.marketplace.builder.urllib.request.urlopen`` remain effective.
        """
        try:
            path_prefix = f"{pkg.subdir}/" if pkg.subdir else ""
            file_path = f"{path_prefix}apm.yml"

            effective_host = pkg.host or self._host  # type: ignore[attr-defined]
            if pkg.host is None or pkg.host == self._host:  # type: ignore[attr-defined]
                host_info = self._host_info  # type: ignore[attr-defined]
                token = self._github_token  # type: ignore[attr-defined]
            else:
                from ..core.auth import AuthResolver  # lazy import

                try:
                    host_info = AuthResolver.classify_host(effective_host)
                except Exception:
                    host_info = None
                token = self._resolve_token_for_host(effective_host)  # type: ignore[attr-defined]

            host_kind = host_info.kind if host_info else "github"

            if host_kind not in ("github", "ghe_cloud", "ghes"):
                logger.debug(
                    "Skipping metadata fetch for %s (non-GitHub host: %s)",
                    pkg.name,
                    effective_host,
                )
                return None

            if host_kind == "ghe_cloud" and not token:
                logger.debug(
                    "Skipping metadata fetch for %s (GHE Cloud requires auth)",
                    pkg.name,
                )
                return None

            # Rule B: access urllib via builder module so patch target is preserved
            from apm_cli.marketplace import builder as _b

            if effective_host == "github.com":
                url = f"https://raw.githubusercontent.com/{pkg.source_repo}/{pkg.sha}/{file_path}"
                req = _b.urllib.request.Request(url)
                if token:
                    req.add_header("Authorization", f"token {token}")
            else:
                api_base = (
                    host_info.api_base if host_info else None
                ) or f"https://{effective_host}/api/v3"
                url = f"{api_base}/repos/{pkg.source_repo}/contents/{file_path}?ref={pkg.sha}"
                req = _b.urllib.request.Request(url)
                req.add_header("Accept", "application/vnd.github.raw")
                if token:
                    req.add_header("Authorization", f"token {token}")

            with _b.urllib.request.urlopen(req, timeout=5) as resp:
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
