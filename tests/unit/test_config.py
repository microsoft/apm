"""Tests for apm_cli.config module-level config file I/O.

These tests exercise the round-trip of non-ASCII content through the global
config file to guard against the cp1252/cp950 UnicodeDecodeError class of
bugs on Windows when ``open()`` is called without an explicit encoding.
"""

import json

import pytest

from apm_cli import config as config_mod


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point CONFIG_DIR / CONFIG_FILE to a temp directory and clear cache."""
    config_dir = tmp_path / ".apm"
    config_file = config_dir / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_file))
    monkeypatch.setattr(config_mod, "_config_cache", None)
    return config_file


class TestConfigUtf8RoundTrip:
    """Round-trip non-ASCII content through the config file."""

    def test_update_config_preserves_non_ascii(self, isolated_config):
        non_ascii_value = "/Users/cafe/projets/\u958b\u59cb"
        config_mod.update_config({"copilot_cowork_skills_dir": non_ascii_value})

        # Force re-read from disk by invalidating the cache.
        config_mod._invalidate_config_cache()
        loaded = config_mod.get_config()

        assert loaded["copilot_cowork_skills_dir"] == non_ascii_value

    def test_config_file_is_utf8_on_disk(self, isolated_config):
        non_ascii_value = "# \u958b\u59cb -- cafe"
        config_mod.update_config({"note": non_ascii_value})

        # Read raw bytes and decode as UTF-8 to assert the on-disk encoding.
        raw = isolated_config.read_bytes()
        decoded = json.loads(raw.decode("utf-8"))
        assert decoded["note"] == non_ascii_value

    def test_ensure_config_exists_uses_utf8(self, isolated_config, monkeypatch):
        # Force ensure_config_exists() to create the file.
        config_mod.ensure_config_exists()
        assert isolated_config.exists()
        # File must be readable as UTF-8 JSON.
        json.loads(isolated_config.read_bytes().decode("utf-8"))
