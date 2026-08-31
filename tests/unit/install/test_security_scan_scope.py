"""Deployable source-plan contracts for install-time security scanning."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan
from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.skill_integrator import copy_skill_to_target
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType
from apm_cli.security.gate import SecurityGate
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.component


def _package(root: Path) -> SimpleNamespace:
    return SimpleNamespace(install_path=root)


def _skill_target() -> SimpleNamespace:
    return SimpleNamespace(primitives={"skills": object()})


def _primitive_target(primitive: str) -> SimpleNamespace:
    values = {"primitives": {primitive: object()}}
    if primitive == "hooks":
        values["name"] = "claude"
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("primitive", "relative_path", "hooks_approved", "canvas_approved"),
    [
        ("prompts", "prompt.prompt.md", False, False),
        ("agents", "agent.agent.md", False, False),
        ("instructions", ".apm/instructions/project.instructions.md", False, False),
        ("hooks", ".apm/hooks/pre-commit.json", True, False),
        ("canvas", ".apm/extensions/canvas/extension.mjs", False, True),
    ],
)
def test_supported_primitives_scan_only_authorized_files(
    tmp_path: Path,
    primitive: str,
    relative_path: str,
    hooks_approved: bool,
    canvas_approved: bool,
) -> None:
    """Every non-skill primitive keeps source-only files outside the scan plan."""
    deployable = tmp_path / relative_path
    deployable.parent.mkdir(parents=True, exist_ok=True)
    deployable.write_text("clean\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "fixture.txt").write_text("source \u202e fixture\n", encoding="utf-8")

    plan = DeployableSourcePlan.create(
        _package(tmp_path),
        [_primitive_target(primitive)],
        skill_subset=None,
        hooks_approved=hooks_approved,
        canvas_approved=canvas_approved,
        skip_bin=True,
    )
    verdict = SecurityGate.scan_files(tmp_path, path_filter=plan.includes)

    assert verdict.should_block is False
    assert verdict.scanned_files == frozenset({relative_path})


def test_source_only_hidden_character_is_not_in_authorized_scan(tmp_path: Path) -> None:
    """A nested clean skill remains installable when source-only fixtures are hostile."""
    skill = tmp_path / "skills" / "clean"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("clean skill\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "fixture.txt").write_text("source \u202e fixture\n", encoding="utf-8")

    plan = DeployableSourcePlan.create(
        _package(tmp_path),
        [_skill_target()],
        skill_subset=None,
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )
    verdict = SecurityGate.scan_files(tmp_path, path_filter=plan.includes)

    assert verdict.should_block is False
    assert verdict.scanned_files == frozenset({"skills/clean/SKILL.md"})
    assert _pre_deploy_security_scan(plan, DiagnosticCollector(), package_name="clean") is True


def test_root_skill_plan_preserves_arbitrary_files_but_excludes_internal_content(
    tmp_path: Path,
) -> None:
    """A root skill keeps its documented whole-directory contract."""
    (tmp_path / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "basic.md").write_text("example\n", encoding="utf-8")
    (tmp_path / "new-file.txt").write_text("resource\n", encoding="utf-8")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tool").write_text("executable\n", encoding="utf-8")
    (tmp_path / ".apm" / "agents").mkdir(parents=True)
    (tmp_path / ".apm" / "agents" / "internal.md").write_text("internal\n", encoding="utf-8")

    plan = DeployableSourcePlan.create(
        _package(tmp_path),
        [_skill_target()],
        skill_subset=None,
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )

    assert plan.paths == frozenset({"SKILL.md", "examples/basic.md", "new-file.txt"})


def test_nested_skill_plan_excludes_unapproved_bin(tmp_path: Path) -> None:
    """A nested skill's denied executable cannot enter the scan plan."""
    skill = tmp_path / "skills" / "selected"
    (skill / "bin").mkdir(parents=True)
    (skill / "SKILL.md").write_text("clean\n", encoding="utf-8")
    (skill / "bin" / "tool").write_text("hostile \u202e\n", encoding="utf-8")

    plan = DeployableSourcePlan.create(
        _package(tmp_path),
        [_skill_target()],
        skill_subset=("selected",),
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )

    assert plan.paths == frozenset({"skills/selected/SKILL.md"})
    assert SecurityGate.scan_files(tmp_path, paths=plan.paths).should_block is False


