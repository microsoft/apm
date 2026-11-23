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
        (None, "claude", False),
        (None, "all", False),
    ],
    ids=[
        "none",
        "own-copilot",
        "own-includes-claude",
        "own-all",
        "edge-copilot",
        "own-wins",
        "scalar-claude-is-no-proof",
        "scalar-all-is-no-proof",
    ],
)
def test_lock_proves_claude_excluded_by_the_edge_or_the_package_itself(
    edge_targets: list[str] | None, package_targets: list[str] | str, excluded: bool
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


def test_recorded_package_targets_read_the_rematerialized_manifest(tmp_path) -> None:
    """A manifest rewritten in this run is re-read, not served from the parse cache (#4850)."""
    from types import SimpleNamespace

    from apm_cli.install.source_helpers import record_package_targets
    from apm_cli.models.apm_package import APMPackage

    manifest = tmp_path / "apm.yml"
    manifest.write_text("name: kit\nversion: 1.0.0\ntargets: [claude]\n", encoding="utf-8")
    APMPackage.from_apm_yml(manifest)
    manifest.write_text("name: kit\nversion: 1.0.0\ntargets: [copilot]\n", encoding="utf-8")
    ctx = SimpleNamespace(package_targets={})

    record_package_targets(ctx, "acme/kit", tmp_path)

    assert ctx.package_targets == {"acme/kit": ["copilot"]}
