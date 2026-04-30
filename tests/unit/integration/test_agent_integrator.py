"""Tests for agent integration functionality."""

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from apm_cli.integration import AgentIntegrator
from apm_cli.models.apm_package import APMPackage, GitReferenceType, PackageInfo, ResolvedReference


class TestAgentIntegrator:
    """Test agent integration logic."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = AgentIntegrator()

    def teardown_method(self):
        """Clean up after tests."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_should_integrate_always_returns_true(self):
        """Test integration is always enabled (zero-config approach)."""
        # No .github/ directory needed
        assert self.integrator.should_integrate(self.project_root) == True  # noqa: E712

        # Even with .github/ present
        github_dir = self.project_root / ".github"
        github_dir.mkdir()
        assert self.integrator.should_integrate(self.project_root) == True  # noqa: E712

    def test_find_agent_files_in_root_new_format(self):
        """Test finding .agent.md files in package root."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()

        # Create test agent files
        (package_dir / "security.agent.md").write_text("# Security Agent")
        (package_dir / "planner.agent.md").write_text("# Planner Agent")
        (package_dir / "readme.md").write_text("# Readme")  # Should not be found

        agents = self.integrator.find_agent_files(package_dir)
        assert len(agents) == 2
        assert all(p.name.endswith(".agent.md") for p in agents)

    def test_find_agent_files_in_root_legacy_format(self):
        """Test finding .chatmode.md files in package root (legacy)."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()

        # Create legacy chatmode files
        (package_dir / "default.chatmode.md").write_text("# Default Chatmode")
        (package_dir / "backend.chatmode.md").write_text("# Backend Chatmode")

        agents = self.integrator.find_agent_files(package_dir)
        assert len(agents) == 2
        assert all(p.name.endswith(".chatmode.md") for p in agents)

    def test_find_agent_files_in_apm_agents(self):
        """Test finding .agent.md files in .apm/agents/ (new standard)."""
        package_dir = self.project_root / "package"
        apm_agents = package_dir / ".apm" / "agents"
        apm_agents.mkdir(parents=True)

        (apm_agents / "security.agent.md").write_text("# Security Agent")

        agents = self.integrator.find_agent_files(package_dir)
        assert len(agents) == 1
        assert agents[0].name == "security.agent.md"

    def test_find_agent_files_in_apm_chatmodes(self):
        """Test finding .chatmode.md files in .apm/chatmodes/ (legacy)."""
        package_dir = self.project_root / "package"
        apm_chatmodes = package_dir / ".apm" / "chatmodes"
        apm_chatmodes.mkdir(parents=True)

        (apm_chatmodes / "default.chatmode.md").write_text("# Default Chatmode")

        agents = self.integrator.find_agent_files(package_dir)
        assert len(agents) == 1
        assert agents[0].name == "default.chatmode.md"

    def test_find_agent_files_mixed_formats(self):
        """Test finding both .agent.md and .chatmode.md files."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()

        (package_dir / "new.agent.md").write_text("# New Agent")
        (package_dir / "old.chatmode.md").write_text("# Old Chatmode")

        agents = self.integrator.find_agent_files(package_dir)
        assert len(agents) == 2
        extensions = {tuple(p.name.split(".")[-2:]) for p in agents}
        assert extensions == {("agent", "md"), ("chatmode", "md")}

    def test_copy_agent_verbatim(self):
        """Test copying agent file verbatim (no metadata injection)."""
        source = self.project_root / "source.agent.md"
        target = self.project_root / "target.agent.md"

        source_content = "# Security Agent\n\nSome agent content."
        source.write_text(source_content)

        self.integrator.copy_agent(source, target)

        target_content = target.read_text()
        assert target_content == source_content

    def test_get_target_filename_agent_format(self):
        """Test target filename generation with clean naming for .agent.md."""
        source = Path("/package/security.agent.md")
        package_name = "acme/security-standards"

        target = self.integrator.get_target_filename(source, package_name)
        # Clean naming: original stem preserved
        assert target == "security.agent.md"

    def test_get_target_filename_chatmode_format(self):
        """Test target filename generation renames .chatmode.md to .agent.md."""
        source = Path("/package/default.chatmode.md")
        package_name = "microsoft/apm-sample-package"

        target = self.integrator.get_target_filename(source, package_name)
        # chatmode is legacy — deploy as .agent.md
        assert target == "default.agent.md"

    def test_integrate_package_agents_creates_directory(self):
        """Test that integration creates .github/agents/ if missing."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text("# Security Agent")

        github_dir = self.project_root / ".github"
        github_dir.mkdir()

        package = APMPackage(name="test-pkg", version="1.0.0", package_path=package_dir)
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        )
        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
        )

        result = self.integrator.integrate_package_agents(package_info, self.project_root)

        assert result.files_integrated == 1
        assert (self.project_root / ".github" / "agents").exists()

    def test_integrate_package_agents_always_overwrites(self):
        """Test that integration always overwrites existing files."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text("# Security Agent")

        github_agents = self.project_root / ".github" / "agents"
        github_agents.mkdir(parents=True)

        # Pre-create the target file with old content
        (github_agents / "security.agent.md").write_text("# Old Content")

        package = APMPackage(
            name="test-pkg",
            version="1.0.0",
            package_path=package_dir,
            source="github.com/test/repo",
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        )
        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=resolved_ref,
            installed_at="2024-01-01T00:00:00",
        )

        result = self.integrator.integrate_package_agents(package_info, self.project_root)

        assert result.files_integrated == 1
        assert result.files_updated == 0
        assert result.files_skipped == 0
        # Verify content was overwritten
        content = (github_agents / "security.agent.md").read_text()
        assert content == "# Security Agent"

    # ========== Verbatim Copy Tests ==========

    def test_copy_agent_preserves_frontmatter(self):
        """Test that copy_agent preserves existing YAML frontmatter as-is."""
        source = self.project_root / "source.agent.md"
        target = self.project_root / "target.agent.md"

        source_content = """---
