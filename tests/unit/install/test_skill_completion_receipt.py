"""The install adapter consumes positive public skill completion, not inventory guesses."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from apm_cli.core.deployment_state import CompletedReintegration
from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.integration.skill_support import SkillIntegrationResult
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.component


@pytest.mark.parametrize("discovered", [False, True])
@pytest.mark.parametrize("materialized", [False, True])
def test_receipt_requires_both_public_completion_witnesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, discovered: bool, materialized: bool
) -> None:
    """Only the positively completed selected skill projection may retire absent claims."""
    source = tmp_path / "source"
    source.mkdir()
    package = PackageInfo(
        package=APMPackage(name="dependency", version="1.0.0"),
        install_path=source,
    )
    plan = DeployableSourcePlan(
        source_root=source,
        paths=frozenset(),
        skill_discovery_complete=discovered,
    )
    create_plan = Mock(return_value=plan)
    monkeypatch.setattr(DeployableSourcePlan, "create", create_plan)
    marker = tmp_path / ".agents/skills/demo/SKILL.md"
    result = SkillIntegrationResult(
        skill_created=False,
        skill_updated=False,
        skill_skipped=False,
        skill_path=None,
        references_copied=0,
        target_paths=[marker.parent],
        deployed_files=(marker,),
        complete=materialized,
    )
    skill = Mock()
    skill.integrate_package_skill.return_value = result
    bundle = IntegratorBundle(
        prompt=None, agent=None, instruction=None, command=None, hook=None, skill=skill
    )
    diagnostics = DiagnosticCollector()
    actual = integrate_package_primitives(
        package,
        tmp_path,
        targets=[KNOWN_TARGETS["claude"], KNOWN_TARGETS["codex"]],
        dep_target_subset=["codex"],
        integrators=bundle,
        force=False,
        managed_files=set(),
        diagnostics=diagnostics,
        package_name="example/dependency",
    )
    assert create_plan.call_args.args[1] == [KNOWN_TARGETS["codex"]]
    assert skill.integrate_package_skill.call_args.kwargs["source_plan"] is plan
    assert actual["deployed_files"] == [
        ".agents/skills/demo",
        ".agents/skills/demo/SKILL.md",
    ]
    if discovered and materialized:
        assert actual["completed_reintegration"] == CompletedReintegration(
            owner="example/dependency",
            targets=frozenset({"codex"}),
            current_values=frozenset(actual["deployed_files"]),
            skill_values=frozenset({".agents/skills/demo/SKILL.md"}),
        )
    else:
        assert "completed_reintegration" not in actual

    skill.integrate_package_skill.return_value = replace(result, complete=False)
    refused = integrate_package_primitives(
        package,
        tmp_path,
        targets=[],
        integrators=bundle,
        force=False,
        managed_files=set(),
        diagnostics=diagnostics,
        package_name="example/dependency",
    )
    assert "completed_reintegration" not in refused
    assert skill.integrate_package_skill.call_count == 1
