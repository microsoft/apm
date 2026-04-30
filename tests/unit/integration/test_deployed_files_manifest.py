"""Tests for deployed_files manifest tracking across lockfile and integrators.

Covers:
- LockedDependency serialization/deserialization of deployed_files
- Migration from legacy deployed_skills → deployed_files
- Collision detection in PromptIntegrator
- Collision detection in AgentIntegrator (github + claude)
- Collision detection in CommandIntegrator
- Collision detection in HookIntegrator
- Manifest-based sync (cleanup) in all integrators
- User file preservation during sync cleanup
- Collision warning output to stderr
"""

import json
from datetime import datetime
from pathlib import Path

import pytest  # noqa: F401

from apm_cli.deps.lockfile import LockedDependency, LockFile  # noqa: F401
from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    ResolvedReference,
)
from apm_cli.utils.diagnostics import CATEGORY_COLLISION, DiagnosticCollector


def _make_package_info(
    tmp_path: Path,
    name: str = "test-pkg",
    prompt_files: dict = None,  # noqa: RUF013
    agent_files: dict = None,  # noqa: RUF013
    command_files: dict = None,  # noqa: RUF013
    hook_files: dict = None,  # noqa: RUF013
    skill_md: str = None,  # noqa: RUF013
) -> PackageInfo:
    """Create a PackageInfo with optional primitive files on disk.

    prompt_files/agent_files: placed in package root (found by integrators)
    command_files: placed in .apm/prompts/ (found by CommandIntegrator)
    hook_files: placed in hooks/ (found by HookIntegrator)
    skill_md: SKILL.md content at package root
    """
    pkg_dir = tmp_path / "apm_modules" / name
    pkg_dir.mkdir(parents=True, exist_ok=True)

    for fname, content in (prompt_files or {}).items():
        (pkg_dir / fname).write_text(content, encoding="utf-8")
    for fname, content in (agent_files or {}).items():
        (pkg_dir / fname).write_text(content, encoding="utf-8")
    if command_files:
        apm_prompts = pkg_dir / ".apm" / "prompts"
        apm_prompts.mkdir(parents=True, exist_ok=True)
        for fname, content in command_files.items():
            (apm_prompts / fname).write_text(content, encoding="utf-8")
    if hook_files:
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in hook_files.items():
            (hooks_dir / fname).write_text(content, encoding="utf-8")
    if skill_md:
        (pkg_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

    package = APMPackage(name=name, version="1.0.0", package_path=pkg_dir)
    resolved = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )
    return PackageInfo(
        package=package,
        install_path=pkg_dir,
        resolved_reference=resolved,
        installed_at=datetime.now().isoformat(),
    )


# ---------------------------------------------------------------------------
# 1. Lockfile deployed_files serialization
# ---------------------------------------------------------------------------


class TestLockedDependencyDeployedFiles:
    """Serialization and deserialization of the deployed_files field."""

    def test_serialize_with_deployed_files(self):
        """Produce a dict containing sorted deployed_files."""
        dep = LockedDependency(
            repo_url="github.com/o/r",
            deployed_files=[".github/prompts/b.prompt.md", ".github/prompts/a.prompt.md"],
        )
        d = dep.to_dict()
        assert d["deployed_files"] == [
            ".github/prompts/a.prompt.md",
            ".github/prompts/b.prompt.md",
        ]

    def test_empty_deployed_files_omitted_from_yaml(self):
        """Omit deployed_files key when the list is empty (smaller lockfile)."""
        dep = LockedDependency(repo_url="github.com/o/r")
        d = dep.to_dict()
        assert "deployed_files" not in d

    def test_deserialize_deployed_files(self):
        """Round-trip through from_dict preserves deployed_files."""
        data = {
            "repo_url": "github.com/o/r",
            "deployed_files": [".github/agents/sec.agent.md"],
        }
        dep = LockedDependency.from_dict(data)
        assert dep.deployed_files == [".github/agents/sec.agent.md"]

    def test_migrate_deployed_skills_to_deployed_files(self):
        """Legacy deployed_skills is migrated to deployed_files paths."""
        data = {
            "repo_url": "github.com/o/r",
            "deployed_skills": ["code-review", "accessibility"],
        }
        dep = LockedDependency.from_dict(data)
        assert ".github/skills/code-review/" in dep.deployed_files
        assert ".github/skills/accessibility/" in dep.deployed_files
        assert ".claude/skills/code-review/" in dep.deployed_files
        assert ".claude/skills/accessibility/" in dep.deployed_files
        assert len(dep.deployed_files) == 4

    def test_deployed_files_wins_over_legacy_skills(self):
        """When both fields exist, deployed_files takes precedence."""
        data = {
            "repo_url": "github.com/o/r",
            "deployed_files": [".github/prompts/a.prompt.md"],
            "deployed_skills": ["ignored-skill"],
        }
        dep = LockedDependency.from_dict(data)
        assert dep.deployed_files == [".github/prompts/a.prompt.md"]


# ---------------------------------------------------------------------------
# 2. Prompt integrator — collision detection
# ---------------------------------------------------------------------------


