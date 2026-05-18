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

import logging
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

from .class_ import ResolvedPackage

logger = logging.getLogger(__name__)
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def _build_metadata_url(
    self, pkg: ResolvedPackage, file_path: str
) -> tuple[str, dict[str, str]] | None:
    """Build metadata fetch URL and headers, or None if unsupported."""
    host_kind = self._host_info.kind if self._host_info else "github"

    if host_kind not in ("github", "ghe_cloud", "ghes"):
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

    headers: dict[str, str] = {}
    if self._github_token:
        headers["Authorization"] = f"token {self._github_token}"

    if self._host == "github.com":
        # github.com -- use fast raw.githubusercontent.com CDN
        url = f"https://raw.githubusercontent.com/{pkg.source_repo}/{pkg.sha}/{file_path}"
    else:
        # GHES / GHE Cloud -- use REST API
        api_base = (
            self._host_info.api_base if self._host_info else None
        ) or f"https://{self._host}/api/v3"
        url = f"{api_base}/repos/{pkg.source_repo}/contents/{file_path}?ref={pkg.sha}"
        headers["Accept"] = "application/vnd.github.raw"

    return (url, headers)


def _parse_metadata_yaml(data: object) -> dict[str, str]:
    """Extract description and version from loaded YAML, if present."""
    if not isinstance(data, dict):
        return {}
    result: dict[str, str] = {}
    desc = data.get("description")
    if isinstance(desc, str) and desc:
        result["description"] = desc
    ver = data.get("version")
    if ver is not None:
        ver_str = str(ver).strip()
        if ver_str:
            result["version"] = ver_str
    return result


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

        url_info = _build_metadata_url(self, pkg, file_path)
        if url_info is None:
            return None
        url, headers = url_info

        req = urllib.request.Request(url)  # noqa: S310
        for key, value in headers.items():
            req.add_header(key, value)

        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
        data = yaml.safe_load(raw)
        result = _parse_metadata_yaml(data)
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
        from ...core.auth import AuthResolver  # lazy import

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
        future_to_name = {pool.submit(self._fetch_remote_metadata, pkg): pkg.name for pkg in remote}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                meta = future.result()
                if meta:
                    results[name] = meta
            except Exception:
                pass
    return results
