"""Unit tests for apm_cli.install.mcp.args module.

Covers:
- parse_kv_pairs with None → empty dict
- parse_kv_pairs with empty iterable → empty dict
- parse_kv_pairs with valid KEY=VALUE pairs → correct dict
- parse_kv_pairs with VALUE containing '=' → key is first segment, value is remainder
- parse_kv_pairs with missing '=' → raises click.UsageError
- parse_kv_pairs with empty key (=value) → raises click.UsageError
- parse_env_pairs delegates with flag_name="--env"
- parse_header_pairs delegates with flag_name="--header"
"""

from __future__ import annotations

import click
import pytest

from apm_cli.install.mcp.args import (
    parse_env_pairs,
    parse_header_pairs,
    parse_kv_pairs,
)


class TestParseKvPairs:
    """Tests for parse_kv_pairs()."""

    # ------------------------------------------------------------------
    # Empty / None input
    # ------------------------------------------------------------------

    def test_none_input_returns_empty_dict(self) -> None:
        result = parse_kv_pairs(None, flag_name="--flag")
        assert result == {}

    def test_empty_iterable_returns_empty_dict(self) -> None:
        result = parse_kv_pairs([], flag_name="--flag")
        assert result == {}

    def test_empty_tuple_returns_empty_dict(self) -> None:
        result = parse_kv_pairs((), flag_name="--flag")
        assert result == {}

    # ------------------------------------------------------------------
    # Valid pairs
    # ------------------------------------------------------------------

    def test_single_valid_pair(self) -> None:
        result = parse_kv_pairs(["KEY=VALUE"], flag_name="--flag")
        assert result == {"KEY": "VALUE"}

    def test_multiple_valid_pairs(self) -> None:
        result = parse_kv_pairs(["FOO=bar", "BAZ=qux", "HELLO=world"], flag_name="--flag")
        assert result == {"FOO": "bar", "BAZ": "qux", "HELLO": "world"}

    def test_value_with_equals_sign_in_it(self) -> None:
        """VALUE may contain '='; only the first '=' splits key/value."""
        result = parse_kv_pairs(["URL=http://host?a=1&b=2"], flag_name="--flag")
        assert result == {"URL": "http://host?a=1&b=2"}

    def test_value_with_multiple_equals_signs(self) -> None:
        result = parse_kv_pairs(["KEY=a=b=c"], flag_name="--flag")
        assert result == {"KEY": "a=b=c"}

    def test_empty_value_is_allowed(self) -> None:
        """KEY= (empty value) is valid."""
        result = parse_kv_pairs(["EMPTY="], flag_name="--flag")
        assert result == {"EMPTY": ""}

    def test_last_duplicate_key_wins(self) -> None:
        """If the same key appears twice, the last value wins."""
        result = parse_kv_pairs(["K=first", "K=second"], flag_name="--flag")
        assert result == {"K": "second"}

    def test_generator_input_accepted(self) -> None:
        pairs = (p for p in ["A=1", "B=2"])
        result = parse_kv_pairs(pairs, flag_name="--flag")
        assert result == {"A": "1", "B": "2"}

    # ------------------------------------------------------------------
    # Error cases
    # ------------------------------------------------------------------

    def test_missing_equals_raises_usage_error(self) -> None:
        with pytest.raises(click.UsageError, match=r"Invalid --env 'NOEQ'"):
            parse_kv_pairs(["NOEQ"], flag_name="--env")

    def test_missing_equals_error_contains_raw_value(self) -> None:
        with pytest.raises(click.UsageError, match=r"expected KEY=VALUE"):
            parse_kv_pairs(["NO_EQUALS_HERE"], flag_name="--flag")

    def test_empty_key_raises_usage_error(self) -> None:
        with pytest.raises(click.UsageError, match=r"key cannot be empty"):
            parse_kv_pairs(["=value"], flag_name="--flag")

    def test_empty_key_error_contains_raw_value(self) -> None:
        with pytest.raises(click.UsageError, match=r"Invalid --header '=v'"):
            parse_kv_pairs(["=v"], flag_name="--header")

    def test_flag_name_included_in_missing_equals_error(self) -> None:
        with pytest.raises(click.UsageError, match=r"Invalid --my-flag 'bad'"):
            parse_kv_pairs(["bad"], flag_name="--my-flag")

    def test_error_on_first_invalid_pair_stops_processing(self) -> None:
        """Exception is raised on the first bad pair; no partial dict is returned."""
        with pytest.raises(click.UsageError):
            parse_kv_pairs(["A=1", "BAD", "C=3"], flag_name="--flag")


# ---------------------------------------------------------------------------
# parse_env_pairs
# ---------------------------------------------------------------------------


class TestParseEnvPairs:
    """Tests for parse_env_pairs() — thin wrapper around parse_kv_pairs."""

    def test_delegates_correctly_with_valid_pairs(self) -> None:
        result = parse_env_pairs(["HOME=/root", "TERM=xterm"])
        assert result == {"HOME": "/root", "TERM": "xterm"}

    def test_none_input_returns_empty_dict(self) -> None:
        assert parse_env_pairs(None) == {}

    def test_flag_name_is_env_in_error(self) -> None:
        """Error message must say '--env'."""
        with pytest.raises(click.UsageError, match=r"--env"):
            parse_env_pairs(["NOEQ"])

    def test_empty_key_error_flag_name(self) -> None:
        with pytest.raises(click.UsageError, match=r"--env"):
            parse_env_pairs(["=val"])


# ---------------------------------------------------------------------------
# parse_header_pairs
# ---------------------------------------------------------------------------


class TestParseHeaderPairs:
    """Tests for parse_header_pairs() — thin wrapper around parse_kv_pairs."""

    def test_delegates_correctly_with_valid_pairs(self) -> None:
        result = parse_header_pairs(
            ["Authorization=Bearer token123", "Content-Type=application/json"]
        )
        assert result == {
            "Authorization": "Bearer token123",
            "Content-Type": "application/json",
        }

    def test_none_input_returns_empty_dict(self) -> None:
        assert parse_header_pairs(None) == {}

    def test_flag_name_is_header_in_error(self) -> None:
        """Error message must say '--header'."""
        with pytest.raises(click.UsageError, match=r"--header"):
            parse_header_pairs(["NOEQ"])

    def test_empty_key_error_flag_name(self) -> None:
        with pytest.raises(click.UsageError, match=r"--header"):
            parse_header_pairs(["=val"])