class TestPromptCollisionDetection:
    """Collision detection in PromptIntegrator.integrate_package_prompts."""

    def test_managed_files_none_no_collision_check(self, tmp_path: Path):
        """Legacy mode: managed_files=None → always overwrite."""
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "review.prompt.md").write_text("# user version")

        info = _make_package_info(tmp_path, prompt_files={"review.prompt.md": "# pkg version"})
        result = PromptIntegrator().integrate_package_prompts(
            info, tmp_path, force=False, managed_files=None
        )
        assert result.files_integrated == 1
        assert result.files_skipped == 0
        assert (prompts_dir / "review.prompt.md").read_text() == "# pkg version"

    def test_empty_managed_set_all_collisions(self, tmp_path: Path):
        """managed_files=set() → every pre-existing file is a collision."""
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "review.prompt.md").write_text("# user version")

        info = _make_package_info(tmp_path, prompt_files={"review.prompt.md": "# pkg version"})
        result = PromptIntegrator().integrate_package_prompts(
            info, tmp_path, force=False, managed_files=set()
        )
        assert result.files_integrated == 0
        assert result.files_skipped == 1
        assert (prompts_dir / "review.prompt.md").read_text() == "# user version"

    def test_managed_file_not_collision(self, tmp_path: Path):
        """File listed in managed_files is overwritten (not a collision)."""
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "review.prompt.md").write_text("# old")

        info = _make_package_info(tmp_path, prompt_files={"review.prompt.md": "# new"})
        managed = {".github/prompts/review.prompt.md"}
        result = PromptIntegrator().integrate_package_prompts(
            info, tmp_path, force=False, managed_files=managed
        )
        assert result.files_integrated == 1
        assert result.files_skipped == 0
        assert (prompts_dir / "review.prompt.md").read_text() == "# new"

    def test_unmanaged_file_is_collision(self, tmp_path: Path):
        """File NOT in managed_files is skipped as a collision."""
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "review.prompt.md").write_text("# user")

        info = _make_package_info(tmp_path, prompt_files={"review.prompt.md": "# pkg"})
        managed = {".github/prompts/OTHER.prompt.md"}
        result = PromptIntegrator().integrate_package_prompts(
            info, tmp_path, force=False, managed_files=managed
        )
        assert result.files_integrated == 0
        assert result.files_skipped == 1

    def test_force_overrides_collision(self, tmp_path: Path):
        """force=True overwrites even unmanaged files."""
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "review.prompt.md").write_text("# user")

        info = _make_package_info(tmp_path, prompt_files={"review.prompt.md": "# pkg"})
        result = PromptIntegrator().integrate_package_prompts(
            info, tmp_path, force=True, managed_files=set()
        )
        assert result.files_integrated == 1
        assert result.files_skipped == 0
        assert (prompts_dir / "review.prompt.md").read_text() == "# pkg"

    def test_target_paths_only_includes_deployed(self, tmp_path: Path):
        """Skipped (collision) files are excluded from target_paths."""
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "a.prompt.md").write_text("# user a")

        info = _make_package_info(
            tmp_path,
            prompt_files={
                "a.prompt.md": "# pkg a",
                "b.prompt.md": "# pkg b",
            },
        )
        managed = {".github/prompts/b.prompt.md"}  # only b is managed
        result = PromptIntegrator().integrate_package_prompts(
            info, tmp_path, force=False, managed_files=managed
        )
        rel_paths = [p.relative_to(tmp_path).as_posix() for p in result.target_paths]
        assert ".github/prompts/b.prompt.md" in rel_paths
        assert ".github/prompts/a.prompt.md" not in rel_paths


# ---------------------------------------------------------------------------
# 3. Prompt integrator — manifest-based sync
# ---------------------------------------------------------------------------


class TestPromptSync:
    """Manifest-based cleanup in PromptIntegrator.sync_integration."""

    def test_sync_removes_managed_files(self, tmp_path: Path):
        """Only files in managed_files are removed."""
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "a.prompt.md").write_text("managed")
        (prompts_dir / "b.prompt.md").write_text("user")

        managed = {".github/prompts/a.prompt.md"}
        stats = PromptIntegrator().sync_integration(None, tmp_path, managed_files=managed)

        assert stats["files_removed"] == 1
        assert not (prompts_dir / "a.prompt.md").exists()
        assert (prompts_dir / "b.prompt.md").exists()

    def test_sync_legacy_fallback_glob(self, tmp_path: Path):
        """managed_files=None → legacy glob removes *-apm.prompt.md only."""
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "review-apm.prompt.md").write_text("old style")
        (prompts_dir / "my-custom.prompt.md").write_text("user")

        stats = PromptIntegrator().sync_integration(None, tmp_path, managed_files=None)

        assert stats["files_removed"] == 1
        assert not (prompts_dir / "review-apm.prompt.md").exists()
        assert (prompts_dir / "my-custom.prompt.md").exists()

    def test_sync_ignores_non_prompt_paths(self, tmp_path: Path):
        """Managed paths outside .github/prompts/ are ignored by prompt sync."""
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        agents_dir = tmp_path / ".github" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "sec.agent.md").write_text("agent")

        managed = {".github/agents/sec.agent.md"}
        stats = PromptIntegrator().sync_integration(None, tmp_path, managed_files=managed)
        assert stats["files_removed"] == 0
        assert (agents_dir / "sec.agent.md").exists()


# ---------------------------------------------------------------------------
# 4. Agent integrator — collision detection (github + claude)
# ---------------------------------------------------------------------------


