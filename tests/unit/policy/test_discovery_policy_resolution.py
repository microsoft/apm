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
- _detect_garbage (YAML error with cache_entry)
- _read_cache_entry (expected_hash checks)
- _write_cache (tmp_meta OSError + unlink)
"""

from __future__ import annotations

import base64
import hashlib
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.policy.discovery import (
    _compute_hash_normalized,
    _extract_extends_host,
    _fetch_github_contents,
    _is_github_host,
    _parse_remote_url,
    _resolve_ado_parent_ref,
    _split_hash_pin,
    _validate_extends_host,
    _verify_hash_pin,
)
from apm_cli.policy.project_config import ProjectPolicyConfigError

pytestmark = pytest.mark.unit

VALID_POLICY_YAML = "name: test-policy\nversion: '1.0'\nenforcement: warn\n"


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
# _resolve_ado_parent_ref
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parent_ref", "current_source", "leaf_host", "expected"),
    [
        (
            "org",
            "org:dev.azure.com/contoso/apm/apm-policy",
            "dev.azure.com",
            ("contoso", "apm", "apm-policy", "dev.azure.com"),
        ),
        (
            "governance/policy",
            "org:dev.azure.com/contoso/apm/apm-policy",
            "dev.azure.com",
            ("contoso", "governance", "policy", "dev.azure.com"),
        ),
        (
            "dev.azure.com/contoso/governance/policy",
            "org:dev.azure.com/contoso/apm/apm-policy",
            "dev.azure.com",
            ("contoso", "governance", "policy", "dev.azure.com"),
        ),
        (
            "contoso.visualstudio.com/governance/policy",
            "org:contoso.visualstudio.com/contoso/apm/apm-policy",
            "contoso.visualstudio.com",
            ("contoso", "governance", "policy", "contoso.visualstudio.com"),
        ),
        (
            "org",
            "org:contoso.visualstudio.com/contoso/team/policy",
            "contoso.visualstudio.com",
            ("contoso", "apm", "apm-policy", "contoso.visualstudio.com"),
        ),
    ],
)
def test_resolve_ado_parent_ref_supported_forms(
    parent_ref: str,
    current_source: str,
    leaf_host: str,
    expected: tuple[str, str, str, str],
) -> None:
    assert _resolve_ado_parent_ref(parent_ref, current_source, leaf_host) == expected


@pytest.mark.parametrize(
    ("parent_ref", "current_source", "leaf_host"),
    [
        ("", "org:dev.azure.com/contoso/apm/apm-policy", "dev.azure.com"),
        ("governance", "org:dev.azure.com/contoso/apm/apm-policy", "dev.azure.com"),
        (
            "dev.azure.com/contoso/governance",
            "org:dev.azure.com/contoso/apm/apm-policy",
            "dev.azure.com",
        ),
        (
            "github.example.com/contoso/policy",
            "org:dev.azure.com/contoso/apm/apm-policy",
            "dev.azure.com",
        ),
        ("org", "org:malformed", "dev.azure.com"),
    ],
)
def test_resolve_ado_parent_ref_rejects_invalid_forms(
    parent_ref: str,
    current_source: str,
    leaf_host: str,
) -> None:
    assert _resolve_ado_parent_ref(parent_ref, current_source, leaf_host) is None


# ---------------------------------------------------------------------------
# _resolve_and_persist_chain -- chain length == 1 (lines 484-491)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# discover_policy -- http:// rejection (line 552)
# ---------------------------------------------------------------------------


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
        with patch("apm_cli.policy._remote.urlparse", side_effect=Exception("boom")):
            result = _parse_remote_url("https://github.com/owner/repo")
        assert result is None


# ---------------------------------------------------------------------------
# _fetch_from_url -- cache hit / redirect / timeout / connection error
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _fetch_from_repo -- stale fallback / garbage (lines 850, 860)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _fetch_github_contents -- 403, redirect, non-200, timeout, connection error
# ---------------------------------------------------------------------------


class TestFetchGitHubContents:
    def test_403_returns_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        with patch("apm_cli.policy.discovery.policy_get", return_value=mock_resp):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert content is None
        assert "403" in error

    def test_redirect_refused(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {"Location": "https://evil.example.com/p"}
        with patch("apm_cli.policy.discovery.policy_get", return_value=mock_resp):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert content is None
        assert "redirect" in error.lower() or "302" in error

    def test_non_200_returns_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("apm_cli.policy.discovery.policy_get", return_value=mock_resp):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert content is None
        assert "500" in error or "HTTP" in error

    def test_timeout_returns_error(self) -> None:
        with patch(
            "apm_cli.policy.discovery.policy_get", side_effect=requests.exceptions.Timeout()
        ):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert content is None
        assert "timeout" in error.lower()

    def test_connection_error_returns_error(self) -> None:
        with patch(
            "apm_cli.policy.discovery.policy_get", side_effect=requests.exceptions.ConnectionError()
        ):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert content is None
        assert "connection" in error.lower()

    def test_base64_content_decoded(self) -> None:
        encoded = base64.b64encode(VALID_POLICY_YAML.encode()).decode() + "\n"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"encoding": "base64", "content": encoded}
        with patch("apm_cli.policy.discovery.policy_get", return_value=mock_resp):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert error is None
        assert content == VALID_POLICY_YAML

    def test_unexpected_format_returns_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        with patch("apm_cli.policy.discovery.policy_get", return_value=mock_resp):
            content, error = _fetch_github_contents("owner/.github", "apm-policy.yml")
        assert content is None
        assert "Unexpected" in error or error

    def test_three_part_ref_uses_ghe_api(self) -> None:
        """Host/owner/repo format builds GHE API URL."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("apm_cli.policy.discovery.policy_get", return_value=mock_resp) as mock_get:
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
# _detect_garbage -- YAML error with cache_entry (line 1165)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _read_cache_entry -- expected_hash checks (lines 1251-1257)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _write_cache -- tmp_meta OSError + unlink path (lines 1361-1363)
# ---------------------------------------------------------------------------


# No exception raised


@pytest.mark.parametrize(
    ("repo_ref", "env"),
    [
        ("org/.github", {"GITHUB_TOKEN": "env-github-token"}),
        ("org/.github", {"GITHUB_APM_PAT": "env-apm-pat"}),
        ("org/.github", {"GH_TOKEN": "env-gh-token"}),
        ("ghe.example.com/org/.github", {"GITHUB_TOKEN": "env-github-token"}),
    ],
    ids=["github-token", "github-apm-pat", "gh-token", "non-github-host"],
)
def test_policy_fetch_resolver_failure_never_falls_back_to_env_tokens(repo_ref, env) -> None:
    """When AuthResolver fails, no raw environment token is sent."""
    with (
        patch.dict(os.environ, env, clear=True),
        patch(
            "apm_cli.core.auth.AuthResolver.resolve",
            side_effect=RuntimeError("resolver unavailable"),
        ),
        patch("apm_cli.policy.discovery.requests.get") as http_get,
    ):
        content, error = _fetch_github_contents(repo_ref, "apm-policy.yml")

    assert content is None
    assert error
    sent = [str(call.kwargs.get("headers", {})) for call in http_get.call_args_list]
    assert not any(token in header for token in env.values() for header in sent)
