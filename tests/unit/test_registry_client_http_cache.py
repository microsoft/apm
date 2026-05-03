"""Tests for the HTTP cache integration in :class:`SimpleRegistryClient`."""

from __future__ import annotations

from unittest import mock

import pytest

from apm_cli.registry.client import SimpleRegistryClient


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Point the cache at a temp dir so tests don't pollute the user cache."""
    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("APM_NO_CACHE", raising=False)
    yield tmp_path


def _mock_response(*, body: bytes, headers: dict[str, str] | None = None, status: int = 200):
    resp = mock.Mock()
    resp.status_code = status
    resp.content = body
    resp.json.return_value = {"servers": [], "metadata": {}}
    resp.raise_for_status.return_value = None
    resp.headers = headers or {}
    return resp


class TestRegistryHttpCache:
    """Verify cached GETs reuse responses across calls."""

    def test_fresh_cache_hit_skips_network(self, isolated_cache):
        """A second list_servers() call within TTL must not hit the network."""
        client = SimpleRegistryClient("https://api.mcp.github.com")
        body = b'{"servers": [{"name": "a"}], "metadata": {}}'
        resp = _mock_response(body=body, headers={"Cache-Control": "max-age=3600"})

        with mock.patch.object(client.session, "get", return_value=resp) as mocked:
            client.list_servers()
            client.list_servers()  # should be served from cache

        assert mocked.call_count == 1, "expected second call to be cache-served"

    def test_etag_revalidation_on_304_reuses_body(self, isolated_cache):
        """When the cache is expired but server returns 304, the cached body is returned."""
        client = SimpleRegistryClient("https://api.mcp.github.com")
        body = b'{"servers": [{"name": "etag"}], "metadata": {}}'

        first = _mock_response(
            body=body,
            headers={"ETag": '"abc123"', "Cache-Control": "max-age=0"},
        )
        not_modified = _mock_response(body=b"", headers={"ETag": '"abc123"'}, status=304)

        with mock.patch.object(client.session, "get", side_effect=[first, not_modified]) as mocked:
            client.list_servers()
            client.list_servers()

        # Second call must include the conditional header
        assert mocked.call_count == 2
        second_call_kwargs = mocked.call_args_list[1].kwargs
        assert second_call_kwargs.get("headers", {}).get("If-None-Match") == '"abc123"'

    def test_apm_no_cache_disables_caching(self, isolated_cache, monkeypatch):
        """APM_NO_CACHE must keep the registry client on a strict network path."""
        monkeypatch.setenv("APM_NO_CACHE", "1")
        client = SimpleRegistryClient("https://api.mcp.github.com")
        body = b'{"servers": [], "metadata": {}}'

        with mock.patch.object(
            client.session, "get", return_value=_mock_response(body=body)
        ) as mocked:
            client.list_servers()
            client.list_servers()

        assert mocked.call_count == 2, "APM_NO_CACHE must bypass the cache"
