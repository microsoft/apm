"""Regression tests for sibling skill link rewriting across primitives (#5531)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.skill_preflight import planned_skill_files
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType


def _make_package(tmp_path: Path, *, with_bin: bool = False) -> tuple[Path, PackageInfo]:
    project_root = tmp_path / "project"
    pkg_root = project_root / "apm_modules" / "_local" / "pkg"
    (pkg_root / ".apm" / "skills" / "demo-skill").mkdir(parents=True)
    (pkg_root / ".apm" / "skills" / "demo-skill" / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill.\n---\n# demo-skill\n",
        encoding="utf-8",
    )
    if with_bin:
        (pkg_root / ".apm" / "skills" / "demo-skill" / "bin").mkdir(parents=True)
        (pkg_root / ".apm" / "skills" / "demo-skill" / "bin" / "run.sh").write_text(
            "#!/bin/sh\necho hi\n",
            encoding="utf-8",
        )
    (pkg_root / ".apm" / "instructions").mkdir(parents=True)
    (pkg_root / ".apm" / "instructions" / "style.instructions.md").write_text(
        "---\napplyTo: '**'\n---\nSee [demo-skill](../skills/demo-skill/SKILL.md).\n",
        encoding="utf-8",
    )
    (pkg_root / ".apm" / "prompts").mkdir(parents=True)
    (pkg_root / ".apm" / "prompts" / "review.prompt.md").write_text(
        "---\ndescription: Review\n---\nSee [demo-skill](../skills/demo-skill/SKILL.md).\n",
        encoding="utf-8",
    )
    package = APMPackage(name="pkg", version="0.1.0")
    package_info = PackageInfo(
        package=package,
        install_path=pkg_root,
        package_type=PackageType.APM_PACKAGE,
    )
    return project_root, package_info


def _link_target(text: str) -> str:
    marker = "[demo-skill]("
    start = text.index(marker) + len(marker)
    return text[start : text.index(")", start)]


@pytest.mark.parametrize("selected", [True, False], ids=["deployed", "not-selected"])
def test_instruction_links_to_sibling_deployed_skill(tmp_path: Path, selected: bool) -> None:
    project_root, package_info = _make_package(tmp_path)
    target = KNOWN_TARGETS["copilot"]
    source_plan = DeployableSourcePlan.create(
        package_info,
        [target],
        skill_subset=None if selected else ("other-skill",),
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )
    result = InstructionIntegrator().integrate_instructions_for_target(
        target,
        package_info,
        project_root,
        source_plan=source_plan,
        skill_targets=[target],
    )
    assert result.files_integrated == 1
    deployed = project_root / ".github" / "instructions" / "style.instructions.md"
    link = _link_target(deployed.read_text(encoding="utf-8"))
    if selected:
        assert link == "../../.agents/skills/demo-skill/SKILL.md"
    else:
        assert "apm_modules/" in link


@pytest.mark.parametrize("selected", [True, False], ids=["deployed", "not-selected"])
def test_prompt_links_to_sibling_deployed_skill(tmp_path: Path, selected: bool) -> None:
    project_root, package_info = _make_package(tmp_path)
    target = KNOWN_TARGETS["copilot"]
    source_plan = DeployableSourcePlan.create(
        package_info,
        [target],
        skill_subset=None if selected else ("other-skill",),
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )
    result = PromptIntegrator().integrate_prompts_for_target(
        target,
        package_info,
        project_root,
        source_plan=source_plan,
        skill_targets=[target],
    )
    assert result.files_integrated == 1
    deployed = project_root / ".github" / "prompts" / "review.prompt.md"
    link = _link_target(deployed.read_text(encoding="utf-8"))
    if selected:
        assert link == "../../.agents/skills/demo-skill/SKILL.md"
    else:
        assert "apm_modules/" in link


@pytest.mark.parametrize("selected", [True, False], ids=["deployed", "not-selected"])
def test_command_links_to_sibling_deployed_skill(tmp_path: Path, selected: bool) -> None:
    project_root, package_info = _make_package(tmp_path)
    (project_root / ".claude").mkdir(parents=True)
    target = KNOWN_TARGETS["claude"]
    source_plan = DeployableSourcePlan.create(
        package_info,
        [target],
        skill_subset=None if selected else ("other-skill",),
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )
    result = CommandIntegrator().integrate_commands_for_target(
        target,
        package_info,
        project_root,
        source_plan=source_plan,
        skill_targets=[target],
    )
    assert result.files_integrated == 1
    deployed = project_root / ".claude" / "commands" / "review.md"
    link = _link_target(deployed.read_text(encoding="utf-8"))
    if selected:
        assert link == "../skills/demo-skill/SKILL.md"
    else:
        assert "apm_modules/" in link


def test_planned_skill_files_respects_skip_bin(tmp_path: Path) -> None:
    project_root, package_info = _make_package(tmp_path, with_bin=True)
    target = KNOWN_TARGETS["copilot"]
    source_plan = DeployableSourcePlan.create(
        package_info,
        [target],
        skill_subset=None,
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=False,
    )
    bin_file = (
        package_info.install_path / ".apm" / "skills" / "demo-skill" / "bin" / "run.sh"
    ).resolve()

    skipped = planned_skill_files(package_info, target, project_root, source_plan, skip_bin=True)
    included = planned_skill_files(package_info, target, project_root, source_plan, skip_bin=False)

    assert bin_file not in skipped
    assert (
        included[bin_file] == project_root / ".agents" / "skills" / "demo-skill" / "bin" / "run.sh"
    )
