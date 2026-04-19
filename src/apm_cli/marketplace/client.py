"""Fetch, parse, and cache marketplace indexes from GitHub repositories or
arbitrary HTTPS URLs (Agent Skills discovery endpoints).

Uses ``AuthResolver.try_with_fallback(unauth_first=False)`` for auth-first
access so private marketplace repos are fetched with credentials when available.
When ``PROXY_REGISTRY_URL`` is set, fetches are routed through the registry
proxy (Artifactory Archive Entry Download) before falling back to the
GitHub Contents API.  When ``PROXY_REGISTRY_ONLY=1``, the GitHub fallback
is blocked entirely.

For URL sources (``source_type='url'``), fetches are made directly to the
fully-qualified HTTPS URL without GitHub auth or proxy.  Index format is
auto-detected: Agent Skills (``"skills"`` key) or legacy marketplace.json
(``"plugins"`` key).

Cache lives at ``~/.apm/cache/marketplace/`` with a 1-hour TTL.
"""

import json
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests

from .errors import MarketplaceFetchError
from .models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
    parse_agent_skills_index,
    parse_marketplace_json,
)
from .registry import get_registered_marketplaces

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchResult:
    """Result of a direct URL fetch."""

    data: Dict
    digest: str
    etag: str = ""
    last_modified: str = ""


_CACHE_TTL_SECONDS = 3600  # 1 hour
_MAX_INDEX_BYTES = 10 * 1024 * 1024  # 10 MB
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
    """Cache key that avoids collisions across hosts and URL sources.

    - GitHub sources: ``name`` (same host) or ``{host}__{name}`` (GHE).
    - URL sources: first 16 hex chars of ``sha256(url)`` -- avoids
      host-based collisions between two URL sources on the same domain.
    """
    if source.is_url_source:
        return hashlib.sha256(source.url.encode()).hexdigest()[:16]
    normalized_host = source.host.lower()
    if normalized_host == "github.com":
        return source.name
    return f"{_sanitize_cache_name(normalized_host)}__{source.name}"


def _cache_data_path(name: str) -> str:
    return os.path.join(_cache_dir(), f"{_sanitize_cache_name(name)}.json")


def _cache_meta_path(name: str) -> str:
    return os.path.join(_cache_dir(), f"{_sanitize_cache_name(name)}.meta.json")


def _read_cache(name: str) -> Optional[Tuple[Dict, str]]:
    """Read cached marketplace data if valid (not expired).

    Returns:
        Tuple of (data dict, stored index_digest) if cache is fresh, else None.
    """
    data_path = _cache_data_path(name)
    meta_path = _cache_meta_path(name)
    if not os.path.exists(data_path) or not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r") as f:
            meta = json.load(f)
        fetched_at = meta.get("fetched_at", 0)
        ttl = meta.get("ttl_seconds", _CACHE_TTL_SECONDS)
        if time.time() - fetched_at > ttl:
            return None  # Expired
        stored_digest = meta.get("index_digest", "")
        with open(data_path, "r") as f:
            return json.load(f), stored_digest
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Cache read failed for '%s': %s", name, exc)
        return None


def _read_stale_cache(name: str) -> Optional[Dict]:
    """Read cached data even if expired (stale-while-revalidate)."""
    data_path = _cache_data_path(name)
    if not os.path.exists(data_path):
        return None
    try:
        with open(data_path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(name: str, data: Dict, *, index_digest: str = "",
                 etag: str = "", last_modified: str = "") -> None:
    """Write marketplace data and metadata to cache."""
    data_path = _cache_data_path(name)
    meta_path = _cache_meta_path(name)
    try:
        with open(data_path, "w") as f:
            json.dump(data, f, indent=2)
        meta: Dict = {"fetched_at": time.time(), "ttl_seconds": _CACHE_TTL_SECONDS}
        if index_digest:
            meta["index_digest"] = index_digest
        if etag:
            meta["etag"] = etag
        if last_modified:
            meta["last_modified"] = last_modified
        with open(meta_path, "w") as f:
            json.dump(meta, f)
    except OSError as exc:
        logger.debug("Cache write failed for '%s': %s", name, exc)


def _clear_cache(name: str) -> None:
    """Remove cached data for a marketplace."""
    for path in (_cache_data_path(name), _cache_meta_path(name)):
        try:
            os.remove(path)
        except OSError:
            pass


def _read_stale_meta(name: str) -> Optional[Dict]:
    """Read cache metadata even if the cache has expired.

    Returns the raw meta dict (may contain etag, last_modified, etc.),
    or ``None`` if no meta file exists.
    """
    meta_path = _cache_meta_path(name)
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Network fetch
# ---------------------------------------------------------------------------


def _try_proxy_fetch(
    source: MarketplaceSource,
    file_path: str,
) -> Optional[Dict]:
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
            source.owner, source.repo, file_path,
        )
        return None


