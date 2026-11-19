"""Audit cannot report an unclaimed differing file as a managed local edit (#3682)."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).parents[2]
RULE = "install-deployment-drift-claim-classification"
DRIFT = "src/apm_cli/install/drift.py"
CLAIMS = "src/apm_cli/install/drift_claims.py"


def test_drift_claim_classification_guard_is_registered_and_clean() -> None:
    result = run_selected_rules(ROOT, (RULE,))
    assert not result.failures
    assert not result.violations


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            DRIFT,
            "unmanaged = unmanaged_skill or claims.unclaimed(rel)",
            "unmanaged = unmanaged_skill",
        ),
        (CLAIMS, "or rel in self.shared)", "or True)"),
    ],
)
def test_drift_claim_classification_guard_rejects_bypass(path: str, old: str, new: str) -> None:
    source = (ROOT / path).read_text()
    mutated = source.replace(old, new, 1)
    assert mutated != source
    result = run_selected_rules(ROOT, (RULE,), source_overrides={path: mutated})
    assert any(item.path == path and item.rule_id == RULE for item in result.violations)
