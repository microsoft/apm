"""Credential decisions use only hosts every URL parser agrees on (#3649)."""

from __future__ import annotations

import pytest

from apm_cli.utils.github_host import (
    has_parser_dependent_url_chars,
    unambiguous_https_hostname,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.githubcopilot.com/mcp/", "api.githubcopilot.com"),
        ("HTTPS://API.GitHub.com/v1", "api.github.com"),
        ("https://api.github.com:443/v1", "api.github.com"),
    ],
)
def test_plain_https_url_returns_lowercase_host(url: str, expected: str) -> None:
    assert unambiguous_https_hostname(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example\\@api.githubcopilot.com/mcp/",
        "https://api.githubcopilot.com\\.other.example/",
        "https://user@api.githubcopilot.com/",
        "https://user:pw@api.githubcopilot.com/",
        "https://api.github\tcom/",
        "https://api.github.com /x",
        "http://api.github.com/",
        "//api.github.com/x",
        "api.github.com/x",
        "https://api.github.com:notaport/",
        "https:///path-only",
        "",
        None,
    ],
)
def test_ambiguous_or_non_https_url_has_no_host(url: str | None) -> None:
    assert unambiguous_https_hostname(url) is None


def test_parser_dependent_characters_are_detected() -> None:
    assert has_parser_dependent_url_chars("https://a\\@b/")
    assert has_parser_dependent_url_chars("https://a b/")
    assert has_parser_dependent_url_chars("https://a\x00b/")
    assert not has_parser_dependent_url_chars("https://a.example/path?q=1#f")