description: My agent
tools: []
---

# Agent content here"""
        source.write_text(source_content)

        self.integrator.copy_agent(source, target)

        assert target.read_text() == source_content

    def test_integrate_first_time_copies_verbatim(self):
        """Test that first-time integration creates files with proper frontmatter metadata."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text("# Security Agent Content")

        github_agents = self.project_root / ".github" / "agents"
        github_agents.mkdir(parents=True)

        package = APMPackage(
            name="test-pkg",
            version="1.0.0",
            package_path=package_dir,
            source="github.com/test/repo",
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        )
        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=resolved_ref,
            installed_at="2024-11-13T10:00:00",
        )

        result = self.integrator.integrate_package_agents(package_info, self.project_root)

        assert result.files_integrated == 1
        assert result.files_updated == 0
        assert result.files_skipped == 0

        # Verify verbatim copy — no frontmatter injected
        target_file = github_agents / "security.agent.md"
        content = target_file.read_text()
        assert content == "# Security Agent Content"
        assert "apm:" not in content

    def test_integrate_overwrites_existing_file(self):
        """Test that integration always overwrites existing files."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text("# Updated Agent Content")

        github_agents = self.project_root / ".github" / "agents"
        github_agents.mkdir(parents=True)

        # Pre-create file with old content
        (github_agents / "security.agent.md").write_text("# Old Content")

        package = APMPackage(
            name="test-pkg",
            version="2.0.0",  # New version
            package_path=package_dir,
            source="github.com/test/repo",
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        )
        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=resolved_ref,
            installed_at="2024-11-13T11:00:00",
        )

        result = self.integrator.integrate_package_agents(package_info, self.project_root)

        assert result.files_integrated == 1
        assert result.files_updated == 0
        assert result.files_skipped == 0

        # Verify content was overwritten verbatim
        target_file = github_agents / "security.agent.md"
        content = target_file.read_text()
        assert content == "# Updated Agent Content"

    def test_integrate_all_files_always_copied(self):
        """Test integration copies all agent files regardless of existing state."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()

        # Create 3 agent files in package
        (package_dir / "new.agent.md").write_text("# New Agent")
        (package_dir / "existing.agent.md").write_text("# Updated Agent")
        (package_dir / "another.agent.md").write_text("# Another Agent")

        github_agents = self.project_root / ".github" / "agents"
        github_agents.mkdir(parents=True)

        # Pre-create some target files
        (github_agents / "existing.agent.md").write_text("# Old Content")
        (github_agents / "another.agent.md").write_text("# Old Another")

        package = APMPackage(
            name="test-pkg",
            version="2.0.0",
            package_path=package_dir,
            source="github.com/test/repo",
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="def456",
            ref_name="main",
        )
        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=resolved_ref,
            installed_at="2024-11-13T11:00:00",
        )

        result = self.integrator.integrate_package_agents(package_info, self.project_root)

        assert result.files_integrated == 3  # All files always copied
        assert result.files_updated == 0
        assert result.files_skipped == 0

        # Verify all files exist with verbatim content
        assert (github_agents / "new.agent.md").read_text() == "# New Agent"
        assert (github_agents / "existing.agent.md").read_text() == "# Updated Agent"
        assert (github_agents / "another.agent.md").read_text() == "# Another Agent"

    # ========== Sync Integration Tests (Nuke & Regenerate) ==========

    def test_sync_integration_removes_all_apm_agents(self):
        """Test that sync removes all APM-managed agent files."""
        github_agents = self.project_root / ".github" / "agents"
        github_agents.mkdir(parents=True)

        # Create APM-managed agent files
        (github_agents / "security-apm.agent.md").write_text("# Security Agent")
        (github_agents / "compliance-apm.agent.md").write_text("# Compliance Agent")

        apm_package = Mock()

        result = self.integrator.sync_integration(apm_package, self.project_root)

        assert result["files_removed"] == 2
        assert not (github_agents / "security-apm.agent.md").exists()
        assert not (github_agents / "compliance-apm.agent.md").exists()

    def test_sync_integration_removes_renamed_chatmode_agents(self):
        """Test that sync removes agents that were originally chatmode files (now deployed as .agent.md)."""
        github_agents = self.project_root / ".github" / "agents"
        github_agents.mkdir(parents=True)

        (github_agents / "default-apm.agent.md").write_text("# Default Agent (was chatmode)")

        apm_package = Mock()

        result = self.integrator.sync_integration(apm_package, self.project_root)

        assert result["files_removed"] == 1
        assert not (github_agents / "default-apm.agent.md").exists()

    def test_sync_integration_preserves_non_apm_files(self):
        """Test that sync does not remove non-APM files."""
        github_agents = self.project_root / ".github" / "agents"
        github_agents.mkdir(parents=True)

        # Create APM and non-APM files
        (github_agents / "security-apm.agent.md").write_text("# APM Agent")
        (github_agents / "custom.agent.md").write_text("# Custom Agent")
        (github_agents / "my-agent.agent.md").write_text("# My Agent")

        apm_package = Mock()

        result = self.integrator.sync_integration(apm_package, self.project_root)

        assert result["files_removed"] == 1
        assert (github_agents / "custom.agent.md").exists()
        assert (github_agents / "my-agent.agent.md").exists()

    def test_sync_integration_handles_missing_agents_dir(self):
        """Test that sync gracefully handles missing .github/agents/ directory."""
        apm_package = Mock()

        # Should not raise exception
        result = self.integrator.sync_integration(apm_package, self.project_root)
        assert result["files_removed"] == 0

    def test_sync_integration_removes_apm_files_regardless_of_content(self):
        """Test that sync removes all *-apm files, regardless of content."""
        github_agents = self.project_root / ".github" / "agents"
        github_agents.mkdir(parents=True)

        # APM-managed file with no frontmatter — still removed by pattern
        (github_agents / "custom-apm.agent.md").write_text("# Custom agent without header")

        apm_package = Mock()

        result = self.integrator.sync_integration(apm_package, self.project_root)

        assert result["files_removed"] == 1
        assert not (github_agents / "custom-apm.agent.md").exists()

    # ========== Skill Separation Regression Tests (T5) ==========
    # ARCHITECTURE DECISION: Skills are NOT Agents
    # Skills go to .github/skills/ via SkillIntegrator
    # Agents go to .github/agents/ via AgentIntegrator
    # These tests verify agent_integrator does NOT transform skills

    def test_skill_files_not_converted_to_agents(self):
        """Regression test: SKILL.md files must NOT be transformed to .agent.md.

        This was removed in T5 of the Skills Strategy refactoring.
        Skills and Agents have different semantics:
        - Skills: Declarative context/knowledge packages (.github/skills/)
        - Agents: Executable VSCode chat modes (.github/agents/)
        """
        package_dir = self.project_root / "package"
        package_dir.mkdir()

        # Create a SKILL.md file
        (package_dir / "SKILL.md").write_text("""---
