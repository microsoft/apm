"""Tests for ref pin cache (immutability advisory) (pure logic cases)."""

import pytest

from apm_cli.marketplace.version_pins import (
    _pin_key,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Unit tests -- load / save
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Unit tests -- record / check
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Unit tests -- key normalization
# ---------------------------------------------------------------------------


class TestPinKey:
    """Pin key construction and normalization."""

    def test_lowercase(self):
        assert _pin_key("MKT", "Plugin") == "mkt/plugin"

    def test_already_lower(self):
        assert _pin_key("mkt", "plugin") == "mkt/plugin"

    def test_with_version(self):
        assert _pin_key("MKT", "Plugin", "2.1.0") == "mkt/plugin/2.1.0"

    def test_empty_version_omits_segment(self):
        assert _pin_key("mkt", "plugin", "") == "mkt/plugin"


# ---------------------------------------------------------------------------
# Unit tests -- pins_path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fail-open behavior
# ---------------------------------------------------------------------------
