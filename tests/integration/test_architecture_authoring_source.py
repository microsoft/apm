"""A read-only authoring adapter must not become a second pack pipeline."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component


def test_authoring_source_guard_rejects_packing():
    root = Path(__file__).parents[2]
    path = "src/apm_cli/marketplace/authoring_source.py"
    original = (root / path).read_text(encoding="utf-8")
    rule = "marketplace-integrations-authoring-source"
    clean = run_selected_rules(root, (rule,))
    assert clean.failures == ()
    assert clean.violations == ()
    mutated = original.replace(
        "resolved = builder.resolve()", "builder.build()\n        resolved = builder.resolve()"
    )
    report = run_selected_rules(root, (rule,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(item.rule_id == rule for item in report.violations)