class TestAgentCollisionDetection:
    """Collision detection in AgentIntegrator for .github/agents/."""

    def test_managed_files_none_no_collision_check(self, tmp_path: Path):
        """Legacy mode: always overwrite when managed_files=None."""
        agents_dir = tmp_path / ".github" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "security.agent.md").write_text("# user")

        info = _make_package_info(tmp_path, agent_files={"security.agent.md": "# pkg"})
        result = AgentIntegrator().integrate_package_agents(
            info, tmp_path, force=False, managed_files=None
        )
        assert result.files_integrated >= 1
        assert result.files_skipped == 0

    def test_empty_managed_set_all_collisions(self, tmp_path: Path):
        """managed_files=set() → every pre-existing file is a collision."""
        agents_dir = tmp_path / ".github" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "security.agent.md").write_text("# user")

        info = _make_package_info(tmp_path, agent_files={"security.agent.md": "# pkg"})
        result = AgentIntegrator().integrate_package_agents(
            info, tmp_path, force=False, managed_files=set()
        )
        assert result.files_skipped >= 1
        # Verify user content is preserved
        assert (agents_dir / "security.agent.md").read_text() == "# user"

    def test_force_overrides_agent_collision(self, tmp_path: Path):
        """force=True overwrites even unmanaged agent files."""
        agents_dir = tmp_path / ".github" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "security.agent.md").write_text("# user")

        info = _make_package_info(tmp_path, agent_files={"security.agent.md": "# pkg"})
        result = AgentIntegrator().integrate_package_agents(
            info, tmp_path, force=True, managed_files=set()
        )
        assert result.files_integrated >= 1
        assert result.files_skipped == 0
        # Verify user content was overwritten
        assert (agents_dir / "security.agent.md").read_text() != "# user"


class TestClaudeAgentCollisionDetection:
    """Collision detection in AgentIntegrator for .claude/agents/."""

    def test_managed_files_none_no_collision_check(self, tmp_path: Path):
        """Legacy mode: always overwrite when managed_files=None."""
        claude_dir = tmp_path / ".claude" / "agents"
        claude_dir.mkdir(parents=True)
        (claude_dir / "security.md").write_text("# user")

        info = _make_package_info(tmp_path, agent_files={"security.agent.md": "# pkg"})
        result = AgentIntegrator().integrate_package_agents_claude(
            info, tmp_path, force=False, managed_files=None
        )
        assert result.files_integrated >= 1
        assert result.files_skipped == 0

    def test_empty_managed_set_all_collisions(self, tmp_path: Path):
        """managed_files=set() → every pre-existing file is a collision."""
        claude_dir = tmp_path / ".claude" / "agents"
        claude_dir.mkdir(parents=True)
        (claude_dir / "security.md").write_text("# user")

        info = _make_package_info(tmp_path, agent_files={"security.agent.md": "# pkg"})
        result = AgentIntegrator().integrate_package_agents_claude(
            info, tmp_path, force=False, managed_files=set()
        )
        assert result.files_skipped >= 1
        # Verify user content is preserved
        assert (claude_dir / "security.md").read_text() == "# user"

    def test_force_overrides_claude_collision(self, tmp_path: Path):
        """force=True bypasses collision check for Claude agents."""
        claude_dir = tmp_path / ".claude" / "agents"
        claude_dir.mkdir(parents=True)
        (claude_dir / "security.md").write_text("# user")

        info = _make_package_info(tmp_path, agent_files={"security.agent.md": "# pkg"})
        result = AgentIntegrator().integrate_package_agents_claude(
            info, tmp_path, force=True, managed_files=set()
        )
        assert result.files_integrated >= 1
        assert result.files_skipped == 0
        # Verify user content was overwritten
        assert (claude_dir / "security.md").read_text() != "# user"


# ---------------------------------------------------------------------------
# 5. Agent integrator — manifest-based sync
# ---------------------------------------------------------------------------


class TestAgentSync:
    """Manifest-based cleanup in AgentIntegrator sync methods."""

    def test_sync_github_removes_managed_files(self, tmp_path: Path):
        """Only managed agent files in .github/agents/ are removed."""
        agents_dir = tmp_path / ".github" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "a.agent.md").write_text("managed")
        (agents_dir / "b.agent.md").write_text("user")

        managed = {".github/agents/a.agent.md"}
        stats = AgentIntegrator().sync_integration(None, tmp_path, managed_files=managed)

        assert stats["files_removed"] == 1
        assert not (agents_dir / "a.agent.md").exists()
        assert (agents_dir / "b.agent.md").exists()

    def test_sync_github_legacy_glob(self, tmp_path: Path):
        """Legacy fallback removes *-apm.agent.md files."""
        agents_dir = tmp_path / ".github" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "sec-apm.agent.md").write_text("old")
        (agents_dir / "custom.agent.md").write_text("user")

        stats = AgentIntegrator().sync_integration(None, tmp_path, managed_files=None)

        assert stats["files_removed"] == 1
        assert not (agents_dir / "sec-apm.agent.md").exists()
        assert (agents_dir / "custom.agent.md").exists()

    def test_sync_claude_removes_managed_files(self, tmp_path: Path):
        """Only managed agent files in .claude/agents/ are removed."""
        claude_dir = tmp_path / ".claude" / "agents"
        claude_dir.mkdir(parents=True)
        (claude_dir / "a.md").write_text("managed")
        (claude_dir / "b.md").write_text("user")

        managed = {".claude/agents/a.md"}
        stats = AgentIntegrator().sync_integration_claude(None, tmp_path, managed_files=managed)

        assert stats["files_removed"] == 1
        assert not (claude_dir / "a.md").exists()
        assert (claude_dir / "b.md").exists()

    def test_sync_claude_legacy_glob(self, tmp_path: Path):
        """Legacy fallback removes *-apm.md files from .claude/agents/."""
        claude_dir = tmp_path / ".claude" / "agents"
        claude_dir.mkdir(parents=True)
        (claude_dir / "sec-apm.md").write_text("old")
        (claude_dir / "custom.md").write_text("user")

        stats = AgentIntegrator().sync_integration_claude(None, tmp_path, managed_files=None)

        assert stats["files_removed"] == 1
        assert not (claude_dir / "sec-apm.md").exists()
        assert (claude_dir / "custom.md").exists()


