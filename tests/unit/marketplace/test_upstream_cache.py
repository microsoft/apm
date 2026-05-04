"""Unit tests for ``apm_cli.marketplace.upstream_cache``.

Locks the cache-key contract (Windows-safe, hashed, delimiter-rejecting),
the integrity-check semantics (poisoned entries treated as miss),
the per-upstream-host auth path (no curator-PAT leakage), and the
classify-host defence-in-depth guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.marketplace.upstream_cache import (
    UpstreamCache,
    UpstreamCacheError,
    UpstreamCacheKey,
    compute_cache_key,
)

# Reusable, valid sample inputs. 40-char hex SHA.
GOOD_SHA = "a" * 40
GOOD_SHA_2 = "b" * 40
GOOD_INPUTS = {
    "host": "github.com",
    "owner": "abhigyanpatwari",
    "repo": "GitNexus",
    "sha": GOOD_SHA,
    "path": ".claude-plugin/marketplace.json",
}


# ---------------------------------------------------------------------------
# compute_cache_key validation
# ---------------------------------------------------------------------------


class TestComputeCacheKey:
    def test_happy_path(self):
        key = compute_cache_key(**GOOD_INPUTS)
        assert isinstance(key, UpstreamCacheKey)
        assert key.host == "github.com"
        assert key.owner == "abhigyanpatwari"
        assert key.repo == "GitNexus"
        assert key.sha == GOOD_SHA
        assert key.path == ".claude-plugin/marketplace.json"

    def test_composite_uses_double_underscore_delim(self):
        key = compute_cache_key(**GOOD_INPUTS)
        # ``__`` between every segment, exactly 5 occurrences:
        # upstream__host__owner__repo__sha__path
        assert key.composite.count("__") == 5
        assert key.composite.startswith("upstream__")
        # Single underscores from internal names must survive.
        assert "abhigyanpatwari" in key.composite

    def test_directory_name_is_windows_safe(self):
        """The on-disk dir name must contain ZERO colons (NTFS-illegal)."""
        key = compute_cache_key(**GOOD_INPUTS)
        assert ":" not in key.directory_name
        # And must still be a single path segment (no slashes).
        assert "/" not in key.directory_name
        assert "\\" not in key.directory_name

    def test_fingerprint_is_16_hex_chars(self):
        key = compute_cache_key(**GOOD_INPUTS)
        assert len(key.fingerprint) == 16
        int(key.fingerprint, 16)  # must parse as hex

    def test_fingerprint_stable_across_calls(self):
        k1 = compute_cache_key(**GOOD_INPUTS)
        k2 = compute_cache_key(**GOOD_INPUTS)
        assert k1.fingerprint == k2.fingerprint
        assert k1.directory_name == k2.directory_name

    def test_different_sha_produces_different_fingerprint(self):
        k1 = compute_cache_key(**GOOD_INPUTS)
        k2 = compute_cache_key(**{**GOOD_INPUTS, "sha": GOOD_SHA_2})
        assert k1.fingerprint != k2.fingerprint

    def test_different_path_produces_different_fingerprint(self):
        k1 = compute_cache_key(**GOOD_INPUTS)
        k2 = compute_cache_key(**{**GOOD_INPUTS, "path": "claude-plugin/marketplace.json"})
        assert k1.fingerprint != k2.fingerprint

    @pytest.mark.parametrize(
        "field",
        ["host", "owner", "repo", "sha", "path"],
    )
    def test_rejects_double_underscore_in_input(self, field):
        bad = {**GOOD_INPUTS, field: GOOD_INPUTS[field][:-1] + "__bad"}
        with pytest.raises(UpstreamCacheError, match="cache delimiter"):
            compute_cache_key(**bad)

    @pytest.mark.parametrize("field", list(GOOD_INPUTS.keys()))
    def test_rejects_empty_input(self, field):
        bad = {**GOOD_INPUTS, field: ""}
        with pytest.raises(UpstreamCacheError):
            compute_cache_key(**bad)

    @pytest.mark.parametrize("field", list(GOOD_INPUTS.keys()))
    def test_rejects_none_input(self, field):
        bad = {**GOOD_INPUTS, field: None}
        with pytest.raises(UpstreamCacheError):
            compute_cache_key(**bad)

    def test_rejects_short_sha(self):
        with pytest.raises(UpstreamCacheError, match="full 40-char hex SHA"):
            compute_cache_key(**{**GOOD_INPUTS, "sha": "abc1234"})

    def test_rejects_uppercase_sha(self):
        with pytest.raises(UpstreamCacheError, match="full 40-char hex SHA"):
            compute_cache_key(**{**GOOD_INPUTS, "sha": "A" * 40})

    def test_rejects_absolute_path(self):
        with pytest.raises(UpstreamCacheError, match="must be non-empty and relative"):
            compute_cache_key(**{**GOOD_INPUTS, "path": "/etc/passwd"})

    def test_rejects_traversal_path(self):
        with pytest.raises(UpstreamCacheError):
            compute_cache_key(**{**GOOD_INPUTS, "path": "../../etc/passwd"})

    def test_rejects_invalid_owner_repo_with_leading_dot(self):
        with pytest.raises(UpstreamCacheError, match="invalid upstream owner/repo"):
            compute_cache_key(**{**GOOD_INPUTS, "owner": ".secret"})

    def test_rejects_invalid_host(self):
        with pytest.raises(UpstreamCacheError, match="invalid upstream host"):
            compute_cache_key(**{**GOOD_INPUTS, "host": "not a host"})


# ---------------------------------------------------------------------------
# UpstreamCache get / put / integrity check
# ---------------------------------------------------------------------------


class TestUpstreamCacheReadWrite:
    def test_miss_returns_none(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path)
        key = compute_cache_key(**GOOD_INPUTS)
        assert cache.get(key) is None

    def test_put_then_get_roundtrip(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path)
        key = compute_cache_key(**GOOD_INPUTS)
        manifest = {"name": "gitnexus", "plugins": [{"name": "gitnexus"}]}
        cache.put(key, manifest)
        assert cache.get(key) == manifest

    def test_put_writes_manifest_and_meta_files(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path)
        key = compute_cache_key(**GOOD_INPUTS)
        cache.put(key, {"name": "x"})
        entry_dir = tmp_path / key.directory_name
        assert (entry_dir / "manifest.json").exists()
        assert (entry_dir / "meta.json").exists()
        meta = json.loads((entry_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["sha"] == key.sha
        assert meta["host"] == key.host
        assert meta["owner"] == key.owner
        assert meta["repo"] == key.repo
        assert meta["path"] == key.path

    def test_integrity_mismatch_treated_as_miss(self, tmp_path: Path):
        """A poisoned meta.json with the wrong SHA is treated as cache miss."""
        cache = UpstreamCache(base_dir=tmp_path)
        key = compute_cache_key(**GOOD_INPUTS)
        cache.put(key, {"name": "x"})
        # Tamper with meta to simulate a poisoned entry / disk
        # corruption / older code that wrote an inconsistent record.
        meta_path = tmp_path / key.directory_name / "meta.json"
        meta_path.write_text(json.dumps({"sha": GOOD_SHA_2, "host": key.host}), encoding="utf-8")
        # Cache miss -- the recorded SHA does not match the requested SHA.
        assert cache.get(key) is None

    def test_corrupt_manifest_treated_as_miss(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path)
        key = compute_cache_key(**GOOD_INPUTS)
        cache.put(key, {"name": "x"})
        manifest_path = tmp_path / key.directory_name / "manifest.json"
        manifest_path.write_text("not json {{{", encoding="utf-8")
        assert cache.get(key) is None

    def test_corrupt_meta_treated_as_miss(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path)
        key = compute_cache_key(**GOOD_INPUTS)
        cache.put(key, {"name": "x"})
        meta_path = tmp_path / key.directory_name / "meta.json"
        meta_path.write_text("not json {{{", encoding="utf-8")
        assert cache.get(key) is None

    def test_different_keys_have_separate_directories(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path)
        k1 = compute_cache_key(**GOOD_INPUTS)
        k2 = compute_cache_key(**{**GOOD_INPUTS, "sha": GOOD_SHA_2})
        cache.put(k1, {"v": 1})
        cache.put(k2, {"v": 2})
        assert cache.get(k1) == {"v": 1}
        assert cache.get(k2) == {"v": 2}


# ---------------------------------------------------------------------------
# get_or_fetch behaviour
# ---------------------------------------------------------------------------


class TestGetOrFetch:
    def test_hit_does_not_call_fetch(self, tmp_path: Path):
        fetch = MagicMock()
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=fetch)
        key = compute_cache_key(**GOOD_INPUTS)
        cache.put(key, {"cached": True})
        result = cache.get_or_fetch(key)
        assert result == {"cached": True}
        fetch.assert_not_called()

    def test_miss_calls_fetch_and_caches(self, tmp_path: Path):
        fetch = MagicMock(return_value={"fetched": True})
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=fetch)
        key = compute_cache_key(**GOOD_INPUTS)
        result = cache.get_or_fetch(key)
        assert result == {"fetched": True}
        fetch.assert_called_once()
        # Second call hits cache.
        cache.get_or_fetch(key)
        fetch.assert_called_once()

    def test_offline_with_miss_raises(self, tmp_path: Path):
        fetch = MagicMock()
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=fetch)
        key = compute_cache_key(**GOOD_INPUTS)
        with pytest.raises(UpstreamCacheError, match="offline mode"):
            cache.get_or_fetch(key, offline=True)
        fetch.assert_not_called()

    def test_offline_with_hit_returns_cached(self, tmp_path: Path):
        fetch = MagicMock()
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=fetch)
        key = compute_cache_key(**GOOD_INPUTS)
        cache.put(key, {"cached": True})
        result = cache.get_or_fetch(key, offline=True)
        assert result == {"cached": True}
        fetch.assert_not_called()

    def test_fetch_returning_non_dict_raises(self, tmp_path: Path):
        fetch = MagicMock(return_value=["not", "a", "dict"])
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=fetch)
        key = compute_cache_key(**GOOD_INPUTS)
        with pytest.raises(UpstreamCacheError, match="non-JSON-object"):
            cache.get_or_fetch(key)


# ---------------------------------------------------------------------------
# Default fetch callback: classify-host guard + auth flow
# ---------------------------------------------------------------------------


class TestDefaultFetchAuthFlow:
    def test_non_github_host_refused_before_token_resolution(self, tmp_path: Path):
        """Defence-in-depth: never forward GitHub creds to a non-GitHub host."""
        # Use a host shape the validator accepts but classify_host rejects.
        # gitlab.com classifies as ``generic``.
        bad_inputs = {**GOOD_INPUTS, "host": "gitlab.com"}
        key = compute_cache_key(**bad_inputs)
        cache = UpstreamCache(base_dir=tmp_path)
        # Inject a sentinel auth_resolver -- the guard must trip BEFORE
        # any auth call is made.
        sentinel_auth = MagicMock()
        with pytest.raises(UpstreamCacheError, match="not a supported GitHub variant"):
            cache.get_or_fetch(key, auth_resolver=sentinel_auth)
        sentinel_auth.try_with_fallback.assert_not_called()
        sentinel_auth.resolve.assert_not_called()

    def test_github_fetch_uses_unauth_first(self, tmp_path: Path, monkeypatch):
        """Curator PAT must NOT be attached to a public-repo upstream fetch."""
        captured: dict = {}

        class FakeAuth:
            def try_with_fallback(self, host, op, *, org=None, port=None, unauth_first=False, **kw):
                captured["host"] = host
                captured["org"] = org
                captured["unauth_first"] = unauth_first
                # Simulate the unauth path: token=None, empty git env.
                return op(None, {})

        # Stub requests.get so the operation completes.
        class FakeResp:
            status_code = 200

            def json(self):
                return {"name": "x"}

            def raise_for_status(self):
                pass

        def fake_get(url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            return FakeResp()

        monkeypatch.setattr(
            "apm_cli.marketplace.upstream_cache.requests",
            type("R", (), {"get": staticmethod(fake_get)}),
            raising=False,
        )
        # Patch via the import path used inside the function.
        import requests

        monkeypatch.setattr(requests, "get", fake_get)

        cache = UpstreamCache(base_dir=tmp_path)
        key = compute_cache_key(**GOOD_INPUTS)
        result = cache.get_or_fetch(key, auth_resolver=FakeAuth())
        assert result == {"name": "x"}
        # unauth_first must be True (no curator PAT on public upstreams).
        assert captured["unauth_first"] is True
        # org passes the upstream owner, NOT the curator's repo owner.
        assert captured["org"] == "abhigyanpatwari"
        # No Authorization header attached when token is None.
        assert "Authorization" not in captured["headers"]
        # URL targets the GitHub Contents API at the pinned SHA.
        assert "/repos/abhigyanpatwari/GitNexus/contents/" in captured["url"]
        assert f"ref={GOOD_SHA}" in captured["url"]

    def test_github_fetch_attaches_token_when_provided(self, tmp_path: Path, monkeypatch):
        """When AuthResolver returns a token (e.g. private upstream, EMU), attach it."""
        captured: dict = {}

        class FakeAuth:
            def try_with_fallback(self, host, op, *, org=None, port=None, unauth_first=False, **kw):
                # Simulate the auth path: token attached.
                return op("ghp_secret_token", {"GIT_TERMINAL_PROMPT": "0"})

        class FakeResp:
            status_code = 200

            def json(self):
                return {"name": "x"}

            def raise_for_status(self):
                pass

        def fake_get(url, headers=None, timeout=None):
            captured["headers"] = headers or {}
            return FakeResp()

        import requests

        monkeypatch.setattr(requests, "get", fake_get)

        cache = UpstreamCache(base_dir=tmp_path)
        key = compute_cache_key(**GOOD_INPUTS)
        cache.get_or_fetch(key, auth_resolver=FakeAuth())
        assert captured["headers"].get("Authorization") == "token ghp_secret_token"

    def test_404_raises_named_error(self, tmp_path: Path, monkeypatch):
        class FakeAuth:
            def try_with_fallback(self, host, op, **kw):
                return op(None, {})

        class FakeResp:
            status_code = 404

            def raise_for_status(self):
                raise AssertionError("must not call raise_for_status on 404")

            def json(self):
                return {}

        def fake_get(url, headers=None, timeout=None):
            return FakeResp()

        import requests

        monkeypatch.setattr(requests, "get", fake_get)

        cache = UpstreamCache(base_dir=tmp_path)
        key = compute_cache_key(**GOOD_INPUTS)
        with pytest.raises(UpstreamCacheError, match="not found"):
            cache.get_or_fetch(key, auth_resolver=FakeAuth())
