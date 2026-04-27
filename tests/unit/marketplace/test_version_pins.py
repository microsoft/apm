"""Tests for ref pin cache (immutability advisory).

Covers:
- Loading from missing / corrupt / valid pin files
- Recording and persisting pins
- Detecting ref changes (possible ref swap)
- Multi-plugin isolation
- Atomic write via os.replace
"""

import json
import os
from unittest.mock import patch

import pytest

from apm_cli.marketplace.version_pins import (
    _pin_key,
    _pins_path,
    check_ref_pin,
    load_ref_pins,
    record_ref_pin,
    save_ref_pins,
)


# ---------------------------------------------------------------------------
# Unit tests -- load / save
# ---------------------------------------------------------------------------


class TestLoadRefPins:
    """Loading the pin file from disk."""

    def test_load_empty_no_file(self, tmp_path):
        """Missing file returns empty dict."""
        result = load_ref_pins(pins_dir=str(tmp_path))
        assert result == {}

    def test_load_missing_no_warning_by_default(self, tmp_path, caplog):
        """Missing file does NOT warn when expect_exists is False (default)."""
        import logging
        with caplog.at_level(logging.WARNING):
            result = load_ref_pins(pins_dir=str(tmp_path))
        assert result == {}
        assert "expected but missing" not in caplog.text

    def test_load_missing_warns_when_expected(self, tmp_path, caplog):
        """Missing file warns when expect_exists=True."""
        import logging
        with caplog.at_level(logging.WARNING):
            result = load_ref_pins(pins_dir=str(tmp_path), expect_exists=True)
        assert result == {}
        assert "expected but missing" in caplog.text
        assert "ref-swap detection is disabled" in caplog.text

    def test_load_corrupt_json(self, tmp_path):
        """Corrupt JSON returns empty dict without raising."""
        path = tmp_path / "version-pins.json"
        path.write_text("{not valid json!!!")
        result = load_ref_pins(pins_dir=str(tmp_path))
        assert result == {}

    def test_load_non_dict_json(self, tmp_path):
        """JSON that is not an object returns empty dict."""
        path = tmp_path / "version-pins.json"
        path.write_text('["a list", "not a dict"]')
        result = load_ref_pins(pins_dir=str(tmp_path))
        assert result == {}

    def test_load_valid(self, tmp_path):
        """Valid JSON is returned as-is."""
        data = {"mkt/plug": "abc123"}
        path = tmp_path / "version-pins.json"
        path.write_text(json.dumps(data))
        result = load_ref_pins(pins_dir=str(tmp_path))
        assert result == data


class TestSaveRefPins:
    """Saving the pin file to disk."""

    def test_save_creates_file(self, tmp_path):
        """Save creates the file if it does not exist."""
        pins = {"mkt/plug": "ref1"}
        save_ref_pins(pins, pins_dir=str(tmp_path))

        path = tmp_path / "version-pins.json"
        assert path.exists()
        assert json.loads(path.read_text()) == pins

    def test_save_creates_parent_dirs(self, tmp_path):
        """Save creates intermediate directories if needed."""
        nested = tmp_path / "a" / "b"
        pins = {"mkt/plug": "ref2"}
        save_ref_pins(pins, pins_dir=str(nested))

        path = nested / "version-pins.json"
        assert path.exists()
        assert json.loads(path.read_text()) == pins


# ---------------------------------------------------------------------------
# Unit tests -- record / check
# ---------------------------------------------------------------------------


