from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.uninstall.engine import _sync_integrations_after_uninstall
from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.base_integrator import IntegrationResult
from apm_cli.integration.canvas_integrator import CanvasIntegrator
from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.integration.coverage import check_primitive_coverage
from apm_cli.integration.dispatch import (
    get_dispatch_table,
    get_integrator_for_target,
    group_target_integrators,
)
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.skill_support import SkillIntegrationResult


def _target(name: str, primitives: dict[str, object]) -> SimpleNamespace:
    target = SimpleNamespace(
        name=name,
        root_dir=f".{name}",
        primitives=primitives,
        auto_create=True,
        resolved_deploy_root=None,
        hooks_config_display=None,
    )
    target.supports = lambda primitive: primitive in primitives
    target.skills_rel_root = f".{name}/skills"
    target.managed_deploy_root = None
    return target


def _adapter_registry(**integrators: type) -> SimpleNamespace:
    return SimpleNamespace(adapters={"custom-harness": SimpleNamespace(integrators=integrators)})


def _bundle(*, prompt=None, instruction=None) -> IntegratorBundle:
    skill = MagicMock(spec=SkillIntegrator)
    skill.preflight_skill_selection.return_value = True
    skill.integrate_package_skill.return_value = SkillIntegrationResult(
        skill_created=False,
        skill_updated=False,
        skill_skipped=False,
        skill_path=None,
        references_copied=0,
        complete=False,
    )
    return IntegratorBundle(
        prompt=prompt or PromptIntegrator(),
        agent=AgentIntegrator(),
        skill=skill,
        instruction=instruction or InstructionIntegrator(),
        command=CommandIntegrator(),
        hook=HookIntegrator(),
        canvas=CanvasIntegrator(),
    )


def _plan(targets: list[SimpleNamespace]) -> SimpleNamespace:
    selection = SimpleNamespace(
        targets=targets,
        excluded_targets=(),
        consumer_allowed_targets=frozenset(),
        consumer_restriction_active=False,
    )
    source_plan = SimpleNamespace(
        hook_source_selection=None,
        skill_discovery_complete=False,
    )
    return SimpleNamespace(
        target_selection=selection,
        approvals=(False, False, False, False, False),
        skip_bin=False,
        bin_skip_reason=None,
        codex_native=None,
        projection_targets=targets,
        source_plan=source_plan,
    )


def _run_install(
    tmp_path: Path,
    targets: list[SimpleNamespace],
    bundle: IntegratorBundle,
    *,
    skill_discovery_complete: bool = False,
):
    package_root = tmp_path / "package"
    package_root.mkdir(exist_ok=True)
    package_info = SimpleNamespace(
        install_path=package_root,
        package=SimpleNamespace(name="test-package"),
    )
    plan = _plan(targets)
    plan.source_plan.skill_discovery_complete = skill_discovery_complete
    with (
        patch("apm_cli.install.services.prepare_package_deployment", return_value=plan),
        patch("apm_cli.install.services.enforce_agent_plugin_deployment_boundary"),
        patch("apm_cli.copilot_plugins.capability.admits_native_plugin", return_value=False),
        patch("apm_cli.install.target_warnings.warn_unsupported_primitives"),
        patch("apm_cli.install.helpers.security_scan._pre_deploy_security_scan", return_value=True),
    ):
        return integrate_package_primitives(
            package_info,
            tmp_path,
            targets=targets,
            integrators=bundle,
            force=False,
            managed_files=set(),
            diagnostics=MagicMock(),
            package_name="test-package",
            logger=MagicMock(),
        )


