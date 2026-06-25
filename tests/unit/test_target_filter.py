"""Unit tests for per-dependency target filtering."""

from __future__ import annotations

from apm_cli.install.target_filter import filter_targets_for_dependency
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.diagnostics import DiagnosticCollector


def test_filter_targets_for_dependency_intersects_active_targets() -> None:
    """Filtering keeps only active targets named by the dependency subset."""
    diagnostics = DiagnosticCollector()
    targets = [KNOWN_TARGETS["claude"], KNOWN_TARGETS["codex"]]

    filtered, allowed_targets, dep_targets_active = filter_targets_for_dependency(
        targets,
        ["codex", "copilot"],
        diagnostics,
        "owner/hooks",
    )

    assert [target.name for target in filtered] == ["codex"]
    assert allowed_targets == {"codex", "copilot"}
    assert dep_targets_active is True
    assert not diagnostics.has_diagnostics


def test_filter_targets_for_dependency_warns_on_empty_intersection() -> None:
    """Disjoint subsets skip integration and record package-attributed warning."""
    diagnostics = DiagnosticCollector()
    targets = [KNOWN_TARGETS["claude"]]

    filtered, allowed_targets, dep_targets_active = filter_targets_for_dependency(
        targets,
        ["codex"],
        diagnostics,
        "owner/hooks",
    )

    assert filtered == []
    assert allowed_targets == {"codex"}
    assert dep_targets_active is True
    assert diagnostics.has_diagnostics
    assert diagnostics.count_for_package("owner/hooks") == 1
