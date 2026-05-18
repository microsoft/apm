"""Fetch, parse, and cache marketplace.json from Git hosting repositories.

Uses ``AuthResolver.try_with_fallback(unauth_first=False)`` for auth-first
access so private marketplace repos are fetched with credentials when available.
When ``PROXY_REGISTRY_URL`` is set, fetches are routed through the registry
proxy (Artifactory Archive Entry Download) before falling back to the
host API: GitHub Contents API for GitHub/GHES, or GitLab REST v4 file raw
when the host classifies as GitLab (``kind='gitlab'``).  When ``PROXY_REGISTRY_ONLY=1``, the
direct host API fallback is blocked entirely.
Cache lives at ``~/.apm/cache/marketplace/`` with a 1-hour TTL.
"""

import contextlib
import json
import logging
import os
import time
from urllib.parse import quote

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

from ._marketplace_cache import (  # noqa: E402
    _cache_key,
    _cache_meta_path,
    _clear_cache,
    _read_cache,
    _read_stale_cache,
    _write_cache,
)

# Candidate locations for marketplace.json in a repository (priority order)
_MARKETPLACE_PATHS = [
    "marketplace.json",
    ".github/plugin/marketplace.json",
    ".claude-plugin/marketplace.json",
]


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

    from ..deps.artifactory_entry import _ArchiveCoords, fetch_entry_from_archive

    content = fetch_entry_from_archive(
        _ArchiveCoords(host=cfg.host, prefix=cfg.prefix, owner=source.owner, repo=source.repo),
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


def _github_contents_url(source: MarketplaceSource, file_path: str, host_info) -> str:
    """Build the GitHub Contents API URL for a file (GitHub / GHES / generic)."""
    api_base = host_info.api_base.rstrip("/")
    return f"{api_base}/repos/{source.owner}/{source.repo}/contents/{file_path}?ref={source.branch}"


def _gitlab_file_raw_url(source: MarketplaceSource, host_info, file_path: str) -> str:
    """Build the GitLab REST v4 repository file raw URL."""
    project_path = f"{source.owner}/{source.repo}"
    encoded_project = quote(project_path, safe="")
    encoded_file = quote(file_path, safe="")
    encoded_ref = quote(source.branch, safe="")
    api_base = host_info.api_base.rstrip("/")
    return (
        f"{api_base}/projects/{encoded_project}/repository/files/"
        f"{encoded_file}/raw?ref={encoded_ref}"
    )


def _gitlab_fetch_handler(
    url: str,
) -> object:
    """Return a GitLab file fetch closure for AuthResolver."""
    from ..core.auth import AuthResolver

    def _do_fetch(token, _git_env):
        headers = {"User-Agent": "apm-cli"}
        headers.update(AuthResolver.gitlab_rest_headers(token))
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        try:
            return json.loads(resp.text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"Invalid JSON in marketplace file: {exc}") from exc

    return _do_fetch


def _github_fetch_handler(
    url: str,
) -> object:
    """Return a GitHub Contents API fetch closure for AuthResolver."""

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

    return _do_fetch


def _build_fetch_handler(
    source: MarketplaceSource,
    file_path: str,
    host_info: object,
) -> object:
    """Build the appropriate fetch handler based on host kind."""
    if host_info.kind == "gitlab":
        url = _gitlab_file_raw_url(source, host_info, file_path)
        return _gitlab_fetch_handler(url)
    if host_info.kind in ("github", "ghe_cloud", "ghes"):
        url = _github_contents_url(source, file_path, host_info)
        return _github_fetch_handler(url)
    raise MarketplaceFetchError(
        source.name,
        f"Host {source.host!r} is not a supported marketplace source. "
        "Only GitHub, GitHub Enterprise Cloud (*.ghe.com), GHES "
        "(GITHUB_HOST), and GitLab are supported. Refusing to fetch to "
        "avoid forwarding GitHub credentials to a non-GitHub host.",
    )


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

    # Fallback: host-native file API (GitLab v4 raw vs GitHub Contents)
    from ..core.auth import AuthResolver

    host_info = AuthResolver.classify_host(source.host)
    fetch_handler = _build_fetch_handler(source, file_path, host_info)

    if auth_resolver is None:
        auth_resolver = AuthResolver()

    try:
        result = auth_resolver.try_with_fallback(
            source.host,
            fetch_handler,
            org=source.owner,
            path=f"{source.owner}/{source.repo}",
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

    # GitLab returns 404 for unauthenticated access to many private projects
    # (indistinguishable from a missing file). ``_do_fetch`` maps 404 to
    # ``None`` without raising, so ``unauth_first`` would skip the PAT.
    if result is None and host_info.kind == "gitlab":
        try:
            result = auth_resolver.try_with_fallback(
                source.host,
                _do_fetch,
                org=source.owner,
                unauth_first=False,
            )
        except Exception as exc:
            raise MarketplaceFetchError(source.name, str(exc)) from exc

    return result


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
