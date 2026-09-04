"""Architecture coverage for host-qualified reference coordinate parsing."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "transport-platform-host-reference-coordinates"
OWNER = "src/apm_cli/models/dependency/host_virtual.py"
REFERENCE = "src/apm_cli/models/dependency/reference.py"


def test_host_reference_parser_has_registered_canonical_owner() -> None:
    owner = (ROOT / OWNER).read_text(encoding="utf-8")
    registry = (ROOT / ".apm/architecture/owners/transport-auth-platform.json").read_text(
        encoding="utf-8"
    )
    rule = next(rule for rule in registered_rules() if rule.id == RULE_ID)

    assert "def parse_host_qualified_reference(" in owner
    assert "def dependency_repository_owner(" in owner
    assert "host-reference-coordinates" in registry
    assert rule.guard_ids == (RULE_ID,)


def test_host_reference_parser_guard_rejects_ad_hoc_host_split() -> None:
    source = (ROOT / REFERENCE).read_text(encoding="utf-8")
    mutated = (
        source
        + "\n"
        + "def _rogue_host_boundary(parts):\n"
        + "    return is_supported_git_host(parts[0])\n"
    )

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={REFERENCE: mutated},
    )

    assert any(
        violation.rule_id == RULE_ID and "host/reference coordinate parsing" in violation.message
        for violation in report.violations
    )
