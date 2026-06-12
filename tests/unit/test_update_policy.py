"""Unit tests for apm_cli.update_policy module.

Covers:
- _is_printable_ascii() boundary conditions
- is_self_update_enabled() with True / False / non-bool values
- get_self_update_disabled_message() edge cases
- get_update_hint_message() branching
"""

from __future__ import annotations

from unittest.mock import patch

import apm_cli.update_policy as policy_mod
from apm_cli.update_policy import (
    DEFAULT_SELF_UPDATE_DISABLED_MESSAGE,
    _is_printable_ascii,
    get_self_update_disabled_message,
    get_update_hint_message,
    is_self_update_enabled,
)

# ---------------------------------------------------------------------------
# _is_printable_ascii
# ---------------------------------------------------------------------------


class TestIsPrintableAscii:
    """Tests for _is_printable_ascii()."""

    def test_empty_string_returns_true(self) -> None:
        """Empty string vacuously satisfies the predicate."""
        assert _is_printable_ascii("") is True

    def test_regular_ascii_returns_true(self) -> None:
        assert _is_printable_ascii("Hello, World!") is True

    def test_space_char_returns_true(self) -> None:
        """Space (0x20) is the lower boundary of printable ASCII."""
        assert _is_printable_ascii(" ") is True

    def test_tilde_char_returns_true(self) -> None:
        """Tilde (0x7E) is the upper boundary of printable ASCII."""
        assert _is_printable_ascii("~") is True

    def test_control_char_returns_false(self) -> None:
        """Tab (0x09) is below the printable boundary."""
        assert _is_printable_ascii("hello\tworld") is False

    def test_newline_returns_false(self) -> None:
        assert _is_printable_ascii("line1\nline2") is False

    def test_null_byte_returns_false(self) -> None:
        assert _is_printable_ascii("\x00") is False

    def test_non_ascii_unicode_returns_false(self) -> None:
        """Characters above 0x7E (e.g. é, ñ) are rejected."""
        assert _is_printable_ascii("café") is False

    def test_del_char_returns_false(self) -> None:
        """DEL (0x7F) is above the upper boundary."""
        assert _is_printable_ascii("\x7f") is False

    def test_mixed_valid_and_invalid_returns_false(self) -> None:
        assert _is_printable_ascii("valid\x01invalid") is False


# ---------------------------------------------------------------------------
# is_self_update_enabled
# ---------------------------------------------------------------------------


class TestIsSelfUpdateEnabled:
    """Tests for is_self_update_enabled()."""

    def test_returns_true_when_enabled_is_true(self) -> None:
        with patch.object(policy_mod, "SELF_UPDATE_ENABLED", True):
            assert is_self_update_enabled() is True

    def test_returns_false_when_enabled_is_false(self) -> None:
        with patch.object(policy_mod, "SELF_UPDATE_ENABLED", False):
            assert is_self_update_enabled() is False

    def test_returns_false_when_enabled_is_int_one(self) -> None:
        """Integer 1 is not True via strict identity (``is True``)."""
        with patch.object(policy_mod, "SELF_UPDATE_ENABLED", 1):
            assert is_self_update_enabled() is False

    def test_returns_false_when_enabled_is_none(self) -> None:
        with patch.object(policy_mod, "SELF_UPDATE_ENABLED", None):
            assert is_self_update_enabled() is False

    def test_returns_false_when_enabled_is_string(self) -> None:
        with patch.object(policy_mod, "SELF_UPDATE_ENABLED", "true"):
            assert is_self_update_enabled() is False


# ---------------------------------------------------------------------------
# get_self_update_disabled_message
# ---------------------------------------------------------------------------


class TestGetSelfUpdateDisabledMessage:
    """Tests for get_self_update_disabled_message()."""

    def test_returns_default_when_message_is_none(self) -> None:
        with patch.object(policy_mod, "SELF_UPDATE_DISABLED_MESSAGE", None):
            result = get_self_update_disabled_message()
        assert result == DEFAULT_SELF_UPDATE_DISABLED_MESSAGE

    def test_returns_default_when_message_is_empty_string(self) -> None:
        with patch.object(policy_mod, "SELF_UPDATE_DISABLED_MESSAGE", ""):
            result = get_self_update_disabled_message()
        assert result == DEFAULT_SELF_UPDATE_DISABLED_MESSAGE

    def test_returns_default_when_message_is_whitespace_only(self) -> None:
        with patch.object(policy_mod, "SELF_UPDATE_DISABLED_MESSAGE", "   "):
            result = get_self_update_disabled_message()
        assert result == DEFAULT_SELF_UPDATE_DISABLED_MESSAGE

    def test_returns_default_when_message_has_non_printable_ascii(self) -> None:
        with patch.object(policy_mod, "SELF_UPDATE_DISABLED_MESSAGE", "update via\x00manager"):
            result = get_self_update_disabled_message()
        assert result == DEFAULT_SELF_UPDATE_DISABLED_MESSAGE

    def test_returns_default_when_message_has_unicode(self) -> None:
        with patch.object(policy_mod, "SELF_UPDATE_DISABLED_MESSAGE", "update via café"):
            result = get_self_update_disabled_message()
        assert result == DEFAULT_SELF_UPDATE_DISABLED_MESSAGE

    def test_returns_custom_message_when_valid(self) -> None:
        custom = "Use brew upgrade apm to update."
        with patch.object(policy_mod, "SELF_UPDATE_DISABLED_MESSAGE", custom):
            result = get_self_update_disabled_message()
        assert result == custom

    def test_strips_leading_trailing_whitespace(self) -> None:
        """Message is stripped before validation and returned stripped."""
        custom = "  Use brew upgrade apm.  "
        with patch.object(policy_mod, "SELF_UPDATE_DISABLED_MESSAGE", custom):
            result = get_self_update_disabled_message()
        assert result == "Use brew upgrade apm."

    def test_non_string_coerced_to_string(self) -> None:
        """Non-string values are str()-coerced before use."""
        with patch.object(policy_mod, "SELF_UPDATE_DISABLED_MESSAGE", 42):
            result = get_self_update_disabled_message()
        assert result == "42"


# ---------------------------------------------------------------------------
# get_update_hint_message
# ---------------------------------------------------------------------------


class TestGetUpdateHintMessage:
    """Tests for get_update_hint_message()."""

    def test_returns_run_apm_update_when_enabled(self) -> None:
        with patch.object(policy_mod, "SELF_UPDATE_ENABLED", True):
            result = get_update_hint_message()
        assert result == "Run apm update to upgrade"

    def test_returns_disabled_message_when_disabled(self) -> None:
        custom = "Use your package manager to update."
        with patch.object(policy_mod, "SELF_UPDATE_ENABLED", False):
            with patch.object(policy_mod, "SELF_UPDATE_DISABLED_MESSAGE", custom):
                result = get_update_hint_message()
        assert result == custom

    def test_returns_default_disabled_message_when_disabled_and_message_none(
        self,
    ) -> None:
        with patch.object(policy_mod, "SELF_UPDATE_ENABLED", False):
            with patch.object(policy_mod, "SELF_UPDATE_DISABLED_MESSAGE", None):
                result = get_update_hint_message()
        assert result == DEFAULT_SELF_UPDATE_DISABLED_MESSAGE