name: test-skill
description: A test skill
---
# Test Skill

This is a skill, not an agent.""")

        github_dir = self.project_root / ".github"
        github_dir.mkdir()

        package = APMPackage(name="skill-pkg", version="1.0.0", package_path=package_dir)
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        )
        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
        )

        result = self.integrator.integrate_package_agents(package_info, self.project_root)

        # No agents should be created from skills
        assert result.files_integrated == 0

        # Verify .github/agents/ does NOT contain skill-derived files
        agents_dir = self.project_root / ".github" / "agents"
        if agents_dir.exists():
            agent_files = list(agents_dir.glob("*.agent.md"))
            for agent_file in agent_files:
                assert "skill" not in agent_file.name.lower(), (
                    f"SKILL.md was incorrectly transformed to agent: {agent_file}"
                )

    def test_find_agent_files_ignores_skill_files(self):
        """AgentIntegrator.find_agent_files() must not find SKILL.md files."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()

        # Create various files
        (package_dir / "security.agent.md").write_text("# Real Agent")
        (package_dir / "SKILL.md").write_text("# This is a skill")
        (package_dir / "skill.md").write_text("# Also a skill")

        agents = self.integrator.find_agent_files(package_dir)

        # Only .agent.md files should be found
        assert len(agents) == 1
        assert agents[0].name == "security.agent.md"

        # Verify no SKILL.md files were picked up
        found_names = [a.name for a in agents]
        assert "SKILL.md" not in found_names
        assert "skill.md" not in found_names

    def test_find_agent_files_includes_all_md(self):
        """All .md files in .apm/agents/ are discovered — the directory
        already implies type, so no name-based filtering."""
        package_dir = self.project_root / "package"
        apm_agents = package_dir / ".apm" / "agents"
        apm_agents.mkdir(parents=True)

        (apm_agents / "planner.md").write_text("# Planner agent")
        (apm_agents / "coder.md").write_text("# Coder agent")
        (apm_agents / "README.md").write_text("# Docs")
        (apm_agents / "CHANGELOG.md").write_text("# Changes")
        (apm_agents / "LICENSE.md").write_text("MIT")
        (apm_agents / "CONTRIBUTING.md").write_text("# Contributing")

        agents = self.integrator.find_agent_files(package_dir)
        names = {a.name for a in agents}

        assert names == {
            "planner.md",
            "coder.md",
            "README.md",
            "CHANGELOG.md",
            "LICENSE.md",
            "CONTRIBUTING.md",
        }

    def test_find_agent_files_discovers_nested_subdirectories(self):
        """find_agent_files uses rglob so agents in subdirs are found."""
        package_dir = self.project_root / "package"
        apm_agents = package_dir / ".apm" / "agents"
        nested = apm_agents / "subdir"
        nested.mkdir(parents=True)

        (apm_agents / "top-level.agent.md").write_text("# Top")
        (nested / "nested.agent.md").write_text("# Nested agent.md")
        (nested / "plain-nested.md").write_text("# Nested plain")

        agents = self.integrator.find_agent_files(package_dir)
        names = {a.name for a in agents}

        assert "top-level.agent.md" in names
        assert "nested.agent.md" in names
        assert "plain-nested.md" in names

    def test_get_target_filename_plain_md(self):
        """Plain .md files get renamed to .agent.md for .github/agents/."""
        source = Path("/package/.apm/agents/context-architect.md")
        result = self.integrator.get_target_filename(source, "my-plugin")
        assert result == "context-architect.agent.md"