# ---------------------------------------------------------------------------
# 6. Command integrator — collision detection (.claude/commands/)
# ---------------------------------------------------------------------------


SAMPLE_PROMPT_MD = "---\nmode: agent\ndescription: test\n---\n# Test Prompt\nDo something.\n"


class TestCommandCollisionDetection:
    """Collision detection in CommandIntegrator.integrate_package_commands."""

    def test_managed_files_none_no_collision_check(self, tmp_path: Path):
        """Legacy mode: managed_files=None → always overwrite."""
        cmds_dir = tmp_path / ".claude" / "commands"
        cmds_dir.mkdir(parents=True)
        (cmds_dir / "review.md").write_text("# user version")

        info = _make_package_info(tmp_path, command_files={"review.prompt.md": SAMPLE_PROMPT_MD})
        result = CommandIntegrator().integrate_package_commands(
            info, tmp_path, force=False, managed_files=None
        )
        assert result.files_integrated == 1
        assert result.files_skipped == 0
        assert (cmds_dir / "review.md").read_text() != "# user version"

    def test_empty_managed_set_all_collisions(self, tmp_path: Path):
        """managed_files=set() → every pre-existing file is a collision."""
        cmds_dir = tmp_path / ".claude" / "commands"
        cmds_dir.mkdir(parents=True)
        (cmds_dir / "review.md").write_text("# user version")

        info = _make_package_info(tmp_path, command_files={"review.prompt.md": SAMPLE_PROMPT_MD})
        result = CommandIntegrator().integrate_package_commands(
            info, tmp_path, force=False, managed_files=set()
        )
        assert result.files_integrated == 0
        assert result.files_skipped == 1
        # Verify user content is preserved
        assert (cmds_dir / "review.md").read_text() == "# user version"

    def test_managed_file_not_collision(self, tmp_path: Path):
        """File listed in managed_files is overwritten (not a collision)."""
        cmds_dir = tmp_path / ".claude" / "commands"
        cmds_dir.mkdir(parents=True)
        (cmds_dir / "review.md").write_text("# old")

        info = _make_package_info(tmp_path, command_files={"review.prompt.md": SAMPLE_PROMPT_MD})
        managed = {".claude/commands/review.md"}
        result = CommandIntegrator().integrate_package_commands(
            info, tmp_path, force=False, managed_files=managed
        )
        assert result.files_integrated == 1
        assert result.files_skipped == 0

    def test_unmanaged_file_is_collision(self, tmp_path: Path):
        """File NOT in managed_files is skipped as a collision."""
        cmds_dir = tmp_path / ".claude" / "commands"
        cmds_dir.mkdir(parents=True)
        (cmds_dir / "review.md").write_text("# user")

        info = _make_package_info(tmp_path, command_files={"review.prompt.md": SAMPLE_PROMPT_MD})
        managed = {".claude/commands/OTHER.md"}
        result = CommandIntegrator().integrate_package_commands(
            info, tmp_path, force=False, managed_files=managed
        )
        assert result.files_integrated == 0
        assert result.files_skipped == 1
        assert (cmds_dir / "review.md").read_text() == "# user"

    def test_force_overrides_collision(self, tmp_path: Path):
        """force=True overwrites even unmanaged command files."""
        cmds_dir = tmp_path / ".claude" / "commands"
        cmds_dir.mkdir(parents=True)
        (cmds_dir / "review.md").write_text("# user")

        info = _make_package_info(tmp_path, command_files={"review.prompt.md": SAMPLE_PROMPT_MD})
        result = CommandIntegrator().integrate_package_commands(
            info, tmp_path, force=True, managed_files=set()
        )
        assert result.files_integrated == 1
        assert result.files_skipped == 0
        assert (cmds_dir / "review.md").read_text() != "# user"

    def test_skipped_files_excluded_from_target_paths(self, tmp_path: Path):
        """Skipped (collision) files are excluded from target_paths."""
        cmds_dir = tmp_path / ".claude" / "commands"
        cmds_dir.mkdir(parents=True)
        (cmds_dir / "a.md").write_text("# user a")

        info = _make_package_info(
            tmp_path,
            command_files={
                "a.prompt.md": SAMPLE_PROMPT_MD,
                "b.prompt.md": SAMPLE_PROMPT_MD,
            },
        )
        managed = {".claude/commands/b.md"}  # only b is managed
        result = CommandIntegrator().integrate_package_commands(
            info, tmp_path, force=False, managed_files=managed
        )
        rel_paths = [p.relative_to(tmp_path).as_posix() for p in result.target_paths]
        assert ".claude/commands/b.md" in rel_paths
        assert ".claude/commands/a.md" not in rel_paths


# ---------------------------------------------------------------------------
# 7. Command integrator — manifest-based sync
# ---------------------------------------------------------------------------


