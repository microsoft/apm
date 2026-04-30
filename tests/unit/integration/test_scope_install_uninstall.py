"""Install-then-uninstall cycle tests for scope-resolved targeting.

For each target x scope combination, verifies:
- Install deploys files to the correct scope-resolved directory
- Deployed file paths are posix-formatted
- Uninstall removes exactly those files
- Files at wrong-scope paths are never created
"""

import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Set  # noqa: F401, UP035

from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference
from apm_cli.models.validation import PackageType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pkg(
    root,
    name="test-pkg",
    instructions=True,
    agents=True,
    commands=False,
    prompts=False,
    skills=False,
):
    """Create a package directory with selected primitives."""
    pkg = root / "apm_modules" / name

    if instructions:
        d = pkg / ".apm" / "instructions"
        d.mkdir(parents=True, exist_ok=True)
        (d / "python.instructions.md").write_text("---\napplyTo: '**/*.py'\n---\n\n# Python rules")

    if agents:
        d = pkg / ".apm" / "agents"
        d.mkdir(parents=True, exist_ok=True)
        (d / "reviewer.agent.md").write_text("# Code reviewer agent")

    if commands:
        # command_integrator.find_prompt_files searches .apm/prompts/
        d = pkg / ".apm" / "prompts"
        d.mkdir(parents=True, exist_ok=True)
        (d / "review.prompt.md").write_text("---\ndescription: Review code\n---\n# Review command")

    if prompts:
        d = pkg / ".apm" / "prompts"
        d.mkdir(parents=True, exist_ok=True)
        (d / "helper.prompt.md").write_text("# Helper prompt")

    if skills:
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "SKILL.md").write_text(
            "---\nname: test-pkg\ndescription: A test skill\n---\n# Test skill"
        )

    # Ensure package root exists even if no primitives selected
    pkg.mkdir(parents=True, exist_ok=True)

    package = APMPackage(
        name=name,
        version="1.0.0",
        package_path=pkg,
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
        install_path=pkg,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
        package_type=PackageType.CLAUDE_SKILL if skills else PackageType.APM_PACKAGE,
    )


def _posix_relpaths(project_root: Path, paths: list[Path]) -> set[str]:
    """Convert absolute target_paths to posix-format relative strings."""
    result = set()
    for p in paths:
        result.add(p.relative_to(project_root).as_posix())
    return result


# ---------------------------------------------------------------------------
# Copilot
# ---------------------------------------------------------------------------


