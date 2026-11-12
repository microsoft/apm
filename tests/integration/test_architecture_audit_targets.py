"""Audit target selection cannot split between replay and content coverage."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE = "registry_delegation.audit_target_selection"


def test_audit_target_owner_boundary() -> None:
    report = run_selected_rules(ROOT, (RULE,))
    assert report.failures == ()
    assert report.violations == ()


@pytest.mark.parametrize(
    "path",
    (
        "src/apm_cli/install/drift.py",
        "src/apm_cli/install/audit_replay.py",
        "src/apm_cli/policy/ci_checks.py",
        "src/apm_cli/security/file_scanner.py",
    ),
)
def test_audit_consumers_cannot_return_to_directory_detection(path: str) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace("resolve_audit_targets(", "resolve_targets(")
    assert mutated != source
    report = run_selected_rules(ROOT, (RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(item.rule_id == RULE and item.path == path for item in report.violations)


def test_historical_claims_cannot_bypass_path_validation() -> None:
    path = "src/apm_cli/policy/ci_checks.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace("BaseIntegrator.validate_deploy_path(", "allow_every_path(")
    assert mutated != source
    report = run_selected_rules(ROOT, (RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(item.rule_id == RULE and item.path == path for item in report.violations)
