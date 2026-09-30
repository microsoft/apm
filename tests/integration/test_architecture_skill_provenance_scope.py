"""Architecture regression for user-scope skill provenance routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "install-deployment-provenance-state"
SKILL_INTEGRATOR = "src/apm_cli/integration/skill_integrator.py"
PIPELINE = "src/apm_cli/install/pipeline.py"


@pytest.mark.parametrize(
    ("path", "old", "new", "message"),
    [
        (
            SKILL_INTEGRATOR,
            "finally:\n            self._ownership_snapshots = None",
            "finally:\n            pass",
            "snapshots must reuse the canonical builder immutably",
        ),
        (
            SKILL_INTEGRATOR,
            "MappingProxyType(owned_by)",
            "owned_by",
            "snapshots must reuse the canonical builder immutably",
        ),
        (
            SKILL_INTEGRATOR,
            "self._ownership_maps(lockfile_root or project_root)",
            "self._build_ownership_maps(lockfile_root or project_root)",
            "all skill layouts must consume",
        ),
        (
            PIPELINE,
            'else ctx.integrators["skill"].ownership_snapshot()',
            "else contextlib.nullcontext()",
            "snapshot lifetime must enclose only integration",
        ),
        (
            PIPELINE,
            "if ctx.lockfile_only\n",
            "if False\n",
            "snapshot lifetime must enclose only integration",
        ),
        (
            PIPELINE,
            "        _run_integration_phase(ctx)",
            "        pass",
            "snapshot lifetime must enclose only integration",
        ),
    ],
)
def test_skill_snapshot_guard_rejects_lifetime_and_owner_bypasses(
    path: str, old: str, new: str, message: str
) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(old, new, 1)
    assert mutated != source
    report = run_selected_rules(ROOT, (RULE_ID,), source_overrides={path: mutated})
    assert any(
        finding.rule_id == RULE_ID and message in finding.message for finding in report.violations
    )


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