class TestCommandSync:
    """Manifest-based cleanup in CommandIntegrator.sync_integration."""

    def test_sync_removes_managed_files(self, tmp_path: Path):
        """Only files in managed_files are removed."""
        cmds_dir = tmp_path / ".claude" / "commands"
        cmds_dir.mkdir(parents=True)
        (cmds_dir / "a.md").write_text("managed")
        (cmds_dir / "b.md").write_text("user")

        managed = {".claude/commands/a.md"}
        stats = CommandIntegrator().sync_integration(None, tmp_path, managed_files=managed)

        assert stats["files_removed"] == 1
        assert not (cmds_dir / "a.md").exists()
        assert (cmds_dir / "b.md").exists()

    def test_sync_legacy_fallback_glob(self, tmp_path: Path):
        """managed_files=None → legacy glob removes *-apm.md only."""
        cmds_dir = tmp_path / ".claude" / "commands"
        cmds_dir.mkdir(parents=True)
        (cmds_dir / "review-apm.md").write_text("old style")
        (cmds_dir / "my-custom.md").write_text("user")

        stats = CommandIntegrator().sync_integration(None, tmp_path, managed_files=None)

        assert stats["files_removed"] == 1
        assert not (cmds_dir / "review-apm.md").exists()
        assert (cmds_dir / "my-custom.md").exists()

    def test_sync_ignores_non_command_paths(self, tmp_path: Path):
        """Managed paths outside .claude/commands/ are ignored by command sync."""
        cmds_dir = tmp_path / ".claude" / "commands"
        cmds_dir.mkdir(parents=True)
        agents_dir = tmp_path / ".github" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "sec.agent.md").write_text("agent")

        managed = {".github/agents/sec.agent.md"}
        stats = CommandIntegrator().sync_integration(None, tmp_path, managed_files=managed)
        assert stats["files_removed"] == 0
        assert (agents_dir / "sec.agent.md").exists()


# ---------------------------------------------------------------------------
# 8. Hook integrator — collision detection (.github/hooks/)
# ---------------------------------------------------------------------------


SAMPLE_HOOK_JSON = json.dumps(
    {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "write_to_file",
                    "hooks": [{"type": "command", "command": "echo lint", "timeout": 5}],
                }
            ]
        }
    }
)


class TestHookCollisionDetection:
    """Collision detection in HookIntegrator.integrate_package_hooks."""

    def test_managed_files_none_no_collision_check(self, tmp_path: Path):
        """Legacy mode: managed_files=None → always overwrite."""
        hooks_dir = tmp_path / ".github" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "test-pkg-hooks.json").write_text('{"user": true}')

        info = _make_package_info(tmp_path, hook_files={"hooks.json": SAMPLE_HOOK_JSON})
        result = HookIntegrator().integrate_package_hooks(
            info, tmp_path, force=False, managed_files=None
        )
        assert result.files_integrated >= 1

    def test_empty_managed_set_all_collisions(self, tmp_path: Path):
        """managed_files=set() → pre-existing hook file is a collision."""
        hooks_dir = tmp_path / ".github" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "test-pkg-hooks.json").write_text('{"user": true}')

        info = _make_package_info(tmp_path, hook_files={"hooks.json": SAMPLE_HOOK_JSON})
        result = HookIntegrator().integrate_package_hooks(
            info, tmp_path, force=False, managed_files=set()
        )
        # Hook file collides → skipped, so no hooks actually integrated
        assert result.files_integrated == 0
        # Verify user content is preserved
        assert json.loads((hooks_dir / "test-pkg-hooks.json").read_text()) == {"user": True}

    def test_managed_file_not_collision(self, tmp_path: Path):
        """Hook file in managed_files is overwritten (not a collision)."""
        hooks_dir = tmp_path / ".github" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "test-pkg-hooks.json").write_text('{"old": true}')

        info = _make_package_info(tmp_path, hook_files={"hooks.json": SAMPLE_HOOK_JSON})
        managed = {".github/hooks/test-pkg-hooks.json"}
        result = HookIntegrator().integrate_package_hooks(
            info, tmp_path, force=False, managed_files=managed
        )
        assert result.files_integrated >= 1

    def test_force_overrides_collision(self, tmp_path: Path):
        """force=True overwrites even unmanaged hook files."""
        hooks_dir = tmp_path / ".github" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "test-pkg-hooks.json").write_text('{"user": true}')

        info = _make_package_info(tmp_path, hook_files={"hooks.json": SAMPLE_HOOK_JSON})
        result = HookIntegrator().integrate_package_hooks(
            info, tmp_path, force=True, managed_files=set()
        )
        assert result.files_integrated >= 1
        # Verify user content was overwritten
        assert json.loads((hooks_dir / "test-pkg-hooks.json").read_text()) != {"user": True}


# ---------------------------------------------------------------------------
# 9. Hook integrator — manifest-based sync
# ---------------------------------------------------------------------------


class TestHookSync:
    """Manifest-based cleanup in HookIntegrator.sync_integration."""

    def test_sync_removes_managed_files(self, tmp_path: Path):
        """Only hook files in managed_files are removed."""
        hooks_dir = tmp_path / ".github" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "pkg-hooks.json").write_text('{"managed": true}')
        (hooks_dir / "user-hooks.json").write_text('{"user": true}')

        managed = {".github/hooks/pkg-hooks.json"}
        stats = HookIntegrator().sync_integration(None, tmp_path, managed_files=managed)

        assert stats["files_removed"] == 1
        assert not (hooks_dir / "pkg-hooks.json").exists()
        assert (hooks_dir / "user-hooks.json").exists()

    def test_sync_ignores_non_hook_paths(self, tmp_path: Path):
        """Managed paths outside .github/hooks/ are ignored by hook sync."""
        hooks_dir = tmp_path / ".github" / "hooks"
        hooks_dir.mkdir(parents=True)
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "a.prompt.md").write_text("prompt")

        managed = {".github/prompts/a.prompt.md"}
        stats = HookIntegrator().sync_integration(None, tmp_path, managed_files=managed)
        assert stats["files_removed"] == 0
        assert (prompts_dir / "a.prompt.md").exists()


