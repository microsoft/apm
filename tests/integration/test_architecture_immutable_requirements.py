"""Prove the immutable compatibility owner cannot be copied into consumers."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]


def test_duplicate_immutable_requirement_owner_is_rejected() -> None:
    rule_id = "install-deployment-immutable-requirements"
    path = "src/apm_cli/install/phases/resolve.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    result = run_selected_rules(
        ROOT,
        (rule_id,),
        source_overrides={path: source + "\nclass ImmutableRequirements:\n    pass\n"},
    )
    assert any(finding.rule_id == rule_id and finding.path == path for finding in result.violations)
