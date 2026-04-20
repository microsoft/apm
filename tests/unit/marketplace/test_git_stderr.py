"""Tests for git stderr translator.

Covers every ``GitErrorKind`` classification branch, hint substitution,
raw-stderr truncation, summary length cap, and the UNKNOWN fallback.
"""

from __future__ import annotations

import pytest

from apm_cli.marketplace.git_stderr import (
    GitErrorKind,
    TranslatedGitError,
    translate_git_stderr,
)


# ---------------------------------------------------------------------------
# AUTH classification
# ---------------------------------------------------------------------------

_AUTH_STDERR_SAMPLES = [
    pytest.param(
        "fatal: Authentication failed for 'https://github.com/acme/tools.git'",
        id="authentication-failed",
    ),
    pytest.param(
        "remote: Invalid credentials",
        id="invalid-credentials",
    ),
    pytest.param(
        "fatal: could not read Password for 'https://github.com': terminal prompts disabled",
        id="could-not-read-password",
    ),
    pytest.param(
        "Permission denied (publickey).\r\nfatal: Could not read from remote repository.",
        id="permission-denied-publickey",
    ),
    pytest.param(
        "The requested URL returned error: 403 Forbidden",
        id="403-forbidden-url",
    ),
    pytest.param(
        "The requested URL returned error: 401 Unauthorized",
        id="401-unauthorized-url",
    ),
    pytest.param(
        "fatal: Authentication required\nremote: Repository not found.",
        id="fatal-authentication-takes-priority",
    ),
    pytest.param(
        "remote: write access to repository not allowed",
        id="remote-write-access",
    ),
    pytest.param(
        "Please make sure you have the correct access rights\n"
        "and the repository exists.",
        id="correct-access-rights",
    ),
    pytest.param(
        "The requested URL returned error: 401",
        id="url-error-401",
    ),
    pytest.param(
        "The requested URL returned error: 403",
        id="url-error-403",
    ),
]


class TestAuthClassification:
    """AUTH patterns are recognized regardless of casing."""

    @pytest.mark.parametrize("stderr", _AUTH_STDERR_SAMPLES)
    def test_auth_detected(self, stderr: str) -> None:
        result = translate_git_stderr(stderr, operation="push", remote="acme/tools")
        assert result.kind == GitErrorKind.AUTH

    def test_auth_summary(self) -> None:
        result = translate_git_stderr(
            "fatal: Authentication failed", operation="ls-remote"
        )
        assert result.summary == "Git authentication failed during ls-remote."

    def test_auth_hint(self) -> None:
        result = translate_git_stderr(
            "fatal: Authentication failed", operation="push"
        )
        assert "GITHUB_TOKEN" in result.hint
        assert "apm marketplace doctor" in result.hint

    def test_auth_case_insensitive(self) -> None:
        result = translate_git_stderr("FATAL: AUTHENTICATION FAILED")
        assert result.kind == GitErrorKind.AUTH


# ---------------------------------------------------------------------------
# NOT_FOUND classification
# ---------------------------------------------------------------------------

_NOT_FOUND_STDERR_SAMPLES = [
    pytest.param(
        "ERROR: Repository not found.\nfatal: Could not read from remote repository.",
        id="repository-not-found",
    ),
    pytest.param(
        "fatal: 'https://github.com/acme/nope' does not appear to be a git repository",
        id="not-a-git-repo",
    ),
    pytest.param(
        "error: pathspec 'v99.0.0' did not match any file(s) known to git -- not a valid ref",
        id="not-a-valid-ref",
    ),
    pytest.param(
        "fatal: couldn't find remote ref refs/heads/nonexistent",
        id="couldnt-find-remote-ref",
    ),
    pytest.param(
        "fatal: could not resolve HEAD to a revision",
        id="could-not-resolve",
    ),
    pytest.param(
        "The requested URL returned error: 404",
        id="url-error-404",
    ),
    pytest.param(
        "fatal: no such ref: refs/tags/v0.0.0",
        id="no-such-ref",
    ),
    pytest.param(
        "fatal: unknown ref: refs/heads/oops",
        id="unknown-ref",
    ),
]


class TestNotFoundClassification:
    """NOT_FOUND patterns are recognized."""

    @pytest.mark.parametrize("stderr", _NOT_FOUND_STDERR_SAMPLES)
    def test_not_found_detected(self, stderr: str) -> None:
        result = translate_git_stderr(stderr, operation="fetch")
        assert result.kind == GitErrorKind.NOT_FOUND

    def test_not_found_summary(self) -> None:
        result = translate_git_stderr(
            "Repository not found", operation="clone"
        )
        assert result.summary == "Git ref or repository not found during clone."

    def test_hint_with_remote(self) -> None:
        result = translate_git_stderr(
            "Repository not found", remote="acme/code-reviewer"
        )
        assert "'acme/code-reviewer'" in result.hint
        assert "ref is spelled correctly" in result.hint

    def test_hint_without_remote(self) -> None:
        result = translate_git_stderr("Repository not found")
        assert "the remote" in result.hint
        assert "ref is spelled correctly" in result.hint