def _fetch_url_direct(url: str, *, etag: str = "", last_modified: str = "",
                      expected_digest: str = ""):
    """Fetch a URL marketplace index directly over HTTPS.

    No GitHub auth or proxy involved -- used for URL sources only.

    Supports conditional requests: when *etag* or *last_modified* are provided
    the corresponding ``If-None-Match`` / ``If-Modified-Since`` headers are sent.
    A 304 Not Modified response returns ``None`` (caller should use cached data).

    When *expected_digest* is non-empty the computed ``sha256:`` digest of the
    response body is compared against it; a mismatch raises ``MarketplaceFetchError``.

    Returns:
        FetchResult with data, digest, etag, and last_modified; or ``None`` on 304.

    Raises:
        MarketplaceFetchError: On non-HTTPS scheme, 404, any other HTTP error,
            network failure, non-JSON response body, or digest mismatch.
    """
    from urllib.parse import urlparse

    if urlparse(url).scheme.lower() != "https":
        raise MarketplaceFetchError(url, "URL sources must use HTTPS")
    try:
        headers = {"User-Agent": "apm-cli"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        resp = requests.get(url, headers=headers, timeout=30)
        # Guard against HTTPS->HTTP redirect (S1)
        final_url = getattr(resp, "url", None)
        if isinstance(final_url, str) and urlparse(final_url).scheme.lower() != "https":
            raise MarketplaceFetchError(
                url, "Redirect to non-HTTPS URL rejected"
            )
        if resp.status_code == 304:
            return None
        if resp.status_code == 404:
            raise MarketplaceFetchError(url, "404 Not Found")
        resp.raise_for_status()
        content_length = resp.headers.get("Content-Length")
        if content_length:
            try:
                size = int(content_length)
            except ValueError:
                pass
            else:
                if size > _MAX_INDEX_BYTES:
                    raise MarketplaceFetchError(
                        url,
                        f"Index exceeds size limit ({size} bytes, max {_MAX_INDEX_BYTES})",
                    )
        raw = resp.content
        if len(raw) > _MAX_INDEX_BYTES:
            raise MarketplaceFetchError(
                url,
                f"Index exceeds size limit ({len(raw)} bytes, max {_MAX_INDEX_BYTES})",
            )
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if expected_digest and digest != expected_digest:
            raise MarketplaceFetchError(
                url,
                f"digest mismatch: expected {expected_digest!r}, got {digest!r}",
            )
        data = json.loads(raw)
        resp_etag = resp.headers.get("ETag", "")
        resp_last_modified = resp.headers.get("Last-Modified", "")
        return FetchResult(data=data, digest=digest,
                           etag=resp_etag, last_modified=resp_last_modified)
    except MarketplaceFetchError:
        raise
    except requests.exceptions.RequestException as exc:
        raise MarketplaceFetchError(url, str(exc)) from exc
    except ValueError as exc:
        raise MarketplaceFetchError(url, f"Invalid JSON response: {exc}") from exc

def _detect_index_format(data: Dict) -> str:
    """Detect whether *data* is an Agent Skills index or a legacy marketplace.json.

    Returns:
        ``"agent-skills"`` if the ``"skills"`` key is present.
        ``"github"`` if the ``"plugins"`` key is present.
        ``"unknown"`` otherwise.
    """
    if "skills" in data:
        return "agent-skills"
    if "plugins" in data:
        return "github"
    return "unknown"


def _github_contents_url(source: MarketplaceSource, file_path: str) -> str:
    """Build the GitHub Contents API URL for a file."""
    from ..core.auth import AuthResolver

    host_info = AuthResolver.classify_host(source.host)
    api_base = host_info.api_base
    return f"{api_base}/repos/{source.owner}/{source.repo}/contents/{file_path}?ref={source.branch}"


def _fetch_file(
    source: MarketplaceSource,
    file_path: str,
    auth_resolver: Optional[object] = None,
) -> Optional[Dict]:
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
            source.owner, source.repo, file_path,
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
            unauth_first=False,
        )
    except Exception as exc:
        raise MarketplaceFetchError(source.name, str(exc)) from exc


