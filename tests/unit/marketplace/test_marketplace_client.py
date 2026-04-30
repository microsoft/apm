"""Tests for marketplace client -- HTTP mock, caching, TTL, auth, auto-detection, proxy."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.marketplace import client as client_mod
from apm_cli.marketplace.errors import MarketplaceFetchError
from apm_cli.marketplace.models import MarketplaceSource


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Point cache and config to temp directories."""
    config_dir = str(tmp_path / ".apm")
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
    yield


def _make_source(name="acme"):
    return MarketplaceSource(name=name, owner="acme-org", repo="plugins")


class TestCache:
    """Cache read/write with TTL."""

    def test_write_and_read(self, tmp_path):
        data = {"name": "Test", "plugins": []}
        client_mod._write_cache("test-mkt", data)

        cached = client_mod._read_cache("test-mkt")
        assert cached is not None
        assert cached["name"] == "Test"

    def test_expired_cache(self, tmp_path, monkeypatch):
        data = {"name": "Test", "plugins": []}
        client_mod._write_cache("test-mkt", data)

        # Make the cache appear old
        meta_path = client_mod._cache_meta_path("test-mkt")
        with open(meta_path, "w") as f:
            json.dump({"fetched_at": time.time() - 7200, "ttl_seconds": 3600}, f)

        assert client_mod._read_cache("test-mkt") is None

    def test_stale_cache_still_readable(self, tmp_path):
        data = {"name": "Stale", "plugins": []}
        client_mod._write_cache("test-mkt", data)

        # Make the cache appear old
        meta_path = client_mod._cache_meta_path("test-mkt")
        with open(meta_path, "w") as f:
            json.dump({"fetched_at": time.time() - 7200, "ttl_seconds": 3600}, f)

        stale = client_mod._read_stale_cache("test-mkt")
        assert stale is not None
        assert stale["name"] == "Stale"

    def test_clear_cache(self, tmp_path):
        data = {"name": "Test", "plugins": []}
        client_mod._write_cache("test-mkt", data)
        client_mod._clear_cache("test-mkt")
        assert client_mod._read_cache("test-mkt") is None

    def test_nonexistent_cache(self):
        assert client_mod._read_cache("nonexistent") is None
        assert client_mod._read_stale_cache("nonexistent") is None


class TestFetchMarketplace:
    """fetch_marketplace with mocked HTTP."""

    def test_fetch_from_network(self, tmp_path):
        source = _make_source()
        raw_data = {
            "name": "Acme Plugins",
            "plugins": [
                {"name": "tool-a", "repository": "acme-org/tool-a"},
            ],
        }
        mock_resolver = MagicMock()
        mock_resolver.try_with_fallback.return_value = raw_data
        mock_resolver.classify_host.return_value = MagicMock(api_base="https://api.github.com")

        manifest = client_mod.fetch_marketplace(
            source, force_refresh=True, auth_resolver=mock_resolver
        )
        assert manifest.name == "Acme Plugins"
        assert len(manifest.plugins) == 1

    def test_serves_from_cache(self, tmp_path):
        source = _make_source()
        raw_data = {
            "name": "Cached",
            "plugins": [{"name": "cached-tool", "repository": "o/r"}],
        }
        client_mod._write_cache(source.name, raw_data)

        # Should not hit network
        manifest = client_mod.fetch_marketplace(source)
        assert manifest.name == "Cached"
        assert len(manifest.plugins) == 1

    def test_force_refresh_bypasses_cache(self, tmp_path):
        source = _make_source()
        client_mod._write_cache(source.name, {"name": "Old", "plugins": []})

        new_data = {"name": "Fresh", "plugins": [{"name": "new", "repository": "o/r"}]}
        mock_resolver = MagicMock()
        mock_resolver.try_with_fallback.return_value = new_data
        mock_resolver.classify_host.return_value = MagicMock(api_base="https://api.github.com")

        manifest = client_mod.fetch_marketplace(
            source, force_refresh=True, auth_resolver=mock_resolver
        )
        assert manifest.name == "Fresh"

    def test_stale_while_revalidate(self, tmp_path):
        source = _make_source()
        stale_data = {"name": "Stale", "plugins": []}
        client_mod._write_cache(source.name, stale_data)

        # Expire the cache
        meta_path = client_mod._cache_meta_path(source.name)
        with open(meta_path, "w") as f:
            json.dump({"fetched_at": time.time() - 7200, "ttl_seconds": 3600}, f)

        # Network fetch will fail
        mock_resolver = MagicMock()
        mock_resolver.try_with_fallback.side_effect = Exception("Network error")
        mock_resolver.classify_host.return_value = MagicMock(api_base="https://api.github.com")

        manifest = client_mod.fetch_marketplace(source, auth_resolver=mock_resolver)
        assert manifest.name == "Stale"  # Falls back to stale cache

    def test_no_cache_no_network_raises(self, tmp_path):
        source = _make_source()
        mock_resolver = MagicMock()
        mock_resolver.try_with_fallback.side_effect = Exception("Network error")
        mock_resolver.classify_host.return_value = MagicMock(api_base="https://api.github.com")

        with pytest.raises(MarketplaceFetchError):
            client_mod.fetch_marketplace(source, force_refresh=True, auth_resolver=mock_resolver)


