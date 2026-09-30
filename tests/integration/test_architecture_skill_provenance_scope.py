"""Architecture regression for user-scope skill provenance routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "install-deployment-provenance-state"
SKILL_INTEGRATOR = "src/apm_cli/integration/skill_integrator.py"


def test_user_scope_skill_provenance_guard_is_registered_and_clean() -> None:
    """The provenance guard includes skill_integrator's user-scope lockfile route."""
    rule = next(rule for rule in registered_rules() if rule.id == RULE_ID)

    assert rule.guard_ids == (RULE_ID,)
    report = run_selected_rules(ROOT, (RULE_ID,))
    assert report.failures == ()
    assert report.violations == ()


def test_user_scope_skill_provenance_guard_rejects_project_root_bypass() -> None:
    """User-scope skill ownership must not read provenance from the project lockfile."""
    source = (ROOT / SKILL_INTEGRATOR).read_text(encoding="utf-8")
    mutated = source.replace(
        "get_apm_dir(InstallScope.USER) if scope is InstallScope.USER else project_root",
        "project_root",
        1,
    )
    assert mutated != source

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={SKILL_INTEGRATOR: mutated},
    )

    assert any(
        violation.rule_id == RULE_ID
        and "user-scope skill ownership must route lockfile provenance through get_apm_dir(InstallScope.USER)"
        in violation.message
        for violation in report.violations
    )


def test_user_scope_skill_provenance_guard_rejects_missing_forwarding() -> None:
    """Every ownership-map integration path must receive the selected lockfile_root."""
    source = (ROOT / SKILL_INTEGRATOR).read_text(encoding="utf-8")
    mutated = source.replace("lockfile_root=lockfile_root,", "", 1)
    assert mutated != source

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={SKILL_INTEGRATOR: mutated},
    )

    assert any(
        violation.rule_id == RULE_ID
        and "skill ownership consumers must forward the canonical lockfile_root through every ownership-map integration path"
        in violation.message
        for violation in report.violations
    )