# ---------------------------------------------------------------------------
# 10. Skill integrator — directory-level behavior + sync
# ---------------------------------------------------------------------------


class TestSkillSync:
    """Manifest-based cleanup in SkillIntegrator.sync_integration."""

    def test_sync_removes_managed_skill_dirs(self, tmp_path: Path):
        """Only skill directories in managed_files are removed."""
        github_skills = tmp_path / ".github" / "skills"
        github_skills.mkdir(parents=True)
        managed_skill = github_skills / "code-review"
        managed_skill.mkdir()
        (managed_skill / "SKILL.md").write_text("managed")
        user_skill = github_skills / "my-custom-skill"
        user_skill.mkdir()
        (user_skill / "SKILL.md").write_text("user authored")

        managed = {".github/skills/code-review/"}
        stats = SkillIntegrator().sync_integration(None, tmp_path, managed_files=managed)

        assert stats["files_removed"] == 1
        assert not managed_skill.exists()
        assert user_skill.exists()
        assert (user_skill / "SKILL.md").read_text() == "user authored"

    def test_sync_removes_claude_skill_dirs(self, tmp_path: Path):
        """Managed skill dirs in .claude/skills/ are also removed."""
        claude_skills = tmp_path / ".claude" / "skills"
        claude_skills.mkdir(parents=True)
        managed_skill = claude_skills / "code-review"
        managed_skill.mkdir()
        (managed_skill / "SKILL.md").write_text("managed")
        user_skill = claude_skills / "my-skill"
        user_skill.mkdir()
        (user_skill / "SKILL.md").write_text("user")

        managed = {".claude/skills/code-review/"}
        stats = SkillIntegrator().sync_integration(None, tmp_path, managed_files=managed)

        assert stats["files_removed"] == 1
        assert not managed_skill.exists()
        assert user_skill.exists()

    def test_sync_ignores_non_skill_paths(self, tmp_path: Path):
        """Managed paths outside skills dirs are ignored."""
        github_skills = tmp_path / ".github" / "skills"
        github_skills.mkdir(parents=True)
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "a.prompt.md").write_text("prompt")

        managed = {".github/prompts/a.prompt.md"}
        stats = SkillIntegrator().sync_integration(None, tmp_path, managed_files=managed)
        assert stats["files_removed"] == 0
        assert (prompts_dir / "a.prompt.md").exists()


# ---------------------------------------------------------------------------
# 11. Collision warning output to stderr
# ---------------------------------------------------------------------------


class TestCollisionWarningOutput:
    """Verify collision detection emits warning message to stderr."""

    def test_prompt_collision_warns_on_stderr(self, tmp_path: Path, capsys):
        """Prompt collision should print warning via _rich_warning."""
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "review.prompt.md").write_text("# user")

        info = _make_package_info(tmp_path, prompt_files={"review.prompt.md": "# pkg"})
        PromptIntegrator().integrate_package_prompts(
            info, tmp_path, force=False, managed_files=set()
        )
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "Skipping" in output
        assert "--force" in output or "apm install --force" in output

    def test_agent_collision_warns_on_stderr(self, tmp_path: Path, capsys):
        """Agent collision should print warning via _rich_warning."""
        agents_dir = tmp_path / ".github" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "security.agent.md").write_text("# user")

        info = _make_package_info(tmp_path, agent_files={"security.agent.md": "# pkg"})
        AgentIntegrator().integrate_package_agents(info, tmp_path, force=False, managed_files=set())
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "Skipping" in output
        assert "--force" in output or "apm install --force" in output

    def test_command_collision_warns_on_stderr(self, tmp_path: Path, capsys):
        """Command collision should print warning via _rich_warning."""
        cmds_dir = tmp_path / ".claude" / "commands"
        cmds_dir.mkdir(parents=True)
        (cmds_dir / "review.md").write_text("# user")

        info = _make_package_info(tmp_path, command_files={"review.prompt.md": SAMPLE_PROMPT_MD})
        CommandIntegrator().integrate_package_commands(
            info, tmp_path, force=False, managed_files=set()
        )
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "Skipping" in output
        assert "--force" in output or "apm install --force" in output

    def test_hook_collision_warns_on_stderr(self, tmp_path: Path, capsys):
        """Hook collision should print warning via _rich_warning."""
        hooks_dir = tmp_path / ".github" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "test-pkg-hooks.json").write_text('{"user": true}')

        info = _make_package_info(tmp_path, hook_files={"hooks.json": SAMPLE_HOOK_JSON})
        HookIntegrator().integrate_package_hooks(info, tmp_path, force=False, managed_files=set())
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "Skipping" in output
        assert "--force" in output or "apm install --force" in output


# ---------------------------------------------------------------------------
# 11b. check_collision() diagnostics parameter
# ---------------------------------------------------------------------------


