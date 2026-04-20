"""Tests for ref_resolver.py -- RefCache, RefResolver, ls-remote parsing."""

from __future__ import annotations

import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.marketplace.errors import GitLsRemoteError, OfflineMissError
from apm_cli.marketplace.ref_resolver import (
    RefCache,
    RefResolver,
    RemoteRef,
    _parse_ls_remote_output,
    _redact_token,
)


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

    def test_peeled_tag_skipped(self) -> None:
        output = (
            "aaaa23456789abcdef1234567890abcdef123456\trefs/tags/v1.0.0\n"
            "bbbb23456789abcdef1234567890abcdef123456\trefs/tags/v1.0.0^{}\n"
        )
        refs = _parse_ls_remote_output(output)
        assert len(refs) == 1
        assert refs[0].name == "refs/tags/v1.0.0"

    def test_invalid_sha_skipped(self) -> None:
        output = "not-a-sha\trefs/tags/v1.0.0\n"
        refs = _parse_ls_remote_output(output)
        assert len(refs) == 0

    def test_blank_lines_skipped(self) -> None:
        output = (
            "\n"
            "aaaa23456789abcdef1234567890abcdef123456\trefs/tags/v1.0.0\n"
            "\n"
        )
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
        text = (
            "https://user:pass1@github.com/a/b "
            "https://user:pass2@github.com/c/d"
        )
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


_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40

_MOCK_LS_REMOTE_OUTPUT = (
    f"{_SHA_A}\trefs/tags/v1.0.0\n"
    f"{_SHA_B}\trefs/tags/v2.0.0\n"
    f"{_SHA_C}\trefs/heads/main\n"
)


def _make_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Create a mock subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(
        args=["git", "ls-remote"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestRefResolver:
    """Tests for RefResolver with mocked subprocess."""

    @patch("apm_cli.marketplace.ref_resolver.subprocess.run")
    def test_list_remote_refs_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_completed(stdout=_MOCK_LS_REMOTE_OUTPUT)
        resolver = RefResolver(timeout_seconds=5.0)
        refs = resolver.list_remote_refs("acme/tools")
        assert len(refs) == 3
        assert refs[0].name == "refs/tags/v1.0.0"
        assert refs[0].sha == _SHA_A
        resolver.close()

    @patch("apm_cli.marketplace.ref_resolver.subprocess.run")
    def test_cache_hit(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_completed(stdout=_MOCK_LS_REMOTE_OUTPUT)
        resolver = RefResolver(timeout_seconds=5.0)
        resolver.list_remote_refs("acme/tools")
        resolver.list_remote_refs("acme/tools")
        # Should only call subprocess once (cache hit)
        assert mock_run.call_count == 1
        resolver.close()

    @patch("apm_cli.marketplace.ref_resolver.subprocess.run")
    def test_different_remotes_separate_calls(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_completed(stdout=_MOCK_LS_REMOTE_OUTPUT)
        resolver = RefResolver(timeout_seconds=5.0)
        resolver.list_remote_refs("acme/tools")
        resolver.list_remote_refs("acme/other")
        assert mock_run.call_count == 2
        resolver.close()

    @patch("apm_cli.marketplace.ref_resolver.subprocess.run")
    def test_git_failure_raises(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_completed(
            returncode=128,
            stderr="fatal: repository 'https://github.com/acme/gone.git' not found",
        )
        resolver = RefResolver(timeout_seconds=5.0)
        with pytest.raises(GitLsRemoteError):
            resolver.list_remote_refs("acme/gone")
        resolver.close()

    @patch("apm_cli.marketplace.ref_resolver.subprocess.run")
    def test_timeout_raises(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=5.0)
        resolver = RefResolver(timeout_seconds=5.0)
        with pytest.raises(GitLsRemoteError, match="timed out"):
            resolver.list_remote_refs("acme/slow")
        resolver.close()

    @patch("apm_cli.marketplace.ref_resolver.subprocess.run")
    def test_os_error_raises(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = OSError("git not found")
        resolver = RefResolver(timeout_seconds=5.0)
        with pytest.raises(GitLsRemoteError, match="git is installed"):
            resolver.list_remote_refs("acme/tools")
        resolver.close()

    def test_offline_mode_miss(self) -> None:
        resolver = RefResolver(timeout_seconds=5.0, offline=True)
        with pytest.raises(OfflineMissError):
            resolver.list_remote_refs("acme/tools")
        resolver.close()

    @patch("apm_cli.marketplace.ref_resolver.subprocess.run")
    def test_offline_mode_cache_hit(self, mock_run: MagicMock) -> None:
        """Pre-populate cache, then switch to offline."""
        mock_run.return_value = _make_completed(stdout=_MOCK_LS_REMOTE_OUTPUT)
        resolver = RefResolver(timeout_seconds=5.0, offline=False)
        resolver.list_remote_refs("acme/tools")

        # Now switch to offline via a new resolver sharing the cache
        resolver._offline = True
        refs = resolver.list_remote_refs("acme/tools")
        assert len(refs) == 3
        resolver.close()

    @patch("apm_cli.marketplace.ref_resolver.subprocess.run")
    def test_token_redacted_in_error(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_completed(
            returncode=128,
            stderr="fatal: auth failed for https://x-access-token:ghp_secret@github.com/acme/priv.git",
        )
        resolver = RefResolver(timeout_seconds=5.0)
        with pytest.raises(GitLsRemoteError) as exc_info:
            resolver.list_remote_refs("acme/priv")
        assert "ghp_secret" not in str(exc_info.value)
        resolver.close()

    @patch("apm_cli.marketplace.ref_resolver.subprocess.run")
    def test_stderr_translator_disabled(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_completed(
            returncode=128,
            stderr="some error",
        )
        resolver = RefResolver(
            timeout_seconds=5.0,
            stderr_translator_enabled=False,
        )
        with pytest.raises(GitLsRemoteError):
            resolver.list_remote_refs("acme/tools")
        resolver.close()

    @patch("apm_cli.marketplace.ref_resolver.subprocess.run")
    def test_empty_repo(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_completed(stdout="")
        resolver = RefResolver(timeout_seconds=5.0)
        refs = resolver.list_remote_refs("acme/empty")
        assert refs == []
        resolver.close()

    @patch("apm_cli.marketplace.ref_resolver.subprocess.run")
    def test_correct_command_args(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_completed(stdout="")
        resolver = RefResolver(timeout_seconds=7.5)
        resolver.list_remote_refs("acme/tools")
        args, kwargs = mock_run.call_args
        assert args[0] == [
            "git", "ls-remote", "--tags", "--heads",
            "https://github.com/acme/tools.git",
        ]
        assert kwargs["timeout"] == 7.5
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        resolver.close()

    def test_close_clears_cache(self) -> None:
        resolver = RefResolver(timeout_seconds=5.0)
        resolver.cache.put("acme/tools", [])
        assert len(resolver.cache) == 1
        resolver.close()
        assert len(resolver.cache) == 0


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
