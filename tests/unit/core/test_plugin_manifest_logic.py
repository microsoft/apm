"""Unit tests for ``apm_cli.core.plugin_manifest`` (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.core.plugin_manifest import (
    PLUGIN_ECOSYSTEM_PATHS,
    PLUGIN_MANIFEST_ECOSYSTEMS,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# TestCollectMcpServers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestBuildPluginManifest
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestWritePluginManifest
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestFindOrSynthesizePluginJson
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_plugin_manifest_ecosystems_frozenset(self) -> None:
        assert isinstance(PLUGIN_MANIFEST_ECOSYSTEMS, frozenset)
        assert {"claude", "copilot", "codex"} == PLUGIN_MANIFEST_ECOSYSTEMS

    def test_plugin_ecosystem_paths_keys_match_ecosystems(self) -> None:
        assert set(PLUGIN_ECOSYSTEM_PATHS.keys()) == PLUGIN_MANIFEST_ECOSYSTEMS

    def test_claude_path(self) -> None:
        assert PLUGIN_ECOSYSTEM_PATHS["claude"] == ".claude-plugin/plugin.json"

    def test_copilot_path(self) -> None:
        assert PLUGIN_ECOSYSTEM_PATHS["copilot"] == ".github/plugin/plugin.json"

    def test_codex_path(self) -> None:
        assert PLUGIN_ECOSYSTEM_PATHS["codex"] == ".codex-plugin/plugin.json"
