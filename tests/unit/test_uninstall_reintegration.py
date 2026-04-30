"""Tests for the uninstall nuke-and-regenerate flow.

When a package is uninstalled, all -apm suffixed integrated files are nuked,
then remaining packages are re-integrated from apm_modules/.
"""

from datetime import datetime
from pathlib import Path

import pytest  # noqa: F401

from apm_cli.integration import AgentIntegrator, PromptIntegrator
from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageContentType,
    PackageInfo,
    PackageType,
    ResolvedReference,
)


def _make_package(
    tmp_path: Path,
    owner: str,
    name: str,
    *,
    prompts: dict[str, str] | None = None,
    agents: dict[str, str] | None = None,
    skill_md: str | None = None,
    pkg_type: PackageContentType | None = None,
) -> PackageInfo:
    """Create a minimal package under apm_modules/<owner>/<name>."""
    pkg_path = tmp_path / "apm_modules" / owner / name
    pkg_path.mkdir(parents=True, exist_ok=True)

    type_line = f"\ntype: {pkg_type.value}" if pkg_type else ""
    (pkg_path / "apm.yml").write_text(f"name: {name}\nversion: 1.0.0{type_line}\n")

    if prompts:
        prompts_dir = pkg_path / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in prompts.items():
            (prompts_dir / fname).write_text(content)

    if agents:
        agents_dir = pkg_path / ".apm" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in agents.items():
            (agents_dir / fname).write_text(content)

    if skill_md is not None:
        (pkg_path / "SKILL.md").write_text(skill_md)

    pkg = APMPackage(
        name=name,
        version="1.0.0",
        package_path=pkg_path,
        type=pkg_type,
    )
    resolved_ref = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )
    package_type = PackageType.CLAUDE_SKILL if skill_md else PackageType.APM_PACKAGE
    if skill_md and (prompts or agents):
        package_type = PackageType.HYBRID

    return PackageInfo(
        package=pkg,
        install_path=pkg_path,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
        package_type=package_type,
    )


# ---------------------------------------------------------------------------
# Prompt nuke-and-regenerate
# ---------------------------------------------------------------------------


class TestUninstallPreservesOtherPackagePrompts:
    """Sync with managed_files removes all deployed files, re-integrate remaining → only remaining survives."""

    def test_uninstall_preserves_other_package_prompts(self, tmp_path: Path):
        project_root = tmp_path
        (project_root / ".github").mkdir()

        # Two packages, each with a prompt
        pkg_a = _make_package(
            tmp_path,
            "owner",
            "pkg-a",
            prompts={"review.prompt.md": "---\nname: review\n---\n# Review A"},
        )
        pkg_b = _make_package(
            tmp_path,
            "owner",
            "pkg-b",
            prompts={"lint.prompt.md": "---\nname: lint\n---\n# Lint B"},
        )

        prompt_int = PromptIntegrator()

        # Integrate both
        prompt_int.integrate_package_prompts(pkg_a, project_root)
        prompt_int.integrate_package_prompts(pkg_b, project_root)

        prompts_dir = project_root / ".github" / "prompts"
        assert (prompts_dir / "review.prompt.md").exists()
        assert (prompts_dir / "lint.prompt.md").exists()

        # --- Simulate uninstall of pkg-a ---
        # Phase 1: remove all APM-deployed prompt files via managed_files
        managed_files = {
            ".github/prompts/review.prompt.md",
            ".github/prompts/lint.prompt.md",
        }
        dummy_pkg = APMPackage(name="root", version="0.0.0")
        prompt_int.sync_integration(dummy_pkg, project_root, managed_files=managed_files)

        # Everything removed
        assert not (prompts_dir / "review.prompt.md").exists()
        assert not (prompts_dir / "lint.prompt.md").exists()

        # Phase 2: re-integrate only pkg-b
        prompt_int.integrate_package_prompts(pkg_b, project_root)

        assert not (prompts_dir / "review.prompt.md").exists()
        assert (prompts_dir / "lint.prompt.md").exists()


