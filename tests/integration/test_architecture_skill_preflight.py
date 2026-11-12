"""The unmanaged collision gate must precede every dependency deployment."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).parents[2]
RULE = "install-deployment-skill-preflight"
PATH = "src/apm_cli/install/phases/integrate.py"


@pytest.mark.parametrize("mutation", ["none", "bypass", "late", "parallel-selector"])
def test_skill_preflight_boundary(mutation: str) -> None:
    source = (ROOT / PATH).read_text()
    gate = "deployment_plans, local_plan = preflight_skill_materializations(ctx, materialized)"
    overrides = {}
    if mutation == "bypass":
        overrides[PATH] = source.replace(gate, "deployment_plans, local_plan = {}, None")
    elif mutation == "late":
        source = source.replace(gate, "deployment_plans, local_plan = {}, None")
        overrides[PATH] = source.replace(
            "root_deltas = _integrate_root_project(ctx, deployment_plan=local_plan)",
            gate + "\n    root_deltas = _integrate_root_project(ctx, deployment_plan=local_plan)",
        )
    elif mutation == "parallel-selector":
        overrides[PATH] = source + "\n\ndef selected_skill_sources(package_info):\n    return {}\n"
    result = run_selected_rules(ROOT, (RULE,), source_overrides=overrides)
    assert result.failures == ()
    assert bool(result.violations) is (mutation != "none")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("selection = resolve_effective_package_targets(", "selection = rogue_targets("),
        ("deployment_plan or prepare_package_deployment(", "deployment_plan or rogue_plan("),
        ("target_selection = deployment_plan.target_selection", "target_selection = rogue"),
        ("PackageDeploymentPlan(selection)", "PackageDeploymentPlan(rogue)"),
        ("PackageDeploymentPlan(selection, approvals", "PackageDeploymentPlan(rogue, approvals"),
        (
            "    return PackageDeploymentPlan(selection, approvals",
            "    selection.targets.append(rogue)\n    return PackageDeploymentPlan(selection, approvals",
        ),
        (
            "from .deployment_plan import PackageDeploymentPlan, prepare_package_deployment",
            "from .rogue import PackageDeploymentPlan, prepare_package_deployment",
        ),
        (
            "    target_selection = deployment_plan.target_selection",
            "    deployment_plan = rogue\n    target_selection = deployment_plan.target_selection",
        ),
    ],
)
def test_prepared_plan_cannot_bypass_canonical_target_authorization(old: str, new: str) -> None:
    path = (
        "src/apm_cli/install/deployment_plan.py"
        if old.startswith(("selection =", "PackageDeploymentPlan(", "    return "))
        else "src/apm_cli/install/services.py"
    )
    source = (ROOT / path).read_text()
    assert old in source
    rule = "install-deployment-package-target-authorization"
    result = run_selected_rules(ROOT, (rule,), source_overrides={path: source.replace(old, new)})
    assert result.failures == ()
    assert any(finding.rule_id == rule for finding in result.violations)


@pytest.mark.parametrize(
    ("rule", "old", "new"),
    [
        (
            "install-deployment-plugin-bin-eligibility",
            "plugin_bin_deployable=plugin_bin_deployable(",
            "plugin_bin_deployable=rogue_bin_eligibility(",
        ),
        (
            "marketplace-integrations-codex-native-lifecycle",
            "codex_native = plan_native_codex_plugin(",
            "codex_native = rogue_native_plan(",
        ),
        (
            "marketplace-integrations-codex-native-lifecycle",
            "native_plugin_root=codex_native.source_root if codex_native else None,",
            "unadmitted_plugin_root=None,",
        ),
    ],
)
def test_prepared_plan_retains_native_and_bin_admission(rule: str, old: str, new: str) -> None:
    path = "src/apm_cli/install/deployment_plan.py"
    source = (ROOT / path).read_text()
    assert old in source
    result = run_selected_rules(ROOT, (rule,), source_overrides={path: source.replace(old, new)})
    assert result.failures == ()
    assert any(finding.rule_id == rule for finding in result.violations)
