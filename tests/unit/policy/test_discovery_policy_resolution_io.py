"""Unit tests for apm_cli.policy.discovery (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
import requests

from apm_cli.policy.discovery import (
    PolicyFetchResult,
    _derive_leaf_host,
    _detect_garbage,
    _fetch_from_repo,
    _fetch_from_url,
    _read_cache_entry,
    _resolve_and_persist_chain,
    _write_cache,
    discover_policy,
)
from apm_cli.policy.parser import load_policy
from apm_cli.policy.schema import ApmPolicy

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-cached-policy-shape", "P-OWN-policy-mcp-transport-projection"),
]

VALID_POLICY_YAML = "name: test-policy\nversion: '1.0'\nenforcement: warn\n"


def _make_test_policy(yaml_str: str = VALID_POLICY_YAML) -> ApmPolicy:
    policy, _ = load_policy(yaml_str)
    return policy


# ---------------------------------------------------------------------------
# _split_hash_pin
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _compute_hash_normalized
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _verify_hash_pin
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _derive_leaf_host
# ---------------------------------------------------------------------------


class TestDeriveLeafHost:
    def test_empty_source_returns_none(self, tmp_path: Path) -> None:
        with patch("apm_cli.policy.discovery._extract_org_from_git_remote", return_value=None):
            result = _derive_leaf_host("", tmp_path)
        assert result is None

    def test_url_prefix_parses_hostname(self, tmp_path: Path) -> None:
        result = _derive_leaf_host("url:https://example.com/policy.yml", tmp_path)
        assert result == "example.com"

    def test_bare_https_parses_hostname(self, tmp_path: Path) -> None:
        result = _derive_leaf_host("https://policy.example.org/p.yml", tmp_path)
        assert result == "policy.example.org"

    def test_org_shorthand_returns_github_com(self, tmp_path: Path) -> None:
        result = _derive_leaf_host("org:owner/repo", tmp_path)
        assert result == "github.com"

    def test_org_three_part_returns_host(self, tmp_path: Path) -> None:
        result = _derive_leaf_host("org:myghe.com/owner/repo", tmp_path)
        assert result == "myghe.com"

    def test_file_source_falls_back_to_git_remote(self, tmp_path: Path) -> None:
        # Use a relative path without slashes so code falls through to git remote
        with patch(
            "apm_cli.policy.discovery._extract_org_from_git_remote",
            return_value=("org", "github.example.com"),
        ):
            result = _derive_leaf_host("file:policy.yml", tmp_path)
        assert result == "github.example.com"

    def test_file_source_no_git_remote_returns_none(self, tmp_path: Path) -> None:
        with patch("apm_cli.policy.discovery._extract_org_from_git_remote", return_value=None):
            result = _derive_leaf_host("file:policy.yml", tmp_path)
        assert result is None

    def test_url_parse_exception_returns_none(self, tmp_path: Path) -> None:
        """If urlparse blows up, returns None rather than crashing."""
        with patch("apm_cli.policy.discovery.urlparse", side_effect=Exception("boom")):
            result = _derive_leaf_host("url:https://bad.url/p.yml", tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# _extract_extends_host
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_extends_host
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_ado_parent_ref
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_and_persist_chain -- chain length == 1 (lines 484-491)
# ---------------------------------------------------------------------------


class TestResolveAndPersistChain:
    def test_single_policy_no_extends_returns_immediately(self, tmp_path: Path) -> None:
        """Policy without extends: -> no-op (chain_policies stays at 1)."""
        policy = _make_test_policy()  # no 'extends' field
        fetch_result = PolicyFetchResult(policy=policy, source="org:owner/.github", outcome="found")
        original_policy = fetch_result.policy

        with patch("apm_cli.utils.console._rich_warning") as mock_warn:
            _resolve_and_persist_chain(fetch_result, tmp_path)

        mock_warn.assert_not_called()
        assert fetch_result.policy is original_policy

    def test_single_policy_parent_fetch_fails_closed(self, tmp_path: Path) -> None:
        """Parent fetch failure clears the weaker leaf policy."""

        extends_yaml = "name: leaf\nversion: '1.0'\nenforcement: warn\nextends: owner/.parent\n"
        leaf_policy, _ = load_policy(extends_yaml)
        fetch_result = PolicyFetchResult(
            policy=leaf_policy, source="org:owner/.github", outcome="found"
        )

        failed_parent = PolicyFetchResult(source="org:owner/.parent", outcome="absent")
        failed_parent.policy = None

        with patch("apm_cli.policy.discovery.discover_policy", return_value=failed_parent):
            _resolve_and_persist_chain(fetch_result, tmp_path)

        assert fetch_result.policy is None
        assert fetch_result.outcome == "incomplete_chain"
        assert "owner/.parent" in (fetch_result.error or "")


# ---------------------------------------------------------------------------
# discover_policy -- http:// rejection (line 552)
# ---------------------------------------------------------------------------


class TestDiscoverPolicy:
    def test_http_url_rejected(self, tmp_path: Path) -> None:
        result = discover_policy(tmp_path, policy_override="http://insecure.example.com/p.yml")
        assert result.error is not None
        assert "http" in result.error.lower() or "plaintext" in result.error.lower()

    def test_https_url_is_accepted(self, tmp_path: Path) -> None:
        """https:// override routes to _fetch_from_url (mocked)."""
        mock_result = PolicyFetchResult(outcome="found", source="url:https://example.com/p.yml")
        with patch("apm_cli.policy.discovery._fetch_from_url", return_value=mock_result) as mock_fn:
            result = discover_policy(tmp_path, policy_override="https://example.com/p.yml")
        mock_fn.assert_called_once()
        assert result is mock_result


