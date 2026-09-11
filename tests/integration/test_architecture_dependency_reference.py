"""Architecture coverage for embedded git URL subpath validation."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).parents[2]
RULE_ID = "contracts-tooling-dependency-identity"
REFERENCE = "src/apm_cli/models/dependency/reference.py"
DOWNLOAD = "src/apm_cli/install/phases/download.py"
INTEGRATE = "src/apm_cli/install/phases/integrate.py"
PLAN = "src/apm_cli/install/plan.py"


def test_embedded_git_url_subpath_has_one_provider_aware_owner() -> None:
    """The URL guard owns primitive-tail classification through host providers."""
    reference = (ROOT / REFERENCE).read_text(encoding="utf-8")
    owner_registry = (ROOT / ".apm/architecture/owners/contracts-tooling.json").read_text(
        encoding="utf-8"
    )
    rule = next(rule for rule in registered_rules() if rule.id == RULE_ID)
    report = run_selected_rules(ROOT, (RULE_ID,))

    assert reference.count("def _check_no_embedded_subpath(") == 1
    assert "classify_host_provider(host, host_type=host_type)" in reference
    assert 'provider.kind == "gitlab"' in reference
    assert DOWNLOAD in owner_registry
    assert INTEGRATE in owner_registry
    assert PLAN in owner_registry
    assert "embedded git URL subpath validation" in owner_registry
    assert rule.guard_ids == (RULE_ID,)
    assert report.failures == ()
    assert report.violations == ()


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


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            DOWNLOAD,
            "_pd_path = _pd_ref.get_install_path(apm_modules_dir)",
            "_pd_path = (apm_modules_dir / _pd_ref.alias) if _pd_ref.alias else _pd_ref.get_install_path(apm_modules_dir)",
        ),
        (
            INTEGRATE,
            "install_path = dep_ref.get_install_path(apm_modules_dir)",
            "install_path = (apm_modules_dir / dep_ref.alias) if dep_ref.alias else dep_ref.get_install_path(apm_modules_dir)",
        ),
    ],
)
def test_install_phase_materialization_guard_rejects_alias_path_bypass(
    path: str, old: str, new: str
) -> None:
    """Download/integrate must not recompute materialization paths from alias branches."""
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(old, new, 1)
    assert mutated != source

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={path: mutated},
    )

    assert any(
        violation.rule_id == RULE_ID
        and "Install phase materialization paths must route through DependencyReference.get_install_path"
        in violation.message
        for violation in report.violations
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("is_full_revision_pin(reference)", "False"),
        ("detect_ref_change(dep, locked_dep)", "False"),
    ],
)
def test_frozen_manifest_identity_guard_rejects_helper_bypass(old: str, new: str) -> None:
    """Frozen plan drift checks must keep both the full-SHA and drift helper routes."""
    source = (ROOT / PLAN).read_text(encoding="utf-8")
    mutated = source.replace(old, new, 1)
    assert mutated != source

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={PLAN: mutated},
    )

    assert any(
        violation.rule_id == RULE_ID
        and "Frozen manifest drift checks must route through full-SHA comparison and drift.detect_ref_change"
        in violation.message
        for violation in report.violations
    )