class TestCheckCollisionDiagnostics:
    """Verify check_collision() routes output to DiagnosticCollector when provided."""

    def test_collision_recorded_in_diagnostics(self, tmp_path: Path):
        """When diagnostics is provided, collision is recorded via skip()."""
        target = tmp_path / "review.prompt.md"
        target.write_text("# user version")
        diag = DiagnosticCollector()

        result = BaseIntegrator.check_collision(
            target,
            ".github/prompts/review.prompt.md",
            managed_files=set(),
            force=False,
            diagnostics=diag,
        )

        assert result is True
        entries = diag.by_category().get(CATEGORY_COLLISION, [])
        assert len(entries) == 1
        assert entries[0].message == ".github/prompts/review.prompt.md"

    def test_collision_no_stdout_when_diagnostics_provided(self, tmp_path: Path, capsys):
        """When diagnostics is provided, nothing is printed to stdout/stderr."""
        target = tmp_path / "agent.md"
        target.write_text("# user")
        diag = DiagnosticCollector()

        BaseIntegrator.check_collision(
            target,
            ".github/agents/agent.md",
            managed_files=set(),
            force=False,
            diagnostics=diag,
        )

        captured = capsys.readouterr()
        assert "Skipping" not in captured.out + captured.err

    def test_fallback_warning_when_diagnostics_none(self, tmp_path: Path, capsys):
        """When diagnostics is None, _rich_warning() fallback fires."""
        target = tmp_path / "hook.json"
        target.write_text("{}")

        result = BaseIntegrator.check_collision(
            target,
            ".github/hooks/hook.json",
            managed_files=set(),
            force=False,
            diagnostics=None,
        )

        assert result is True
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "Skipping" in output
        assert "--force" in output or "apm install --force" in output

    def test_no_collision_no_diagnostic_recorded(self, tmp_path: Path):
        """When there is no collision, diagnostics remains empty."""
        target = tmp_path / "missing.md"  # does not exist on disk
        diag = DiagnosticCollector()

        result = BaseIntegrator.check_collision(
            target,
            ".github/prompts/missing.md",
            managed_files=set(),
            force=False,
            diagnostics=diag,
        )

        assert result is False
        assert not diag.has_diagnostics

    def test_force_bypasses_diagnostics(self, tmp_path: Path):
        """force=True skips collision even when diagnostics is provided."""
        target = tmp_path / "cmd.md"
        target.write_text("# user")
        diag = DiagnosticCollector()

        result = BaseIntegrator.check_collision(
            target,
            ".claude/commands/cmd.md",
            managed_files=set(),
            force=True,
            diagnostics=diag,
        )

        assert result is False
        assert not diag.has_diagnostics

    def test_managed_file_bypasses_diagnostics(self, tmp_path: Path):
        """File in managed_files is not a collision — no diagnostic recorded."""
        target = tmp_path / "review.prompt.md"
        target.write_text("# managed")
        diag = DiagnosticCollector()

        result = BaseIntegrator.check_collision(
            target,
            ".github/prompts/review.prompt.md",
            managed_files={".github/prompts/review.prompt.md"},
            force=False,
            diagnostics=diag,
        )

        assert result is False
        assert not diag.has_diagnostics

    def test_multiple_collisions_accumulate(self, tmp_path: Path):
        """Multiple collisions accumulate in the same collector."""
        diag = DiagnosticCollector()
        paths = [
            ".github/prompts/a.prompt.md",
            ".github/agents/b.agent.md",
            ".claude/commands/c.md",
        ]
        for rel in paths:
            target = tmp_path / Path(rel).name
            target.write_text("# user")
            BaseIntegrator.check_collision(
                target,
                rel,
                managed_files=set(),
                force=False,
                diagnostics=diag,
            )

        entries = diag.by_category().get(CATEGORY_COLLISION, [])
        assert len(entries) == 3
        recorded_paths = {e.message for e in entries}
        assert recorded_paths == set(paths)


# ---------------------------------------------------------------------------
# 12. Successful deployment — happy path across all types
# ---------------------------------------------------------------------------


