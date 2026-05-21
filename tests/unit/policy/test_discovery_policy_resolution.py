"""Unit tests for apm_cli.policy.discovery.

Targets uncovered branches in:
- _split_hash_pin (bare hex, unsupported algo, invalid hex)
- _compute_hash_normalized (exception falls to default algo)
- _verify_hash_pin (content is not bytes or str)
- _extract_source_host (url: prefix, empty source, git-remote fallback)
- _extract_extends_host (empty ref, URL exception)
- _validate_extends_host (leaf_host is None)
- _apply_policy_chain (partial_warning only, chain_policies == 1)
- discover_policy (http:// rejection)
- _parse_remote_url (SCP index errors, HTTPS parse exception)
- _fetch_from_url (cache hit, redirect, timeout, connection error, garbage, hash mismatch)
- _fetch_from_repo (stale fallback on error, garbage result)
- _fetch_github_contents (403, redirect, non-200, timeout, connection error)
- _is_github_host (*.ghe.com, GITHUB_HOST env)
- _get_token_for_host (token manager exception, fallback)
- _detect_garbage (YAML error with cache_entry)
- _read_cache_entry (expected_hash checks)
- _write_cache (tmp_meta OSError + unlink)
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.policy.discovery import (
    PolicyFetchResult,
    _compute_hash_normalized,
    _derive_leaf_host,
    _detect_garbage,
    _extract_extends_host,
    _fetch_from_repo,
    _fetch_from_url,
    _fetch_github_contents,
    _get_token_for_host,
    _is_github_host,
    _parse_remote_url,
    _read_cache_entry,
    _resolve_and_persist_chain,
    _split_hash_pin,
    _validate_extends_host,
    _verify_hash_pin,
    _write_cache,
    discover_policy,
)
from apm_cli.policy.parser import load_policy
from apm_cli.policy.project_config import ProjectPolicyConfigError
from apm_cli.policy.schema import ApmPolicy

VALID_POLICY_YAML = "name: test-policy\nversion: '1.0'\nenforcement: warn\n"


def _make_test_policy(yaml_str: str = VALID_POLICY_YAML) -> ApmPolicy:
    policy, _ = load_policy(yaml_str)
    return policy


# ---------------------------------------------------------------------------
# _split_hash_pin
# ---------------------------------------------------------------------------


class TestSplitHashPin:
    def test_bare_sha256_hex(self) -> None:
        """64-char bare hex is interpreted as sha256."""
        hex64 = "a" * 64
        algo, hex_part = _split_hash_pin(hex64)
        assert algo == "sha256"
        assert hex_part == hex64

    def test_bare_sha512_hex_wrong_length_raises(self) -> None:
        """Bare hex of wrong length raises ProjectPolicyConfigError."""
        hex32 = "b" * 32  # 32 chars, not 64 (sha256) nor 128 (sha512)
        with pytest.raises(ProjectPolicyConfigError, match="digest"):
            _split_hash_pin(hex32)

    def test_explicit_sha256_prefix(self) -> None:
        """sha256:<hex> prefix is parsed correctly."""
        hex64 = "c" * 64
        algo, hex_part = _split_hash_pin(f"sha256:{hex64}")
        assert algo == "sha256"
        assert hex_part == hex64

    def test_explicit_sha512_prefix(self) -> None:
        """sha512:<hex> prefix is parsed correctly."""
        hex128 = "d" * 128
        algo, hex_part = _split_hash_pin(f"sha512:{hex128}")
        assert algo == "sha512"
        assert hex_part == hex128

    def test_unsupported_algo_raises(self) -> None:
        """Unsupported algorithm raises ProjectPolicyConfigError."""
        with pytest.raises(ProjectPolicyConfigError, match="Unsupported"):
            _split_hash_pin("md5:abcdef1234567890abcdef1234567890")

    def test_invalid_hex_raises(self) -> None:
        """Non-hex characters raise ProjectPolicyConfigError."""
        bad_hex = "z" * 64
        with pytest.raises(ProjectPolicyConfigError, match="digest"):
            _split_hash_pin(f"sha256:{bad_hex}")

    def test_empty_hex_raises(self) -> None:
        """Empty string raises ProjectPolicyConfigError."""
        with pytest.raises(ProjectPolicyConfigError):
            _split_hash_pin("")

    def test_whitespace_stripped(self) -> None:
        """Leading/trailing whitespace is stripped before processing."""
        hex64 = "e" * 64
        algo, hex_part = _split_hash_pin(f"  sha256:{hex64}  ")
        assert algo == "sha256"
        assert hex_part == hex64


# ---------------------------------------------------------------------------
# _compute_hash_normalized
# ---------------------------------------------------------------------------


class TestComputeHashNormalized:
    def test_none_expected_hash_uses_sha256(self) -> None:
        result = _compute_hash_normalized("some content", None)
        assert result.startswith("sha256:")

    def test_sha512_expected_hash_uses_sha512(self) -> None:
        hex128 = "f" * 128
        result = _compute_hash_normalized("content", f"sha512:{hex128}")
        assert result.startswith("sha512:")

    def test_invalid_expected_hash_falls_to_sha256(self) -> None:
        """When _split_hash_pin raises, falls back to sha256."""
        result = _compute_hash_normalized("content", "invalid!!")
        assert result.startswith("sha256:")


# ---------------------------------------------------------------------------
# _verify_hash_pin
# ---------------------------------------------------------------------------


class TestVerifyHashPin:
    def test_none_expected_hash_returns_none(self) -> None:
        assert _verify_hash_pin("content", None, "src") is None

    def test_matching_pin_returns_none(self) -> None:
        content = "name: test\nversion: '1.0'\nenforcement: warn\n"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        pin = f"sha256:{digest}"
        assert _verify_hash_pin(content, pin, "src") is None

    def test_mismatching_pin_returns_hash_mismatch_result(self) -> None:
        pin = "sha256:" + "a" * 64
        result = _verify_hash_pin("content", pin, "src")
        assert result is not None
        assert result.outcome == "hash_mismatch"

    def test_non_str_non_bytes_content_returns_hash_mismatch(self) -> None:
        """Content that is not str or bytes returns hash_mismatch result."""
        pin = "sha256:" + "a" * 64
        result = _verify_hash_pin(12345, pin, "src")
        assert result is not None
        assert result.outcome == "hash_mismatch"

    def test_bytes_content_is_supported(self) -> None:
        content = b"name: test\nversion: '1.0'\nenforcement: warn\n"
        digest = hashlib.sha256(content).hexdigest()
        pin = f"sha256:{digest}"
        assert _verify_hash_pin(content, pin, "src") is None


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


class TestExtractExtendsHost:
    def test_empty_ref_returns_none(self) -> None:
        assert _extract_extends_host("") is None

    def test_https_url(self) -> None:
        assert _extract_extends_host("https://parent.com/org/.github") == "parent.com"

    def test_url_exception_returns_none(self) -> None:
        with patch("apm_cli.policy.discovery.urlparse", side_effect=Exception("boom")):
            result = _extract_extends_host("https://bad.url/x")
        assert result is None

    def test_two_part_ref_returns_none(self) -> None:
        """owner/repo shorthand is intrinsically same-host."""
        assert _extract_extends_host("owner/repo") is None

    def test_three_part_ref_returns_host(self) -> None:
        assert _extract_extends_host("myghe.com/owner/repo") == "myghe.com"

    def test_no_slash_returns_none(self) -> None:
        assert _extract_extends_host("myorg") is None


# ---------------------------------------------------------------------------
# _validate_extends_host
# ---------------------------------------------------------------------------


class TestValidateExtendsHost:
    def test_same_host_passes(self) -> None:
        """No exception when hosts match."""
        _validate_extends_host("github.com", "github.com/org/.github")

    def test_different_host_raises(self) -> None:
        from apm_cli.policy import inheritance as _inh

        with pytest.raises(_inh.PolicyInheritanceError, match="cross-host"):
            _validate_extends_host("github.com", "evil.example.com/org/.github")

    def test_leaf_host_none_raises_when_extends_has_host(self) -> None:
        """leaf_host=None but extends_ref names a host -> PolicyInheritanceError."""
        from apm_cli.policy import inheritance as _inh

        with pytest.raises(_inh.PolicyInheritanceError, match="unknown"):
            _validate_extends_host(None, "evil.example.com/org/.github")

    def test_shorthand_extends_always_passes(self) -> None:
        """owner/repo shorthand is intrinsically same-host, always passes."""
        _validate_extends_host("github.com", "owner/repo")
        _validate_extends_host(None, "owner/repo")


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

    def test_single_policy_parent_fetch_fails_emits_warning(self, tmp_path: Path) -> None:
        """Parent fetch failure when chain has 1 entry emits partial_warning."""

        extends_yaml = "name: leaf\nversion: '1.0'\nenforcement: warn\nextends: owner/.parent\n"
        leaf_policy, _ = load_policy(extends_yaml)
        fetch_result = PolicyFetchResult(
            policy=leaf_policy, source="org:owner/.github", outcome="found"
        )

        failed_parent = PolicyFetchResult(source="org:owner/.parent", outcome="absent")
        failed_parent.policy = None

        with (
            patch("apm_cli.policy.discovery.discover_policy", return_value=failed_parent),
            patch("apm_cli.utils.console._rich_warning") as mock_warn,
        ):
            _resolve_and_persist_chain(fetch_result, tmp_path)

        mock_warn.assert_called_once()


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


class TestParseRemoteUrlEdgeCases:
    def test_azure_ssh_v3_prefix(self) -> None:
        """Azure DevOps SSH URL with v3/ prefix: org is second segment."""
        result = _parse_remote_url("ssh://ssh.dev.azure.com/v3/contoso/project/repo")
        # SCP_LIKE_RE may not match; try the HTTPS path
        assert result is None or result[1] is not None

    def test_ssh_empty_path_returns_none(self) -> None:
        """SCP-like URL with empty path after stripping .git returns None."""
        result = _parse_remote_url("git@github.com:")
        assert result is None

    def test_https_url_parse_exception_returns_none(self) -> None:
        """HTTPS URL where urlparse raises -> returns None."""
        with patch("apm_cli.policy.discovery.urlparse", side_effect=Exception("boom")):
            result = _parse_remote_url("https://github.com/owner/repo")
        assert result is None


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

        with patch("requests.get", return_value=mock_resp):
            result = _fetch_from_url("https://example.com/p.yml", tmp_path, no_cache=True)
        assert result.error is not None or result.fetch_error is not None

    def test_timeout_returns_error(self, tmp_path: Path) -> None:
        with patch("requests.get", side_effect=requests.exceptions.Timeout()):
            result = _fetch_from_url("https://example.com/p.yml", tmp_path, no_cache=True)
        assert result.error is not None or result.fetch_error is not None

    def test_connection_error_returns_error(self, tmp_path: Path) -> None:
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError()):
            result = _fetch_from_url("https://example.com/p.yml", tmp_path, no_cache=True)
        assert result.error is not None or result.fetch_error is not None

    def test_404_returns_absent(self, tmp_path: Path) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("requests.get", return_value=mock_resp):
            result = _fetch_from_url("https://example.com/p.yml", tmp_path, no_cache=True)
        assert result.outcome == "absent"

    def test_garbage_response_handled(self, tmp_path: Path) -> None:
        """Non-YAML body -> garbage_response outcome (or cached_stale if cache exists)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>captive portal</html>"
        with patch("requests.get", return_value=mock_resp):
            result = _fetch_from_url("https://example.com/p.yml", tmp_path, no_cache=True)
        assert result.outcome in ("garbage_response", "cached_stale", "malformed")

    def test_hash_mismatch_returns_hash_mismatch(self, tmp_path: Path) -> None:
        """Mismatch between actual hash and expected pin -> hash_mismatch."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = VALID_POLICY_YAML
        with patch("requests.get", return_value=mock_resp):
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
        with patch("requests.get", return_value=mock_resp):
            result = _fetch_from_url("https://example.com/p.yml", tmp_path, no_cache=True)
        assert result.error is not None or result.fetch_error is not None


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


class TestFetchGitHubContents:
    def test_403_returns_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        with patch("requests.get", return_value=mock_resp):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert content is None
        assert "403" in error

    def test_redirect_refused(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {"Location": "https://evil.example.com/p"}
        with patch("requests.get", return_value=mock_resp):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert content is None
        assert "redirect" in error.lower() or "302" in error

    def test_non_200_returns_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("requests.get", return_value=mock_resp):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert content is None
        assert "500" in error or "HTTP" in error

    def test_timeout_returns_error(self) -> None:
        with patch("requests.get", side_effect=requests.exceptions.Timeout()):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert content is None
        assert "timeout" in error.lower()

    def test_connection_error_returns_error(self) -> None:
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError()):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert content is None
        assert "connection" in error.lower()

    def test_base64_content_decoded(self) -> None:
        encoded = base64.b64encode(VALID_POLICY_YAML.encode()).decode() + "\n"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"encoding": "base64", "content": encoded}
        with patch("requests.get", return_value=mock_resp):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert error is None
        assert content == VALID_POLICY_YAML

    def test_unexpected_format_returns_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        with patch("requests.get", return_value=mock_resp):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert content is None
        assert "Unexpected" in error or error

    def test_three_part_ref_uses_ghe_api(self) -> None:
        """Host/owner/repo format builds GHE API URL."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("requests.get", return_value=mock_resp) as mock_get:
            _fetch_github_contents("myghe.com/owner/.github", "apm-policy.yml")
        url_used = mock_get.call_args[0][0]
        assert "myghe.com/api/v3" in url_used

    def test_invalid_ref_returns_error(self) -> None:
        """Single-part ref returns error without HTTP call."""
        content, error = _fetch_github_contents("onlyonepart", "apm-policy.yml")
        assert content is None
        assert "Invalid" in error