# ---------------------------------------------------------------------------
# _parse_remote_url -- edge cases (lines 688, 694-695, 706-707)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _fetch_from_url -- cache hit / redirect / timeout / connection error
# ---------------------------------------------------------------------------


class TestFetchFromUrl:
    def test_cache_hit_returns_cached(self, tmp_path: Path) -> None:
        from apm_cli.policy.discovery import _CacheEntry

        policy = _make_test_policy()
        cached = _CacheEntry(
            policy=policy,
            source="url:https://example.com/p.yml",
            age_seconds=60,
            stale=False,
        )
        with patch("apm_cli.policy.discovery._read_cache_entry", return_value=cached):
            result = _fetch_from_url("https://example.com/p.yml", tmp_path)
        assert result.cached is True
        assert result.outcome in ("found", "empty")

    def test_redirect_refused(self, tmp_path: Path) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 301
        mock_resp.headers = {"Location": "https://other.com/p.yml"}

        with patch("apm_cli.policy.discovery.policy_get", return_value=mock_resp):
            result = _fetch_from_url("https://example.com/p.yml", tmp_path, no_cache=True)
        assert result.error is not None or result.fetch_error is not None

    def test_timeout_returns_error(self, tmp_path: Path) -> None:
        with patch(
            "apm_cli.policy.discovery.policy_get", side_effect=requests.exceptions.Timeout()
        ):
            result = _fetch_from_url("https://example.com/p.yml", tmp_path, no_cache=True)
        assert result.error is not None or result.fetch_error is not None

    def test_connection_error_returns_error(self, tmp_path: Path) -> None:
        with patch(
            "apm_cli.policy.discovery.policy_get", side_effect=requests.exceptions.ConnectionError()
        ):
            result = _fetch_from_url("https://example.com/p.yml", tmp_path, no_cache=True)
        assert result.error is not None or result.fetch_error is not None

    def test_404_returns_absent(self, tmp_path: Path) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("apm_cli.policy.discovery.policy_get", return_value=mock_resp):
            result = _fetch_from_url("https://example.com/p.yml", tmp_path, no_cache=True)
        assert result.outcome == "absent"

    def test_garbage_response_handled(self, tmp_path: Path) -> None:
        """Non-YAML body -> garbage_response outcome (or cached_stale if cache exists)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>captive portal</html>"
        with patch("apm_cli.policy.discovery.policy_get", return_value=mock_resp):
            result = _fetch_from_url("https://example.com/p.yml", tmp_path, no_cache=True)
        assert result.outcome in ("garbage_response", "cached_stale", "malformed")

    def test_hash_mismatch_returns_hash_mismatch(self, tmp_path: Path) -> None:
        """Mismatch between actual hash and expected pin -> hash_mismatch."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = VALID_POLICY_YAML
        with patch("apm_cli.policy.discovery.policy_get", return_value=mock_resp):
            result = _fetch_from_url(
                "https://example.com/p.yml",
                tmp_path,
                no_cache=True,
                expected_hash="sha256:" + "a" * 64,
            )
        assert result.outcome == "hash_mismatch"

    def test_non_200_error(self, tmp_path: Path) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("apm_cli.policy.discovery.policy_get", return_value=mock_resp):
            result = _fetch_from_url("https://example.com/p.yml", tmp_path, no_cache=True)
        assert result.error is not None or result.fetch_error is not None

    def test_authenticated_url_is_dispatched_raw_but_never_persisted_or_reported(
        self,
        tmp_path: Path,
    ) -> None:
        raw_url = "https://alice:s3cr3t@policy.example.com/apm-policy.yml?sig=private-signature"
        mock_resp = MagicMock(status_code=200, text=VALID_POLICY_YAML)

        with patch("apm_cli.policy.discovery.policy_get", return_value=mock_resp) as mock_get:
            result = _fetch_from_url(raw_url, tmp_path, no_cache=True)

        dispatched = urlparse(mock_get.call_args.args[0])
        assert dispatched.username == "alice"
        assert dispatched.password == "s3cr3t"
        assert dispatched.query == "sig=private-signature"

        reported = urlparse(result.source.removeprefix("url:"))
        assert reported.username is None
        assert reported.password is None
        assert reported.query == ""
        assert reported.hostname == "policy.example.com"
        assert reported.path == "/apm-policy.yml"
        assert "s3cr3t" not in result.source
        assert "private-signature" not in result.source

        entry = _read_cache_entry(raw_url, tmp_path)
        assert entry is not None
        assert entry.policy.name == "test-policy"