def test_integrator_resolution_preserves_default_instance_and_groups_overrides() -> None:
    fallback = PromptIntegrator()
    first = _target("custom-harness", {"prompts": SimpleNamespace()})
    second = _target("custom-harness", {"prompts": SimpleNamespace()})

    class CustomPromptIntegrator(PromptIntegrator):
        pass

    cache: dict[tuple[str, type], object] = {}
    registry = SimpleNamespace(
        adapters={
            "custom-harness": SimpleNamespace(integrators={"prompts": CustomPromptIntegrator}),
            "original": SimpleNamespace(integrators={"prompts": PromptIntegrator}),
        }
    )
    with patch(
        "apm_cli.harnesses.registry.get_harness_registry",
        return_value=registry,
    ):
        assert (
            get_integrator_for_target(
                _target("original", {}),
                "prompts",
                fallback,
                default_class=PromptIntegrator,
                cache=cache,
            )
            is fallback
        )
        groups = group_target_integrators(
            [first, second],
            "prompts",
            fallback,
            default_class=PromptIntegrator,
            cache=cache,
        )

    assert len(groups) == 1
    assert isinstance(groups[0][0], CustomPromptIntegrator)
    assert groups[0][1] == [first, second]


def test_override_requires_no_argument_constructor() -> None:
    class NeedsArgumentPromptIntegrator(PromptIntegrator):
        def __init__(self, required):
            super().__init__()

    with (
        patch(
            "apm_cli.harnesses.registry.get_harness_registry",
            return_value=_adapter_registry(prompts=NeedsArgumentPromptIntegrator),
        ),
        pytest.raises(RuntimeError, match="no-argument construction"),
    ):
        get_integrator_for_target(
            _target("custom-harness", {"prompts": SimpleNamespace()}),
            "prompts",
            PromptIntegrator(),
            default_class=PromptIntegrator,
        )


def test_custom_instruction_preflight_rejects_before_prompt_deployment(tmp_path: Path) -> None:
    deployed_targets: list[str] = []

    class CustomPromptIntegrator(PromptIntegrator):
        def integrate_prompts_for_target(self, target, package_info, project_root, **kwargs):
            deployed_targets.append(target.name)
            return IntegrationResult(1, 0, 0, [])

    class RejectingInstructionIntegrator(InstructionIntegrator):
        def preflight_instructions_for_targets(self, *args, **kwargs):
            raise ValueError("custom instruction preflight rejected")

    target = _target(
        "custom-harness",
        {
            "prompts": SimpleNamespace(deploy_root=None, subdir="prompts"),
            "instructions": SimpleNamespace(deploy_root=None, subdir="instructions"),
        },
    )
    with (
        patch(
            "apm_cli.harnesses.registry.get_harness_registry",
            return_value=_adapter_registry(
                prompts=CustomPromptIntegrator,
                instructions=RejectingInstructionIntegrator,
            ),
        ),
        pytest.raises(ValueError, match="custom instruction preflight rejected"),
    ):
        _run_install(tmp_path, [target], _bundle())

    assert deployed_targets == []


def test_skill_overrides_merge_results_and_completion_receipt(tmp_path: Path) -> None:
    class CustomSkillIntegratorA(SkillIntegrator):
        def preflight_skill_selection(self, *args, **kwargs):
            return True

        def integrate_package_skill(self, package_info, project_root, **kwargs):
            path = project_root / ".custom-harness/skills/a/SKILL.md"
            return SkillIntegrationResult(
                skill_created=False,
                skill_updated=False,
                skill_skipped=False,
                skill_path=None,
                references_copied=0,
                sub_skills_promoted=2,
                target_paths=[path.parent],
                deployed_files=(path,),
                complete=True,
            )

    class CustomSkillIntegratorB(SkillIntegrator):
        def preflight_skill_selection(self, *args, **kwargs):
            return True

        def integrate_package_skill(self, package_info, project_root, **kwargs):
            path = project_root / ".other-harness/skills/b/SKILL.md"
            return SkillIntegrationResult(
                skill_created=False,
                skill_updated=False,
                skill_skipped=False,
                skill_path=None,
                references_copied=0,
                sub_skills_promoted=3,
                target_paths=[path.parent],
                deployed_files=(path,),
                complete=True,
            )

    first = _target(
        "custom-harness",
        {"skills": SimpleNamespace(deploy_root=None, subdir="skills")},
    )
    second = _target(
        "other-harness",
        {"skills": SimpleNamespace(deploy_root=None, subdir="skills")},
    )
    registry = SimpleNamespace(
        adapters={
            "custom-harness": SimpleNamespace(integrators={"skills": CustomSkillIntegratorA}),
            "other-harness": SimpleNamespace(integrators={"skills": CustomSkillIntegratorB}),
        }
    )
    with patch("apm_cli.harnesses.registry.get_harness_registry", return_value=registry):
        result = _run_install(
            tmp_path,
            [first, second],
            _bundle(),
            skill_discovery_complete=True,
        )

    assert result["sub_skills"] == 5
    assert sum(Path(path).name == "SKILL.md" for path in result["deployed_files"]) == 2
    receipt = result["completed_reintegration"]
    assert receipt.targets == frozenset({"custom-harness", "other-harness"})
    assert len(receipt.skill_values) == 2