# ---------------------------------------------------------------------------
# Agent nuke-and-regenerate
# ---------------------------------------------------------------------------


class TestUninstallPreservesOtherPackageAgents:
    """Sync with managed_files removes all deployed files, re-integrate remaining → only remaining survives."""

    def test_uninstall_preserves_other_package_agents(self, tmp_path: Path):
        project_root = tmp_path
        (project_root / ".github").mkdir()

        pkg_a = _make_package(
            tmp_path,
            "owner",
            "pkg-a",
            agents={"security.agent.md": "---\nname: security\n---\n# Security A"},
        )
        pkg_b = _make_package(
            tmp_path,
            "owner",
            "pkg-b",
            agents={"planner.agent.md": "---\nname: planner\n---\n# Planner B"},
        )

        agent_int = AgentIntegrator()

        agent_int.integrate_package_agents(pkg_a, project_root)
        agent_int.integrate_package_agents(pkg_b, project_root)

        agents_dir = project_root / ".github" / "agents"
        assert (agents_dir / "security.agent.md").exists()
        assert (agents_dir / "planner.agent.md").exists()

        # Phase 1: remove all APM-deployed agent files via managed_files
        managed_files = {
            ".github/agents/security.agent.md",
            ".github/agents/planner.agent.md",
        }
        dummy_pkg = APMPackage(name="root", version="0.0.0")
        agent_int.sync_integration(dummy_pkg, project_root, managed_files=managed_files)

        assert not (agents_dir / "security.agent.md").exists()
        assert not (agents_dir / "planner.agent.md").exists()

        # Phase 2: re-integrate only pkg-b
        agent_int.integrate_package_agents(pkg_b, project_root)

        assert not (agents_dir / "security.agent.md").exists()
        assert (agents_dir / "planner.agent.md").exists()


# ---------------------------------------------------------------------------
# Skill name-based cleanup
# ---------------------------------------------------------------------------


class TestUninstallPreservesOtherPackageSkills:
    """Skills use name-based matching: only the uninstalled skill dir is removed."""

    def test_uninstall_preserves_other_package_skills(self, tmp_path: Path):
        project_root = tmp_path
        (project_root / ".github").mkdir()

        pkg_a = _make_package(
            tmp_path,
            "owner",
            "skill-a",
            skill_md="---\nname: skill-a\ndescription: test A\n---\n# Skill A",
            pkg_type=PackageContentType.SKILL,
        )
        pkg_b = _make_package(
            tmp_path,
            "owner",
            "skill-b",
            skill_md="---\nname: skill-b\ndescription: test B\n---\n# Skill B",
            pkg_type=PackageContentType.SKILL,
        )

        skill_int = SkillIntegrator()

        skill_int.integrate_package_skill(pkg_a, project_root)
        skill_int.integrate_package_skill(pkg_b, project_root)

        skills_dir = project_root / ".github" / "skills"
        assert (skills_dir / "skill-a").is_dir()
        assert (skills_dir / "skill-b").is_dir()

        # Build an APMPackage that only lists skill-b as a remaining dependency.
        # sync_integration derives expected names from get_apm_dependencies().
        # We use a real APMPackage loaded from a manifest that references skill-b only.
        remaining_manifest = tmp_path / "remaining_apm.yml"
        remaining_manifest.write_text(
            "name: root\nversion: 0.0.0\ndependencies:\n  apm:\n    - owner/skill-b\n"
        )
        root_pkg = APMPackage.from_apm_yml(remaining_manifest)

        skill_int.sync_integration(root_pkg, project_root)

        # skill-a removed, skill-b preserved
        assert not (skills_dir / "skill-a").exists()
        assert (skills_dir / "skill-b").is_dir()


# ---------------------------------------------------------------------------
# User files not touched
# ---------------------------------------------------------------------------