def _auto_detect_path(
    source: MarketplaceSource,
    auth_resolver: Optional[object] = None,
) -> Optional[str]:
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


def _parse_manifest(
    data: Dict,
    source: MarketplaceSource,
    *,
    source_url: str = "",
    source_digest: str = "",
) -> MarketplaceManifest:
    """Parse *data* using the correct parser for *source*.

    For URL sources the index format is auto-detected via
    ``_detect_index_format`` and dispatched to the appropriate parser.
    For GitHub sources ``parse_marketplace_json`` is used directly
    (``source_url`` and ``source_digest`` are ignored -- GitHub sources
    have no URL provenance).

    Args:
        data: Parsed JSON dict from the marketplace index.
        source: The marketplace source that produced *data*.
        source_url: Optional URL to attach as provenance metadata.
        source_digest: Optional digest to attach as provenance metadata.

    Returns:
        Parsed ``MarketplaceManifest``.

    Raises:
        MarketplaceFetchError: If the URL source index has an
            unrecognised format (neither ``skills`` nor ``plugins`` key).
    """
    if source.is_url_source:
        fmt = _detect_index_format(data)
        if fmt == "agent-skills":
            return parse_agent_skills_index(
                data, source.name,
                source_url=source_url, source_digest=source_digest,
            )
        if fmt == "github":
            return parse_marketplace_json(
                data, source.name,
                source_url=source_url, source_digest=source_digest,
            )
        raise MarketplaceFetchError(
            source.url or source.name,
            "Unrecognised index format; run `apm marketplace update` to refresh",
        )
    return parse_marketplace_json(data, source.name)