# ---------------------------------------------------------------------------
# TIMEOUT classification
# ---------------------------------------------------------------------------

_TIMEOUT_STDERR_SAMPLES = [
    pytest.param(
        "fatal: unable to access 'https://github.com/acme/repo.git/': Operation timed out",
        id="operation-timed-out",
    ),
    pytest.param(
        "fatal: unable to access: Connection timed out after 30001 milliseconds",
        id="connection-timed-out",
    ),
    pytest.param(
        "fatal: unable to access: Could not resolve host: github.com",
        id="could-not-resolve-host",
    ),
    pytest.param(
        "fatal: unable to connect: Connection refused",
        id="connection-refused",
    ),
    pytest.param(
        "fatal: unable to access: Network is unreachable",
        id="network-unreachable",
    ),
    pytest.param(
        "fatal: unable to look up github.com (Temporary failure in name resolution)",
        id="temporary-failure-dns",
    ),
    pytest.param(
        "LibreSSL SSL_read: Connection reset by peer, errno 54",
        id="ssl-read-connection-reset",
    ),
    pytest.param(
        "error: RPC failed; curl 56 GnuTLS recv error (-110): early EOF",
        id="early-eof",
    ),
    pytest.param(
        "error: RPC failed; curl 18 transfer closed",
        id="rpc-failed",
    ),
]


class TestTimeoutClassification:
    """TIMEOUT patterns are recognized."""

    @pytest.mark.parametrize("stderr", _TIMEOUT_STDERR_SAMPLES)
    def test_timeout_detected(self, stderr: str) -> None:
        result = translate_git_stderr(stderr, operation="fetch")
        assert result.kind == GitErrorKind.TIMEOUT

    def test_timeout_summary(self) -> None:
        result = translate_git_stderr(
            "Connection timed out", operation="push"
        )
        assert result.summary == "Git network timeout during push."

    def test_timeout_hint(self) -> None:
        result = translate_git_stderr("Connection timed out")
        assert "Retry or check your connection" in result.hint


# ---------------------------------------------------------------------------
# UNKNOWN fallback
# ---------------------------------------------------------------------------


class TestUnknownFallback:
    """Unrecognized stderr maps to UNKNOWN."""

    def test_unknown_kind(self) -> None:
        result = translate_git_stderr(
            "error: something completely unexpected happened"
        )
        assert result.kind == GitErrorKind.UNKNOWN

    def test_unknown_summary_with_exit_code(self) -> None:
        result = translate_git_stderr(
            "kaboom", exit_code=1, operation="rebase"
        )
        assert result.summary == "Git failed during rebase (exit 1)."

    def test_unknown_summary_without_exit_code(self) -> None:
        result = translate_git_stderr("kaboom", operation="rebase")
        assert result.summary == "Git failed during rebase."

    def test_unknown_hint(self) -> None:
        result = translate_git_stderr("kaboom", operation="merge")
        assert result.hint == "Git failed during merge. See raw stderr above."

    def test_empty_stderr(self) -> None:
        result = translate_git_stderr("")
        assert result.kind == GitErrorKind.UNKNOWN
        assert result.raw == ""


# ---------------------------------------------------------------------------
# Priority ordering (AUTH > NOT_FOUND > TIMEOUT)
# ---------------------------------------------------------------------------


class TestPriorityOrder:
    """When stderr matches multiple categories, priority order wins."""

    def test_auth_beats_not_found(self) -> None:
        stderr = (
            "fatal: Authentication failed\n"
            "ERROR: Repository not found."
        )
        result = translate_git_stderr(stderr)
        assert result.kind == GitErrorKind.AUTH

    def test_auth_beats_timeout(self) -> None:
        stderr = (
            "fatal: Authentication failed\n"
            "error: RPC failed; curl 56"
        )
        result = translate_git_stderr(stderr)
        assert result.kind == GitErrorKind.AUTH

    def test_not_found_beats_timeout(self) -> None:
        stderr = (
            "Repository not found\n"
            "Connection timed out"
        )
        result = translate_git_stderr(stderr)
        assert result.kind == GitErrorKind.NOT_FOUND


# ---------------------------------------------------------------------------
# Raw stderr truncation
# ---------------------------------------------------------------------------


