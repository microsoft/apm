"""Unit tests for per-dependency target filtering."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.core.errors import ConflictingTargetsError
from apm_cli.install.target_filter import (
    filter_targets_for_dependency,
    resolve_effective_package_targets,
)
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.unit


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


@pytest.mark.parametrize(
    ("active", "dependency_targets", "package_kwargs", "expected"),
    (
        (("claude", "cursor"), None, {}, ("claude", "cursor")),
        (("claude", "cursor"), ("cursor",), {}, ("cursor",)),
        (("claude", "cursor"), None, {"target": "claude"}, ("claude",)),
        (("claude", "cursor"), ("cursor",), {"target": "claude"}, ()),
        (("claude", "cursor"), None, {"targets": ["claude", "codex"]}, ("claude",)),
        (("claude", "cursor"), ("cursor",), {"target": "all"}, ("cursor",)),
        (("copilot", "claude"), None, {"target": "vscode"}, ("copilot",)),
        (("copilot", "kiro"), None, {"target": "agents"}, ("copilot",)),
    ),
    ids=(
        "no-package-declaration",
        "consumer-restriction",
        "package-restriction",
        "package-cannot-expand-consumer",
        "package-cannot-expand-active",
        "legacy-all-adds-no-restriction",
        "scalar-alias-normalizes",
        "deprecated-scalar-alias-normalizes",
    ),
)
def test_effective_package_target_intersection_is_restriction_only(
    active: tuple[str, ...],
    dependency_targets: tuple[str, ...] | None,
    package_kwargs: dict[str, object],
    expected: tuple[str, ...],
) -> None:
    """Package intent can only narrow active and consumer-authorized targets."""
    diagnostics = DiagnosticCollector()
    package = APMPackage(name="targeted-hooks", version="1.0.0", **package_kwargs)

    selection = resolve_effective_package_targets(
        [KNOWN_TARGETS[name] for name in active],
        list(dependency_targets) if dependency_targets is not None else None,
        package,
        diagnostics,
        "owner/targeted-hooks",
    )

    assert tuple(target.name for target in selection.targets) == expected
    assert {target.name for target in selection.excluded_targets} == set(active) - set(expected)


def test_legacy_package_projection_normalizes_alias_before_filtering() -> None:
    """Compatibility stand-ins infer scalar target intent through the model helper."""
    package = SimpleNamespace(
        canonical_targets=(),
        targets=None,
        target="vscode",
    )

    selection = resolve_effective_package_targets(
        [KNOWN_TARGETS["copilot"], KNOWN_TARGETS["claude"]],
        None,
        package,
        DiagnosticCollector(),
        "owner/legacy-hooks",
    )

    assert tuple(target.name for target in selection.targets) == ("copilot",)


def test_conflicting_dependency_target_keys_fail_before_authorization() -> None:
    """A dependency cannot use a conflicting universal value to expand reach."""
    package = APMPackage(
        name="conflicting-hooks",
        version="1.0.0",
        target="claude",
        targets=["all"],
    )

    with pytest.raises(ConflictingTargetsError):
        resolve_effective_package_targets(
            [KNOWN_TARGETS["claude"], KNOWN_TARGETS["cursor"]],
            None,
            package,
            DiagnosticCollector(),
            "owner/conflicting-hooks",
        )
