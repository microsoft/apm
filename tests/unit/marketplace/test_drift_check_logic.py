"""Tests for the ``apm pack --check-clean`` drift gate (Wave 4) (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.marketplace.drift_check import (
    DriftDifference,
    DriftOutputReport,
    json_key_diff,
    render_diff_lines,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# json_key_diff helper
# ---------------------------------------------------------------------------


class TestJsonKeyDiff:
    def test_identical_returns_empty(self):
        a = {"x": 1, "y": [1, 2]}
        b = {"x": 1, "y": [1, 2]}
        assert json_key_diff(a, b) == []

    def test_leaf_change_detected(self):
        a = {"x": 1}
        b = {"x": 2}
        diffs = json_key_diff(a, b)
        assert len(diffs) == 1
        assert diffs[0].path == "x"
        assert diffs[0].old == 1
        assert diffs[0].new == 2

    def test_nested_path_format(self):
        a = {"plugins": [{"source": {"sha": "aaa"}}]}
        b = {"plugins": [{"source": {"sha": "bbb"}}]}
        diffs = json_key_diff(a, b)
        assert any(d.path == "plugins[0].source.sha" for d in diffs)


# ---------------------------------------------------------------------------
# Clean (unchanged) case
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dirty (drift) case
# ---------------------------------------------------------------------------


class TestDriftDirtyCase:
    def test_render_diff_lines_caps_output(self):
        # Build a report with > 20 diffs to test cap.
        diffs = tuple(DriftDifference(path=f"k{i}", old=i, new=i + 1) for i in range(25))
        out = DriftOutputReport(
            format="claude", path="marketplace.json", status="drift", differences=diffs
        )
        lines = render_diff_lines(out)
        # 20 visible + 1 footer line ("...N more...")
        assert len(lines) <= 21


# ---------------------------------------------------------------------------
# Missing case
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Mixed outputs
# ---------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