class TestAutoDetectPath:
    """Auto-detect marketplace.json location in a repo."""

    def test_found_at_root(self, tmp_path):
        source = _make_source()
        mock_resolver = MagicMock()

        def mock_fetch(host, op, org=None, unauth_first=False):
            # First probe: marketplace.json at root -- found
            return {"name": "Test", "plugins": []}

        mock_resolver.try_with_fallback.side_effect = mock_fetch
        mock_resolver.classify_host.return_value = MagicMock(api_base="https://api.github.com")

        path = client_mod._auto_detect_path(source, auth_resolver=mock_resolver)
        assert path == "marketplace.json"

    def test_found_at_github_plugin(self, tmp_path):
        source = _make_source()
        mock_resolver = MagicMock()
        call_count = [0]

        def mock_fetch(host, op, org=None, unauth_first=False):
            call_count[0] += 1
            if call_count[0] == 1:
                # First probe: root -- not found (404)
                return None
            # Second probe: .github/plugin/ -- found
            return {"name": "Test", "plugins": []}

        mock_resolver.try_with_fallback.side_effect = mock_fetch
        mock_resolver.classify_host.return_value = MagicMock(api_base="https://api.github.com")

        path = client_mod._auto_detect_path(source, auth_resolver=mock_resolver)
        assert path == ".github/plugin/marketplace.json"

    def test_not_found_anywhere(self, tmp_path):
        source = _make_source()
        mock_resolver = MagicMock()
        mock_resolver.try_with_fallback.return_value = None
        mock_resolver.classify_host.return_value = MagicMock(api_base="https://api.github.com")

        path = client_mod._auto_detect_path(source, auth_resolver=mock_resolver)
        assert path is None


