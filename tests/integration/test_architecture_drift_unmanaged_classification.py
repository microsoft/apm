"""Audit cannot report an unclaimed differing file as a managed local edit (#3682)."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).parents[2]
RULE = "registry_delegation.drift_unmanaged_classification"
PATH = "src/apm_cli/install/drift.py"


def test_drift_unmanaged_classification_guard_is_registered_and_clean() -> None:
    result = run_selected_rules(ROOT, (RULE,))
    assert not result.failures
    assert not result.violations


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("unmanaged = unmanaged_skill or _unclaimed(rel)", "unmanaged = unmanaged_skill"),
        ("or rel in merge_config_paths)\n\n    for rel", "or True)\n\n    for rel"),
    ],
)
def test_drift_unmanaged_classification_guard_rejects_bypass(old: str, new: str) -> None:
    source = (ROOT / PATH).read_text()
    mutated = source.replace(old, new, 1)
    assert mutated != source
    result = run_selected_rules(ROOT, (RULE,), source_overrides={PATH: mutated})
    assert any(item.path == PATH and item.rule_id == RULE for item in result.violations)
