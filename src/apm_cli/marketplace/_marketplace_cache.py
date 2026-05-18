"""Marketplace JSON cache helpers extracted from client.py."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import MarketplaceSource

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 3600  # 1 hour
_CACHE_DIR_NAME = os.path.join("cache", "marketplace")


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


def _write_cache(name: str, data: dict) -> None:
    """Write marketplace data and metadata to cache."""
    data_path = _cache_data_path(name)
    meta_path = _cache_meta_path(name)
    try:
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(
                {"fetched_at": time.time(), "ttl_seconds": _CACHE_TTL_SECONDS},
                f,
            )
    except OSError as exc:
        logger.debug("Cache write failed for '%s': %s", name, exc)


def _clear_cache(name: str) -> None:
    """Remove cached data for a marketplace."""
    for path in (_cache_data_path(name), _cache_meta_path(name)):
        with contextlib.suppress(OSError):
            os.remove(path)