class TestAgentSuffixPattern:
    """Test clean naming pattern edge cases for agents."""

    def setup_method(self):
        """Set up test fixtures."""
        self.integrator = AgentIntegrator()

    def test_clean_naming_simple_agent_filename(self):
        """Test clean naming with simple agent filename."""
        source = Path("security.agent.md")
        result = self.integrator.get_target_filename(source, "pkg")
        assert result == "security.agent.md"

    def test_clean_naming_chatmode_to_agent(self):
        """Test clean naming renames chatmode to agent format."""
        source = Path("default.chatmode.md")
        result = self.integrator.get_target_filename(source, "pkg")
        assert result == "default.agent.md"

    def test_clean_naming_hyphenated_filename(self):
        """Test clean naming with hyphenated filename."""
        source = Path("backend-engineer.agent.md")
        result = self.integrator.get_target_filename(source, "pkg")
        assert result == "backend-engineer.agent.md"

    def test_clean_naming_multi_part_filename(self):
        """Test clean naming with multi-part filename."""
        source = Path("security-audit-tool.agent.md")
        result = self.integrator.get_target_filename(source, "pkg")
        assert result == "security-audit-tool.agent.md"

    def test_clean_naming_preserves_original_name(self):
        """Test that original filename structure is preserved."""
        source = Path("my_custom-agent.agent.md")
        result = self.integrator.get_target_filename(source, "pkg")
        assert result == "my_custom-agent.agent.md"


