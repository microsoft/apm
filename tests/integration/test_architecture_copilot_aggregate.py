"""Executable boundary checks for generated-aggregate lifecycle routing."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE = "install-deployment-copilot-aggregate"


def test_copilot_aggregate_routes_are_registered_and_satisfied() -> None:
    """The stable architecture entrypoint accepts the production routes."""
    report = run_selected_rules(ROOT, (RULE,))
    assert report.failures == ()
    assert report.violations == ()


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/install/phases/lockfile.py",
            "DeploymentReconciler.merge_aggregate_records(",
            "parallel_merge(",
        ),
        (
            "src/apm_cli/commands/uninstall/engine.py",
            "root_result = integrate_local_content(",
            "root_result = unscanned_local_content(",
        ),
        (
            "src/apm_cli/integration/instruction_integrator.py",
            "cleanup = remove_stale_deployed_files(",
            "cleanup = unsafe_delete(",
        ),
        (
            "src/apm_cli/models/apm_package.py",
            "package.source = dep_ref.to_github_url()",
            "package.source = None",
        ),
        (
            "src/apm_cli/commands/uninstall/engine.py",
            "rebuild_result = finalize_install_result(",
            "rebuild_result = parallel_outcome(",
        ),
        (
            "src/apm_cli/commands/uninstall/cli.py",
            "removed_paths=(",
            "discarded_removed_paths=(",
        ),
        (
            "src/apm_cli/commands/uninstall/engine.py",
            "removed_paths=frozenset(removed_aggregate_paths)",
            "removed_paths=frozenset()",
        ),
        (
            "src/apm_cli/commands/uninstall/lockfile_state.py",
            "status=MaterializationStatus.REMOVED",
            "status=MaterializationStatus.SKIPPED",
        ),
        (
            "src/apm_cli/install/phases/lockfile.py",
            "current_aggregates = InstructionIntegrator.aggregate_paths(",
            "current_aggregates = parallel_aggregate_paths(",
        ),
    ],
)
def test_copilot_aggregate_guard_rejects_bypassed_owner(path: str, old: str, new: str) -> None:
    """Each centralization stays guarded against a sibling or missing authority."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    report = run_selected_rules(ROOT, (RULE,), source_overrides={path: source.replace(old, new)})
    assert report.failures == ()
    assert any(violation.rule_id == RULE for violation in report.violations)
