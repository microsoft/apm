"""Cache I/O helpers for the marketplace JSON sidecar cache.

Extracted from client.py to keep module complexity bounded.
All functions in this module are private to the marketplace package;
``client.py`` re-imports them so callers see no change.

The only external dependency is the shared config dir (lazy-imported at
call time to avoid circular imports with client.py).
"""

import contextlib
import hashlib
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 3600  # 1 hour
_CACHE_DIR_NAME = os.path.join("cache", "marketplace")

# ---------------------------------------------------------------------------
# URL utility (used by cache key; kept here so client can re-import it)
# ---------------------------------------------------------------------------


def _host_from_url(url: str) -> str:
    """Extract host from a URL (handles SCP-like SSH URLs too)."""
    if not url:
        return ""
    # SCP-like: git@host:path
    if "@" in url and not url.startswith(("http", "git://", "ssh://", "file://")):
        try:
            return url.split("@", 1)[1].split(":", 1)[0]
        except (IndexError, ValueError):
            return ""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Cache directory helpers
# ---------------------------------------------------------------------------


def _cache_dir() -> str:
    """Return the cache directory, creating it if needed."""
    from ..config import CONFIG_DIR

    d = os.path.join(CONFIG_DIR, _CACHE_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def _sanitize_cache_name(name: str) -> str:
    """Sanitize marketplace name for safe use in file paths."""
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


def _cache_key(source) -> str:
    """Cache key that includes kind+host to avoid collisions across hosts."""
    kind = source.kind
    if kind == "url":
        return f"url__{hashlib.sha256(source.url.encode()).hexdigest()[:16]}"
    if kind == "local":
        return f"local__{_sanitize_cache_name(source.name)}"
    if kind == "git":
        # Generic git: include host so a.com/o/r vs b.com/o/r never collapse.
        host = _host_from_url(source.url) or source.host or "unknown"
        return f"git__{_sanitize_cache_name(host)}__{_sanitize_cache_name(source.name)}"
    normalized_host = (source.host or "github.com").lower()
    if normalized_host == "github.com":
        return source.name
    return f"{_sanitize_cache_name(normalized_host)}__{source.name}"


def _cache_data_path(name: str) -> str:
    return os.path.join(_cache_dir(), f"{_sanitize_cache_name(name)}.json")


def _cache_meta_path(name: str) -> str:
    return os.path.join(_cache_dir(), f"{_sanitize_cache_name(name)}.meta.json")


# ---------------------------------------------------------------------------
# Cache read / write / clear
# ---------------------------------------------------------------------------


def _read_cache(name: str) -> dict | None:
    """Read cached marketplace data if valid (not expired)."""
    data_path = _cache_data_path(name)
    meta_path = _cache_meta_path(name)
    if not os.path.exists(data_path) or not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        fetched_at = meta.get("fetched_at", 0)
        ttl = meta.get("ttl_seconds", _CACHE_TTL_SECONDS)
        if time.time() - fetched_at > ttl:
            return None  # Expired
        with open(data_path, encoding="utf-8") as f:
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
        with open(data_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(
    name: str,
    data: dict,
    *,
    index_digest: str = "",
    etag: str = "",
    last_modified: str = "",
) -> None:
    """Write marketplace data and metadata to cache."""
    data_path = _cache_data_path(name)
    meta_path = _cache_meta_path(name)
    try:
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        meta: dict = {"fetched_at": time.time(), "ttl_seconds": _CACHE_TTL_SECONDS}
        if index_digest:
            meta["index_digest"] = index_digest
        if etag:
            meta["etag"] = etag
        if last_modified:
            meta["last_modified"] = last_modified
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    except OSError as exc:
        logger.debug("Cache write failed for '%s': %s", name, exc)


def _read_stale_meta(name: str) -> dict | None:
    """Read cache metadata even when the data cache is expired."""
    meta_path = _cache_meta_path(name)
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _clear_cache(name: str) -> None:
    """Remove cached data for a marketplace."""
    for path in (_cache_data_path(name), _cache_meta_path(name)):
        with contextlib.suppress(OSError):
            os.remove(path)
