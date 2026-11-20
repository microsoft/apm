"""Tests for the canonical current MCP configuration view (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.integration.mcp_config_view import (
    CurrentMcpConfigView,
    McpConfigDiff,
)

pytestmark = pytest.mark.unit


def test_symmetric_diff_reports_changed_source_only_and_lock_only() -> None:
    """Diff partitions changed and one-sided server names."""
    diff = McpConfigDiff.between(
        {
            "changed": {"name": "changed", "command": "new"},
            "source-only": {"name": "source-only"},
            "same": {"name": "same"},
        },
        {
            "changed": {"name": "changed", "command": "old"},
            "lock-only": {"name": "lock-only"},
            "same": {"name": "same"},
        },
    )

    assert diff.changed == frozenset({"changed"})
    assert diff.source_only == frozenset({"source-only"})
    assert diff.lock_only == frozenset({"lock-only"})
    assert not diff.is_empty
    assert McpConfigDiff.between({}, {}).is_empty


def test_provenance_never_exempts_lock_only_name() -> None:
    """Historical provenance cannot prove a current declaration exists."""
    view = CurrentMcpConfigView(
        dependencies=(),
        configs={},
        provenance={"removed": "old-package"},
        problems=(),
    )

    diff = view.diff({"removed": {"name": "removed"}})

    assert diff.lock_only == frozenset({"removed"})
