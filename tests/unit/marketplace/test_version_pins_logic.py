"""Tests for ref pin cache (immutability advisory) (pure logic cases)."""

import os
from unittest.mock import patch

import pytest

from apm_cli.marketplace.version_pins import (
    _pin_key,
    _pins_path,
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


class TestPinsPath:
    """Path construction for the pins file."""

    def test_default_dir(self):
        """Default path (no pins_dir) includes version-pins.json under CONFIG_DIR."""
        with patch("apm_cli.config.CONFIG_DIR", "/fake/.apm"):
            result = _pins_path(pins_dir=None)
        assert result == os.path.join("/fake/.apm", "cache", "marketplace", "version-pins.json")


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fail-open behavior
# ---------------------------------------------------------------------------