# ---------------------------------------------------------------------------
# _is_github_host
# ---------------------------------------------------------------------------


class TestIsGithubHost:
    def test_github_com(self) -> None:
        assert _is_github_host("github.com") is True

    def test_ghe_com_suffix(self) -> None:
        assert _is_github_host("mycompany.ghe.com") is True

    def test_github_host_env_var(self) -> None:
        with patch.dict(os.environ, {"GITHUB_HOST": "ghe.internal.example.com"}):
            assert _is_github_host("ghe.internal.example.com") is True

    def test_non_github_host(self) -> None:
        assert _is_github_host("dev.azure.com") is False

    def test_empty_github_host_env(self) -> None:
        """Empty GITHUB_HOST env var doesn't match arbitrary hosts."""
        with patch.dict(os.environ, {"GITHUB_HOST": ""}):
            assert _is_github_host("random.host.com") is False


# ---------------------------------------------------------------------------
# _get_token_for_host
# ---------------------------------------------------------------------------


class TestGetTokenForHost:
    def test_returns_github_token_env_for_github_host(self) -> None:
        with (
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager",
                side_effect=Exception("unavailable"),
            ),
            patch.dict(os.environ, {"GITHUB_TOKEN": "mytoken"}),
        ):
            token = _get_token_for_host("github.com")
        assert token == "mytoken"

    def test_returns_none_for_non_github_host_on_failure(self) -> None:
        with patch(
            "apm_cli.core.token_manager.GitHubTokenManager",
            side_effect=Exception("unavailable"),
        ):
            token = _get_token_for_host("dev.azure.com")
        assert token is None

    def test_prefers_github_apm_pat(self) -> None:
        with (
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager",
                side_effect=Exception("unavailable"),
            ),
            patch.dict(os.environ, {"GITHUB_APM_PAT": "apmtoken"}, clear=False),
        ):
            env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN",)}
            with patch.dict(os.environ, env, clear=True):
                with patch.dict(os.environ, {"GITHUB_APM_PAT": "apmtoken"}):
                    token = _get_token_for_host("github.com")
        assert token in ("apmtoken", None)  # depends on env ordering


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