class TestCopilotInstallUninstallCycle:
    """Install/uninstall cycle for Copilot at project and user scope."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_project_scope(self):
        """Install instructions + agents + prompts to .github/, then uninstall."""
        target = KNOWN_TARGETS["copilot"].for_scope(user_scope=False)
        pkg_info = _make_pkg(
            self.project_root,
            instructions=True,
            agents=True,
            prompts=True,
        )

        inst_integrator = InstructionIntegrator()
        agent_integrator = AgentIntegrator()
        prompt_integrator = PromptIntegrator()

        # -- install -------------------------------------------------------
        inst_result = inst_integrator.integrate_instructions_for_target(
            target, pkg_info, self.project_root
        )
        agent_result = agent_integrator.integrate_agents_for_target(
            target, pkg_info, self.project_root
        )
        prompt_result = prompt_integrator.integrate_prompts_for_target(
            target, pkg_info, self.project_root
        )

        assert inst_result.files_integrated >= 1
        assert agent_result.files_integrated >= 1
        assert prompt_result.files_integrated >= 1

        # Collect deployed paths
        all_paths = (
            inst_result.target_paths + agent_result.target_paths + prompt_result.target_paths
        )
        deployed = _posix_relpaths(self.project_root, all_paths)

        # Verify files exist at expected locations
        assert any(p.startswith(".github/instructions/") for p in deployed)
        assert any(p.startswith(".github/agents/") for p in deployed)
        assert any(p.startswith(".github/prompts/") for p in deployed)

        for p in deployed:
            assert "/" in p, "paths must use forward slashes"
            assert (self.project_root / p).exists(), f"deployed file missing: {p}"

        # Verify wrong-scope paths not created
        assert not (self.project_root / ".copilot").exists()

        # -- uninstall -----------------------------------------------------
        inst_sync = inst_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )
        agent_sync = agent_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )
        prompt_sync = prompt_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )

        assert inst_sync["errors"] == 0
        assert agent_sync["errors"] == 0
        assert prompt_sync["errors"] == 0

        total_removed = (
            inst_sync["files_removed"] + agent_sync["files_removed"] + prompt_sync["files_removed"]
        )
        assert total_removed == len(deployed)

        for p in deployed:
            assert not (self.project_root / p).exists(), f"not removed: {p}"

    def test_user_scope(self):
        """At user scope, agents deploy to .copilot/; instructions filtered."""
        target = KNOWN_TARGETS["copilot"].for_scope(user_scope=True)
        assert target is not None
        assert target.root_dir == ".copilot"
        # instructions and prompts filtered out at user scope
        assert "instructions" not in target.primitives
        assert "prompts" not in target.primitives

        pkg_info = _make_pkg(self.project_root, instructions=True, agents=True, prompts=True)

        agent_integrator = AgentIntegrator()
        inst_integrator = InstructionIntegrator()
        prompt_integrator = PromptIntegrator()

        # -- install -------------------------------------------------------
        agent_result = agent_integrator.integrate_agents_for_target(
            target, pkg_info, self.project_root
        )
        # instructions should be no-op because primitive not in target
        inst_result = inst_integrator.integrate_instructions_for_target(
            target, pkg_info, self.project_root
        )
        prompt_result = prompt_integrator.integrate_prompts_for_target(
            target, pkg_info, self.project_root
        )

        assert agent_result.files_integrated >= 1
        assert inst_result.files_integrated == 0
        assert prompt_result.files_integrated == 0

        deployed = _posix_relpaths(self.project_root, agent_result.target_paths)
        assert any(p.startswith(".copilot/agents/") for p in deployed)

        for p in deployed:
            assert (self.project_root / p).exists()

        # .github/ must NOT be touched
        assert not (self.project_root / ".github").exists()

        # -- uninstall -----------------------------------------------------
        sync = agent_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )
        assert sync["errors"] == 0
        assert sync["files_removed"] == len(deployed)
        for p in deployed:
            assert not (self.project_root / p).exists()


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


class TestClaudeInstallUninstallCycle:
    """Install/uninstall cycle for Claude at project and user scope."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_project_scope(self):
        """Install instructions + agents + commands to .claude/, then uninstall."""
        target = KNOWN_TARGETS["claude"].for_scope(user_scope=False)
        # auto_create=False: create target dir to simulate opt-in
        (self.project_root / ".claude").mkdir()

        pkg_info = _make_pkg(
            self.project_root,
            instructions=True,
            agents=True,
            commands=True,
        )

        inst_integrator = InstructionIntegrator()
        agent_integrator = AgentIntegrator()
        cmd_integrator = CommandIntegrator()

        # -- install -------------------------------------------------------
        inst_result = inst_integrator.integrate_instructions_for_target(
            target, pkg_info, self.project_root
        )
        agent_result = agent_integrator.integrate_agents_for_target(
            target, pkg_info, self.project_root
        )
        cmd_result = cmd_integrator.integrate_commands_for_target(
            target, pkg_info, self.project_root
        )

        assert inst_result.files_integrated >= 1
        assert agent_result.files_integrated >= 1
        assert cmd_result.files_integrated >= 1

        all_paths = inst_result.target_paths + agent_result.target_paths + cmd_result.target_paths
        deployed = _posix_relpaths(self.project_root, all_paths)

        # claude_rules format -> .claude/rules/*.md
        assert any(p.startswith(".claude/rules/") for p in deployed)
        assert any(p.startswith(".claude/agents/") for p in deployed)
        assert any(p.startswith(".claude/commands/") for p in deployed)

        for p in deployed:
            assert (self.project_root / p).exists()

        # -- uninstall -----------------------------------------------------
        inst_sync = inst_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )
        agent_sync = agent_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )
        cmd_sync = cmd_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )

        assert inst_sync["errors"] == 0
        assert agent_sync["errors"] == 0
        assert cmd_sync["errors"] == 0

        total_removed = (
            inst_sync["files_removed"] + agent_sync["files_removed"] + cmd_sync["files_removed"]
        )
        assert total_removed == len(deployed)
        for p in deployed:
            assert not (self.project_root / p).exists()

    def test_user_scope(self, monkeypatch):
        """Claude user scope: same root (.claude/), all primitives available."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        target = KNOWN_TARGETS["claude"].for_scope(user_scope=True)
        assert target is not None
        assert target.root_dir == ".claude"
        # All primitives available at user scope
        assert "instructions" in target.primitives
        assert "agents" in target.primitives
        assert "commands" in target.primitives

        # auto_create=False: create the dir
        (self.project_root / ".claude").mkdir()

        pkg_info = _make_pkg(
            self.project_root,
            instructions=True,
            agents=True,
            commands=True,
        )

        inst_integrator = InstructionIntegrator()
        agent_integrator = AgentIntegrator()
        cmd_integrator = CommandIntegrator()

        # -- install -------------------------------------------------------
        inst_result = inst_integrator.integrate_instructions_for_target(
            target, pkg_info, self.project_root
        )
        agent_result = agent_integrator.integrate_agents_for_target(
            target, pkg_info, self.project_root
        )
        cmd_result = cmd_integrator.integrate_commands_for_target(
            target, pkg_info, self.project_root
        )

        assert inst_result.files_integrated >= 1
        assert agent_result.files_integrated >= 1
        assert cmd_result.files_integrated >= 1

        all_paths = inst_result.target_paths + agent_result.target_paths + cmd_result.target_paths
        deployed = _posix_relpaths(self.project_root, all_paths)

        for p in deployed:
            assert (self.project_root / p).exists()

        # -- uninstall -----------------------------------------------------
        inst_sync = inst_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )
        agent_sync = agent_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )
        cmd_sync = cmd_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )

        total_removed = (
            inst_sync["files_removed"] + agent_sync["files_removed"] + cmd_sync["files_removed"]
        )
        assert total_removed == len(deployed)
        for p in deployed:
            assert not (self.project_root / p).exists()

    def test_user_scope_with_claude_config_dir(self, monkeypatch):
        """CLAUDE_CONFIG_DIR override: deploy lands at custom root and uninstall cleans it."""
        monkeypatch.setenv("HOME", str(self.project_root))
        custom = self.project_root / ".config" / "test-claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        custom.mkdir(parents=True)

        target = KNOWN_TARGETS["claude"].for_scope(user_scope=True)
        assert target is not None
        assert target.root_dir == ".config/test-claude"

        pkg_info = _make_pkg(self.project_root, instructions=False, agents=True)
        integrator = AgentIntegrator()

        result = integrator.integrate_agents_for_target(target, pkg_info, self.project_root)
        deployed = _posix_relpaths(self.project_root, result.target_paths)
        assert deployed
        for p in deployed:
            assert p.startswith(".config/test-claude/agents/"), f"unexpected path: {p}"

        sync = integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )
        assert sync["files_removed"] == len(deployed)
        for p in deployed:
            assert not (self.project_root / p).exists()


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


class TestCursorInstallUninstallCycle:
    """Install/uninstall cycle for Cursor at project and user scope."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_project_scope(self):
        """Install instructions (.mdc) + agents to .cursor/, then uninstall."""
        target = KNOWN_TARGETS["cursor"].for_scope(user_scope=False)
        # auto_create=False: simulate opt-in
        (self.project_root / ".cursor").mkdir()

        pkg_info = _make_pkg(self.project_root, instructions=True, agents=True)

        inst_integrator = InstructionIntegrator()
        agent_integrator = AgentIntegrator()

        # -- install -------------------------------------------------------
        inst_result = inst_integrator.integrate_instructions_for_target(
            target, pkg_info, self.project_root
        )
        agent_result = agent_integrator.integrate_agents_for_target(
            target, pkg_info, self.project_root
        )

        assert inst_result.files_integrated >= 1
        assert agent_result.files_integrated >= 1

        all_paths = inst_result.target_paths + agent_result.target_paths
        deployed = _posix_relpaths(self.project_root, all_paths)

        # cursor_rules format -> .cursor/rules/*.mdc
        assert any(p.startswith(".cursor/rules/") for p in deployed)
        assert any(p.endswith(".mdc") for p in deployed)
        assert any(p.startswith(".cursor/agents/") for p in deployed)

        for p in deployed:
            assert (self.project_root / p).exists()

        # -- uninstall -----------------------------------------------------
        inst_sync = inst_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )
        agent_sync = agent_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )

        assert inst_sync["errors"] == 0
        assert agent_sync["errors"] == 0
        total_removed = inst_sync["files_removed"] + agent_sync["files_removed"]
        assert total_removed == len(deployed)
        for p in deployed:
            assert not (self.project_root / p).exists()

    def test_user_scope(self):
        """Cursor user scope: instructions filtered, agents deploy to .cursor/."""
        target = KNOWN_TARGETS["cursor"].for_scope(user_scope=True)
        assert target is not None
        # No user_root_dir -> same root
        assert target.root_dir == ".cursor"
        assert "instructions" not in target.primitives
        assert "agents" in target.primitives

        # auto_create=False: create dir
        (self.project_root / ".cursor").mkdir()

        pkg_info = _make_pkg(self.project_root, instructions=True, agents=True)

        inst_integrator = InstructionIntegrator()
        agent_integrator = AgentIntegrator()

        # -- install -------------------------------------------------------
        inst_result = inst_integrator.integrate_instructions_for_target(
            target, pkg_info, self.project_root
        )
        agent_result = agent_integrator.integrate_agents_for_target(
            target, pkg_info, self.project_root
        )

        assert inst_result.files_integrated == 0  # filtered
        assert agent_result.files_integrated >= 1

        deployed = _posix_relpaths(self.project_root, agent_result.target_paths)
        assert any(p.startswith(".cursor/agents/") for p in deployed)
        for p in deployed:
            assert (self.project_root / p).exists()

        # .cursor/rules/ must NOT be created
        assert not (self.project_root / ".cursor" / "rules").exists()

        # -- uninstall -----------------------------------------------------
        sync = agent_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )
        assert sync["errors"] == 0
        assert sync["files_removed"] == len(deployed)
        for p in deployed:
            assert not (self.project_root / p).exists()


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------


