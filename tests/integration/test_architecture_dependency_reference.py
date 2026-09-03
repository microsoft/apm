"""Architecture coverage for embedded git URL subpath validation."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).parents[2]
RULE_ID = "contracts-tooling-dependency-identity"
REFERENCE = "src/apm_cli/models/dependency/reference.py"


def test_embedded_git_url_subpath_has_one_provider_aware_owner() -> None:
    """The URL guard owns primitive-tail classification through host providers."""
    reference = (ROOT / REFERENCE).read_text(encoding="utf-8")
    owner_registry = (ROOT / ".apm/architecture/owners/contracts-tooling.json").read_text(
        encoding="utf-8"
    )
    rule = next(rule for rule in registered_rules() if rule.id == RULE_ID)

    assert reference.count("def _check_no_embedded_subpath(") == 1
    assert "classify_host_provider(host, host_type=host_type)" in reference
    assert 'provider.kind == "gitlab"' in reference
    assert "embedded git URL subpath validation" in owner_registry
    assert rule.guard_ids == (RULE_ID,)


def test_embedded_git_url_subpath_guard_rejects_provider_bypass() -> None:
    """The registered static guard rejects a universal GitLab bypass mutation."""
    source = (ROOT / REFERENCE).read_text(encoding="utf-8")
    mutated = source.replace('provider.kind == "gitlab"', "True", 1)
    assert mutated != source

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={REFERENCE: mutated},
    )

    assert any(
        violation.rule_id == RULE_ID and "Embedded git URL subpath validation" in violation.message
        for violation in report.violations
    )
