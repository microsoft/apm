"""Tests for URL-based marketplace client fetch path.

Covers: _cache_key() URL sources, _fetch_url_direct(), fetch_marketplace()
URL branch, format auto-detection, cache read/write, stale-while-revalidate.
GitHub paths are not touched -- regression tests confirm they still work.
Tests are separate from test_marketplace_client.py which covers GitHub only.
"""

import hashlib
import json
import os
import time

import pytest
import requests

from apm_cli.marketplace.client import (
    FetchResult,
    _cache_data_path,
    _cache_dir,
    _cache_key,
    _cache_meta_path,
    _detect_index_format,
    _fetch_url_direct,
    _read_stale_meta,
    _write_cache,
    fetch_marketplace,
)
from apm_cli.marketplace.errors import MarketplaceFetchError
from apm_cli.marketplace.models import MarketplaceSource

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_KNOWN_SCHEMA = "https://schemas.agentskills.io/discovery/0.2.0/schema.json"
_VALID_DIGEST = "sha256:" + "a" * 64

_AGENT_SKILLS_INDEX = {
    "$schema": _KNOWN_SCHEMA,
    "skills": [
        {
            "name": "code-review",
            "type": "skill-md",
            "description": "Code review helper",
            "url": "/.well-known/agent-skills/code-review/SKILL.md",
            "digest": _VALID_DIGEST,
        }
    ],
}

