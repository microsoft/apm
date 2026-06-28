"""Vectors 3, 4, 7: header expansion abuse and edge cases.

Covers case-insensitivity, multi-var headers mixing a safe and a
suffix-gap secret, CRLF header injection through an expanded value, and
the single-pass (no recursive double-expansion) guarantee.
"""

from __future__ import annotations

import pytest

from apm_cli.core.script_executors import _expand_env_vars


class TestCaseInsensitivity:
    """IGNORECASE means lowercase credential names are still blocked."""

    def test_lowercase_token_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("my_token", "lowercase-secret")
        assert _expand_env_vars("$my_token") == ""

    def test_mixedcase_secret_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("My_Secret", "mixed-secret")
        assert _expand_env_vars("${My_Secret}") == ""


class TestMultiVarHeader:
    """A header mixing a safe var and a suffix-gap secret must not leak the secret."""

    def test_safe_var_still_expands(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REGION", "us-east-1")
        assert _expand_env_vars("region=$REGION") == "region=us-east-1"

    def test_mixed_header_does_not_leak_suffix_gap_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIASECRETVALUE99")
        out = _expand_env_vars("pre-$AWS_ACCESS_KEY_ID-$REGION-post")
        # Secure expectation: the secret component is blank, region still fills.
        assert "AKIASECRETVALUE99" not in out, "suffix-gap secret leaked in multi-var header"
        assert "us-east-1" in out


class TestCrlfInjection:
    """An expanded env value containing CRLF must not inject extra headers."""

    def test_expanded_value_strips_crlf(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A non-denylisted var whose value carries a smuggled header.
        monkeypatch.setenv("TRACE_ID", "abc\r\nX-Evil: injected")
        out = _expand_env_vars("trace=$TRACE_ID")
        # Secure expectation: CR/LF removed so no header smuggling is possible.
        assert "\r" not in out and "\n" not in out, "CRLF survived header expansion"


class TestSinglePassExpansion:
    """Expansion is single-pass: a value that itself looks like $OTHER is literal."""

    def test_no_recursive_double_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OUTER", "$GITHUB_TOKEN")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_appear")
        out = _expand_env_vars("$OUTER")
        # Defended behaviour: literal "$GITHUB_TOKEN" passes through, never resolved.
        assert out == "$GITHUB_TOKEN"
        assert "ghp_should_not_appear" not in out