class TestOpenCodeInstallUninstallCycle:
    """Install/uninstall cycle for OpenCode at project and user scope."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_project_scope(self):
        """Install agents + commands to .opencode/, then uninstall."""
        target = KNOWN_TARGETS["opencode"].for_scope(user_scope=False)
        # auto_create=False
        (self.project_root / ".opencode").mkdir()

        pkg_info = _make_pkg(
            self.project_root,
            instructions=False,
            agents=True,
            commands=True,
        )

        agent_integrator = AgentIntegrator()
        cmd_integrator = CommandIntegrator()

        # -- install -------------------------------------------------------
        agent_result = agent_integrator.integrate_agents_for_target(
            target, pkg_info, self.project_root
        )
        cmd_result = cmd_integrator.integrate_commands_for_target(
            target, pkg_info, self.project_root
        )

        assert agent_result.files_integrated >= 1
        assert cmd_result.files_integrated >= 1

        all_paths = agent_result.target_paths + cmd_result.target_paths
        deployed = _posix_relpaths(self.project_root, all_paths)

        assert any(p.startswith(".opencode/agents/") for p in deployed)
        assert any(p.startswith(".opencode/commands/") for p in deployed)

        for p in deployed:
            assert (self.project_root / p).exists()

        # -- uninstall -----------------------------------------------------
        agent_sync = agent_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )
        cmd_sync = cmd_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )

        assert agent_sync["errors"] == 0
        assert cmd_sync["errors"] == 0
        total_removed = agent_sync["files_removed"] + cmd_sync["files_removed"]
        assert total_removed == len(deployed)
        for p in deployed:
            assert not (self.project_root / p).exists()

    def test_user_scope(self):
        """OpenCode user scope: deploy to .config/opencode/, verify .opencode/ untouched."""
        target = KNOWN_TARGETS["opencode"].for_scope(user_scope=True)
        assert target is not None
        assert target.root_dir == ".config/opencode"

        # User scope still requires the original root_dir for detect_by_dir check
        # but for_scope replaces root_dir. auto_create may apply. Create it.
        (self.project_root / ".config" / "opencode").mkdir(parents=True)

        pkg_info = _make_pkg(
            self.project_root,
            instructions=False,
            agents=True,
            commands=True,
        )

        agent_integrator = AgentIntegrator()
        cmd_integrator = CommandIntegrator()

        # -- install -------------------------------------------------------
        agent_result = agent_integrator.integrate_agents_for_target(
            target, pkg_info, self.project_root
        )
        cmd_result = cmd_integrator.integrate_commands_for_target(
            target, pkg_info, self.project_root
        )

        assert agent_result.files_integrated >= 1
        assert cmd_result.files_integrated >= 1

        all_paths = agent_result.target_paths + cmd_result.target_paths
        deployed = _posix_relpaths(self.project_root, all_paths)

        assert any(p.startswith(".config/opencode/agents/") for p in deployed)
        assert any(p.startswith(".config/opencode/commands/") for p in deployed)

        for p in deployed:
            assert (self.project_root / p).exists()

        # .opencode/ must NOT be touched
        assert not (self.project_root / ".opencode").exists()

        # -- uninstall -----------------------------------------------------
        agent_sync = agent_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )
        cmd_sync = cmd_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )

        assert agent_sync["errors"] == 0
        assert cmd_sync["errors"] == 0
        total_removed = agent_sync["files_removed"] + cmd_sync["files_removed"]
        assert total_removed == len(deployed)
        for p in deployed:
            assert not (self.project_root / p).exists()


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


class TestCodexInstallUninstallCycle:
    """Install/uninstall cycle for Codex (project scope only)."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_project_scope(self):
        """Install agents (.toml) to .codex/, then uninstall."""
        target = KNOWN_TARGETS["codex"].for_scope(user_scope=False)
        # auto_create=False
        (self.project_root / ".codex").mkdir()

        pkg_info = _make_pkg(
            self.project_root,
            instructions=False,
            agents=True,
            commands=False,
        )

        agent_integrator = AgentIntegrator()

        # -- install -------------------------------------------------------
        agent_result = agent_integrator.integrate_agents_for_target(
            target, pkg_info, self.project_root
        )

        assert agent_result.files_integrated >= 1

        deployed = _posix_relpaths(self.project_root, agent_result.target_paths)
        assert any(p.startswith(".codex/agents/") for p in deployed)
        assert any(p.endswith(".toml") for p in deployed)

        for p in deployed:
            assert (self.project_root / p).exists()

        # -- uninstall -----------------------------------------------------
        sync = agent_integrator.sync_for_target(
            target, pkg_info.package, self.project_root, managed_files=deployed
        )
        assert sync["errors"] == 0
        assert sync["files_removed"] == len(deployed)
        for p in deployed:
            assert not (self.project_root / p).exists()

    def test_user_scope(self):
        """Codex agents deploy to .codex/agents/ at user scope as well."""
        target = KNOWN_TARGETS["codex"].for_scope(user_scope=True)
        assert target is not None
        (self.project_root / ".codex").mkdir()

        pkg_info = _make_pkg(
            self.project_root,
            instructions=False,
            agents=True,
            commands=False,
        )

        agent_integrator = AgentIntegrator()
        agent_result = agent_integrator.integrate_agents_for_target(
            target, pkg_info, self.project_root
        )

        assert agent_result.files_integrated >= 1

        deployed = _posix_relpaths(self.project_root, agent_result.target_paths)
        assert any(p.startswith(".codex/agents/") for p in deployed)
        assert any(p.endswith(".toml") for p in deployed)