class TestClaudeAgentIntegration:
    """Tests for Claude agent integration (.claude/agents/)."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = AgentIntegrator()

    def teardown_method(self):
        """Clean up after tests."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_package_info(self, package_dir):
        """Helper to create a PackageInfo object."""
        package = APMPackage(name="test-pkg", version="1.0.0", package_path=package_dir)
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
        )

    def test_get_target_filename_claude_from_agent_md(self):
        """Test Claude filename from .agent.md uses .md extension."""
        source = Path("security.agent.md")
        result = self.integrator.get_target_filename_claude(source, "pkg")
        assert result == "security.md"

    def test_get_target_filename_claude_from_chatmode_md(self):
        """Test Claude filename from .chatmode.md uses .md extension."""
        source = Path("default.chatmode.md")
        result = self.integrator.get_target_filename_claude(source, "pkg")
        assert result == "default.md"

    def test_get_target_filename_claude_hyphenated(self):
        """Test Claude filename with hyphenated source name."""
        source = Path("backend-engineer.agent.md")
        result = self.integrator.get_target_filename_claude(source, "pkg")
        assert result == "backend-engineer.md"

    def test_integrate_creates_claude_agents_directory(self):
        """Test that integration creates .claude/agents/ if missing."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text("# Security Agent")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_claude(package_info, self.project_root)

        assert result.files_integrated == 1
        assert (self.project_root / ".claude" / "agents").exists()

    def test_integrate_copies_agent_to_claude_agents(self):
        """Test agent files are copied to .claude/agents/ with .md extension."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text(
            "# Security Agent\nReview code for vulnerabilities."
        )

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_claude(package_info, self.project_root)

        assert result.files_integrated == 1
        target_file = self.project_root / ".claude" / "agents" / "security.md"
        assert target_file.exists()
        content = target_file.read_text()
        assert "Security Agent" in content
        assert "Review code for vulnerabilities" in content

    def test_integrate_handles_chatmode_files(self):
        """Test .chatmode.md files are integrated to .claude/agents/."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "backend.chatmode.md").write_text("# Backend Mode")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_claude(package_info, self.project_root)

        assert result.files_integrated == 1
        target_file = self.project_root / ".claude" / "agents" / "backend.md"
        assert target_file.exists()

    def test_integrate_multiple_agents(self):
        """Test multiple agent files are all integrated."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text("# Security")
        (package_dir / "planner.agent.md").write_text("# Planner")
        (package_dir / "default.chatmode.md").write_text("# Default")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_claude(package_info, self.project_root)

        assert result.files_integrated == 3
        assert (self.project_root / ".claude" / "agents" / "security.md").exists()
        assert (self.project_root / ".claude" / "agents" / "planner.md").exists()
        assert (self.project_root / ".claude" / "agents" / "default.md").exists()

    def test_integrate_agents_from_apm_agents_dir(self):
        """Test finding agents in .apm/agents/ subdirectory."""
        package_dir = self.project_root / "package"
        apm_agents = package_dir / ".apm" / "agents"
        apm_agents.mkdir(parents=True)
        (apm_agents / "reviewer.agent.md").write_text("# Code Reviewer")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_claude(package_info, self.project_root)

        assert result.files_integrated == 1
        assert (self.project_root / ".claude" / "agents" / "reviewer.md").exists()

    def test_integrate_no_agents_returns_empty_result(self):
        """Test empty result when no agent files found."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "readme.md").write_text("# Not an agent")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_claude(package_info, self.project_root)

        assert result.files_integrated == 0
        assert not (self.project_root / ".claude" / "agents").exists()

    def test_integrate_always_overwrites(self):
        """Test that integration always overwrites existing files."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text("# Updated Content")

        # Pre-create target
        agents_dir = self.project_root / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "security.md").write_text("# Old Content")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_claude(package_info, self.project_root)

        assert result.files_integrated == 1
        content = (agents_dir / "security.md").read_text()
        assert "Updated Content" in content

    def test_integrate_preserves_frontmatter(self):
        """Test that YAML frontmatter is preserved in Claude agents."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        content = """---