class TestUninstallPreservesUserFiles:
    """Nuke only touches *-apm.* files; user-created files survive."""

    def test_uninstall_preserves_user_files(self, tmp_path: Path):
        project_root = tmp_path
        (project_root / ".github").mkdir()

        # User-created prompt (no -apm suffix)
        prompts_dir = project_root / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        user_file = prompts_dir / "my-review.prompt.md"
        user_file.write_text("# My custom review prompt")

        # User-created agent
        agents_dir = project_root / ".github" / "agents"
        agents_dir.mkdir(parents=True)
        user_agent = agents_dir / "my-agent.agent.md"
        user_agent.write_text("# My custom agent")

        # User-created command
        commands_dir = project_root / ".claude" / "commands"
        commands_dir.mkdir(parents=True)
        user_cmd = commands_dir / "my-command.md"
        user_cmd.write_text("# My custom command")

        # Also add an APM-managed file to confirm it gets nuked
        (prompts_dir / "pkg-review-apm.prompt.md").write_text("# APM managed")
        (agents_dir / "pkg-agent-apm.agent.md").write_text("# APM managed")
        (commands_dir / "pkg-cmd-apm.md").write_text("# APM managed")

        dummy_pkg = APMPackage(name="root", version="0.0.0")

        PromptIntegrator().sync_integration(dummy_pkg, project_root)
        AgentIntegrator().sync_integration(dummy_pkg, project_root)
        CommandIntegrator().sync_integration(dummy_pkg, project_root)

        # APM files gone
        assert not (prompts_dir / "pkg-review-apm.prompt.md").exists()
        assert not (agents_dir / "pkg-agent-apm.agent.md").exists()
        assert not (commands_dir / "pkg-cmd-apm.md").exists()

        # User files untouched
        assert user_file.exists()
        assert user_file.read_text() == "# My custom review prompt"
        assert user_agent.exists()
        assert user_agent.read_text() == "# My custom agent"
        assert user_cmd.exists()
        assert user_cmd.read_text() == "# My custom command"


# ---------------------------------------------------------------------------
# Last package uninstall → clean state
# ---------------------------------------------------------------------------


class TestUninstallLastPackageLeavesCleanDirs:
    """Installing one package and uninstalling it removes all deployed artifacts."""

    def test_uninstall_last_package_leaves_clean_dirs(self, tmp_path: Path):
        project_root = tmp_path
        (project_root / ".github").mkdir()

        pkg = _make_package(
            tmp_path,
            "owner",
            "only-pkg",
            prompts={"guide.prompt.md": "---\nname: guide\n---\n# Guide"},
            agents={"helper.agent.md": "---\nname: helper\n---\n# Helper"},
        )

        prompt_int = PromptIntegrator()
        agent_int = AgentIntegrator()
        cmd_int = CommandIntegrator()

        prompt_int.integrate_package_prompts(pkg, project_root)
        agent_int.integrate_package_agents(pkg, project_root)
        cmd_int.integrate_package_commands(pkg, project_root)

        prompts_dir = project_root / ".github" / "prompts"
        agents_dir = project_root / ".github" / "agents"
        commands_dir = project_root / ".claude" / "commands"

        # Verify files were created (clean naming, no -apm suffix)
        assert (prompts_dir / "guide.prompt.md").exists()
        assert (agents_dir / "helper.agent.md").exists()
        assert (commands_dir / "guide.md").exists()

        # Nuke everything via managed_files (no re-integration — last package removed)
        managed_files = {
            ".github/prompts/guide.prompt.md",
            ".github/agents/helper.agent.md",
            ".claude/commands/guide.md",
        }
        dummy_pkg = APMPackage(name="root", version="0.0.0")
        prompt_int.sync_integration(dummy_pkg, project_root, managed_files=managed_files)
        agent_int.sync_integration(dummy_pkg, project_root, managed_files=managed_files)
        cmd_int.sync_integration(dummy_pkg, project_root, managed_files=managed_files)

        assert not (prompts_dir / "guide.prompt.md").exists()
        assert not (agents_dir / "helper.agent.md").exists()
        assert not (commands_dir / "guide.md").exists()
