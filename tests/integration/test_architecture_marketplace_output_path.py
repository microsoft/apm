"""Architecture guardrails for effective marketplace output paths."""

from __future__ import annotations

from pathlib import Path

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

_RULES_BY_ID = {rule.id: rule for rule in registered_rules()}


def test_effective_marketplace_output_path_has_single_owner() -> None:
    """Generation and drift checking must use the profile-level resolver."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/marketplace/output_profiles.py").read_text(encoding="utf-8")
    producer = (root / "src/apm_cli/core/build_orchestrator.py").read_text(encoding="utf-8")
    builder = (root / "src/apm_cli/marketplace/builder.py").read_text(encoding="utf-8")
    drift_check = (root / "src/apm_cli/marketplace/drift_check.py").read_text(encoding="utf-8")
    rule = _RULES_BY_ID["marketplace-integrations-output-path"]

    assert owner.count("def resolve_effective_output_path(") == 1
    assert "resolve_effective_output_path(" in producer
    assert "resolve_effective_output_path(" in builder
    assert "resolve_effective_output_path(" in drift_check
    assert (
        "Effective marketplace output path stays owned by marketplace/output_profiles.py"
        in rule.description
    )


def test_effective_marketplace_output_path_guard_rejects_parallel_decision() -> None:
    """The registered rule rejects a second output-path resolution."""
    root = Path(__file__).parents[2]
    path = "src/apm_cli/marketplace/drift_check.py"
    mutated = (root / path).read_text(encoding="utf-8")
    mutated += (
        "\n\ndef _parallel_output_path(config, project_root):\n"
        "    return project_root / config.claude.output\n"
    )

    report = run_selected_rules(
        root,
        ("marketplace-integrations-output-path",),
        source_overrides={path: mutated},
    )

    assert report.failures == ()
    assert any(
        violation.rule_id == "marketplace-integrations-output-path"
        for violation in report.violations
    )
