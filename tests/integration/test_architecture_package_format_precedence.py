"""Architecture guard for centralized package-format precedence."""

from __future__ import annotations

from pathlib import Path

from scripts.architecture_linter.runner import run_selected_rules

ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "marketplace-integrations-package-format-precedence"


def test_agent_plugin_ingress_routes_through_package_format_precedence() -> None:
    report = run_selected_rules(ROOT, (RULE_ID,))

    assert report.failures == ()
    assert report.violations == ()


def test_agent_plugin_ingress_cannot_bypass_package_format_precedence() -> None:
    path = "src/apm_cli/bundle/local_bundle.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(
        "package_type, _ = detect_package_type(",
        "package_type, _ = bypass_package_type_precedence(",
        1,
    )
    assert mutated != source

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={path: mutated},
    )

    assert report.failures == ()
    assert any(violation.rule_id == RULE_ID for violation in report.violations)