# ---------------------------------------------------------------------------
# _fetch_from_repo -- stale fallback / garbage (lines 850, 860)
# ---------------------------------------------------------------------------


class TestFetchFromRepo:
    def test_fetch_error_with_stale_cache_returns_stale(self, tmp_path: Path) -> None:
        from apm_cli.policy.discovery import _CacheEntry

        policy = _make_test_policy()
        stale_entry = _CacheEntry(
            policy=policy,
            source="org:owner/.github",
            age_seconds=7200,
            stale=True,
        )
        with (
            patch("apm_cli.policy.discovery._read_cache_entry", return_value=stale_entry),
            patch(
                "apm_cli.policy.discovery._fetch_github_contents",
                return_value=(None, "connection error"),
            ),
        ):
            result = _fetch_from_repo("owner/.github", tmp_path, no_cache=True)
        # Should fall back to stale cache
        assert result.cached is True or result.outcome == "cache_miss_fetch_fail"

    def test_garbage_body_returns_garbage_outcome(self, tmp_path: Path) -> None:
        with (
            patch("apm_cli.policy.discovery._read_cache_entry", return_value=None),
            patch(
                "apm_cli.policy.discovery._fetch_github_contents",
                return_value=("<html>portal</html>", None),
            ),
        ):
            result = _fetch_from_repo("owner/.github", tmp_path, no_cache=True)
        assert result.outcome in ("garbage_response", "cached_stale", "malformed")


# ---------------------------------------------------------------------------
# _fetch_github_contents -- 403, redirect, non-200, timeout, connection error
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _is_github_host
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _detect_garbage -- YAML error with cache_entry (line 1165)
# ---------------------------------------------------------------------------


