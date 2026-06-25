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


class TestAuditOnInstallConfig:
    """get/set/unset for the audit-on-install user default."""

    def test_default_is_off(self, isolated_config):
        assert config_mod.get_audit_on_install() == "off"

    def test_set_and_get_roundtrip(self, isolated_config):
        config_mod.set_audit_on_install("warn")
        assert config_mod.get_audit_on_install() == "warn"

    def test_set_normalizes_case(self, isolated_config):
        config_mod.set_audit_on_install("BLOCK")
        assert config_mod.get_audit_on_install() == "block"

    def test_set_rejects_invalid(self, isolated_config):
        with pytest.raises(ValueError, match="Invalid value"):
            config_mod.set_audit_on_install("nope")

    def test_unset_falls_back_to_default(self, isolated_config):
        config_mod.set_audit_on_install("block")
        config_mod.unset_audit_on_install()
        assert config_mod.get_audit_on_install() == "off"

    def test_unset_is_noop_when_absent(self, isolated_config):
        # Should not raise when the key was never set.
        config_mod.unset_audit_on_install()
        assert config_mod.get_audit_on_install() == "off"

    def test_corrupt_value_falls_back_to_default(self, isolated_config):
        config_mod.update_config({"audit_on_install": "garbage"})
        assert config_mod.get_audit_on_install() == "off"


class TestInstallTargetConfig:
    """get/set/unset for install default target."""

    def test_default_is_none(self, isolated_config):
        assert config_mod.get_install_target() is None

    def test_set_and_get_roundtrip(self, isolated_config):
        config_mod.set_install_target("claude")
        assert config_mod.get_install_target() == "claude"

    def test_set_rejects_invalid(self, isolated_config):
        with pytest.raises(ValueError, match="is not a valid target"):
            config_mod.set_install_target("MyAIProvider")

    def test_unset_removes_key(self, isolated_config):
        config_mod.set_install_target("claude")
        config_mod.unset_install_target()
        assert config_mod.get_install_target() is None

    def test_corrupt_value_falls_back_to_none(self, isolated_config):
        config_mod.update_config({"install_target": "not-a-target"})
        assert config_mod.get_install_target() is None


class TestExternalScannerOptions:
    """Round-trip the external_scanners config helpers."""

    def test_defaults_are_none(self, isolated_config):
        assert config_mod.get_scanner_config("skillspector") is None
        assert config_mod.get_scanner_options("skillspector") == (None, None)

    def test_set_and_get_llm(self, isolated_config):
        config_mod.set_scanner_llm("skillspector", True)
        llm, args = config_mod.get_scanner_options("skillspector")
        assert llm is True
        assert args is None

    def test_set_and_get_args(self, isolated_config):
        config_mod.set_scanner_args("skillspector", ["--model", "gpt-4o"])
        llm, args = config_mod.get_scanner_options("skillspector")
        assert llm is None
        assert args == ("--model", "gpt-4o")

    def test_set_both_fields_coexist(self, isolated_config):
        config_mod.set_scanner_llm("skillspector", False)
        config_mod.set_scanner_args("skillspector", ["--severity", "high"])
        assert config_mod.get_scanner_options("skillspector") == (
            False,
            ("--severity", "high"),
        )

    def test_unset_llm_keeps_args(self, isolated_config):
        config_mod.set_scanner_llm("skillspector", True)
        config_mod.set_scanner_args("skillspector", ["--model", "x"])
        config_mod.unset_scanner_llm("skillspector")
        assert config_mod.get_scanner_options("skillspector") == (None, ("--model", "x"))

    def test_unset_last_field_prunes_entry(self, isolated_config):
        config_mod.set_scanner_llm("skillspector", True)
        config_mod.unset_scanner_llm("skillspector")
        assert config_mod.get_scanner_config("skillspector") is None

    def test_unset_scanner_removes_entry(self, isolated_config):
        config_mod.set_scanner_llm("skillspector", True)
        config_mod.set_scanner_args("skillspector", ["--model", "x"])
        config_mod.unset_scanner("skillspector")
        assert config_mod.get_scanner_config("skillspector") is None

    def test_corrupt_llm_falls_back_to_none(self, isolated_config):
        config_mod.update_config({"external_scanners": {"skillspector": {"llm": "garbage"}}})
        llm, _ = config_mod.get_scanner_options("skillspector")
        assert llm is None

    def test_corrupt_args_falls_back_to_none(self, isolated_config):
        config_mod.update_config({"external_scanners": {"skillspector": {"args": "not-a-list"}}})
        _, args = config_mod.get_scanner_options("skillspector")
        assert args is None


class TestMcpRegistryUrlConfig:
    """get/set/unset for the mcp-registry-url user config -- issue #818."""

    def test_get_returns_none_when_absent(self, isolated_config):
        assert config_mod.get_mcp_registry_url() is None

    def test_set_and_get_round_trip(self, isolated_config):
        config_mod.set_mcp_registry_url("https://corp.mcp.example.com")
        config_mod._invalidate_config_cache()
        assert config_mod.get_mcp_registry_url() == "https://corp.mcp.example.com"

    def test_set_strips_trailing_slash(self, isolated_config):
        config_mod.set_mcp_registry_url("https://corp.mcp.example.com/")
        assert config_mod.get_mcp_registry_url() == "https://corp.mcp.example.com"

    def test_set_allows_http_url(self, isolated_config):
        config_mod.set_mcp_registry_url("http://internal.corp/mcp")
        assert config_mod.get_mcp_registry_url() == "http://internal.corp/mcp"

    def test_set_rejects_empty_url(self, isolated_config):
        with pytest.raises(ValueError, match="cannot be empty"):
            config_mod.set_mcp_registry_url("   ")

    def test_set_rejects_file_scheme(self, isolated_config):
        with pytest.raises(ValueError, match="not supported"):
            config_mod.set_mcp_registry_url("file:///etc/hosts")

    def test_set_rejects_ws_scheme(self, isolated_config):
        with pytest.raises(ValueError, match="not supported"):
            config_mod.set_mcp_registry_url("ws://example.com/mcp")

    def test_set_rejects_missing_netloc(self, isolated_config):
        with pytest.raises(ValueError, match="Invalid URL"):
            config_mod.set_mcp_registry_url("https://")

    def test_set_rejects_embedded_credentials(self, isolated_config):
        with pytest.raises(ValueError, match="must not contain credentials"):
            config_mod.set_mcp_registry_url("https://user:token@corp.mcp.example.com")

    def test_unset_removes_key(self, isolated_config):
        config_mod.set_mcp_registry_url("https://corp.mcp.example.com")
        config_mod.unset_mcp_registry_url()
        assert config_mod.get_mcp_registry_url() is None

    def test_unset_is_noop_when_absent(self, isolated_config):
        config_mod.unset_mcp_registry_url()
        assert config_mod.get_mcp_registry_url() is None
