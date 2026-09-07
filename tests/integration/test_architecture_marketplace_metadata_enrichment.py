"""Architecture guardrails for marketplace metadata enrichment outcomes."""

from __future__ import annotations

from pathlib import Path

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

_RULE_ID = "marketplace-integrations-metadata-enrichment"
_RULES_BY_ID = {rule.id: rule for rule in registered_rules()}


def test_marketplace_metadata_certifiability_has_single_owner() -> None:
    """Metadata outcomes and certification must route through the builder."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/marketplace/builder.py").read_text(encoding="utf-8")
    drift = (root / "src/apm_cli/marketplace/drift_check.py").read_text(encoding="utf-8")
    rule = _RULES_BY_ID[_RULE_ID]

    assert owner.count("class MetadataEnrichmentOutcome:") == 1
    assert owner.count("class MetadataEnrichmentResult(") == 1
    assert owner.count("def _prefetch_metadata(") == 1
    assert "def remote_metadata_for_profile(" in owner
    assert "not remote_metadata.certifiable" in drift
    assert (
        "Marketplace metadata certifiability stays owned by marketplace/builder.py"
        in rule.description
    )


def test_metadata_certifiability_guard_rejects_parallel_owner() -> None:
    """The registered rule rejects a duplicate outcome owner."""
    root = Path(__file__).parents[2]
    path = "src/apm_cli/core/build_orchestrator.py"
    mutated = (root / path).read_text(encoding="utf-8")
    mutated += "\n\nclass MetadataEnrichmentResult:\n    pass\n"

    report = run_selected_rules(
        root,
        (_RULE_ID,),
        source_overrides={path: mutated},
    )

    assert report.failures == ()
    assert any(violation.rule_id == _RULE_ID for violation in report.violations)
