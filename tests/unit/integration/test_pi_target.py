"""Acceptance tests for the Pi coding agent target profile.

Pi (earendil-works/pi-coding-agent) loads project resources from ``.pi/``
and global resources from ``~/.pi/agent/``.  Its loadable project primitives
(verified against the shipped dist ``core/resource-loader.js``) are skills,
prompt templates, themes, and extensions -- there is NO native
agent-definition directory, so this target intentionally exposes only the
``skills`` and ``commands`` primitives.

* Skills follow the Agent Skills standard and converge on the cross-tool
  ``.agents/skills/<name>/SKILL.md`` path (like codex/opencode/gemini).
* Prompt templates are Pi's slash-command surface (read from ``.pi/prompts/``);
  they reuse the shared ``claude_command`` transformer because Pi's
  ``description``/``argument-hint`` frontmatter and ``$1``/``$@``/``$ARGUMENTS``
  substitution match Claude commands exactly.
* Instructions are compile-only (Pi reads ``AGENTS.md``), so instructions are
  not an installed primitive.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from apm_cli.core.target_catalog import get_target_capability
from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    PackageType,
    ResolvedReference,
)


def _make_package_info(
    package_dir: Path,
    name: str = "test-pkg",
    package_type: PackageType | None = None,
) -> PackageInfo:
    package = APMPackage(
        name=name,
        version="1.0.0",
        package_path=package_dir,
        source=f"github.com/test/{name}",
    )
    resolved_ref = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )
    return PackageInfo(
        package=package,
        install_path=package_dir,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
        package_type=package_type,
    )


def _make_command_package(project_root: Path, prompts: dict[str, str]) -> MagicMock:
    """Create a package on disk with ``.apm/prompts/*.prompt.md`` command sources."""
    pkg_dir = project_root / "apm_modules" / "cmd-pkg"
    prompts_dir = pkg_dir / ".apm" / "prompts"
    prompts_dir.mkdir(parents=True)
    for name, content in prompts.items():
        (prompts_dir / name).write_text(content, encoding="utf-8")

    mock_info = MagicMock()
    mock_info.install_path = pkg_dir
    mock_info.resolved_reference = None
    mock_info.package = MagicMock()
    mock_info.package.name = "cmd-pkg"
    return mock_info


# ---------------------------------------------------------------------------
# Profile + capability layout
# ---------------------------------------------------------------------------


def test_pi_target_profile_matches_ratified_layout() -> None:
    target = KNOWN_TARGETS["pi"]

    assert target.root_dir == ".pi"
    assert target.auto_create is False
    assert target.detect_by_dir is True
    assert target.user_supported is True
    assert target.user_root_dir == ".pi/agent"
    # Pi has no native agent-definition dir and no hooks concept, so only
    # skills + commands (prompt templates) are exposed.
    assert set(target.primitives) == {"skills", "commands"}

    skills = target.primitives["skills"]
    assert skills.subdir == "skills"
    assert skills.extension == "/SKILL.md"
    assert skills.format_id == "skill_standard"
    assert skills.deploy_root == ".agents"

    commands = target.primitives["commands"]
    assert commands.subdir == "prompts"
    assert commands.extension == ".md"
    assert commands.format_id == "claude_command"
    assert commands.deploy_root is None


def test_pi_capability_is_canonical_agents_family() -> None:
    cap = get_target_capability("pi")

    assert cap.primitive_profile == "pi"
    # Pi reads AGENTS.md, so it joins the AGENTS.md compile family.
    assert cap.compile_family == "agents"
    # Pi is a first-class default target (part of --target all).
    assert cap.in_all is True
    assert cap.explicit_only is False


def test_pi_user_scope_resolves_to_pi_agent_dir() -> None:
    target = KNOWN_TARGETS["pi"]

    proj = target.for_scope(user_scope=False)
    usr = target.for_scope(user_scope=True)

    # Project scope: skills converge onto .agents/skills/.
    assert proj.primitives["skills"].deploy_root == ".agents"

    # User scope: root becomes .pi/agent and skills drop the .agents
    # convergence so they land in ~/.pi/agent/skills/.
    assert usr.root_dir == ".pi/agent"
    assert usr.primitives["skills"].deploy_root is None


# ---------------------------------------------------------------------------
# Skills -> .agents/skills/<name>/SKILL.md
# ---------------------------------------------------------------------------


def test_pi_skills_deploy_skill_md_to_agents_skills_dir(tmp_path: Path) -> None:
    (tmp_path / ".pi").mkdir()
    package_dir = tmp_path / "skill-pkg"
    package_dir.mkdir()
    (package_dir / "SKILL.md").write_text(
        "---\nname: skill-pkg\ndescription: Demo skill\n---\n\n# Demo\n",
        encoding="utf-8",
    )

    result = SkillIntegrator().integrate_package_skill(
        _make_package_info(package_dir, "skill-pkg", PackageType.CLAUDE_SKILL),
        tmp_path,
        targets=[KNOWN_TARGETS["pi"]],
    )

    target = tmp_path / ".agents" / "skills" / "skill-pkg" / "SKILL.md"
    assert result.skill_created is True
    assert target.read_text(encoding="utf-8") == (
        "---\nname: skill-pkg\ndescription: Demo skill\n---\n\n# Demo\n"
    )


# ---------------------------------------------------------------------------
# Prompt templates (commands) -> .pi/prompts/<name>.md
# ---------------------------------------------------------------------------


def test_pi_commands_deploy_prompts_to_pi_prompts_dir(tmp_path: Path) -> None:
    (tmp_path / ".pi").mkdir()
    pkg_info = _make_command_package(
        tmp_path,
        {"review.prompt.md": "---\ndescription: Review staged changes\n---\n# Review"},
    )

    result = CommandIntegrator().integrate_commands_for_target(
        KNOWN_TARGETS["pi"],
        pkg_info,
        tmp_path,
    )

    assert result.files_integrated == 1
    target = tmp_path / ".pi" / "prompts" / "review.md"
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "Review" in content


def test_pi_commands_skip_when_no_pi_dir(tmp_path: Path) -> None:
    """Opt-in: skip command deployment when .pi/ does not exist."""
    pkg_info = _make_command_package(
        tmp_path,
        {"test.prompt.md": "---\ndescription: Test\n---\n# Test"},
    )

    result = CommandIntegrator().integrate_commands_for_target(
        KNOWN_TARGETS["pi"],
        pkg_info,
        tmp_path,
    )

    assert result.files_integrated == 0
    assert not (tmp_path / ".pi" / "prompts").exists()


def test_pi_skills_converge_to_agents_dir_without_pi_dir(tmp_path: Path) -> None:
    """Skills converge onto the shared .agents/skills/ root and deploy even
    without a .pi/ directory -- identical to codex/opencode/gemini, since
    .agents/ is the cross-tool skills root Pi also reads natively."""
    package_dir = tmp_path / "skill-pkg"
    package_dir.mkdir()
    (package_dir / "SKILL.md").write_text(
        "---\nname: skill-pkg\ndescription: Demo skill\n---\n\n# Demo\n",
        encoding="utf-8",
    )

    result = SkillIntegrator().integrate_package_skill(
        _make_package_info(package_dir, "skill-pkg", PackageType.CLAUDE_SKILL),
        tmp_path,
        targets=[KNOWN_TARGETS["pi"]],
    )

    assert result.skill_created is True
    assert (tmp_path / ".agents" / "skills" / "skill-pkg" / "SKILL.md").exists()
