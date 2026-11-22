"""Pack's proof that a dependency without its cache excludes Claude (#4850)."""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency
from apm_cli.integration.instruction_integrator import _lock_proves_claude_excluded

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-claude-agents-md-instruction-routing")]


@pytest.mark.parametrize(
    ("edge_targets", "package_targets", "excluded"),
    [
        (None, [], False),
        (None, ["copilot"], True),
        (None, ["claude", "copilot"], False),
        (None, ["all"], False),
        (["copilot"], [], True),
        (["claude"], ["copilot"], True),
    ],
    ids=["none", "own-copilot", "own-includes-claude", "own-all", "edge-copilot", "own-wins"],
)
def test_lock_proves_claude_excluded_by_the_edge_or_the_package_itself(
    edge_targets: list[str] | None, package_targets: list[str], excluded: bool
) -> None:
    row = {"repo_url": "acme/guard-kit", "package_targets": package_targets}
    if edge_targets is not None:
        row["target_subset"] = edge_targets
    dep = LockedDependency.from_dict(row)

    assert _lock_proves_claude_excluded(dep, dep.to_dependency_ref()) is excluded


def test_old_lock_rows_load_without_package_targets_and_round_trip_them() -> None:
    old = LockedDependency.from_dict({"repo_url": "acme/guard-kit"})
    new = LockedDependency.from_dict({"repo_url": "acme/guard-kit", "package_targets": ["copilot"]})

    assert old.package_targets == []
    assert "package_targets" not in old.to_dict()
    assert new.to_dict()["package_targets"] == ["copilot"]
