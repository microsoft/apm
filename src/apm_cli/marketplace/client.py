"""Fetch, parse, and cache marketplace.json from GitHub repositories.

Uses ``AuthResolver.try_with_fallback(unauth_first=False)`` for auth-first
access so private marketplace repos are fetched with credentials when available.
When ``PROXY_REGISTRY_URL`` is set, fetches are routed through the registry
proxy (Artifactory Archive Entry Download) before falling back to the
GitHub Contents API.  When ``PROXY_REGISTRY_ONLY=1``, the GitHub fallback
is blocked entirely.
Cache lives at ``~/.apm/cache/marketplace/`` with a 1-hour TTL.
"""

import json
import logging
import os
import time
from typing import Dict, List, Optional  # noqa: F401, UP035

import requests

from .errors import MarketplaceFetchError
from .models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
    parse_marketplace_json,
)
from .registry import get_registered_marketplaces

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 3600  # 1 hour
_CACHE_DIR_NAME = os.path.join("cache", "marketplace")

# Candidate locations for marketplace.json in a repository (priority order)
_MARKETPLACE_PATHS = [
    "marketplace.json",
    ".github/plugin/marketplace.json",
    ".claude-plugin/marketplace.json",
]


def _cache_dir() -> str:
    """Return the cache directory, creating it if needed."""
    from ..config import CONFIG_DIR

    d = os.path.join(CONFIG_DIR, _CACHE_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def _sanitize_cache_name(name: str) -> str:
    """Sanitize marketplace name for safe use in file paths."""
    import re

    from ..utils.path_security import PathTraversalError, validate_path_segments

    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    # Prevent path traversal even after sanitization
    safe = safe.strip(".").strip("_") or "unnamed"
    # Defense-in-depth: validate with centralized path security
    try:
        validate_path_segments(safe, context="cache name")
    except PathTraversalError:
        safe = "unnamed"
    return safe


def _cache_key(source: MarketplaceSource) -> str:
    """Cache key that includes host to avoid collisions across hosts."""
    normalized_host = source.host.lower()
    if normalized_host == "github.com":
        return source.name
    return f"{_sanitize_cache_name(normalized_host)}__{source.name}"


def _cache_data_path(name: str) -> str:
    return os.path.join(_cache_dir(), f"{_sanitize_cache_name(name)}.json")


def _cache_meta_path(name: str) -> str:
    return os.path.join(_cache_dir(), f"{_sanitize_cache_name(name)}.meta.json")


def _read_cache(name: str) -> dict | None:
    """Read cached marketplace data if valid (not expired)."""
    data_path = _cache_data_path(name)
    meta_path = _cache_meta_path(name)
    if not os.path.exists(data_path) or not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        fetched_at = meta.get("fetched_at", 0)
        ttl = meta.get("ttl_seconds", _CACHE_TTL_SECONDS)
        if time.time() - fetched_at > ttl:
            return None  # Expired
        with open(data_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, KeyError) as exc:
        logger.debug("Cache read failed for '%s': %s", name, exc)
        return None


def _read_stale_cache(name: str) -> dict | None:
    """Read cached data even if expired (stale-while-revalidate)."""
    data_path = _cache_data_path(name)
    if not os.path.exists(data_path):
        return None
    try:
        with open(data_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(name: str, data: dict) -> None:
    """Write marketplace data and metadata to cache."""
    data_path = _cache_data_path(name)
    meta_path = _cache_meta_path(name)
    try:
        with open(data_path, "w") as f:
            json.dump(data, f, indent=2)
        with open(meta_path, "w") as f:
            json.dump(
                {"fetched_at": time.time(), "ttl_seconds": _CACHE_TTL_SECONDS},
                f,
            )
    except OSError as exc:
        logger.debug("Cache write failed for '%s': %s", name, exc)


def _clear_cache(name: str) -> None:
    """Remove cached data for a marketplace."""
    for path in (_cache_data_path(name), _cache_meta_path(name)):
        try:  # noqa: SIM105
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Network fetch
# ---------------------------------------------------------------------------


def _try_proxy_fetch(
    source: MarketplaceSource,
    file_path: str,
) -> dict | None:
    """Try to fetch marketplace JSON via the registry proxy.

    Returns parsed JSON dict on success, ``None`` when no proxy is
    configured or the entry download fails.
    """
    from ..deps.registry_proxy import RegistryConfig

    cfg = RegistryConfig.from_env()
    if cfg is None:
        return None

    from ..deps.artifactory_entry import fetch_entry_from_archive

    content = fetch_entry_from_archive(
        host=cfg.host,
        prefix=cfg.prefix,
        owner=source.owner,
        repo=source.repo,
        file_path=file_path,
        ref=source.branch,
        scheme=cfg.scheme,
        headers=cfg.get_headers(),
    )
    if content is None:
        return None

    try:
        return json.loads(content)
    except (json.JSONDecodeError, ValueError):
        logger.debug(
            "Proxy returned non-JSON for %s/%s %s",
            source.owner,
            source.repo,
            file_path,
        )
        return None


def _github_contents_url(source: MarketplaceSource, file_path: str) -> str:
    """Build the GitHub Contents API URL for a file."""
    from ..core.auth import AuthResolver

    host_info = AuthResolver.classify_host(source.host)
    api_base = host_info.api_base
    return f"{api_base}/repos/{source.owner}/{source.repo}/contents/{file_path}?ref={source.branch}"


def _fetch_file(
    source: MarketplaceSource,
    file_path: str,
    auth_resolver: object | None = None,
) -> dict | None:
    """Fetch a JSON file from a GitHub repo.

    When ``PROXY_REGISTRY_URL`` is set, tries the registry proxy first via
    Artifactory Archive Entry Download.  Falls back to the GitHub Contents
    API unless ``PROXY_REGISTRY_ONLY=1`` blocks direct access.

    Returns parsed JSON or ``None`` if the file does not exist (404).
    Raises ``MarketplaceFetchError`` on unexpected failures.
    """
    # Proxy-first: try Artifactory Archive Entry Download
    proxy_result = _try_proxy_fetch(source, file_path)
    if proxy_result is not None:
        return proxy_result

    # When registry-only mode is active, block direct GitHub API access
    from ..deps.registry_proxy import RegistryConfig

    cfg = RegistryConfig.from_env()
    if cfg is not None and cfg.enforce_only:
        logger.debug(
            "PROXY_REGISTRY_ONLY blocks direct GitHub fetch for %s/%s %s",
            source.owner,
            source.repo,
            file_path,
        )
        return None

    # Fallback: GitHub Contents API
    url = _github_contents_url(source, file_path)

    def _do_fetch(token, _git_env):
        headers = {
            "Accept": "application/vnd.github.v3.raw",
            "User-Agent": "apm-cli",
        }
        if token:
            headers["Authorization"] = f"token {token}"
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    if auth_resolver is None:
        from ..core.auth import AuthResolver

        auth_resolver = AuthResolver()

    try:
        return auth_resolver.try_with_fallback(
            source.host,
            _do_fetch,
            org=source.owner,
            # Auth-first: marketplace repos may be private/org-scoped and the
            # GitHub API returns 404 (not 403) for unauthenticated requests to
            # private repos.  Because _do_fetch returns None on 404 (no
            # exception), unauth_first would swallow the error instead of
            # retrying with a token.
            unauth_first=False,
        )
    except Exception as exc:
        logger.debug("Fetch failed for '%s'", source.name, exc_info=True)
        raise MarketplaceFetchError(source.name, str(exc)) from exc


def _auto_detect_path(
    source: MarketplaceSource,
    auth_resolver: object | None = None,
) -> str | None:
    """Probe candidate locations and return the first that exists.

    Returns ``None`` if no location contains a marketplace.json.
    Raises ``MarketplaceFetchError`` on non-404 failures (auth errors, etc.).
    """
    for candidate in _MARKETPLACE_PATHS:
        data = _fetch_file(source, candidate, auth_resolver=auth_resolver)
        if data is not None:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_marketplace(
    source: MarketplaceSource,
    *,
    force_refresh: bool = False,
    auth_resolver: object | None = None,
) -> MarketplaceManifest:
    """Fetch and parse a marketplace manifest.

    Uses cache when available (1h TTL). Falls back to stale cache on
    network errors.

    Args:
        source: Marketplace source to fetch.
        force_refresh: Skip cache and re-fetch from network.
        auth_resolver: Optional ``AuthResolver`` instance (created if None).

    Returns:
        MarketplaceManifest: Parsed manifest.

    Raises:
        MarketplaceFetchError: If fetch fails and no cache is available.
    """
    cache_name = _cache_key(source)

    # Try fresh cache first
    if not force_refresh:
        cached = _read_cache(cache_name)
        if cached is not None:
            logger.debug("Using cached marketplace data for '%s'", source.name)
            return parse_marketplace_json(cached, source.name)

    # Fetch from network
    try:
        data = _fetch_file(source, source.path, auth_resolver=auth_resolver)
        if data is None:
            raise MarketplaceFetchError(
                source.name,
                f"marketplace.json not found at '{source.path}' in {source.owner}/{source.repo}",
            )
        _write_cache(cache_name, data)
        return parse_marketplace_json(data, source.name)
    except MarketplaceFetchError:
        # Stale-while-revalidate: serve expired cache on network error
        stale = _read_stale_cache(cache_name)
        if stale is not None:
            logger.warning("Network error fetching '%s'; using stale cache", source.name)
            return parse_marketplace_json(stale, source.name)
        raise


def fetch_or_cache(
    source: MarketplaceSource,
    *,
    auth_resolver: object | None = None,
) -> MarketplaceManifest:
    """Convenience wrapper -- same as ``fetch_marketplace`` with defaults."""
    return fetch_marketplace(source, auth_resolver=auth_resolver)


def search_marketplace(
    query: str,
    source: MarketplaceSource,
    *,
    auth_resolver: object | None = None,
) -> list[MarketplacePlugin]:
    """Search a single marketplace for plugins matching *query*."""
    manifest = fetch_marketplace(source, auth_resolver=auth_resolver)
    return manifest.search(query)


def search_all_marketplaces(
    query: str,
    *,
    auth_resolver: object | None = None,
) -> list[MarketplacePlugin]:
    """Search across all registered marketplaces.

    Returns plugins matching the query, annotated with their source marketplace.
    """
    results: list[MarketplacePlugin] = []
    for source in get_registered_marketplaces():
        try:
            manifest = fetch_marketplace(source, auth_resolver=auth_resolver)
            results.extend(manifest.search(query))
        except MarketplaceFetchError as exc:
            logger.warning("Skipping marketplace '%s': %s", source.name, exc)
    return results


def clear_marketplace_cache(
    name: str | None = None,
    host: str = "github.com",
) -> int:
    """Clear cached data for one or all marketplaces.

    Returns the number of caches cleared.
    """
    if name:
        # Build a minimal source to derive the cache key
        _src = MarketplaceSource(name=name, owner="", repo="", host=host)
        _clear_cache(_cache_key(_src))
        return 1
    count = 0
    for source in get_registered_marketplaces():
        _clear_cache(_cache_key(source))
        count += 1
    return count