def test_uninstall_sync_uses_target_integrator_override(tmp_path: Path) -> None:
    target_root = tmp_path / ".custom-harness"
    (target_root / "prompts").mkdir(parents=True)
    (target_root / "hooks").mkdir()
    managed_path = ".custom-harness/prompts/owned.md"
    managed_hook_path = ".custom-harness/hooks/owned.json"
    (tmp_path / managed_path).write_text("owned", encoding="utf-8")
    (tmp_path / managed_hook_path).write_text("{}", encoding="utf-8")
    synced_targets: list[str] = []
    synced_hook_targets: list[str] = []

    class CustomPromptIntegrator(PromptIntegrator):
        def sync_for_target(self, target, apm_package, project_root, managed_files=None):
            synced_targets.append(target.name)
            removed = 0
            for relative_path in managed_files or ():
                path = project_root / relative_path
                path.unlink(missing_ok=True)
                removed += 1
            return {"files_removed": removed, "errors": 0}

    class CustomHookIntegrator(HookIntegrator):
        def sync_integration(
            self,
            apm_package,
            project_root,
            managed_files=None,
            managed_file_hashes=None,
            targets=None,
        ):
            synced_hook_targets.extend(target.name for target in targets or ())
            removed = 0
            for relative_path in managed_files or ():
                path = project_root / relative_path
                path.unlink(missing_ok=True)
                removed += 1
            return {"files_removed": removed, "errors": 0}

    target = _target(
        "custom-harness",
        {
            "prompts": SimpleNamespace(deploy_root=None, subdir="prompts"),
            "hooks": SimpleNamespace(deploy_root=None, subdir="hooks"),
        },
    )
    apm_package = SimpleNamespace(canonical_targets=[], get_all_apm_dependencies=lambda: [])
    logger = MagicMock()
    with (
        patch(
            "apm_cli.harnesses.registry.get_harness_registry",
            return_value=_adapter_registry(
                prompts=CustomPromptIntegrator,
                hooks=CustomHookIntegrator,
            ),
        ),
        patch("apm_cli.integration.targets.resolve_targets", return_value=[target]),
        patch("apm_cli.integration.targets.KNOWN_TARGETS", {"custom-harness": target}),
        patch(
            "apm_cli.commands.uninstall.engine._native_hook_state_exists",
            return_value=False,
        ),
    ):
        outcome = _sync_integrations_after_uninstall(
            apm_package,
            tmp_path,
            {managed_path, managed_hook_path},
            logger,
            lockfile=None,
            modules_dir=tmp_path,
            survivor_plan=[],
        )

    assert synced_targets == ["custom-harness"]
    assert synced_hook_targets == ["custom-harness"]
    assert outcome.counts["prompts"] == 1
    assert outcome.counts["hooks"] == 1
    assert not (tmp_path / managed_path).exists()
    assert not (tmp_path / managed_hook_path).exists()


def test_coverage_rejects_non_subclass_adapter_override() -> None:
    from apm_cli.integration.targets import KNOWN_TARGETS

    registry = SimpleNamespace(
        adapters={"custom-harness": SimpleNamespace(integrators={"prompts": object})},
        external_names=frozenset({"custom-harness"}),
    )
    targets = {**KNOWN_TARGETS, "custom-harness": SimpleNamespace(primitives={})}
    with (
        patch("apm_cli.harnesses.registry.get_harness_registry", return_value=registry),
        patch("apm_cli.integration.targets.KNOWN_TARGETS", targets),
        pytest.raises(RuntimeError, match="must subclass"),
    ):
        check_primitive_coverage(get_dispatch_table())