class TestSuccessfulDeployment:
    """Verify that when conditions are right, files are actually deployed."""

    def test_prompt_deployed_to_github(self, tmp_path: Path):
        """Prompt files are deployed to .github/prompts/ on fresh install."""
        info = _make_package_info(
            tmp_path, prompt_files={"review.prompt.md": "# Review\nDo review."}
        )
        result = PromptIntegrator().integrate_package_prompts(
            info, tmp_path, force=False, managed_files=set()
        )
        assert result.files_integrated == 1
        target = tmp_path / ".github" / "prompts" / "review.prompt.md"
        assert target.exists()

    def test_agent_deployed_to_github(self, tmp_path: Path):
        """Agent files are deployed to .github/agents/."""
        (tmp_path / ".github").mkdir(parents=True, exist_ok=True)
        info = _make_package_info(
            tmp_path, agent_files={"sec.agent.md": "---\ndescription: sec\n---\n# Sec"}
        )
        result = AgentIntegrator().integrate_package_agents(
            info, tmp_path, force=False, managed_files=set()
        )
        assert result.files_integrated >= 1
        assert (tmp_path / ".github" / "agents" / "sec.agent.md").exists()

    def test_agent_deployed_to_claude(self, tmp_path: Path):
        """Agent files are deployed to .claude/agents/ when .claude/ exists."""
        (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
        info = _make_package_info(
            tmp_path, agent_files={"sec.agent.md": "---\ndescription: sec\n---\n# Sec"}
        )
        result = AgentIntegrator().integrate_package_agents_claude(
            info, tmp_path, force=False, managed_files=set()
        )
        assert result.files_integrated >= 1
        assert (tmp_path / ".claude" / "agents" / "sec.md").exists()

    def test_command_deployed_to_claude(self, tmp_path: Path):
        """Command files are deployed to .claude/commands/."""
        info = _make_package_info(tmp_path, command_files={"review.prompt.md": SAMPLE_PROMPT_MD})
        result = CommandIntegrator().integrate_package_commands(
            info, tmp_path, force=False, managed_files=set()
        )
        assert result.files_integrated == 1
        assert (tmp_path / ".claude" / "commands" / "review.md").exists()

    def test_hook_deployed_to_github(self, tmp_path: Path):
        """Hook JSON files are deployed to .github/hooks/."""
        info = _make_package_info(tmp_path, hook_files={"hooks.json": SAMPLE_HOOK_JSON})
        result = HookIntegrator().integrate_package_hooks(
            info, tmp_path, force=False, managed_files=set()
        )
        assert result.files_integrated >= 1
        hooks_dir = tmp_path / ".github" / "hooks"
        assert hooks_dir.exists()
        json_files = list(hooks_dir.glob("*.json"))
        assert len(json_files) >= 1


# ---------------------------------------------------------------------------
# 13. Sync preserves user files across ALL integrator types
# ---------------------------------------------------------------------------


class TestSyncPreservesUserFiles:
    """Verify that sync only removes managed files and preserves user-authored ones."""

    def test_prompt_sync_preserves_user_files(self, tmp_path: Path):
        """User-authored prompts survive sync cleanup."""
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "managed.prompt.md").write_text("managed by APM")
        (prompts_dir / "user-custom.prompt.md").write_text("my custom prompt")

        managed = {".github/prompts/managed.prompt.md"}
        PromptIntegrator().sync_integration(None, tmp_path, managed_files=managed)

        assert not (prompts_dir / "managed.prompt.md").exists()
        assert (prompts_dir / "user-custom.prompt.md").exists()
        assert (prompts_dir / "user-custom.prompt.md").read_text() == "my custom prompt"

    def test_agent_sync_preserves_user_files(self, tmp_path: Path):
        """User-authored agents in .github/agents/ survive sync cleanup."""
        agents_dir = tmp_path / ".github" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "managed.agent.md").write_text("managed")
        (agents_dir / "my-agent.agent.md").write_text("user authored agent")

        managed = {".github/agents/managed.agent.md"}
        AgentIntegrator().sync_integration(None, tmp_path, managed_files=managed)

        assert not (agents_dir / "managed.agent.md").exists()
        assert (agents_dir / "my-agent.agent.md").exists()
        assert (agents_dir / "my-agent.agent.md").read_text() == "user authored agent"

    def test_claude_agent_sync_preserves_user_files(self, tmp_path: Path):
        """User-authored agents in .claude/agents/ survive sync cleanup."""
        claude_dir = tmp_path / ".claude" / "agents"
        claude_dir.mkdir(parents=True)
        (claude_dir / "managed.md").write_text("managed")
        (claude_dir / "my-agent.md").write_text("user authored")

        managed = {".claude/agents/managed.md"}
        AgentIntegrator().sync_integration_claude(None, tmp_path, managed_files=managed)

        assert not (claude_dir / "managed.md").exists()
        assert (claude_dir / "my-agent.md").exists()
        assert (claude_dir / "my-agent.md").read_text() == "user authored"

    def test_command_sync_preserves_user_files(self, tmp_path: Path):
        """User-authored commands in .claude/commands/ survive sync."""
        cmds_dir = tmp_path / ".claude" / "commands"
        cmds_dir.mkdir(parents=True)
        (cmds_dir / "managed.md").write_text("managed")
        (cmds_dir / "my-command.md").write_text("user command")

        managed = {".claude/commands/managed.md"}
        CommandIntegrator().sync_integration(None, tmp_path, managed_files=managed)

        assert not (cmds_dir / "managed.md").exists()
        assert (cmds_dir / "my-command.md").exists()
        assert (cmds_dir / "my-command.md").read_text() == "user command"

    def test_skill_sync_preserves_user_dirs(self, tmp_path: Path):
        """User-authored skill directories survive sync."""
        skills_dir = tmp_path / ".github" / "skills"
        skills_dir.mkdir(parents=True)
        managed_skill = skills_dir / "pkg-skill"
        managed_skill.mkdir()
        (managed_skill / "SKILL.md").write_text("managed")
        user_skill = skills_dir / "my-skill"
        user_skill.mkdir()
        (user_skill / "SKILL.md").write_text("user skill")

        managed = {".github/skills/pkg-skill/"}
        SkillIntegrator().sync_integration(None, tmp_path, managed_files=managed)

        assert not managed_skill.exists()
        assert user_skill.exists()
        assert (user_skill / "SKILL.md").read_text() == "user skill"

    def test_hook_sync_preserves_user_files(self, tmp_path: Path):
        """User-authored hook files survive sync."""
        hooks_dir = tmp_path / ".github" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "managed.json").write_text('{"managed": true}')
        (hooks_dir / "user-hooks.json").write_text('{"user": true}')

        managed = {".github/hooks/managed.json"}
        HookIntegrator().sync_integration(None, tmp_path, managed_files=managed)

        assert not (hooks_dir / "managed.json").exists()
        assert (hooks_dir / "user-hooks.json").exists()
        assert json.loads((hooks_dir / "user-hooks.json").read_text()) == {"user": True}