_GITHUB_MARKETPLACE_JSON = {
    "plugins": [
        {
            "name": "my-plugin",
            "description": "A plugin",
            "source": {"type": "github", "repo": "owner/my-plugin"},
        }
    ]
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def url_source():
    return MarketplaceSource(
        name="example-skills",
        source_type="url",
        url="https://example.com/.well-known/agent-skills/index.json",
    )


@pytest.fixture
def github_source():
    return MarketplaceSource(
        name="acme",
        owner="acme-org",
        repo="plugins",
    )


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Redirect cache and registry to tmp dir for every test in this file."""
    config_dir = str(tmp_path / ".apm")
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)


def _write_cache_files(source, data, *, expired=False):
    """Helper: write cache data + meta for a source (fresh or expired)."""
    cache_name = _cache_key(source)
    os.makedirs(_cache_dir(), exist_ok=True)
    with open(_cache_data_path(cache_name), "w") as f:
        json.dump(data, f)
    fetched_at = 0 if expired else time.time()
    with open(_cache_meta_path(cache_name), "w") as f:
        json.dump({"fetched_at": fetched_at, "ttl_seconds": 3600}, f)


def _write_cache_files_with_digest(source, data, *, digest="", expired=False):
    """Helper: write cache data + meta including an index_digest field."""
    cache_name = _cache_key(source)
    os.makedirs(_cache_dir(), exist_ok=True)
    with open(_cache_data_path(cache_name), "w") as f:
        json.dump(data, f)
    fetched_at = 0 if expired else time.time()
    meta = {"fetched_at": fetched_at, "ttl_seconds": 3600}
    if digest:
        meta["index_digest"] = digest
    with open(_cache_meta_path(cache_name), "w") as f:
        json.dump(meta, f)


# ---------------------------------------------------------------------------
# _cache_key -- URL sources
# ---------------------------------------------------------------------------


class TestCacheKey:
    """_cache_key() must use SHA-256 of URL for URL sources."""

    def test_url_source_returns_sha256_of_url(self, url_source):
        expected = hashlib.sha256(url_source.url.encode()).hexdigest()[:16]
        assert _cache_key(url_source) == expected

    def test_url_source_key_is_not_name(self, url_source):
        """Key must not fall back to the name-based GitHub key."""
        assert _cache_key(url_source) != url_source.name

    def test_two_url_sources_same_host_get_different_keys(self):
        """Two marketplaces on the same hostname must not share a cache slot."""
        src_a = MarketplaceSource(
            name="a", source_type="url",
            url="https://example.com/a/index.json",
        )
        src_b = MarketplaceSource(
            name="b", source_type="url",
            url="https://example.com/b/index.json",
        )
        assert _cache_key(src_a) != _cache_key(src_b)

    def test_github_com_source_returns_name(self, github_source):
        """github.com sources keep the existing name-based key (regression)."""
        assert _cache_key(github_source) == github_source.name

    def test_github_custom_host_includes_host_in_key(self):
        """Custom-host GitHub sources keep the host__name format (regression)."""
        src = MarketplaceSource(name="acme", owner="o", repo="r", host="ghe.corp.com")
        key = _cache_key(src)
        assert "acme" in key
        assert "ghe" in key.lower()

    def test_empty_url_produces_deterministic_key(self):
        """Empty-string URL has a deterministic SHA-256 key, not a crash."""
        src = MarketplaceSource(name="x", source_type="url", url="")
        key = _cache_key(src)
        assert key == hashlib.sha256(b"").hexdigest()[:16]

    def test_very_long_url_key_is_always_16_chars(self):
        """Key is always truncated to 16 hex characters regardless of URL length."""
        long_url = "https://example.com/" + "a" * 2000
        src = MarketplaceSource(name="x", source_type="url", url=long_url)
        assert len(_cache_key(src)) == 16


# ---------------------------------------------------------------------------
# _fetch_url_direct -- network layer


class TestFetchUrlDirectEmptyUrl:
    """_fetch_url_direct raises clearly when given an empty URL."""

    def test_empty_url_raises_fetch_error(self):
        """An empty URL has no scheme and raises immediately without a network call."""
        with pytest.raises(MarketplaceFetchError):
            _fetch_url_direct("")


# ---------------------------------------------------------------------------
# _detect_index_format -- direct unit tests
# ---------------------------------------------------------------------------


class TestDetectIndexFormat:
    """_detect_index_format dispatches correctly on index shape."""

    def test_skills_key_returns_agent_skills(self):
        assert _detect_index_format({"skills": []}) == "agent-skills"

    def test_plugins_key_returns_github(self):
        assert _detect_index_format({"plugins": []}) == "github"

    def test_neither_key_returns_unknown(self):
        assert _detect_index_format({}) == "unknown"

    def test_both_keys_present_agent_skills_wins(self):
        """When both keys are present, the Agent Skills format takes precedence."""
        assert _detect_index_format({"skills": [], "plugins": []}) == "agent-skills"
# ---------------------------------------------------------------------------


class TestFetchUrlDirect:
    """_fetch_url_direct() must handle all HTTP and network outcomes."""

    def test_http_200_returns_parsed_json(self, monkeypatch):
        mock_resp = _mock_response(200, json_body=_AGENT_SKILLS_INDEX)
        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", lambda *a, **kw: mock_resp)
        result = _fetch_url_direct("https://example.com/index.json")
        assert result.data == _AGENT_SKILLS_INDEX

    def test_http_404_raises_fetch_error(self, monkeypatch):
        mock_resp = _mock_response(404)
        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", lambda *a, **kw: mock_resp)
        with pytest.raises(MarketplaceFetchError, match="404"):
            _fetch_url_direct("https://example.com/index.json")

    def test_http_500_raises_fetch_error(self, monkeypatch):
        mock_resp = _mock_response(500, raise_for_status=requests.exceptions.HTTPError("500"))
        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", lambda *a, **kw: mock_resp)
        with pytest.raises(MarketplaceFetchError):
            _fetch_url_direct("https://example.com/index.json")

    def test_network_timeout_raises_fetch_error(self, monkeypatch):
        def _timeout(*a, **kw):
            raise requests.exceptions.Timeout("timed out")
        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", _timeout)
        with pytest.raises(MarketplaceFetchError):
            _fetch_url_direct("https://example.com/index.json")

    def test_non_json_response_raises_fetch_error(self, monkeypatch):
        mock_resp = _mock_response(200, json_error=ValueError("not JSON"))
        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", lambda *a, **kw: mock_resp)
        with pytest.raises(MarketplaceFetchError):
            _fetch_url_direct("https://example.com/index.json")


# ---------------------------------------------------------------------------
# fetch_marketplace -- URL branch
# ---------------------------------------------------------------------------


class TestFetchMarketplaceURL:
    """fetch_marketplace() must route URL sources through _fetch_url_direct."""

    def test_url_source_calls_fetch_url_direct_not_fetch_file(self, url_source, monkeypatch):
        """_fetch_file must never be called for a URL source."""
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            lambda url, **kw: FetchResult(data=_AGENT_SKILLS_INDEX, digest=_VALID_DIGEST),
        )
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_file",
            _never_called("_fetch_file"),
        )
        result = fetch_marketplace(url_source)
        assert len(result.plugins) == 1

    def test_url_source_skills_key_uses_agent_skills_parser(self, url_source, monkeypatch):
        """Index with 'skills' key must be parsed by parse_agent_skills_index."""
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            lambda url, **kw: FetchResult(data=_AGENT_SKILLS_INDEX, digest=_VALID_DIGEST),
        )
        result = fetch_marketplace(url_source)
        assert result.plugins[0].name == "code-review"

    def test_url_source_plugins_key_uses_marketplace_parser(self, url_source, monkeypatch):
        """Index with 'plugins' key must be parsed by parse_marketplace_json."""
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            lambda url, **kw: FetchResult(data=_GITHUB_MARKETPLACE_JSON, digest=_VALID_DIGEST),
        )
        result = fetch_marketplace(url_source)
        assert result.plugins[0].name == "my-plugin"

    def test_url_source_uses_fresh_cache_without_network_call(self, url_source, monkeypatch):
        """A fresh cache must short-circuit the network entirely."""
        _write_cache_files(url_source, _AGENT_SKILLS_INDEX, expired=False)
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            _never_called("_fetch_url_direct"),
        )
        result = fetch_marketplace(url_source)
        assert result.plugins[0].name == "code-review"

    def test_url_source_writes_data_and_meta_to_cache(self, url_source, monkeypatch):
        """After a successful network fetch both .json and .meta.json must exist."""
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            lambda url, **kw: FetchResult(data=_AGENT_SKILLS_INDEX, digest=_VALID_DIGEST),
        )
        fetch_marketplace(url_source)
        cache_name = _cache_key(url_source)
        assert os.path.exists(_cache_data_path(cache_name))
        assert os.path.exists(_cache_meta_path(cache_name))

    def test_url_source_stale_cache_returned_on_network_error(self, url_source, monkeypatch):
        """Stale-while-revalidate: expired cache beats a network failure."""
        _write_cache_files(url_source, _AGENT_SKILLS_INDEX, expired=True)
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            _raises_fetch_error(url_source.name),
        )
        result = fetch_marketplace(url_source)
        assert result.plugins[0].name == "code-review"

    def test_url_source_unknown_format_raises(self, url_source, monkeypatch):
        """Fresh-fetch with unrecognized format raises MarketplaceFetchError."""
        bad_index = {"unexpected": "data"}
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            lambda url, **kw: FetchResult(data=bad_index, digest=_VALID_DIGEST),
        )
        with pytest.raises(MarketplaceFetchError, match="[Uu]nrecogni"):
            fetch_marketplace(url_source)

    def test_url_source_no_cache_network_error_raises(self, url_source, monkeypatch):
        """No cache + network failure must propagate MarketplaceFetchError."""
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            _raises_fetch_error(url_source.name),
        )
        with pytest.raises(MarketplaceFetchError):
            fetch_marketplace(url_source)

    def test_github_source_uses_fetch_file_not_fetch_url_direct(
        self, github_source, monkeypatch
    ):
        """GitHub sources must never touch _fetch_url_direct (regression)."""
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            _never_called("_fetch_url_direct"),
        )
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_file",
            lambda source, path, **kw: _GITHUB_MARKETPLACE_JSON,
        )
        result = fetch_marketplace(github_source)
        assert result.plugins[0].name == "my-plugin"


# ---------------------------------------------------------------------------
# FetchResult -- digest capture (t5-test-01)
# ---------------------------------------------------------------------------


class TestFetchUrlDirectDigest:
    """_fetch_url_direct must return a FetchResult with .data and .digest."""

    def test_returns_fetch_result_not_plain_dict(self, monkeypatch):
        mock_resp = _mock_response(200, json_body=_AGENT_SKILLS_INDEX)
        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", lambda *a, **kw: mock_resp)
        result = _fetch_url_direct("https://example.com/index.json")
        assert isinstance(result, FetchResult)

    def test_data_field_equals_parsed_json(self, monkeypatch):
        mock_resp = _mock_response(200, json_body=_AGENT_SKILLS_INDEX)
        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", lambda *a, **kw: mock_resp)
        result = _fetch_url_direct("https://example.com/index.json")
        assert result.data == _AGENT_SKILLS_INDEX

    def test_digest_is_sha256_of_raw_bytes(self, monkeypatch):
        mock_resp = _mock_response(200, json_body=_AGENT_SKILLS_INDEX)
        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", lambda *a, **kw: mock_resp)
        result = _fetch_url_direct("https://example.com/index.json")
        expected = "sha256:" + hashlib.sha256(mock_resp.content).hexdigest()
        assert result.digest == expected

    def test_digest_format_is_sha256_colon_64hex(self, monkeypatch):
        mock_resp = _mock_response(200, json_body=_AGENT_SKILLS_INDEX)
        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", lambda *a, **kw: mock_resp)
        result = _fetch_url_direct("https://example.com/index.json")
        assert result.digest.startswith("sha256:")
        hex_part = result.digest[len("sha256:"):]
        assert len(hex_part) == 64
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_matching_expected_digest_does_not_raise(self, monkeypatch):
        mock_resp = _mock_response(200, json_body=_AGENT_SKILLS_INDEX)
        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", lambda *a, **kw: mock_resp)
        expected = "sha256:" + hashlib.sha256(mock_resp.content).hexdigest()
        result = _fetch_url_direct("https://example.com/index.json",
                                   expected_digest=expected)
        assert result.digest == expected

    def test_mismatched_expected_digest_raises_fetch_error(self, monkeypatch):
        mock_resp = _mock_response(200, json_body=_AGENT_SKILLS_INDEX)
        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", lambda *a, **kw: mock_resp)
        wrong = "sha256:" + "0" * 64
        with pytest.raises(MarketplaceFetchError, match="digest mismatch"):
            _fetch_url_direct("https://example.com/index.json", expected_digest=wrong)

    def test_empty_expected_digest_skips_verification(self, monkeypatch):
        mock_resp = _mock_response(200, json_body=_AGENT_SKILLS_INDEX)
        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", lambda *a, **kw: mock_resp)
        result = _fetch_url_direct("https://example.com/index.json", expected_digest="")
        assert isinstance(result, FetchResult)


# ---------------------------------------------------------------------------
# fetch_marketplace -- index digest storage + manifest fields (t5-test-02/03)
# ---------------------------------------------------------------------------

_FIXED_DIGEST = "sha256:" + "b" * 64


def _fetch_url_direct_stub(url, *, etag="", last_modified=""):
    return FetchResult(data=_AGENT_SKILLS_INDEX, digest=_FIXED_DIGEST)


class TestFetchMarketplaceDigestStorage:
    """fetch_marketplace must persist index_digest to .meta.json for URL sources."""

    def test_meta_contains_index_digest_after_live_fetch(self, url_source, monkeypatch):
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            _fetch_url_direct_stub,
        )
        fetch_marketplace(url_source, force_refresh=True)
        meta_path = _cache_meta_path(_cache_key(url_source))
        with open(meta_path) as f:
            meta = json.load(f)
        assert meta.get("index_digest") == _FIXED_DIGEST

    def test_manifest_source_url_equals_source_url(self, url_source, monkeypatch):
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            _fetch_url_direct_stub,
        )
        manifest = fetch_marketplace(url_source, force_refresh=True)
        assert manifest.source_url == url_source.url

    def test_manifest_source_digest_equals_fetched_digest(self, url_source, monkeypatch):
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            _fetch_url_direct_stub,
        )
        manifest = fetch_marketplace(url_source, force_refresh=True)
        assert manifest.source_digest == _FIXED_DIGEST

    def test_cache_hit_manifest_carries_stored_digest(self, url_source):
        """Warm cache that includes index_digest surfaces it on the manifest."""
        _write_cache_files_with_digest(url_source, _AGENT_SKILLS_INDEX, digest=_FIXED_DIGEST)
        manifest = fetch_marketplace(url_source)
        assert manifest.source_digest == _FIXED_DIGEST

    def test_cache_hit_without_digest_has_empty_source_digest(self, url_source):
        """Older cache entries without index_digest return empty string (backward compat)."""
        _write_cache_files(url_source, _AGENT_SKILLS_INDEX)
        manifest = fetch_marketplace(url_source)
        assert manifest.source_digest == ""


def _mock_response(status_code, *, json_body=None, json_error=None, raise_for_status=None):
    """Build a minimal mock for requests.Response."""
    import unittest.mock as mock

    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    if json_body is not None:
        body_bytes = json.dumps(json_body).encode()
        resp.content = body_bytes
        resp.json.return_value = json_body
    else:
        resp.content = b""
    if json_error is not None:
        resp.json.side_effect = json_error
    if raise_for_status is not None:
        resp.raise_for_status.side_effect = raise_for_status
    else:
        resp.raise_for_status.return_value = None
    return resp


def _never_called(name: str):
    """Return a callable that fails loudly if invoked."""
    def _fn(*a, **kw):
        raise AssertionError(f"{name} must not be called in this test")
    return _fn


def _raises_fetch_error(source_name: str):
    """Return a callable that raises MarketplaceFetchError."""
    def _fn(*a, **kw):
        raise MarketplaceFetchError(source_name, "simulated network error")
    return _fn


# ---------------------------------------------------------------------------
# Step 6: ETag / conditional refresh
# ---------------------------------------------------------------------------

def _mock_response_with_headers(status_code, *, json_body=None, response_headers=None):
    """Build a mock requests.Response with custom response headers."""
    import unittest.mock as mock

    resp = mock.MagicMock()
    resp.status_code = status_code
    response_headers = response_headers or {}
    resp.headers = response_headers
    if json_body is not None:
        body_bytes = json.dumps(json_body).encode()
        resp.content = body_bytes
        resp.json.return_value = json_body
    else:
        resp.content = b""
    resp.raise_for_status.return_value = None
    return resp


class TestConditionalFetchHeaders:
    """_fetch_url_direct must send conditional request headers and handle 304."""

    @pytest.mark.parametrize("kwarg,value,header_name", [
        ("etag", '"abc"', "If-None-Match"),
        ("last_modified", "Mon, 13 Apr 2026 00:00:00 GMT", "If-Modified-Since"),
    ])
    def test_conditional_header_sent(self, monkeypatch, kwarg, value, header_name):
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["headers"] = headers or {}
            return _mock_response_with_headers(200, json_body=_AGENT_SKILLS_INDEX)

        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", fake_get)
        _fetch_url_direct("https://example.com/index.json", **{kwarg: value})
        assert captured["headers"].get(header_name) == value

    def test_304_response_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "apm_cli.marketplace.client.requests.get",
            lambda *a, **kw: _mock_response_with_headers(304),
        )
        result = _fetch_url_direct("https://example.com/index.json", etag='"abc"')
        assert result is None

    @pytest.mark.parametrize("resp_header,resp_value,result_field", [
        ("ETag", '"new-etag"', "etag"),
        ("Last-Modified", "Sun, 12 Apr 2026 00:00:00 GMT", "last_modified"),
    ])
    def test_fetch_result_captures_response_header(self, monkeypatch,
                                                    resp_header, resp_value, result_field):
        monkeypatch.setattr(
            "apm_cli.marketplace.client.requests.get",
            lambda *a, **kw: _mock_response_with_headers(
                200, json_body=_AGENT_SKILLS_INDEX,
                response_headers={resp_header: resp_value},
            ),
        )
        result = _fetch_url_direct("https://example.com/index.json")
        assert getattr(result, result_field) == resp_value

    def test_fetch_result_etag_empty_when_header_absent(self, monkeypatch):
        monkeypatch.setattr(
            "apm_cli.marketplace.client.requests.get",
            lambda *a, **kw: _mock_response_with_headers(200, json_body=_AGENT_SKILLS_INDEX),
        )
        result = _fetch_url_direct("https://example.com/index.json")
        assert result.etag == ""


class TestConditionalCacheMeta:
    """_write_cache/_read_stale_meta must round-trip ETag and Last-Modified."""

    @pytest.mark.parametrize("kwarg,meta_key,value", [
        ("etag", "etag", '"v1"'),
        ("last_modified", "last_modified", "Mon, 13 Apr 2026 00:00:00 GMT"),
    ])
    def test_write_cache_stores_header_in_meta(self, tmp_path, kwarg, meta_key, value):
        _write_cache("test-mkt", {"skills": []}, **{kwarg: value})
        meta_path = _cache_meta_path("test-mkt")
        with open(meta_path) as f:
            meta = json.load(f)
        assert meta.get(meta_key) == value

    @pytest.mark.parametrize("kwarg,meta_key,value", [
        ("etag", "etag", '"v1"'),
        ("last_modified", "last_modified", "Mon, 13 Apr 2026 00:00:00 GMT"),
    ])
    def test_read_stale_meta_returns_header_from_expired_cache(self, tmp_path,
                                                                kwarg, meta_key, value):
        _write_cache("test-mkt", {"skills": []}, **{kwarg: value})
        meta_path = _cache_meta_path("test-mkt")
        with open(meta_path) as f:
            meta = json.load(f)
        meta["fetched_at"] = time.time() - 7200
        with open(meta_path, "w") as f:
            json.dump(meta, f)

        stale_meta = _read_stale_meta("test-mkt")
        assert stale_meta is not None
        assert stale_meta.get(meta_key) == value

    def test_read_stale_meta_returns_none_when_no_cache(self):
        assert _read_stale_meta("nonexistent-abc123") is None


def _write_cache_files_with_etag(source, data, *, etag="", last_modified="", expired=False):
    """Write cache files including optional ETag/Last-Modified for test setup."""
    import apm_cli.marketplace.client as client_mod
    cache_name = _cache_key(source)
    data_path = _cache_data_path(cache_name)
    meta_path = _cache_meta_path(cache_name)
    with open(data_path, "w") as f:
        json.dump(data, f)
    fetched_at = time.time() - 7200 if expired else time.time()
    meta = {"fetched_at": fetched_at, "ttl_seconds": 3600}
    if etag:
        meta["etag"] = etag
    if last_modified:
        meta["last_modified"] = last_modified
    with open(meta_path, "w") as f:
        json.dump(meta, f)


class TestFetchMarketplaceConditionalRefresh:
    """fetch_marketplace must use conditional headers on stale URL cache re-fetch."""

    @pytest.mark.parametrize("header_kwarg,header_value", [
        ("etag", '"stored-etag"'),
        ("last_modified", "Mon, 13 Apr 2026 00:00:00 GMT"),
    ])
    def test_expired_cache_sends_stored_header_on_refetch(
        self, url_source, monkeypatch, header_kwarg, header_value
    ):
        _write_cache_files_with_etag(
            url_source, _AGENT_SKILLS_INDEX, **{header_kwarg: header_value}, expired=True
        )
        captured = {}

        def fake_fetch(url, *, etag=None, last_modified=None):
            captured["etag"] = etag
            captured["last_modified"] = last_modified
            return FetchResult(data=_AGENT_SKILLS_INDEX, digest=_VALID_DIGEST,
                               etag=etag or "", last_modified=last_modified or "")

        monkeypatch.setattr("apm_cli.marketplace.client._fetch_url_direct", fake_fetch)
        fetch_marketplace(url_source)
        assert captured.get(header_kwarg) == header_value

    def test_expired_cache_304_serves_stale_data(self, url_source, monkeypatch):
        _write_cache_files_with_etag(
            url_source, _AGENT_SKILLS_INDEX, etag='"stored-etag"', expired=True
        )
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            lambda url, *, etag=None, last_modified=None: None,
        )
        manifest = fetch_marketplace(url_source)
        assert manifest.plugins[0].name == "code-review"

    def test_expired_cache_304_resets_ttl(self, url_source, monkeypatch):
        _write_cache_files_with_etag(
            url_source, _AGENT_SKILLS_INDEX, etag='"stored-etag"', expired=True
        )
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            lambda url, *, etag=None, last_modified=None: None,
        )
        before = time.time()
        fetch_marketplace(url_source)
        meta_path = _cache_meta_path(_cache_key(url_source))
        with open(meta_path) as f:
            meta = json.load(f)
        assert meta["fetched_at"] >= before

    def test_new_etag_from_200_response_stored_in_meta(self, url_source, monkeypatch):
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            lambda url, *, etag=None, last_modified=None: FetchResult(
                data=_AGENT_SKILLS_INDEX, digest=_VALID_DIGEST,
                etag='"brand-new"', last_modified="",
            ),
        )
        fetch_marketplace(url_source, force_refresh=True)
        meta_path = _cache_meta_path(_cache_key(url_source))
        with open(meta_path) as f:
            meta = json.load(f)
        assert meta.get("etag") == '"brand-new"'


# ---------------------------------------------------------------------------
# D4/T8: HTTPS scheme enforcement
# ---------------------------------------------------------------------------


class TestFetchUrlDirectHttpsEnforcement:
    """_fetch_url_direct must reject non-HTTPS schemes before making any network call."""

    @pytest.mark.parametrize("url", [
        "http://example.com/index.json",
        "ftp://example.com/index.json",
        "file:///etc/passwd",
    ])
    def test_non_https_scheme_raises(self, monkeypatch, url):
        monkeypatch.setattr(
            "apm_cli.marketplace.client.requests.get",
            _never_called("requests.get"),
        )
        with pytest.raises(MarketplaceFetchError, match="HTTPS"):
            _fetch_url_direct(url)

    def test_https_url_is_accepted(self, monkeypatch):
        mock_resp = _mock_response(200, json_body=_AGENT_SKILLS_INDEX)
        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", lambda *a, **kw: mock_resp)
        result = _fetch_url_direct("https://example.com/index.json")
        assert isinstance(result, FetchResult)


# ---------------------------------------------------------------------------
# T12: timeout error message includes URL
# ---------------------------------------------------------------------------


class TestFetchUrlDirectErrorMessages:
    """_fetch_url_direct must produce informative MarketplaceFetchError messages."""

    def test_timeout_error_message_includes_url(self, monkeypatch):
        url = "https://example.com/index.json"

        def fake_get(*a, **kw):
            raise requests.exceptions.Timeout("timed out after 30s")

        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", fake_get)
        exc = pytest.raises(MarketplaceFetchError, _fetch_url_direct, url)
        assert url in str(exc.value)
        assert "timed" in str(exc.value).lower() or "timeout" in str(exc.value).lower() or "30" in str(exc.value)

    def test_connection_error_message_includes_url(self, monkeypatch):
        url = "https://example.com/index.json"

        def fake_get(*a, **kw):
            raise requests.exceptions.ConnectionError("connection refused")

        monkeypatch.setattr("apm_cli.marketplace.client.requests.get", fake_get)
        exc = pytest.raises(MarketplaceFetchError, _fetch_url_direct, url)
        assert url in str(exc.value)


# ---------------------------------------------------------------------------
# E5: on_stale_warning callback
# ---------------------------------------------------------------------------


class TestFetchMarketplaceStaleWarning:
    """fetch_marketplace must call on_stale_warning when serving expired cache."""

    def test_on_stale_warning_called_on_network_error(self, url_source, monkeypatch):
        _write_cache_files(url_source, _AGENT_SKILLS_INDEX, expired=True)
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            _raises_fetch_error(url_source.name),
        )
        warnings = []
        fetch_marketplace(url_source, on_stale_warning=warnings.append)
        assert len(warnings) == 1
        assert url_source.name in warnings[0]

    def test_on_stale_warning_not_called_on_fresh_fetch(self, url_source, monkeypatch):
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            lambda url, **kw: FetchResult(data=_AGENT_SKILLS_INDEX, digest=_VALID_DIGEST),
        )
        warnings = []
        fetch_marketplace(url_source, on_stale_warning=warnings.append)
        assert warnings == []

    def test_on_stale_warning_not_called_on_cache_hit(self, url_source, monkeypatch):
        _write_cache_files(url_source, _AGENT_SKILLS_INDEX, expired=False)
        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            _never_called("_fetch_url_direct"),
        )
        warnings = []
        fetch_marketplace(url_source, on_stale_warning=warnings.append)
        assert warnings == []


# ---------------------------------------------------------------------------
# Stale-while-revalidate -- source_digest preservation
# ---------------------------------------------------------------------------


class TestFetchMarketplaceStaleDigestPreservation:
    """Stale fallback on network error must preserve source_digest from cached meta."""

    def test_stale_fallback_preserves_source_digest(self, url_source, monkeypatch):
        """source_digest must equal stored index_digest when stale cache is served."""
        stored_digest = "sha256:" + "b" * 64
        _write_cache_files_with_digest(
            url_source, _AGENT_SKILLS_INDEX, digest=stored_digest, expired=True
        )
        from apm_cli.marketplace.errors import MarketplaceFetchError as MFE

        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            lambda url, **kw: (_ for _ in ()).throw(
                MFE(url_source.name, "network error")
            ),
        )
        manifest = fetch_marketplace(url_source)
        assert manifest.source_digest == stored_digest

    def test_stale_fallback_preserves_source_url(self, url_source, monkeypatch):
        """source_url must equal source.url when stale cache is served."""
        _write_cache_files_with_digest(
            url_source, _AGENT_SKILLS_INDEX, digest=_VALID_DIGEST, expired=True
        )
        from apm_cli.marketplace.errors import MarketplaceFetchError as MFE

        monkeypatch.setattr(
            "apm_cli.marketplace.client._fetch_url_direct",
            lambda url, **kw: (_ for _ in ()).throw(
                MFE(url_source.name, "network error")
            ),
        )
        manifest = fetch_marketplace(url_source)
        assert manifest.source_url == url_source.url


# ---------------------------------------------------------------------------
# S1: HTTPS redirect bypass
# ---------------------------------------------------------------------------


class TestFetchUrlDirectRedirectEnforcement:
    """_fetch_url_direct must reject responses redirected to non-HTTPS URLs."""

    def test_redirect_to_http_raises(self, monkeypatch):
        """An HTTPS->HTTP redirect must be caught after the request completes."""
        mock_resp = _mock_response(200, json_body=_AGENT_SKILLS_INDEX)
        mock_resp.url = "http://evil.com/index.json"
        monkeypatch.setattr(
            "apm_cli.marketplace.client.requests.get",
            lambda *a, **kw: mock_resp,
        )
        with pytest.raises(MarketplaceFetchError, match="non-HTTPS"):
            _fetch_url_direct("https://example.com/index.json")

    def test_redirect_to_https_accepted(self, monkeypatch):
        """HTTPS->HTTPS redirect is fine."""
        mock_resp = _mock_response(200, json_body=_AGENT_SKILLS_INDEX)
        mock_resp.url = "https://cdn.example.com/index.json"
        monkeypatch.setattr(
            "apm_cli.marketplace.client.requests.get",
            lambda *a, **kw: mock_resp,
        )
        result = _fetch_url_direct("https://example.com/index.json")
        assert isinstance(result, FetchResult)


# ---------------------------------------------------------------------------
# Response size limit -- C18
# ---------------------------------------------------------------------------


class TestFetchUrlDirectSizeLimit:
    """_fetch_url_direct must reject oversized responses."""

    def test_content_length_over_limit_raises(self, monkeypatch):
        mock_resp = _mock_response_with_headers(
            200, json_body={"skills": []},
            response_headers={"Content-Length": str(11 * 1024 * 1024)},
        )
        monkeypatch.setattr(
            "apm_cli.marketplace.client.requests.get",
            lambda *a, **kw: mock_resp,
        )
        with pytest.raises(MarketplaceFetchError, match="size limit"):
            _fetch_url_direct("https://example.com/index.json")

    def test_content_length_under_limit_succeeds(self, monkeypatch):
        mock_resp = _mock_response_with_headers(
            200, json_body={"skills": []},
            response_headers={"Content-Length": "1024"},
        )
        monkeypatch.setattr(
            "apm_cli.marketplace.client.requests.get",
            lambda *a, **kw: mock_resp,
        )
        result = _fetch_url_direct("https://example.com/index.json")
        assert isinstance(result, FetchResult)

    def test_body_over_limit_without_content_length_raises(self, monkeypatch):
        import unittest.mock as mock

        oversized_body = b"x" * (11 * 1024 * 1024)
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://example.com/index.json"
        mock_resp.headers = {}
        mock_resp.content = oversized_body
        mock_resp.raise_for_status.return_value = None
        monkeypatch.setattr(
            "apm_cli.marketplace.client.requests.get",
            lambda *a, **kw: mock_resp,
        )
        with pytest.raises(MarketplaceFetchError, match="size limit"):
            _fetch_url_direct("https://example.com/index.json")
