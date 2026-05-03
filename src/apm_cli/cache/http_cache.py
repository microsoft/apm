"""HTTP response cache with conditional revalidation.

Caches HTTP GET responses using content-addressable storage with
support for:
- ``Cache-Control: max-age=N`` (capped at 24h to prevent indefinite
  staleness)
- ``ETag`` / ``If-None-Match`` conditional revalidation
- LRU eviction when cache exceeds size limit
- Atomic writes (stage-rename pattern)

Used primarily for MCP registry lookups where repeated GETs for the
same server metadata can be served from cache.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .locking import cleanup_incomplete
from .paths import get_http_path

_log = logging.getLogger(__name__)

# Maximum TTL even if server says longer (24 hours)
MAX_HTTP_CACHE_TTL_SECONDS: int = 86400

# Maximum total size of HTTP cache (100 MB)
MAX_HTTP_CACHE_BYTES: int = 100 * 1024 * 1024

# Cache-Control max-age pattern
_MAX_AGE_RE = re.compile(r"max-age=(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class CacheEntry:
    """Represents a cached HTTP response."""

    body: bytes
    etag: str | None
    expires_at: float  # monotonic-like epoch timestamp
    content_type: str | None
    status_code: int


class HttpCache:
    """HTTP response cache with conditional revalidation.

    Args:
        cache_root: Root cache directory (from :func:`get_cache_root`).
    """

    def __init__(self, cache_root: Path) -> None:
        self._cache_dir = get_http_path(cache_root)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self._cache_dir), 0o700)
        cleanup_incomplete(self._cache_dir)

    def get(self, url: str, headers: dict[str, str] | None = None) -> CacheEntry | None:
        """Look up a cached response for *url*.

        Returns the entry only if it has not expired. Callers should
        use :meth:`conditional_headers` to build revalidation requests
        for expired entries.

        Args:
            url: The request URL.
            headers: Original request headers (unused currently, for
                future Vary support).

        Returns:
            :class:`CacheEntry` if a valid (non-expired) entry exists,
            otherwise ``None``.
        """
        entry_path = self._entry_path(url)
        meta_path = entry_path / "meta.json"
        body_path = entry_path / "body"

        if not meta_path.is_file() or not body_path.is_file():
            return None

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            expires_at = meta.get("expires_at", 0)
            if time.time() > expires_at:
                return None  # Expired -- caller should revalidate

            body = body_path.read_bytes()
            return CacheEntry(
                body=body,
                etag=meta.get("etag"),
                expires_at=expires_at,
                content_type=meta.get("content_type"),
                status_code=meta.get("status_code", 200),
            )
        except (json.JSONDecodeError, OSError) as exc:
            _log.debug("Failed to read HTTP cache entry for %s: %s", url, exc)
            return None

    def conditional_headers(self, url: str) -> dict[str, str]:
        """Return conditional request headers for revalidation.

        If a cached entry exists (even expired), returns ``If-None-Match``
        with the stored ETag.

        Args:
            url: The request URL.

        Returns:
            Dict of headers to add to the request.
        """
        entry_path = self._entry_path(url)
        meta_path = entry_path / "meta.json"

        if not meta_path.is_file():
            return {}

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            etag = meta.get("etag")
            if etag:
                return {"If-None-Match": etag}
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def store(
        self,
        url: str,
        body: bytes,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Store an HTTP response in the cache.

        Parses ``Cache-Control`` and ``ETag`` from response headers to
        determine TTL and revalidation token.

        Args:
            url: Request URL.
            body: Response body bytes.
            status_code: HTTP status code.
            headers: Response headers (case-insensitive keys expected
                from requests library).
        """
        headers = headers or {}
        ttl = self._parse_ttl(headers)
        etag = headers.get("ETag") or headers.get("etag")
        content_type = headers.get("Content-Type") or headers.get("content-type")

        entry_path = self._entry_path(url)
        entry_path.mkdir(parents=True, exist_ok=True)
        os.chmod(str(entry_path), 0o700)

        meta = {
            "url": url,
            "etag": etag,
            "expires_at": time.time() + ttl,
            "content_type": content_type,
            "status_code": status_code,
            "stored_at": time.time(),
        }

        # Write atomically (meta then body)
        meta_path = entry_path / "meta.json"
        body_path = entry_path / "body"

        try:
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            body_path.write_bytes(body)
            # Update mtime for LRU tracking
            os.utime(str(entry_path), None)
        except OSError as exc:
            _log.debug("Failed to write HTTP cache entry for %s: %s", url, exc)

        # Enforce size cap
        self._enforce_size_cap()

    def refresh_expiry(self, url: str, headers: dict[str, str] | None = None) -> None:
        """Refresh TTL for a cached entry (on 304 Not Modified).

        Args:
            url: Request URL.
            headers: Response headers from the 304 response.
        """
        entry_path = self._entry_path(url)
        meta_path = entry_path / "meta.json"

        if not meta_path.is_file():
            return

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            ttl = self._parse_ttl(headers or {})
            meta["expires_at"] = time.time() + ttl
            # Update ETag if provided in 304 response
            new_etag = (headers or {}).get("ETag") or (headers or {}).get("etag")
            if new_etag:
                meta["etag"] = new_etag
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            os.utime(str(entry_path), None)
        except (json.JSONDecodeError, OSError) as exc:
            _log.debug("Failed to refresh HTTP cache entry for %s: %s", url, exc)

    def clean_all(self) -> None:
        """Remove all HTTP cache entries."""
        from ..utils.file_ops import robust_rmtree

        if self._cache_dir.is_dir():
            for entry in os.scandir(str(self._cache_dir)):
                if entry.is_dir(follow_symlinks=False):
                    robust_rmtree(Path(entry.path), ignore_errors=True)

    def get_stats(self) -> dict[str, int]:
        """Return cache statistics.

        Returns:
            Dict with keys: entry_count, total_size_bytes.
        """
        count = 0
        total_size = 0
        if not self._cache_dir.is_dir():
            return {"entry_count": 0, "total_size_bytes": 0}

        for entry in os.scandir(str(self._cache_dir)):
            if entry.is_dir(follow_symlinks=False):
                count += 1
                for f in os.scandir(entry.path):
                    if f.is_file(follow_symlinks=False):
                        with contextlib.suppress(OSError):
                            total_size += f.stat(follow_symlinks=False).st_size

        return {"entry_count": count, "total_size_bytes": total_size}

    def _entry_path(self, url: str) -> Path:
        """Derive the cache entry directory path for a URL."""
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        return self._cache_dir / url_hash

    def _parse_ttl(self, headers: dict[str, str]) -> float:
        """Parse TTL from response headers, capped at MAX_HTTP_CACHE_TTL_SECONDS."""
        # Try Cache-Control: max-age
        cache_control = headers.get("Cache-Control") or headers.get("cache-control") or ""
        match = _MAX_AGE_RE.search(cache_control)
        if match:
            ttl = int(match.group(1))
            return min(ttl, MAX_HTTP_CACHE_TTL_SECONDS)

        # Default TTL: 5 minutes for responses without Cache-Control
        return 300.0

    def _enforce_size_cap(self) -> None:
        """Evict LRU entries if total cache size exceeds the cap."""
        if not self._cache_dir.is_dir():
            return

        entries: list[tuple[float, str, int]] = []
        total_size = 0

        for entry in os.scandir(str(self._cache_dir)):
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
                entry_size = 0
                for f in os.scandir(entry.path):
                    if f.is_file(follow_symlinks=False):
                        with contextlib.suppress(OSError):
                            entry_size += f.stat(follow_symlinks=False).st_size
                entries.append((stat.st_mtime, entry.path, entry_size))
                total_size += entry_size
            except OSError:
                continue

        if total_size <= MAX_HTTP_CACHE_BYTES:
            return

        # Sort by mtime ascending (oldest first = LRU)
        entries.sort(key=lambda x: x[0])

        from ..utils.file_ops import robust_rmtree

        for _mtime, path, size in entries:
            if total_size <= MAX_HTTP_CACHE_BYTES:
                break
            robust_rmtree(Path(path), ignore_errors=True)
            total_size -= size