class TestRawTruncation:
    """Raw stderr is truncated to <= 500 chars."""

    def test_499_chars_not_truncated(self) -> None:
        stderr = "x" * 499
        result = translate_git_stderr(stderr)
        assert result.raw == stderr
        assert len(result.raw) == 499

    def test_500_chars_not_truncated(self) -> None:
        stderr = "y" * 500
        result = translate_git_stderr(stderr)
        assert result.raw == stderr
        assert len(result.raw) == 500

    def test_501_chars_truncated(self) -> None:
        stderr = "z" * 501
        result = translate_git_stderr(stderr)
        assert result.raw == "z" * 500 + "... (truncated)"
        assert len(result.raw) == 500 + len("... (truncated)")
        assert result.raw.endswith("... (truncated)")

    def test_long_stderr_truncated(self) -> None:
        stderr = "a" * 2000
        result = translate_git_stderr(stderr)
        assert result.raw.startswith("a" * 500)
        assert result.raw.endswith("... (truncated)")


# ---------------------------------------------------------------------------
# Summary length cap (80 chars)
# ---------------------------------------------------------------------------


class TestSummaryLengthCap:
    """Summary is capped at 80 characters."""

    def test_short_operation_under_cap(self) -> None:
        result = translate_git_stderr("kaboom", operation="push")
        assert len(result.summary) <= 80

    def test_long_operation_capped(self) -> None:
        long_op = "a-very-long-operation-name-that-goes-on-and-on-" * 3
        result = translate_git_stderr("kaboom", operation=long_op)
        assert len(result.summary) <= 80
        assert result.summary.endswith("...")

    def test_all_kinds_respect_cap(self) -> None:
        long_op = "x" * 200
        for stderr, expected_kind in [
            ("fatal: Authentication failed", GitErrorKind.AUTH),
            ("Repository not found", GitErrorKind.NOT_FOUND),
            ("Connection timed out", GitErrorKind.TIMEOUT),
            ("kaboom", GitErrorKind.UNKNOWN),
        ]:
            result = translate_git_stderr(
                stderr, operation=long_op, exit_code=128
            )
            assert result.kind == expected_kind
            assert len(result.summary) <= 80, (
                f"{expected_kind}: summary is {len(result.summary)} chars"
            )


# ---------------------------------------------------------------------------
# Dataclass properties
# ---------------------------------------------------------------------------


class TestTranslatedGitErrorDataclass:
    """TranslatedGitError is a frozen dataclass."""

    def test_frozen(self) -> None:
        result = translate_git_stderr("kaboom")
        with pytest.raises(AttributeError):
            result.kind = GitErrorKind.AUTH  # type: ignore[misc]

    def test_fields(self) -> None:
        result = translate_git_stderr(
            "fatal: Authentication failed",
            exit_code=128,
            operation="push",
            remote="acme/tools",
        )
        assert isinstance(result.kind, GitErrorKind)
        assert isinstance(result.summary, str)
        assert isinstance(result.hint, str)
        assert isinstance(result.raw, str)


# ---------------------------------------------------------------------------
# ASCII-only enforcement
# ---------------------------------------------------------------------------


class TestAsciiOnly:
    """Every string field must be pure ASCII."""

    @pytest.mark.parametrize(
        "stderr",
        [
            "fatal: Authentication failed",
            "Repository not found",
            "Connection timed out",
            "unknown error",
            "",
        ],
        ids=["auth", "not-found", "timeout", "unknown", "empty"],
    )
    def test_all_fields_are_ascii(self, stderr: str) -> None:
        result = translate_git_stderr(
            stderr, operation="push", remote="acme/tools", exit_code=1
        )
        for field_name in ("summary", "hint", "raw"):
            value = getattr(result, field_name)
            value.encode("ascii")  # raises UnicodeEncodeError if non-ASCII


# ---------------------------------------------------------------------------
# GitErrorKind enum values
# ---------------------------------------------------------------------------


class TestGitErrorKindEnum:
    """Enum has exactly the expected members and values."""

    def test_members(self) -> None:
        assert set(GitErrorKind) == {
            GitErrorKind.AUTH,
            GitErrorKind.NOT_FOUND,
            GitErrorKind.TIMEOUT,
            GitErrorKind.UNKNOWN,
        }

    def test_values(self) -> None:
        assert GitErrorKind.AUTH.value == "auth"
        assert GitErrorKind.NOT_FOUND.value == "not_found"
        assert GitErrorKind.TIMEOUT.value == "timeout"
        assert GitErrorKind.UNKNOWN.value == "unknown"


# ---------------------------------------------------------------------------
# Default parameter values
# ---------------------------------------------------------------------------


class TestDefaults:
    """Default parameter values are applied correctly."""

    def test_default_operation(self) -> None:
        result = translate_git_stderr("kaboom")
        assert "git operation" in result.summary

    def test_default_exit_code_none(self) -> None:
        result = translate_git_stderr("kaboom")
        assert "exit" not in result.summary

    def test_default_remote_none_in_not_found_hint(self) -> None:
        result = translate_git_stderr("Repository not found")
        assert "the remote" in result.hint
        assert "'" not in result.hint or "the remote" in result.hint