# ---------------------------------------------------------------------------
# Skill integration (cross-target)
# ---------------------------------------------------------------------------


class TestSkillInstallUninstallCycle:
    """Skill install/uninstall across targets and scopes."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_copilot_project_scope(self):
        """Skill deploys to .github/skills/ at project scope."""
        target = KNOWN_TARGETS["copilot"].for_scope(user_scope=False)
        pkg_info = _make_pkg(
            self.project_root,
            name="test-skill",
            instructions=False,
            agents=False,
            skills=True,
        )

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(pkg_info, self.project_root, targets=[target])

        assert result.skill_created or result.skill_updated
        assert not result.skill_skipped
        assert len(result.target_paths) >= 1

        deployed = _posix_relpaths(self.project_root, result.target_paths)
        assert any(p.startswith(".github/skills/") for p in deployed)

        for p in deployed:
            assert (self.project_root / p).exists()

        # -- uninstall -----------------------------------------------------
        sync = integrator.sync_integration(
            pkg_info.package,
            self.project_root,
            managed_files=deployed,
            targets=[target],
        )
        assert sync["errors"] == 0
        assert sync["files_removed"] >= 1
        for p in deployed:
            assert not (self.project_root / p).exists()

    def test_copilot_user_scope(self):
        """Skill deploys to .copilot/skills/ at user scope."""
        target = KNOWN_TARGETS["copilot"].for_scope(user_scope=True)
        assert target is not None

        pkg_info = _make_pkg(
            self.project_root,
            name="test-skill",
            instructions=False,
            agents=False,
            skills=True,
        )

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(pkg_info, self.project_root, targets=[target])

        assert result.skill_created or result.skill_updated
        assert len(result.target_paths) >= 1

        deployed = _posix_relpaths(self.project_root, result.target_paths)
        assert any(p.startswith(".copilot/skills/") for p in deployed)

        # .github/ must NOT be touched
        assert not (self.project_root / ".github").exists()

        for p in deployed:
            assert (self.project_root / p).exists()

        # -- uninstall -----------------------------------------------------
        sync = integrator.sync_integration(
            pkg_info.package,
            self.project_root,
            managed_files=deployed,
            targets=[target],
        )
        assert sync["errors"] == 0
        assert sync["files_removed"] >= 1
        for p in deployed:
            assert not (self.project_root / p).exists()

    def test_opencode_user_scope(self):
        """Skill deploys to .config/opencode/skills/ at user scope."""
        target = KNOWN_TARGETS["opencode"].for_scope(user_scope=True)
        assert target is not None

        # Create target dir for detect_by_dir
        (self.project_root / ".config" / "opencode").mkdir(parents=True)

        pkg_info = _make_pkg(
            self.project_root,
            name="test-skill",
            instructions=False,
            agents=False,
            skills=True,
        )

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(pkg_info, self.project_root, targets=[target])

        assert result.skill_created or result.skill_updated
        assert len(result.target_paths) >= 1

        deployed = _posix_relpaths(self.project_root, result.target_paths)
        assert any(p.startswith(".config/opencode/skills/") for p in deployed)

        # .opencode/ must NOT be touched
        assert not (self.project_root / ".opencode").exists()

        for p in deployed:
            assert (self.project_root / p).exists()

        # -- uninstall -----------------------------------------------------
        sync = integrator.sync_integration(
            pkg_info.package,
            self.project_root,
            managed_files=deployed,
            targets=[target],
        )
        assert sync["errors"] == 0
        assert sync["files_removed"] >= 1
        for p in deployed:
            assert not (self.project_root / p).exists()

    def test_codex_project_scope(self):
        """Codex skills deploy to .agents/skills/ (deploy_root override)."""
        target = KNOWN_TARGETS["codex"].for_scope(user_scope=False)
        # auto_create=False: codex requires .codex/ to exist
        (self.project_root / ".codex").mkdir()

        pkg_info = _make_pkg(
            self.project_root,
            name="test-skill",
            instructions=False,
            agents=False,
            skills=True,
        )

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(pkg_info, self.project_root, targets=[target])

        assert result.skill_created or result.skill_updated
        assert len(result.target_paths) >= 1

        deployed = _posix_relpaths(self.project_root, result.target_paths)
        assert any(p.startswith(".agents/skills/") for p in deployed)

        for p in deployed:
            assert (self.project_root / p).exists()

        # -- uninstall -----------------------------------------------------
        sync = integrator.sync_integration(
            pkg_info.package,
            self.project_root,
            managed_files=deployed,
            targets=[target],
        )
        assert sync["errors"] == 0
        assert sync["files_removed"] >= 1
        for p in deployed:
            assert not (self.project_root / p).exists()

    def test_codex_user_scope(self):
        """Codex skills keep using .agents/skills/ at user scope."""
        target = KNOWN_TARGETS["codex"].for_scope(user_scope=True)
        assert target is not None
        (self.project_root / ".codex").mkdir()

        pkg_info = _make_pkg(
            self.project_root,
            name="test-skill",
            instructions=False,
            agents=False,
            skills=True,
        )

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(pkg_info, self.project_root, targets=[target])

        assert result.skill_created or result.skill_updated
        deployed = _posix_relpaths(self.project_root, result.target_paths)
        assert any(p.startswith(".agents/skills/") for p in deployed)

    def test_claude_project_scope(self):
        """Skill deploys to .claude/skills/ at project scope."""
        target = KNOWN_TARGETS["claude"].for_scope(user_scope=False)
        # auto_create=False
        (self.project_root / ".claude").mkdir()

        pkg_info = _make_pkg(
            self.project_root,
            name="test-skill",
            instructions=False,
            agents=False,
            skills=True,
        )

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(pkg_info, self.project_root, targets=[target])

        assert result.skill_created or result.skill_updated
        assert len(result.target_paths) >= 1

        deployed = _posix_relpaths(self.project_root, result.target_paths)
        assert any(p.startswith(".claude/skills/") for p in deployed)

        for p in deployed:
            assert (self.project_root / p).exists()

        # -- uninstall -----------------------------------------------------
        sync = integrator.sync_integration(
            pkg_info.package,
            self.project_root,
            managed_files=deployed,
            targets=[target],
        )
        assert sync["errors"] == 0
        assert sync["files_removed"] >= 1
        for p in deployed:
            assert not (self.project_root / p).exists()