def test_hook_plan_routes_descriptors_before_scanning(tmp_path: Path) -> None:
    """Copilot cannot scan a hostile Claude-only descriptor or its script."""
    hooks_dir = tmp_path / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks-claude.json").write_text(
        '{"hooks":{"SessionStart":[{"hooks":[{"command":"./claude.sh"}]}]},"note":"\u202e"}',
        encoding="utf-8",
    )
    (hooks_dir / "claude.sh").write_text("#!/bin/sh\n# \u202e\n", encoding="utf-8")

    copilot_plan = DeployableSourcePlan.create(
        _package(tmp_path),
        [KNOWN_TARGETS["copilot"]],
        skill_subset=None,
        hooks_approved=True,
        canvas_approved=False,
        skip_bin=True,
    )
    claude_plan = DeployableSourcePlan.create(
        _package(tmp_path),
        [KNOWN_TARGETS["claude"]],
        skill_subset=None,
        hooks_approved=True,
        canvas_approved=False,
        skip_bin=True,
    )

    assert copilot_plan.paths == frozenset()
    assert SecurityGate.scan_files(tmp_path, paths=copilot_plan.paths).should_block is False
    assert claude_plan.paths == frozenset({".apm/hooks/hooks-claude.json", ".apm/hooks/claude.sh"})
    assert SecurityGate.scan_files(tmp_path, paths=claude_plan.paths).should_block is True


def test_source_only_canvas_content_is_not_authorized_for_scan(tmp_path: Path) -> None:
    """Only immediate canvas bundles recognized by CanvasIntegrator enter the plan."""
    bundle = tmp_path / ".apm" / "extensions" / "valid"
    bundle.mkdir(parents=True)
    (bundle / "extension.mjs").write_text("export default {};\n", encoding="utf-8")
    source_only = tmp_path / ".apm" / "extensions" / "source-only"
    source_only.mkdir()
    (source_only / "fixture.txt").write_text("source \u202e fixture\n", encoding="utf-8")

    plan = DeployableSourcePlan.create(
        _package(tmp_path),
        [_primitive_target("canvas")],
        skill_subset=None,
        hooks_approved=False,
        canvas_approved=True,
        skip_bin=True,
    )
    verdict = SecurityGate.scan_files(tmp_path, paths=plan.paths)

    assert verdict.should_block is False
    assert verdict.scanned_files == frozenset({".apm/extensions/valid/extension.mjs"})
    assert ".apm/extensions/valid" in plan.authorized_parent_prefixes
    assert plan.copy_ignore(
        str(tmp_path / ".apm" / "extensions"),
        ["valid", "source-only"],
    ) == ["source-only"]


@pytest.mark.windows_compat
def test_direct_skill_copy_normalizes_equivalent_source_alias(
    tmp_path: Path,
) -> None:
    """Direct skill deployment accepts a symlink alias of the resolved plan root."""
    source = tmp_path / "direct-skill"
    source.mkdir()
    (source / "SKILL.md").write_text("direct skill\n", encoding="utf-8")
    alias = tmp_path / "source-alias"
    alias.symlink_to(source, target_is_directory=True)
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    package_info = PackageInfo(
        package=APMPackage(name="direct-skill", version="1.0.0"),
        install_path=source,
        package_type=PackageType.CLAUDE_SKILL,
    )
    plan = DeployableSourcePlan.create(
        _package(source),
        [KNOWN_TARGETS["claude"]],
        skill_subset=None,
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )

    deployed = copy_skill_to_target(
        package_info,
        alias,
        project,
        targets=[KNOWN_TARGETS["claude"]],
        source_plan=plan,
    )

    assert deployed == [project / ".claude" / "skills" / "direct-skill"]
    assert (deployed[0] / "SKILL.md").read_text(encoding="utf-8") == "direct skill\n"


