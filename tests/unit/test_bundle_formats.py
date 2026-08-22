"""Unit tests for canonical bundle format selection."""

from __future__ import annotations

import pytest

from apm_cli.bundle import formats
from apm_cli.bundle.formats import (
    PREFERRED_PLUGIN_FORMAT,
    BundleFormat,
    agent_plugin_warning,
    coerce_bundle_format,
    resolve_bundle_format,
)


class TestBundleFormatSelection:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, BundleFormat.CLAUDE_PLUGIN),
            ("plugin", BundleFormat.CLAUDE_PLUGIN),
            ("agent-plugin", BundleFormat.AGENT_PLUGIN),
            ("claude", BundleFormat.CLAUDE_PLUGIN),
            ("claude-plugin", BundleFormat.CLAUDE_PLUGIN),
            ("apm", BundleFormat.APM),
        ],
    )
    def test_coerce_bundle_format(self, value, expected):
        assert coerce_bundle_format(value) is expected

    def test_preferred_format_seam_preserves_legacy_until_t10(self):
        assert PREFERRED_PLUGIN_FORMAT is BundleFormat.CLAUDE_PLUGIN
        assert resolve_bundle_format(None) is PREFERRED_PLUGIN_FORMAT

    def test_conflicting_selectors_raise(self):
        with pytest.raises(ValueError, match="Choose one bundle format selector"):
            resolve_bundle_format("apm", claude_plugin=True)

    @pytest.mark.parametrize(
        "fmt",
        [
            "plugin",
            "claude-plugin",
        ],
    )
    def test_redundant_selectors_are_rejected(self, fmt):
        with pytest.raises(ValueError, match="Choose one bundle format selector"):
            resolve_bundle_format(fmt, claude_plugin=True)


class TestAgentPluginWarningWindow:
    def test_warning_is_disabled_before_default_flip(self):
        assert agent_plugin_warning("0.30.0") is None

    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("0.28.9", None),
            ("0.29.0", "apm pack now defaults to Agent Plugin output"),
            ("0.33.99", "apm pack now defaults to Agent Plugin output"),
            ("0.34.0", None),
        ],
    )
    def test_warning_window_after_t10(self, version, expected, monkeypatch):
        monkeypatch.setattr(formats, "PREFERRED_PLUGIN_FORMAT", BundleFormat.AGENT_PLUGIN)
        warning = agent_plugin_warning(version)
        if expected is None:
            assert warning is None
        else:
            assert warning is not None
            assert expected in warning