class TestRecordAndCheck:
    """Recording pins and checking for ref changes."""

    def test_record_and_load(self, tmp_path):
        """Record a pin and verify it persists on disk."""
        record_ref_pin("mkt", "plug", "sha-aaa", pins_dir=str(tmp_path))
        pins = load_ref_pins(pins_dir=str(tmp_path))
        assert pins["mkt/plug"] == "sha-aaa"

    def test_check_new_pin(self, tmp_path):
        """First time seeing a plugin returns None (no warning)."""
        result = check_ref_pin("mkt", "plug", "sha-aaa", pins_dir=str(tmp_path))
        assert result is None

    def test_check_matching_pin(self, tmp_path):
        """Same ref as previously recorded returns None."""
        record_ref_pin("mkt", "plug", "sha-aaa", pins_dir=str(tmp_path))
        result = check_ref_pin("mkt", "plug", "sha-aaa", pins_dir=str(tmp_path))
        assert result is None

    def test_check_changed_pin(self, tmp_path):
        """Different ref returns the previous (old) ref string."""
        record_ref_pin("mkt", "plug", "sha-aaa", pins_dir=str(tmp_path))
        result = check_ref_pin("mkt", "plug", "sha-bbb", pins_dir=str(tmp_path))
        assert result == "sha-aaa"

    def test_record_overwrites(self, tmp_path):
        """Recording the same plugin twice overwrites the old ref."""
        record_ref_pin("mkt", "plug", "sha-aaa", pins_dir=str(tmp_path))
        record_ref_pin("mkt", "plug", "sha-bbb", pins_dir=str(tmp_path))
        pins = load_ref_pins(pins_dir=str(tmp_path))
        assert pins["mkt/plug"] == "sha-bbb"

    def test_multiple_plugins(self, tmp_path):
        """Different plugins do not interfere with each other."""
        record_ref_pin("mkt", "alpha", "ref-a", pins_dir=str(tmp_path))
        record_ref_pin("mkt", "beta", "ref-b", pins_dir=str(tmp_path))

        assert check_ref_pin("mkt", "alpha", "ref-a", pins_dir=str(tmp_path)) is None
        assert check_ref_pin("mkt", "beta", "ref-b", pins_dir=str(tmp_path)) is None
        # Alpha ref changed, beta unchanged
        assert check_ref_pin("mkt", "alpha", "ref-x", pins_dir=str(tmp_path)) == "ref-a"
        assert check_ref_pin("mkt", "beta", "ref-b", pins_dir=str(tmp_path)) is None

    def test_version_scoped_pins_do_not_conflict(self, tmp_path):
        """Different versions of the same plugin get independent pins."""
        record_ref_pin("mkt", "plug", "sha-aaa", version="1.0.0", pins_dir=str(tmp_path))
        record_ref_pin("mkt", "plug", "sha-bbb", version="2.0.0", pins_dir=str(tmp_path))

        # Each version has its own pin
        assert check_ref_pin("mkt", "plug", "sha-aaa", version="1.0.0", pins_dir=str(tmp_path)) is None
        assert check_ref_pin("mkt", "plug", "sha-bbb", version="2.0.0", pins_dir=str(tmp_path)) is None

        # Changing v1's ref flags it without affecting v2
        assert check_ref_pin("mkt", "plug", "sha-xxx", version="1.0.0", pins_dir=str(tmp_path)) == "sha-aaa"
        assert check_ref_pin("mkt", "plug", "sha-bbb", version="2.0.0", pins_dir=str(tmp_path)) is None


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

    def test_custom_dir(self, tmp_path):
        result = _pins_path(pins_dir=str(tmp_path))
        assert result == os.path.join(str(tmp_path), "version-pins.json")

    def test_default_dir(self):
        """Default path (no pins_dir) includes version-pins.json under CONFIG_DIR."""
        with patch("apm_cli.config.CONFIG_DIR", "/fake/.apm"):
            result = _pins_path(pins_dir=None)
        assert result == os.path.join("/fake/.apm", "cache", "marketplace", "version-pins.json")


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    """Verify save uses atomic write pattern (tmp + os.replace)."""

    def test_atomic_write_uses_replace(self, tmp_path):
        """os.replace is called to atomically move the temp file."""
        pins = {"mkt/plug": "ref1"}

        with patch("apm_cli.marketplace.version_pins.os.replace", wraps=os.replace) as mock_replace:
            save_ref_pins(pins, pins_dir=str(tmp_path))
            mock_replace.assert_called_once()
            args = mock_replace.call_args[0]
            assert args[0].endswith(".tmp")
            assert args[1].endswith("version-pins.json")

    def test_no_tmp_file_remains(self, tmp_path):
        """After save, no .tmp file should remain on disk."""
        save_ref_pins({"k": "r"}, pins_dir=str(tmp_path))
        remaining = list(tmp_path.iterdir())
        assert all(not f.name.endswith(".tmp") for f in remaining)


# ---------------------------------------------------------------------------
# Fail-open behavior
# ---------------------------------------------------------------------------


class TestFailOpen:
    """Advisory system must never raise on I/O errors."""

    def test_save_to_readonly_dir_does_not_raise(self, tmp_path):
        """Save to an unwritable location logs and returns without error."""
        bad_dir = "/dev/null/impossible"
        save_ref_pins({"k": "r"}, pins_dir=bad_dir)

    def test_check_with_corrupt_file_returns_none(self, tmp_path):
        """check_ref_pin with corrupt file returns None (no warning)."""
        path = tmp_path / "version-pins.json"
        path.write_text("CORRUPT!!!")
        result = check_ref_pin("mkt", "plug", "ref", pins_dir=str(tmp_path))
        assert result is None

    def test_check_with_non_string_plugin_entry(self, tmp_path):
        """If the plugin entry is not a string, return None gracefully."""
        data = {"mkt/plug": {"nested": "dict"}}
        path = tmp_path / "version-pins.json"
        path.write_text(json.dumps(data))
        result = check_ref_pin("mkt", "plug", "ref", pins_dir=str(tmp_path))
        assert result is None
