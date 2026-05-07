"""Tests for HTTP response cache."""

import json
import time
from pathlib import Path
from unittest.mock import patch

from apm_cli.cache.http_cache import (
    MAX_HTTP_CACHE_TTL_SECONDS,
    HttpCache,
)


class TestHttpCacheHitMiss:
    """Test basic cache hit/miss behavior."""

    def test_miss_returns_none(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path)
        result = cache.get("https://registry.example.com/api/servers/test")
        assert result is None

    def test_store_and_hit(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path)
        url = "https://registry.example.com/api/servers/test"
        body = b'{"name": "test-server"}'
        headers = {"Cache-Control": "max-age=3600", "ETag": '"abc123"'}

        cache.store(url, body, headers=headers)
        entry = cache.get(url)

        assert entry is not None
        assert entry.body == body
        assert entry.etag == '"abc123"'

    def test_expired_entry_returns_none(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path)
        url = "https://registry.example.com/api/servers/expired"
        body = b'{"name": "expired"}'
        headers = {"Cache-Control": "max-age=1"}

        cache.store(url, body, headers=headers)
        # Manually expire by patching the meta file
        import hashlib

        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        meta_path = tmp_path / "http_v1" / url_hash / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["expires_at"] = time.time() - 100
        meta_path.write_text(json.dumps(meta))

        result = cache.get(url)
        assert result is None


class TestHttpCacheConditionalRevalidation:
    """Test ETag-based conditional revalidation."""

    def test_conditional_headers_with_etag(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path)
        url = "https://registry.example.com/api/servers/test"
        cache.store(url, b"body", headers={"ETag": '"v1"', "Cache-Control": "max-age=3600"})

        headers = cache.conditional_headers(url)
        assert headers == {"If-None-Match": '"v1"'}

    def test_conditional_headers_no_entry(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path)
        headers = cache.conditional_headers("https://not-cached.example.com/foo")
        assert headers == {}

    def test_refresh_expiry_on_304(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path)
        url = "https://registry.example.com/api/servers/test"
        cache.store(url, b"body", headers={"ETag": '"v1"', "Cache-Control": "max-age=1"})

        # Expire it
        import hashlib

        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        meta_path = tmp_path / "http_v1" / url_hash / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["expires_at"] = time.time() - 100
        meta_path.write_text(json.dumps(meta))

        # Refresh on 304
        cache.refresh_expiry(url, headers={"Cache-Control": "max-age=3600", "ETag": '"v2"'})

        # Should be valid again
        entry = cache.get(url)
        assert entry is not None
        assert entry.body == b"body"


class TestHttpCacheTTLCap:
    """Test that max-age is capped at MAX_HTTP_CACHE_TTL_SECONDS."""

    def test_max_age_capped(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path)
        url = "https://registry.example.com/api/long-lived"
        # Server says cache for 7 days
        headers = {"Cache-Control": "max-age=604800"}
        cache.store(url, b"body", headers=headers)

        import hashlib

        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        meta_path = tmp_path / "http_v1" / url_hash / "meta.json"
        meta = json.loads(meta_path.read_text())

        # Should be capped at 24h from store time
        max_expiry = meta["stored_at"] + MAX_HTTP_CACHE_TTL_SECONDS
        assert meta["expires_at"] <= max_expiry + 1  # +1 for timing slack


class TestHttpCacheSizeCap:
    """Test LRU eviction when size cap is exceeded."""

    def test_eviction_on_size_cap(self, tmp_path: Path) -> None:
        # Use a very small cap for testing
        with patch("apm_cli.cache.http_cache.MAX_HTTP_CACHE_BYTES", 500):
            cache = HttpCache(tmp_path)

            # Store entries that exceed 500 bytes total
            for i in range(20):
                url = f"https://registry.example.com/api/entry/{i}"
                body = b"x" * 100  # 100 bytes each
                cache.store(url, body, headers={"Cache-Control": "max-age=3600"})
                # Small delay to ensure different mtimes for LRU
                time.sleep(0.01)

            # Some entries should have been evicted
            stats = cache.get_stats()
            assert stats["total_size_bytes"] <= 1000  # Generous bound


class TestHttpCacheClean:
    """Test cache cleaning."""

    def test_clean_removes_all(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path)
        cache.store(
            "https://example.com/1",
            b"body1",
            headers={"Cache-Control": "max-age=3600"},
        )
        cache.store(
            "https://example.com/2",
            b"body2",
            headers={"Cache-Control": "max-age=3600"},
        )

        cache.clean_all()
        stats = cache.get_stats()
        assert stats["entry_count"] == 0
