"""Unit tests for apm_cli.models.format_detection (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.models.format_detection import (
    ApmYmlDetector,
    ClaudePluginDetector,
    FormatDetector,
    HookJsonDetector,
    SkillMdDetector,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-package-format-precedence")]

# ---------------------------------------------------------------------------
# ApmYmlDetector
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AgentPluginDetector
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SkillMdDetector
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# HookJsonDetector
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ClaudePluginDetector
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FormatDetector protocol conformance
# ---------------------------------------------------------------------------


class TestFormatDetectorProtocol:
    """Verify all detectors satisfy the FormatDetector protocol."""

    @pytest.mark.parametrize(
        "detector",
        [ApmYmlDetector(), SkillMdDetector(), HookJsonDetector(), ClaudePluginDetector()],
    )
    def test_conforms_to_protocol(self, detector: object) -> None:
        assert isinstance(detector, FormatDetector)


# ---------------------------------------------------------------------------
# PackageFormatRegistry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# NormalizationPlanner
# ---------------------------------------------------------------------------