class TestProxyAwareFetch:
    """Proxy-aware marketplace fetch via Artifactory Archive Entry Download."""

    _MARKETPLACE_JSON = {"name": "Test", "plugins": [{"name": "p1", "repository": "o/r"}]}  # noqa: RUF012

    def _make_cfg(self, enforce_only=False):
        cfg = MagicMock()
        cfg.host = "art.example.com"
        cfg.prefix = "artifactory/github"
        cfg.scheme = "https"
        cfg.enforce_only = enforce_only
        cfg.get_headers.return_value = {"Authorization": "Bearer tok"}
        return cfg

    def test_proxy_fetch_success(self):
        """Proxy returns valid JSON -- GitHub API is never called."""
        source = _make_source()
        cfg = self._make_cfg()
        raw = json.dumps(self._MARKETPLACE_JSON).encode()
        with (
            patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=cfg),
            patch(
                "apm_cli.deps.artifactory_entry.fetch_entry_from_archive", return_value=raw
            ) as mock_fetch,
        ):
            result = client_mod._fetch_file(source, "marketplace.json")

        assert result == self._MARKETPLACE_JSON
        mock_fetch.assert_called_once_with(
            host="art.example.com",
            prefix="artifactory/github",
            owner="acme-org",
            repo="plugins",
            file_path="marketplace.json",
            ref="main",
            scheme="https",
            headers={"Authorization": "Bearer tok"},
        )

    def test_proxy_none_falls_through_to_github(self):
        """Proxy returns None, no enforce_only -- falls through to GitHub API."""
        source = _make_source()
        cfg = self._make_cfg(enforce_only=False)
        with (
            patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=cfg),
            patch("apm_cli.deps.artifactory_entry.fetch_entry_from_archive", return_value=None),
        ):
            mock_resolver = MagicMock()
            mock_resolver.try_with_fallback.return_value = self._MARKETPLACE_JSON
            mock_resolver.classify_host.return_value = MagicMock(api_base="https://api.github.com")
            result = client_mod._fetch_file(source, "marketplace.json", auth_resolver=mock_resolver)

        assert result == self._MARKETPLACE_JSON
        mock_resolver.try_with_fallback.assert_called_once()

    def test_proxy_only_blocks_github_fallback(self):
        """Proxy returns None + enforce_only -- returns None, no GitHub call."""
        source = _make_source()
        cfg = self._make_cfg(enforce_only=True)
        with (
            patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=cfg),
            patch("apm_cli.deps.artifactory_entry.fetch_entry_from_archive", return_value=None),
        ):
            mock_resolver = MagicMock()
            result = client_mod._fetch_file(source, "marketplace.json", auth_resolver=mock_resolver)

        assert result is None
        mock_resolver.try_with_fallback.assert_not_called()

    def test_no_proxy_uses_github(self):
        """No proxy configured -- standard GitHub API path."""
        source = _make_source()
        with patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=None):
            mock_resolver = MagicMock()
            mock_resolver.try_with_fallback.return_value = self._MARKETPLACE_JSON
            mock_resolver.classify_host.return_value = MagicMock(api_base="https://api.github.com")
            result = client_mod._fetch_file(source, "marketplace.json", auth_resolver=mock_resolver)

        assert result == self._MARKETPLACE_JSON

    def test_proxy_non_json_falls_through(self):
        """Proxy returns non-JSON bytes -- treated as failure, falls to GitHub."""
        source = _make_source()
        cfg = self._make_cfg(enforce_only=False)
        with (
            patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=cfg),
            patch(
                "apm_cli.deps.artifactory_entry.fetch_entry_from_archive",
                return_value=b"\x89PNG binary",
            ),
        ):
            mock_resolver = MagicMock()
            mock_resolver.try_with_fallback.return_value = self._MARKETPLACE_JSON
            mock_resolver.classify_host.return_value = MagicMock(api_base="https://api.github.com")
            result = client_mod._fetch_file(source, "marketplace.json", auth_resolver=mock_resolver)

        assert result == self._MARKETPLACE_JSON

    def test_auto_detect_through_proxy(self):
        """Auto-detect probes candidate paths through the proxy."""
        source = _make_source()
        cfg = self._make_cfg()
        call_count = [0]

        def mock_entry(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 1:
                return None  # first candidate not found
            return json.dumps(self._MARKETPLACE_JSON).encode()

        mock_resolver = MagicMock()
        mock_resolver.try_with_fallback.return_value = None
        with (
            patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=cfg),
            patch(
                "apm_cli.deps.artifactory_entry.fetch_entry_from_archive", side_effect=mock_entry
            ),
        ):
            path = client_mod._auto_detect_path(source, auth_resolver=mock_resolver)

        assert path == ".github/plugin/marketplace.json"

    def test_fetch_marketplace_via_proxy_end_to_end(self):
        """Full fetch_marketplace through proxy -- parses manifest correctly."""
        source = _make_source()
        cfg = self._make_cfg()
        raw = json.dumps(self._MARKETPLACE_JSON).encode()
        with (
            patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=cfg),
            patch("apm_cli.deps.artifactory_entry.fetch_entry_from_archive", return_value=raw),
        ):
            manifest = client_mod.fetch_marketplace(source, force_refresh=True)

        assert manifest.name == "Test"
        assert len(manifest.plugins) == 1
        assert manifest.plugins[0].name == "p1"


