"""Tests for ref_resolver.py -- RefCache, RefResolver, ls-remote parsing."""

from __future__ import annotations

import base64
import time

import pytest

from apm_cli.marketplace.ref_resolver import (
    RefCache,
    RefResolver,
    RemoteRef,
    _parse_ls_remote_output,
    _redact_token,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# _parse_ls_remote_output
# ---------------------------------------------------------------------------


class TestParseLsRemoteOutput:
    """Tests for parsing raw ls-remote stdout."""

    def test_empty_output(self) -> None:
        assert _parse_ls_remote_output("") == []

    def test_single_tag(self) -> None:
        line = "abcd23456789abcdef1234567890abcdef123456\trefs/tags/v1.0.0"
        refs = _parse_ls_remote_output(line)
        assert len(refs) == 1
        assert refs[0].name == "refs/tags/v1.0.0"
        assert refs[0].sha == "abcd23456789abcdef1234567890abcdef123456"

    def test_multiple_refs(self) -> None:
        output = (
            "aaaa23456789abcdef1234567890abcdef123456\trefs/tags/v1.0.0\n"
            "bbbb23456789abcdef1234567890abcdef123456\trefs/tags/v2.0.0\n"
            "cccc23456789abcdef1234567890abcdef123456\trefs/heads/main\n"
        )
        refs = _parse_ls_remote_output(output)
        assert len(refs) == 3

    def test_annotated_tag_takes_peeled_commit_sha(self) -> None:
        # Tag object first, then the commit it points to (#3048).
        output = (
            "aaaa23456789abcdef1234567890abcdef123456\trefs/tags/v1.0.0\n"
            "bbbb23456789abcdef1234567890abcdef123456\trefs/tags/v1.0.0^{}\n"
        )
        refs = _parse_ls_remote_output(output)
        assert refs == [
            RemoteRef(
                name="refs/tags/v1.0.0",
                sha="bbbb23456789abcdef1234567890abcdef123456",
                peeled_sha="bbbb23456789abcdef1234567890abcdef123456",
            )
        ]

    def test_peeled_line_before_tag_line(self) -> None:
        output = (
            "bbbb23456789abcdef1234567890abcdef123456\trefs/tags/v1.0.0^{}\n"
            "aaaa23456789abcdef1234567890abcdef123456\trefs/tags/v1.0.0\n"
        )
        refs = _parse_ls_remote_output(output)
        assert refs == [
            RemoteRef(
                name="refs/tags/v1.0.0",
                sha="bbbb23456789abcdef1234567890abcdef123456",
                peeled_sha="bbbb23456789abcdef1234567890abcdef123456",
            )
        ]

    def test_only_annotated_tags_change(self) -> None:
        output = (
            "1111111111111111111111111111111111111111\trefs/heads/main\n"
            "2222222222222222222222222222222222222222\trefs/tags/v1.0.0\n"
            "3333333333333333333333333333333333333333\trefs/tags/v2.0.0\n"
            "4444444444444444444444444444444444444444\trefs/tags/v2.0.0^{}\n"
        )
        refs = _parse_ls_remote_output(output)
        assert refs == [
            RemoteRef(name="refs/heads/main", sha="1" * 40),
            RemoteRef(name="refs/tags/v1.0.0", sha="2" * 40),
            RemoteRef(name="refs/tags/v2.0.0", sha="4" * 40, peeled_sha="4" * 40),
        ]

    def test_peeled_line_without_tag_line_adds_nothing(self) -> None:
        output = "bbbb23456789abcdef1234567890abcdef123456\trefs/tags/v1.0.0^{}\n"
        assert _parse_ls_remote_output(output) == []

    @pytest.mark.parametrize("reverse", [False, True])
    def test_peeled_tag_retained_with_corrected_source_sha(self, reverse: bool) -> None:
        output = f"{'a' * 40}\trefs/tags/v1.0.0\n{'b' * 40}\trefs/tags/v1.0.0^{{}}\n"
        if reverse:
            output = "\n".join(reversed(output.splitlines()))
        refs = _parse_ls_remote_output(output)
        assert len(refs) == 1
        assert refs[0].name == "refs/tags/v1.0.0"
        assert refs[0].sha == "b" * 40
        assert refs[0].peeled_sha == "b" * 40

    def test_invalid_sha_skipped(self) -> None:
        output = "not-a-sha\trefs/tags/v1.0.0\n"
        refs = _parse_ls_remote_output(output)
        assert len(refs) == 0

    def test_blank_lines_skipped(self) -> None:
        output = "\naaaa23456789abcdef1234567890abcdef123456\trefs/tags/v1.0.0\n\n"
        refs = _parse_ls_remote_output(output)
        assert len(refs) == 1

    def test_no_tab_separator_skipped(self) -> None:
        output = "aaaa23456789abcdef1234567890abcdef123456 refs/tags/v1.0.0\n"
        refs = _parse_ls_remote_output(output)
        assert len(refs) == 0

    def test_whitespace_trimmed(self) -> None:
        output = "  aaaa23456789abcdef1234567890abcdef123456\t  refs/tags/v1.0.0  \n"
        refs = _parse_ls_remote_output(output)
        assert len(refs) == 1
        assert refs[0].name == "refs/tags/v1.0.0"


# ---------------------------------------------------------------------------
# _redact_token
# ---------------------------------------------------------------------------


class TestRedactToken:
    """Tests for token redaction in error messages."""

    def test_redact_access_token(self) -> None:
        text = "fatal: auth failed for https://x-access-token:ghp_abc123@github.com/acme/tools"
        result = _redact_token(text)
        assert "ghp_abc123" not in result
        assert "***" in result

    def test_redact_oauth_token(self) -> None:
        text = "https://oauth2:gho_secret@github.com/acme/repo.git"
        result = _redact_token(text)
        assert "gho_secret" not in result

    def test_no_token_unchanged(self) -> None:
        text = "fatal: repository not found"
        assert _redact_token(text) == text

    def test_multiple_tokens_redacted(self) -> None:
        text = "https://user:pass1@github.com/a/b https://user:pass2@github.com/c/d"
        result = _redact_token(text)
        assert "pass1" not in result
        assert "pass2" not in result


# ---------------------------------------------------------------------------
# RefCache
# ---------------------------------------------------------------------------


class TestRefCache:
    """Tests for in-memory ref cache."""

    def test_put_and_get(self) -> None:
        cache = RefCache()
        refs = [RemoteRef(name="refs/tags/v1.0.0", sha="a" * 40)]
        cache.put("acme/tools", refs)
        result = cache.get("acme/tools")
        assert result is not None
        assert len(result) == 1
        assert result[0].name == "refs/tags/v1.0.0"

    def test_miss_returns_none(self) -> None:
        cache = RefCache()
        assert cache.get("acme/unknown") is None

    def test_expiry(self) -> None:
        cache = RefCache(ttl_seconds=0.01)
        refs = [RemoteRef(name="refs/tags/v1.0.0", sha="a" * 40)]
        cache.put("acme/tools", refs)
        time.sleep(0.02)
        assert cache.get("acme/tools") is None

    def test_not_expired_within_ttl(self) -> None:
        cache = RefCache(ttl_seconds=60.0)
        refs = [RemoteRef(name="refs/tags/v1.0.0", sha="a" * 40)]
        cache.put("acme/tools", refs)
        result = cache.get("acme/tools")
        assert result is not None

    def test_clear(self) -> None:
        cache = RefCache()
        cache.put("acme/tools", [])
        cache.clear()
        assert len(cache) == 0
        assert cache.get("acme/tools") is None

    def test_get_returns_copy(self) -> None:
        """Mutating returned list does not affect cache."""
        cache = RefCache()
        refs = [RemoteRef(name="refs/tags/v1.0.0", sha="a" * 40)]
        cache.put("acme/tools", refs)
        result = cache.get("acme/tools")
        assert result is not None
        result.clear()
        assert len(cache.get("acme/tools")) == 1  # type: ignore[arg-type]

    def test_len(self) -> None:
        cache = RefCache()
        assert len(cache) == 0
        cache.put("acme/a", [])
        cache.put("acme/b", [])
        assert len(cache) == 2


# ---------------------------------------------------------------------------
# RefResolver
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RefResolver.resolve_ref_sha
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RemoteRef frozen dataclass
# ---------------------------------------------------------------------------


class TestRemoteRef:
    """Basic dataclass tests."""

    def test_frozen(self) -> None:
        ref = RemoteRef(name="refs/tags/v1.0.0", sha="a" * 40)
        with pytest.raises(AttributeError):
            ref.name = "other"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = RemoteRef(name="refs/tags/v1.0.0", sha="a" * 40)
        b = RemoteRef(name="refs/tags/v1.0.0", sha="a" * 40)
        assert a == b

    def test_inequality(self) -> None:
        a = RemoteRef(name="refs/tags/v1.0.0", sha="a" * 40)
        b = RemoteRef(name="refs/tags/v2.0.0", sha="b" * 40)
        assert a != b


# ---------------------------------------------------------------------------
# S3: GIT_TERMINAL_PROMPT suppression
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GHE / custom host support
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Token injection into ls-remote URLs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# #2368: auth header injection must preserve retained GIT_CONFIG entries
# ---------------------------------------------------------------------------


class TestGitUrlAndEnvPreservesRetainedGitConfig:
    """The env returned by _git_url_and_env must keep non-auth git config."""

    @staticmethod
    def _retained() -> dict[str, str]:
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.sslCAInfo",
            "GIT_CONFIG_VALUE_0": "/corporate/ca.pem",
        }

    def test_ado_bearer_appends_after_retained_entries(self) -> None:
        resolver = RefResolver(
            host="dev.azure.com",
            token="aad-jwt",
            auth_scheme="bearer",
            git_env=self._retained(),
        )
        _url, env = resolver._git_url_and_env("contoso/platform/repo")
        assert env["GIT_CONFIG_COUNT"] == "3"
        assert env["GIT_CONFIG_KEY_0"] == "http.sslCAInfo"
        assert env["GIT_CONFIG_VALUE_0"] == "/corporate/ca.pem"
        assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_1"] == ""
        assert env["GIT_CONFIG_KEY_2"] == "http.extraheader"
        assert env["GIT_CONFIG_VALUE_2"] == "Authorization: Bearer aad-jwt"
        resolver.close()

    def test_ado_basic_appends_after_retained_entries(self) -> None:
        resolver = RefResolver(
            host="dev.azure.com",
            token="ado_pat",
            auth_scheme="basic",
            git_env=self._retained(),
        )
        _url, env = resolver._git_url_and_env("contoso/platform/repo")
        expected = base64.b64encode(b":ado_pat").decode()
        assert env["GIT_CONFIG_COUNT"] == "3"
        assert env["GIT_CONFIG_KEY_0"] == "http.sslCAInfo"
        assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_1"] == ""
        assert env["GIT_CONFIG_KEY_2"] == "http.extraheader"
        assert env["GIT_CONFIG_VALUE_2"] == f"Authorization: Basic {expected}"
        resolver.close()

    def test_ado_bearer_replaces_stale_header_instead_of_stacking(self) -> None:
        stale = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": "Authorization: Bearer stale-jwt",
            "GIT_CONFIG_KEY_1": "http.sslCAInfo",
            "GIT_CONFIG_VALUE_1": "/corporate/ca.pem",
        }
        resolver = RefResolver(
            host="dev.azure.com",
            token="fresh-jwt",
            auth_scheme="bearer",
            git_env=stale,
        )
        _url, env = resolver._git_url_and_env("contoso/platform/repo")
        headers = [
            v
            for k, v in env.items()
            if k.startswith("GIT_CONFIG_VALUE_") and "Authorization" in str(v)
        ]
        assert headers == ["Authorization: Bearer fresh-jwt"]
        assert env["GIT_CONFIG_COUNT"] == "3"
        assert env["GIT_CONFIG_KEY_0"] == "http.sslCAInfo"
        assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_1"] == ""
        resolver.close()


# ---------------------------------------------------------------------------
# commit_exists -- real git against a local bare remote (#3458)
# ---------------------------------------------------------------------------
