"""Tests for _git_utils.py -- shared git token redaction."""

from __future__ import annotations

import pytest

from apm_cli.marketplace._git_utils import redact_token


class TestRedactToken:
    """Tests for the improved ``redact_token()`` function."""

    def test_https_token_redacted(self) -> None:
        """Standard ``https://TOKEN@host`` pattern is redacted."""
        text = "fatal: auth failed for https://x-access-token:ghp_abc123@github.com/acme/tools"
        result = redact_token(text)
        assert "ghp_abc123" not in result
        assert "https://***@" in result

    def test_http_token_redacted(self) -> None:
        """Plain ``http://TOKEN@host`` pattern is now also redacted."""
        text = "error: http://oauth2:gho_SECRET@github.com/acme/repo.git"
        result = redact_token(text)
        assert "gho_SECRET" not in result
        assert "***@" in result

    def test_query_param_token_redacted(self) -> None:
        """``?token=VALUE`` query parameter is redacted, preserving ``?``."""
        text = "https://github.com/archive?token=abc123"
        result = redact_token(text)
        assert "abc123" not in result
        assert "?token=***" in result

    def test_ampersand_query_param_redacted(self) -> None:
        """``&token=VALUE`` query parameter is redacted, preserving ``&``."""
        text = "https://host/path?ref=main&token=secret42"
        result = redact_token(text)
        assert "secret42" not in result
        assert "&token=***" in result

    def test_no_token_passthrough(self) -> None:
        """Text without any token patterns is returned unchanged."""
        text = "fatal: repository not found"
        assert redact_token(text) == text

    def test_multiple_tokens_in_one_string(self) -> None:
        """All token occurrences in a single string are redacted."""
        text = (
            "https://user:pass1@github.com/a/b "
            "https://user:pass2@github.com/c/d"
        )
        result = redact_token(text)
        assert "pass1" not in result
        assert "pass2" not in result

    def test_mixed_patterns(self) -> None:
        """A string containing both URL-auth and query-param tokens."""
        text = (
            "clone https://tok@host/repo "
            "archive at https://host/file?token=xyz"
        )
        result = redact_token(text)
        assert "tok@" not in result
        assert "xyz" not in result
        assert "***" in result
