"""Unit tests for ``apm_cli.core.build_orchestrator`` (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.core.build_orchestrator import (
    BuildOrchestrator,
    BuildResult,  # noqa: F401
    OutputKind,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# detect_outputs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BuildOrchestrator
# ---------------------------------------------------------------------------


class TestBuildOrchestrator:
    def test_default_producers_are_bundle_and_marketplace(self):
        orch = BuildOrchestrator()
        kinds = [p.kind for p in orch._producers]
        assert OutputKind.BUNDLE in kinds
        assert OutputKind.MARKETPLACE in kinds

    def test_default_producers_include_plugin_manifest(self):
        orch = BuildOrchestrator()
        kinds = [p.kind for p in orch._producers]
        assert OutputKind.PLUGIN_MANIFEST in kinds

    def test_plugin_manifest_producer_ordering(self):
        orch = BuildOrchestrator()
        kinds = [p.kind for p in orch._producers]
        bundle_idx = kinds.index(OutputKind.BUNDLE)
        marketplace_idx = kinds.index(OutputKind.MARKETPLACE)
        plugin_idx = kinds.index(OutputKind.PLUGIN_MANIFEST)
        assert plugin_idx > bundle_idx
        assert plugin_idx > marketplace_idx


# ---------------------------------------------------------------------------
# TestPluginManifestProducer
# ---------------------------------------------------------------------------
