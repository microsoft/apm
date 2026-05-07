"""Unit tests for ``format_short_sha`` (F3, microsoft/apm#1116).

Why this helper exists:
- Every install download/cached line previously did its own
  ``commit[:8]`` slice, which silently truncated sentinel strings
  (``"cached"``, ``"unknown"``) and non-hex garbage to a plausible
  8-char prefix. Reviewers could not tell whether the SHA was real.
- Centralising the truncation in one helper, with one rule, means the
  install summary either shows a real short SHA or shows nothing.
"""

import pytest

from apm_cli.utils.short_sha import format_short_sha


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "cached",
        "unknown",
        "CACHED",
        "Unknown",
        "abc",  # too short
        "abcdefg",  # 7 chars, still too short
        "deadbeefXY",  # contains non-hex
        b"abcdef0123",  # not str
        12345,
        ("abcdef0123",),
    ],
)
def test_invalid_inputs_collapse_to_empty(value):
    assert format_short_sha(value) == ""


def test_valid_full_sha1_truncates_to_8():
    full = "abcdef0123456789abcdef0123456789abcdef01"
    assert format_short_sha(full) == "abcdef01"


def test_valid_short_hex_8_chars_passes_through():
    assert format_short_sha("abcdef01") == "abcdef01"


def test_valid_full_sha256_truncates_to_8():
    full = "f" * 64
    assert format_short_sha(full) == "ffffffff"


def test_uppercase_hex_accepted():
    assert format_short_sha("ABCDEF0123") == "ABCDEF01"


def test_whitespace_stripped_before_validation():
    assert format_short_sha("  abcdef0123  ") == "abcdef01"
