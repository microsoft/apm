"""pack --target all is universal."""

from __future__ import annotations

import pytest

from apm_cli.bundle.local_bundle import check_target_mismatch

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-accepted-target-vocabulary")]

# ---------------------------------------------------------------------------
# Minimal fixtures -- intentionally lighter than the air-gap E2E suite.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# D1: pack.target "all" -> no mismatch warning
# ---------------------------------------------------------------------------


class TestPackTargetAllIsUniversal:
    def test_check_target_mismatch_returns_none_for_all(self) -> None:
        assert check_target_mismatch(["all"], ["opencode"]) is None
        assert check_target_mismatch(["all"], ["copilot", "claude"]) is None
        assert check_target_mismatch(["all"], []) is None

    def test_check_target_mismatch_ignores_legacy_minimal_pack_target(self) -> None:
        # Older `apm pack` recorded "minimal" for projects with no target folder.
        assert check_target_mismatch(["minimal"], ["claude"]) is None

    def test_check_target_mismatch_ignores_unknown_pack_targets(self) -> None:
        # pack.target is informational: a name from a newer APM must not crash install.
        assert check_target_mismatch(["future-harness"], ["claude"]) is None
        warning = check_target_mismatch(["future-harness", "copilot"], ["opencode"])
        assert warning is not None and "copilot" in warning and "future-harness" not in warning

    def test_check_target_mismatch_still_warns_on_real_mismatch(self) -> None:
        warning = check_target_mismatch(["copilot"], ["opencode"])
        assert warning is not None
        assert "copilot" in warning
        assert "opencode" in warning


# ---------------------------------------------------------------------------
# D2.a: plugin.json never deployed (case-insensitive)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# D2: .mcp.json never deployed as a flat file
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# D2.b: instructions staged for compile-only targets
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# D3: no false "Install interrupted"
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# D2.c: bundle .mcp.json wired through MCPIntegrator (per-target dispatch)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PR #1217 review: nested instruction paths must not collide at staging
# ---------------------------------------------------------------------------