@patch("apm_cli.marketplace.client._try_proxy_fetch", return_value=None)
class TestPrivateRepoAuth:
    """Verify unauth_first=False so private repos get credentials before unauthenticated fallback.

    GitHub returns 404 (not 403) for unauthenticated requests to private repos.
    With unauth_first=True the old code would try unauthenticated first, receive a 404, and
    silently treat the repo as non-existent.  The fix sets unauth_first=False so the token
    is used on the first attempt.
    """

    _MARKETPLACE_JSON = {"name": "Private Plugins", "plugins": []}  # noqa: RUF012

    def test_fetch_file_private_repo_auth_first(self, _proxy):
        """_fetch_file passes unauth_first=False so private repos are reached via auth first."""
        source = _make_source()
        with patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=None):
            mock_resolver = MagicMock()
            mock_resolver.try_with_fallback.return_value = self._MARKETPLACE_JSON
            mock_resolver.classify_host.return_value = MagicMock(api_base="https://api.github.com")

            result = client_mod._fetch_file(source, "marketplace.json", auth_resolver=mock_resolver)

        assert result == self._MARKETPLACE_JSON
        mock_resolver.try_with_fallback.assert_called_once()
        _, call_kwargs = mock_resolver.try_with_fallback.call_args
        assert call_kwargs.get("unauth_first") is False, (
            "unauth_first must be False -- private repos respond 404 to unauthenticated requests"
        )

    def test_fetch_file_no_proxy_passes_unauth_first_false(self, _proxy):
        """With no proxy, try_with_fallback is explicitly called with unauth_first=False (not True)."""
        source = _make_source()
        with patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=None):
            mock_resolver = MagicMock()
            # Simulate private repo returning None (404) for unauthenticated; would succeed with auth
            mock_resolver.try_with_fallback.return_value = None
            mock_resolver.classify_host.return_value = MagicMock(api_base="https://api.github.com")

            client_mod._fetch_file(source, "marketplace.json", auth_resolver=mock_resolver)

        mock_resolver.try_with_fallback.assert_called_once()
        call_kwargs = mock_resolver.try_with_fallback.call_args.kwargs
        assert "unauth_first" in call_kwargs, (
            "unauth_first kwarg must be passed explicitly to try_with_fallback"
        )
        assert call_kwargs["unauth_first"] is False, (
            f"Expected unauth_first=False, got {call_kwargs['unauth_first']!r}"
        )

    def test_auto_detect_private_repo_succeeds_with_auth(self, _proxy):
        """_auto_detect_path finds a private repo's manifest via auth on the third candidate path."""
        source = _make_source()
        call_count = [0]

        def mock_try_with_fallback(host, op, org=None, unauth_first=False):
            call_count[0] += 1
            if call_count[0] < 3:
                # marketplace.json and .github/plugin/marketplace.json: 404 on private repo
                return None
            # .claude-plugin/marketplace.json: found with auth
            return self._MARKETPLACE_JSON

        mock_resolver = MagicMock()
        mock_resolver.try_with_fallback.side_effect = mock_try_with_fallback
        mock_resolver.classify_host.return_value = MagicMock(api_base="https://api.github.com")

        with patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=None):
            path = client_mod._auto_detect_path(source, auth_resolver=mock_resolver)

        assert path == ".claude-plugin/marketplace.json"
        # All three candidates were probed before finding it on the third
        assert mock_resolver.try_with_fallback.call_count == 3
        # Every probe used unauth_first=False (auth credentials always tried first)
        for call in mock_resolver.try_with_fallback.call_args_list:
            assert call.kwargs.get("unauth_first") is False, (
                f"Expected unauth_first=False for all probes, got {call.kwargs!r}"
            )


class TestCacheKey:
    """Cache key includes host for non-github.com sources."""

    def test_github_default_unchanged(self):
        source = MarketplaceSource(name="skills", owner="o", repo="r")
        assert client_mod._cache_key(source) == "skills"

    def test_non_default_host_includes_host(self):
        source = MarketplaceSource(name="skills", owner="o", repo="r", host="ghes.corp.com")
        key = client_mod._cache_key(source)
        assert key.startswith("ghes.corp.com") or key.startswith("ghes_corp_com")
        assert key.endswith("skills")
        assert key != "skills"

    def test_different_hosts_different_keys(self):
        s1 = MarketplaceSource(name="mkt", owner="o", repo="r", host="a.com")
        s2 = MarketplaceSource(name="mkt", owner="o", repo="r", host="b.com")
        assert client_mod._cache_key(s1) != client_mod._cache_key(s2)