def fetch_marketplace(
    source: MarketplaceSource,
    *,
    force_refresh: bool = False,
    auth_resolver: Optional[object] = None,
    on_stale_warning: Optional[object] = None,
) -> MarketplaceManifest:
    """Fetch and parse a marketplace manifest.

    Uses cache when available (1h TTL). Falls back to stale cache on
    network errors.

    Args:
        source: Marketplace source to fetch.
        force_refresh: Skip cache and re-fetch from network.
        auth_resolver: Optional ``AuthResolver`` instance (created if None).
        on_stale_warning: Optional callable invoked with a warning message when
            stale cache is served due to a network error.  Use this to surface
            the warning to the terminal in the command layer.

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
            cached_data, stored_digest = cached
            logger.debug("Using cached marketplace data for '%s'", source.name)
            return _parse_manifest(
                cached_data, source,
                source_url=source.url if source.is_url_source else "",
                source_digest=stored_digest,
            )

    # Fetch from network
    try:
        # URL source -- direct HTTPS fetch, no GitHub auth or proxy
        if source.is_url_source:
            # Read stale ETag/Last-Modified for conditional request
            stale_meta = _read_stale_meta(cache_name)
            stored_etag = (stale_meta or {}).get("etag", "")
            stored_last_modified = (stale_meta or {}).get("last_modified", "")

            result = _fetch_url_direct(
                source.url, etag=stored_etag, last_modified=stored_last_modified
            )

            if result is None:
                # 304 Not Modified -- reset TTL on existing cache, serve stale data
                stale = _read_stale_cache(cache_name)
                if stale is not None:
                    _write_cache(
                        cache_name, stale,
                        index_digest=(stale_meta or {}).get("index_digest", ""),
                        etag=stored_etag,
                        last_modified=stored_last_modified,
                    )
                    logger.debug("304 Not Modified for '%s'; serving cached data", source.name)
                    return _parse_manifest(
                        stale, source,
                        source_url=source.url,
                        source_digest=(stale_meta or {}).get("index_digest", ""),
                    )
                raise MarketplaceFetchError(
                    source.name,
                    "Got 304 Not Modified but no cached data is available",
                )

            _write_cache(
                cache_name, result.data,
                index_digest=result.digest,
                etag=result.etag,
                last_modified=result.last_modified,
            )
            return _parse_manifest(
                result.data, source,
                source_url=source.url, source_digest=result.digest,
            )

        # GitHub source -- proxy-first, then GitHub Contents API
        data = _fetch_file(source, source.path, auth_resolver=auth_resolver)
        if data is None:
            raise MarketplaceFetchError(
                source.name,
                f"marketplace.json not found at '{source.path}' "
                f"in {source.owner}/{source.repo}",
            )
        _write_cache(cache_name, data)
        return parse_marketplace_json(data, source.name)
    except MarketplaceFetchError:
        # Stale-while-revalidate: serve expired cache on network error
        stale = _read_stale_cache(cache_name)
        if stale is not None:
            warning_msg = f"Network error fetching '{source.name}'; using stale cache"
            logger.warning(warning_msg)
            if on_stale_warning is not None:
                on_stale_warning(warning_msg)
            if source.is_url_source:
                _stale_meta = _read_stale_meta(cache_name)
                return _parse_manifest(
                    stale, source,
                    source_url=source.url,
                    source_digest=(_stale_meta or {}).get("index_digest", ""),
                )
            return _parse_manifest(stale, source)
        raise


def fetch_or_cache(
    source: MarketplaceSource,
    *,
    auth_resolver: Optional[object] = None,
) -> MarketplaceManifest:
    """Convenience wrapper -- same as ``fetch_marketplace`` with defaults."""
    return fetch_marketplace(source, auth_resolver=auth_resolver)


def search_marketplace(
    query: str,
    source: MarketplaceSource,
    *,
    auth_resolver: Optional[object] = None,
) -> List[MarketplacePlugin]:
    """Search a single marketplace for plugins matching *query*."""
    manifest = fetch_marketplace(source, auth_resolver=auth_resolver)
    return manifest.search(query)


def search_all_marketplaces(
    query: str,
    *,
    auth_resolver: Optional[object] = None,
) -> List[MarketplacePlugin]:
    """Search across all registered marketplaces.

    Returns plugins matching the query, annotated with their source marketplace.
    """
    results: List[MarketplacePlugin] = []
    for source in get_registered_marketplaces():
        try:
            manifest = fetch_marketplace(source, auth_resolver=auth_resolver)
            results.extend(manifest.search(query))
        except MarketplaceFetchError as exc:
            logger.warning("Skipping marketplace '%s': %s", source.name, exc)
    return results


def clear_marketplace_cache(
    name: Optional[str] = None,
    host: str = "github.com",
    source: Optional[MarketplaceSource] = None,
) -> int:
    """Clear cached data for one or all marketplaces.

    Returns the number of caches cleared.
    """
    if source is not None:
        # Use the actual source object to derive the correct cache key
        # (required for URL sources whose key is sha256-based, not name-based)
        _clear_cache(_cache_key(source))
        return 1
    if name:
        # Build a minimal source to derive the cache key
        _src = MarketplaceSource(name=name, owner="", repo="", host=host)
        _clear_cache(_cache_key(_src))
        return 1
    count = 0
    for src in get_registered_marketplaces():
        _clear_cache(_cache_key(src))
        count += 1
    return count
