"""Clone failures must carry git's own diagnostic, redacted.

``CalledProcessError.__str__`` renders only the exit status, so wrapping
it with plain interpolation drops git's stderr. That loss is not
cosmetic: ``AuthResolver.is_public_github_auth_failure`` classifies a
failure by string-matching the message, so a stripped message turns an
authentication failure into a reported network failure and sends users
to check their connectivity and proxy settings for what is a credential
problem.

These tests pin both halves of the contract: the diagnostic survives the
wrap so the classifier still recognises it, and credentials that git
echoes back in that diagnostic are redacted before it reaches a message.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.cache.git_cache import (
    GitCache,
    _clone_failure_detail,
    _sanitize_git_stderr,
)
from apm_cli.core.auth import AuthResolver

CLONE_ARGV = ["git", "clone", "--bare", "https://github.com/acme/widgets.git", "/tmp/x"]
GIT_EXIT_FAILURE = 128

REPO_URL = "https://github.com/acme/widgets.git"
SHARD_KEY = "1ca76695ff46485c"
COMMIT_SHA = "0" * 40
EMPTY_GIT_ENV: dict[str, str] = {}

AUTH_FAILURE_STDERR = (
    "Cloning into bare repository '/tmp/x'...\n"
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"
)
NETWORK_FAILURE_STDERR = "fatal: unable to access 'https://github.com/': Could not resolve host\n"
TOKEN_VALUE = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
REDACTION_MARKER = "***"


def _clone_error(stderr: str | bytes | None) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(GIT_EXIT_FAILURE, CLONE_ARGV, stderr=stderr)


class TestCloneFailureDetail:
    def test_includes_git_stderr(self) -> None:
        detail = _clone_failure_detail(_clone_error(AUTH_FAILURE_STDERR))

        assert "could not read Username" in detail

    def test_decodes_bytes_stderr(self) -> None:
        detail = _clone_failure_detail(_clone_error(AUTH_FAILURE_STDERR.encode()))

        assert "could not read Username" in detail

    def test_falls_back_to_exception_text_when_stderr_absent(self) -> None:
        detail = _clone_failure_detail(_clone_error(None))

        assert str(GIT_EXIT_FAILURE) in detail

    def test_falls_back_to_exception_text_when_stderr_empty(self) -> None:
        detail = _clone_failure_detail(_clone_error(""))

        assert str(GIT_EXIT_FAILURE) in detail

    def test_handles_exception_without_stderr_attribute(self) -> None:
        detail = _clone_failure_detail(OSError("disk full"))

        assert "disk full" in detail

    def test_truncates_a_flooding_remote(self) -> None:
        detail = _clone_failure_detail(_clone_error("remote: " + "y" * 10_000))

        assert len(detail) < 1_000
        assert detail.endswith("...")

    def test_redacts_token_echoed_in_stderr(self) -> None:
        stderr = f"fatal: Authentication failed for 'https://{TOKEN_VALUE}@github.com/acme.git'"

        detail = _clone_failure_detail(_clone_error(stderr))

        assert TOKEN_VALUE not in detail
        assert REDACTION_MARKER in detail


class TestAuthFailureStaysClassifiable:
    """The regression the wrapping caused: auth read as network."""

    def test_wrapped_auth_failure_is_still_an_auth_failure(self) -> None:
        exc = _clone_error(AUTH_FAILURE_STDERR)
        assert AuthResolver.is_public_github_auth_failure(exc) is True

        wrapped = RuntimeError(f"Failed to clone URL: {_clone_failure_detail(exc)}")

        assert AuthResolver.is_public_github_auth_failure(wrapped) is True

    def test_bare_interpolation_loses_the_classification(self) -> None:
        """Pins why the helper exists -- plain f-string drops stderr."""
        exc = _clone_error(AUTH_FAILURE_STDERR)

        assert AuthResolver.is_public_github_auth_failure(RuntimeError(f"{exc}")) is False

    def test_network_failure_is_not_reclassified_as_auth(self) -> None:
        wrapped = RuntimeError(_clone_failure_detail(_clone_error(NETWORK_FAILURE_STDERR)))

        assert AuthResolver.is_public_github_auth_failure(wrapped) is False


class TestBareCloneFailurePropagatesDetail:
    """The classifier reads the message ``_ensure_bare_repo`` raises."""

    def _failing_clone(self, tmp_path: Path, *, partial: bool) -> RuntimeError:
        cache = GitCache(tmp_path)

        def always_fail(*_args: object, **_kwargs: object) -> None:
            raise _clone_error(AUTH_FAILURE_STDERR)

        with patch("apm_cli.cache.git_cache.subprocess.run", side_effect=always_fail):
            with pytest.raises(RuntimeError) as raised:
                cache._ensure_bare_repo(
                    REPO_URL,
                    SHARD_KEY,
                    COMMIT_SHA,
                    env=EMPTY_GIT_ENV,
                    partial=partial,
                )
        return raised.value

    def test_full_clone_failure_stays_an_auth_failure(self, tmp_path: Path) -> None:
        error = self._failing_clone(tmp_path, partial=False)

        assert AuthResolver.is_public_github_auth_failure(error) is True

    def test_partial_fallback_failure_stays_an_auth_failure(self, tmp_path: Path) -> None:
        error = self._failing_clone(tmp_path, partial=True)

        assert AuthResolver.is_public_github_auth_failure(error) is True


class TestSanitizeGitStderr:
    def test_redacts_token_as_username(self) -> None:
        sanitized = _sanitize_git_stderr(f"https://{TOKEN_VALUE}@github.com/acme.git")

        assert TOKEN_VALUE not in sanitized
        assert sanitized == f"https://{REDACTION_MARKER}@github.com/acme.git"

    def test_redacts_basic_auth_pair(self) -> None:
        sanitized = _sanitize_git_stderr(f"https://user:{TOKEN_VALUE}@github.com/acme.git")

        assert TOKEN_VALUE not in sanitized

    def test_redacts_bare_platform_token(self) -> None:
        sanitized = _sanitize_git_stderr(f"remote: token {TOKEN_VALUE} is not supported")

        assert TOKEN_VALUE not in sanitized
        assert REDACTION_MARKER in sanitized

    def test_preserves_a_credential_free_message(self) -> None:
        message = "fatal: repository 'https://github.com/acme/widgets.git' not found"

        assert _sanitize_git_stderr(message) == message