class TestDetectGarbage:
    def test_yaml_error_with_cache_returns_cached_stale(self, tmp_path: Path) -> None:
        from apm_cli.policy.discovery import _CacheEntry

        policy = _make_test_policy()
        cache_entry = _CacheEntry(
            policy=policy,
            source="org:owner/.github",
            age_seconds=3600,
            stale=True,
        )
        result = _detect_garbage(
            "{{invalid: yaml: :", "owner/.github", "org:owner/.github", cache_entry
        )
        assert result is not None
        assert result.outcome == "cached_stale"

    def test_yaml_error_no_cache_returns_garbage_response(self) -> None:
        result = _detect_garbage("{{invalid yaml", "owner/.github", "org:owner/.github", None)
        assert result is not None
        assert result.outcome == "garbage_response"

    def test_valid_yaml_mapping_returns_none(self) -> None:
        result = _detect_garbage(VALID_POLICY_YAML, "owner/.github", "org:owner/.github", None)
        assert result is None

    def test_none_content_returns_none(self) -> None:
        result = _detect_garbage(None, "owner/.github", "org:owner/.github", None)
        assert result is None


# ---------------------------------------------------------------------------
# _read_cache_entry -- expected_hash checks (lines 1251-1257)
# ---------------------------------------------------------------------------


class TestReadCacheEntryExpectedHash:
    def _write_cache_for_test(self, tmp_path: Path, raw_bytes_hash: str = "") -> str:
        """Write a minimal cache entry and return the repo_ref used."""
        repo_ref = "owner/.github"
        policy = _make_test_policy()
        _write_cache(repo_ref, policy, tmp_path, raw_bytes_hash=raw_bytes_hash)
        return repo_ref

    def test_expected_hash_matches_cache(self, tmp_path: Path) -> None:
        content = VALID_POLICY_YAML
        digest = hashlib.sha256(content.encode()).hexdigest()
        raw_hash = f"sha256:{digest}"
        repo_ref = self._write_cache_for_test(tmp_path, raw_bytes_hash=raw_hash)

        # Rewind cache TTL so it's fresh
        entry = _read_cache_entry(repo_ref, tmp_path, expected_hash=raw_hash)
        assert entry is not None

    def test_expected_hash_mismatch_returns_none(self, tmp_path: Path) -> None:
        content = VALID_POLICY_YAML
        digest = hashlib.sha256(content.encode()).hexdigest()
        raw_hash = f"sha256:{digest}"
        repo_ref = self._write_cache_for_test(tmp_path, raw_bytes_hash=raw_hash)

        wrong_pin = "sha256:" + "0" * 64
        entry = _read_cache_entry(repo_ref, tmp_path, expected_hash=wrong_pin)
        assert entry is None

    def test_invalid_expected_hash_returns_none(self, tmp_path: Path) -> None:
        repo_ref = self._write_cache_for_test(tmp_path)
        entry = _read_cache_entry(repo_ref, tmp_path, expected_hash="sha256:NOTVALID")
        assert entry is None


# ---------------------------------------------------------------------------
# _write_cache -- tmp_meta OSError + unlink path (lines 1361-1363)
# ---------------------------------------------------------------------------


class TestWriteCacheOsError:
    def test_policy_write_os_error_returns_gracefully(self, tmp_path: Path) -> None:
        """OSError writing policy file -> returns without raising."""
        policy = _make_test_policy()
        with patch("pathlib.Path.write_text", side_effect=OSError("no space")):
            _write_cache("owner/.github", policy, tmp_path)
        # No exception raised

    def test_meta_write_os_error_returns_gracefully(self, tmp_path: Path) -> None:
        """OSError writing meta sidecar after policy write -> returns without raising."""
        policy = _make_test_policy()
        call_count = [0]
        original_write = Path.write_text

        def selective_fail(self, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] > 1:
                raise OSError("no space on meta write")
            return original_write(self, *args, **kwargs)

        with patch.object(Path, "write_text", selective_fail):
            _write_cache("owner/.github", policy, tmp_path)
        # No exception raised