def test_direct_skill_copy_excludes_non_content_and_symlink_sources(tmp_path: Path) -> None:
    """Direct skill copies retain only the canonical plan's regular content."""
    source = tmp_path / "direct-skill"
    source.mkdir()
    (source / "SKILL.md").write_text("direct skill\n", encoding="utf-8")
    (source / ".apm-pin").write_text("cache marker\n", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret\n", encoding="utf-8")
    (source / "linked-secret").symlink_to(secret)
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    package_info = PackageInfo(
        package=APMPackage(name="direct-skill", version="1.0.0"),
        install_path=source,
        package_type=PackageType.CLAUDE_SKILL,
    )
    plan = DeployableSourcePlan.create(
        _package(source),
        [KNOWN_TARGETS["claude"]],
        skill_subset=None,
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )

    deployed = copy_skill_to_target(
        package_info,
        source,
        project,
        targets=[KNOWN_TARGETS["claude"]],
        source_plan=plan,
    )

    assert deployed == [project / ".claude" / "skills" / "direct-skill"]
    assert (deployed[0] / "SKILL.md").is_file()
    assert not (deployed[0] / ".apm-pin").exists()
    assert not (deployed[0] / "linked-secret").exists()


def test_nested_deployable_hidden_character_blocks_without_force(tmp_path: Path) -> None:
    """A hostile selected skill is fail-closed while source-only files stay excluded."""
    skill = tmp_path / "skills" / "hostile"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("hostile \u202e skill\n", encoding="utf-8")

    plan = DeployableSourcePlan.create(
        _package(tmp_path),
        [_skill_target()],
        skill_subset=("hostile",),
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )
    verdict = SecurityGate.scan_files(tmp_path, path_filter=plan.includes)

    assert verdict.should_block is True
    assert verdict.scanned_files == frozenset({"skills/hostile/SKILL.md"})
    assert _pre_deploy_security_scan(plan, DiagnosticCollector(), package_name="hostile") is False


@pytest.mark.parametrize(
    ("primitive", "relative_path", "integrator_type", "finder_name"),
    [
        ("prompts", "prompt.prompt.md", PromptIntegrator, "find_prompt_files"),
        ("agents", "agent.agent.md", AgentIntegrator, "find_agent_files"),
        ("commands", "command.prompt.md", CommandIntegrator, "find_prompt_files"),
        (
            "instructions",
            ".apm/instructions/project.instructions.md",
            InstructionIntegrator,
            "find_instruction_files",
        ),
        ("hooks", ".apm/hooks/pre-commit.json", HookIntegrator, "find_hook_files"),
    ],
)
def test_primitive_discovery_excludes_symlink_sources_from_plan(
    tmp_path: Path,
    primitive: str,
    relative_path: str,
    integrator_type,
    finder_name: str,
) -> None:
    """Primitive materializers must consume the plan's symlink exclusion."""
    deployable = tmp_path / relative_path
    deployable.parent.mkdir(parents=True, exist_ok=True)
    deployable.write_text("clean\n", encoding="utf-8")
    symlink = deployable.with_name(f"linked-{deployable.name}")
    symlink.symlink_to(deployable)

    plan = DeployableSourcePlan.create(
        _package(tmp_path),
        [_primitive_target(primitive)],
        skill_subset=None,
        hooks_approved=primitive == "hooks",
        canvas_approved=False,
        skip_bin=True,
    )

    files = getattr(integrator_type(), finder_name)(tmp_path, plan)

    assert files == [deployable]


@pytest.mark.parametrize("symlink_root", ("skills", ".apm/skills"))
def test_symlinked_skill_roots_are_not_traversed(tmp_path: Path, symlink_root: str) -> None:
    """A symlinked skill discovery root cannot admit external package content."""
    package_root = tmp_path / "package"
    external_skill = tmp_path / "external" / "selected"
    external_skill.mkdir(parents=True)
    (external_skill / "SKILL.md").write_text("external\n", encoding="utf-8")
    root = package_root / symlink_root
    root.parent.mkdir(parents=True, exist_ok=True)
    root.symlink_to(external_skill.parent, target_is_directory=True)

    plan = DeployableSourcePlan.create(
        _package(package_root),
        [_skill_target()],
        skill_subset=("selected",),
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )

    assert plan.paths == frozenset()