name: security-reviewer
description: Reviews code for security issues
tools: Read, Grep, Glob
model: sonnet
---

You are a security reviewer. Analyze code for vulnerabilities."""
        (package_dir / "security.agent.md").write_text(content)

        package_info = self._create_package_info(package_dir)
        self.integrator.integrate_package_agents_claude(package_info, self.project_root)

        target_content = (self.project_root / ".claude" / "agents" / "security.md").read_text()
        assert "name: security-reviewer" in target_content
        assert "description: Reviews code for security issues" in target_content
        assert "security reviewer" in target_content

    def test_sync_integration_claude_removes_apm_agents(self):
        """Test sync removes APM-managed agents from .claude/agents/."""
        agents_dir = self.project_root / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "security-apm.md").write_text("# APM managed")
        (agents_dir / "planner-apm.md").write_text("# APM managed")
        (agents_dir / "custom.md").write_text("# User created")

        result = self.integrator.sync_integration_claude(None, self.project_root)

        assert result["files_removed"] == 2
        assert not (agents_dir / "security-apm.md").exists()
        assert not (agents_dir / "planner-apm.md").exists()
        assert (agents_dir / "custom.md").exists()  # Preserved

    def test_sync_integration_claude_handles_missing_dir(self):
        """Test sync handles missing .claude/agents/ gracefully."""
        result = self.integrator.sync_integration_claude(None, self.project_root)

        assert result["files_removed"] == 0
        assert result["errors"] == 0


class TestCursorAgentIntegration:
    """Tests for Cursor agent integration (.cursor/agents/)."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = AgentIntegrator()

    def teardown_method(self):
        """Clean up after tests."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_package_info(self, package_dir):
        """Helper to create a PackageInfo object."""
        package = APMPackage(name="test-pkg", version="1.0.0", package_path=package_dir)
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
        )

    def test_get_target_filename_cursor_from_agent_md(self):
        """Test Cursor filename from .agent.md uses .md extension."""
        source = Path("security.agent.md")
        result = self.integrator.get_target_filename_cursor(source, "pkg")
        assert result == "security.md"

    def test_get_target_filename_cursor_from_chatmode_md(self):
        """Test Cursor filename from .chatmode.md uses .md extension."""
        source = Path("default.chatmode.md")
        result = self.integrator.get_target_filename_cursor(source, "pkg")
        assert result == "default.md"

    def test_get_target_filename_cursor_hyphenated(self):
        """Test Cursor filename with hyphenated source name."""
        source = Path("backend-engineer.agent.md")
        result = self.integrator.get_target_filename_cursor(source, "pkg")
        assert result == "backend-engineer.md"

    def test_integrate_skips_when_cursor_dir_missing(self):
        """Test that integration returns empty when .cursor/ doesn't exist."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text("# Security Agent")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_cursor(package_info, self.project_root)

        assert result.files_integrated == 0
        assert not (self.project_root / ".cursor" / "agents").exists()

    def test_integrate_creates_cursor_agents_directory(self):
        """Test that integration creates .cursor/agents/ when .cursor/ exists."""
        # Pre-create .cursor/ to opt in
        (self.project_root / ".cursor").mkdir()

        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text("# Security Agent")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_cursor(package_info, self.project_root)

        assert result.files_integrated == 1
        assert (self.project_root / ".cursor" / "agents").exists()

    def test_integrate_copies_agent_to_cursor_agents(self):
        """Test agent files are copied to .cursor/agents/ with .md extension."""
        (self.project_root / ".cursor").mkdir()

        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text(
            "# Security Agent\nReview code for vulnerabilities."
        )

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_cursor(package_info, self.project_root)

        assert result.files_integrated == 1
        target_file = self.project_root / ".cursor" / "agents" / "security.md"
        assert target_file.exists()
        content = target_file.read_text()
        assert "Security Agent" in content
        assert "Review code for vulnerabilities" in content

    def test_integrate_multiple_agents(self):
        """Test multiple agent files are all integrated."""
        (self.project_root / ".cursor").mkdir()

        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text("# Security")
        (package_dir / "planner.agent.md").write_text("# Planner")
        (package_dir / "default.chatmode.md").write_text("# Default")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_cursor(package_info, self.project_root)

        assert result.files_integrated == 3
        assert (self.project_root / ".cursor" / "agents" / "security.md").exists()
        assert (self.project_root / ".cursor" / "agents" / "planner.md").exists()
        assert (self.project_root / ".cursor" / "agents" / "default.md").exists()

    def test_integrate_no_agents_returns_empty_result(self):
        """Test empty result when no agent files found."""
        (self.project_root / ".cursor").mkdir()

        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "readme.md").write_text("# Not an agent")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_cursor(package_info, self.project_root)

        assert result.files_integrated == 0

    def test_integrate_preserves_frontmatter(self):
        """Test that YAML frontmatter is preserved in Cursor agents."""
        (self.project_root / ".cursor").mkdir()

        package_dir = self.project_root / "package"
        package_dir.mkdir()
        content = """---
name: security-reviewer
description: Reviews code for security issues
---

You are a security reviewer. Analyze code for vulnerabilities."""
        (package_dir / "security.agent.md").write_text(content)

        package_info = self._create_package_info(package_dir)
        self.integrator.integrate_package_agents_cursor(package_info, self.project_root)

        target_content = (self.project_root / ".cursor" / "agents" / "security.md").read_text()
        assert "name: security-reviewer" in target_content
        assert "description: Reviews code for security issues" in target_content
        assert "security reviewer" in target_content

    def test_integrate_package_agents_deploys_to_cursor_when_dir_exists(self):
        """Test integrate_package_agents() also deploys to .cursor/agents/."""
        (self.project_root / ".cursor").mkdir()

        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text("# Security Agent")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents(package_info, self.project_root)

        # Should deploy to both .github/agents/ and .cursor/agents/
        assert (self.project_root / ".github" / "agents" / "security.agent.md").exists()
        assert (self.project_root / ".cursor" / "agents" / "security.md").exists()
        posix_paths = [tp.relative_to(self.project_root).as_posix() for tp in result.target_paths]
        assert ".cursor/agents/security.md" in posix_paths

    def test_integrate_package_agents_skips_cursor_when_dir_missing(self):
        """Test integrate_package_agents() skips .cursor/ when it doesn't exist."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text("# Security Agent")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents(package_info, self.project_root)  # noqa: F841

        assert (self.project_root / ".github" / "agents" / "security.agent.md").exists()
        assert not (self.project_root / ".cursor" / "agents").exists()

    def test_sync_integration_cursor_removes_apm_agents(self):
        """Test sync removes APM-managed agents from .cursor/agents/."""
        agents_dir = self.project_root / ".cursor" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "security-apm.md").write_text("# APM managed")
        (agents_dir / "planner-apm.md").write_text("# APM managed")
        (agents_dir / "custom.md").write_text("# User created")

        result = self.integrator.sync_integration_cursor(None, self.project_root)

        assert result["files_removed"] == 2
        assert not (agents_dir / "security-apm.md").exists()
        assert not (agents_dir / "planner-apm.md").exists()
        assert (agents_dir / "custom.md").exists()  # Preserved

    def test_sync_integration_cursor_handles_missing_dir(self):
        """Test sync handles missing .cursor/agents/ gracefully."""
        result = self.integrator.sync_integration_cursor(None, self.project_root)

        assert result["files_removed"] == 0
        assert result["errors"] == 0


class TestOpenCodeAgentIntegration:
    """Tests for OpenCode agent integration."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.project_root = self.temp_dir / "project"
        self.project_root.mkdir()
        self.integrator = AgentIntegrator()

    def teardown_method(self):
        """Clean up after tests."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_package_info(self, package_dir):
        """Helper to create a PackageInfo object."""
        package = APMPackage(name="test-pkg", version="1.0.0", package_path=package_dir)
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
        )

    def test_integrate_skips_when_opencode_dir_missing(self):
        """Opt-in: skip if .opencode/ does not exist."""
        package_dir = self.project_root / "apm_modules" / "test-pkg"
        package_dir.mkdir(parents=True)
        apm_dir = package_dir / ".apm" / "agents"
        apm_dir.mkdir(parents=True)
        (apm_dir / "security.agent.md").write_text("# Security Agent")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_opencode(package_info, self.project_root)

        assert result.files_integrated == 0
        assert not (self.project_root / ".opencode" / "agents").exists()

    def test_integrate_deploys_to_opencode_agents(self):
        """Deploy agents to .opencode/agents/ when .opencode/ exists."""
        (self.project_root / ".opencode").mkdir()
        package_dir = self.project_root / "apm_modules" / "test-pkg"
        package_dir.mkdir(parents=True)
        apm_dir = package_dir / ".apm" / "agents"
        apm_dir.mkdir(parents=True)
        (apm_dir / "security.agent.md").write_text("# Security Agent")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_opencode(package_info, self.project_root)

        assert result.files_integrated == 1
        assert (self.project_root / ".opencode" / "agents" / "security.md").exists()

    def test_integrate_multiple_agents_opencode(self):
        """Deploy multiple agents to .opencode/agents/."""
        (self.project_root / ".opencode").mkdir()
        package_dir = self.project_root / "apm_modules" / "test-pkg"
        package_dir.mkdir(parents=True)
        apm_dir = package_dir / ".apm" / "agents"
        apm_dir.mkdir(parents=True)
        (apm_dir / "security.agent.md").write_text("# Security")
        (apm_dir / "planner.agent.md").write_text("# Planner")

        package_info = self._create_package_info(package_dir)
        result = self.integrator.integrate_package_agents_opencode(package_info, self.project_root)

        assert result.files_integrated == 2

    def test_sync_integration_opencode_removes_apm_agents(self):
        """Sync removes APM-managed agents from .opencode/agents/."""
        agents_dir = self.project_root / ".opencode" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "security-apm.md").write_text("# APM managed")
        (agents_dir / "custom.md").write_text("# User created")

        result = self.integrator.sync_integration_opencode(None, self.project_root)

        assert result["files_removed"] == 1
        assert not (agents_dir / "security-apm.md").exists()
        assert (agents_dir / "custom.md").exists()

    def test_sync_integration_opencode_handles_missing_dir(self):
        """Sync handles missing .opencode/agents/ gracefully."""
        result = self.integrator.sync_integration_opencode(None, self.project_root)

        assert result["files_removed"] == 0
        assert result["errors"] == 0


class TestCodexAgentIntegration:
    """Tests for Codex TOML agent transformation."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)
        (self.root / ".codex").mkdir()

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_agent_md_to_toml_with_frontmatter(self):
        """Agent .md with YAML frontmatter is converted to .toml."""
        import toml

        source = self.root / "test.agent.md"
        source.write_text(
            "---\nname: my-agent\ndescription: A test agent\n---\nDo something useful.\n",
            encoding="utf-8",
        )
        target = self.root / ".codex" / "agents" / "test.toml"
        target.parent.mkdir(parents=True, exist_ok=True)

        AgentIntegrator._write_codex_agent(source, target)

        assert target.exists()
        data = toml.loads(target.read_text(encoding="utf-8"))
        assert data["name"] == "my-agent"
        assert data["description"] == "A test agent"
        assert data["developer_instructions"] == "Do something useful."

    def test_agent_md_to_toml_without_frontmatter(self):
        """Agent .md without frontmatter uses filename as name."""
        import toml

        source = self.root / "helper.agent.md"
        source.write_text("Instructions for the helper agent.\n", encoding="utf-8")
        target = self.root / ".codex" / "agents" / "helper.toml"
        target.parent.mkdir(parents=True, exist_ok=True)

        AgentIntegrator._write_codex_agent(source, target)

        data = toml.loads(target.read_text(encoding="utf-8"))
        assert data["name"] == "helper"
        assert data["description"] == ""
        assert "Instructions for the helper agent." in data["developer_instructions"]

    def test_codex_agent_target_filename_is_toml(self):
        """AgentIntegrator generates .toml filenames for Codex target."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator = AgentIntegrator()
        codex = KNOWN_TARGETS["codex"]
        source = Path("/fake/test.agent.md")
        filename = integrator.get_target_filename_for_target(source, "pkg", codex)
        assert filename == "test.toml"
