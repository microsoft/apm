"""Tests for skill integration functionality (Claude Code SKILL.md support)."""

import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch
from datetime import datetime

from apm_cli.integration.skill_integrator import (
    SkillIntegrator,
    SkillIntegrationResult,
    to_hyphen_case,
    validate_skill_name,
    normalize_skill_name,
    copy_skill_to_target,
)
from apm_cli.models.apm_package import (
    PackageInfo,
    APMPackage,
    ResolvedReference,
    GitReferenceType,
    DependencyReference,
    PackageType,
    PackageContentType,
)


class TestToHyphenCase:
    """Test the to_hyphen_case helper function."""

    def test_basic_lowercase(self):
        """Test simple lowercase string."""
        assert to_hyphen_case("mypackage") == "mypackage"

    def test_camel_case(self):
        """Test camelCase conversion."""
        assert to_hyphen_case("myPackage") == "my-package"

    def test_pascal_case(self):
        """Test PascalCase conversion."""
        assert to_hyphen_case("MyPackage") == "my-package"

    def test_multi_camel_case(self):
        """Test multiple camelCase words."""
        assert to_hyphen_case("myAwesomePackageName") == "my-awesome-package-name"

    def test_with_underscores(self):
        """Test underscore replacement."""
        assert to_hyphen_case("my_package") == "my-package"

    def test_with_spaces(self):
        """Test space replacement."""
        assert to_hyphen_case("my package") == "my-package"

    def test_owner_repo_format(self):
        """Test owner/repo format extracts repo name."""
        assert to_hyphen_case("microsoft/apm-sample-package") == "apm-sample-package"
        assert to_hyphen_case("owner/MyRepo") == "my-repo"

    def test_mixed_separators(self):
        """Test mixed underscores and camelCase."""
        assert to_hyphen_case("my_AwesomePackage") == "my-awesome-package"

    def test_removes_invalid_characters(self):
        """Test removal of invalid characters."""
        assert to_hyphen_case("my@package!name") == "mypackagename"

    def test_removes_consecutive_hyphens(self):
        """Test consecutive hyphens are collapsed."""
        assert to_hyphen_case("my--package") == "my-package"
        assert to_hyphen_case("my___package") == "my-package"

    def test_strips_leading_trailing_hyphens(self):
        """Test leading/trailing hyphens are stripped."""
        assert to_hyphen_case("-mypackage-") == "mypackage"
        assert to_hyphen_case("_mypackage_") == "mypackage"

    def test_truncates_to_64_chars(self):
        """Test truncation to Claude Skills spec limit of 64 chars."""
        long_name = "a" * 100
        result = to_hyphen_case(long_name)
        assert len(result) == 64
        assert result == "a" * 64

    def test_empty_string(self):
        """Test empty string handling."""
        assert to_hyphen_case("") == ""

    def test_numbers_preserved(self):
        """Test numbers are preserved."""
        assert to_hyphen_case("package123") == "package123"
        assert to_hyphen_case("my2ndPackage") == "my2nd-package"


class TestSkillIntegrator:
    """Test SkillIntegrator class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = SkillIntegrator()

    def teardown_method(self):
        """Clean up after tests."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _get_skill_path(self, package_info) -> Path:
        """Get the expected skill directory path for a package.

        Uses the install folder name for simplicity and consistency.
        """
        skill_name = package_info.install_path.name
        return self.project_root / ".github" / "skills" / skill_name

    # ========== should_integrate tests ==========

    def test_should_integrate_always_returns_true(self):
        """Test that integration is always enabled."""
        assert self.integrator.should_integrate(self.project_root) is True

        # Even with various directories present
        (self.project_root / ".github").mkdir()
        assert self.integrator.should_integrate(self.project_root) is True

    # ========== find_instruction_files tests ==========

    def test_find_instruction_files_in_apm_instructions(self):
        """Test finding instruction files in .apm/instructions/."""
        package_dir = self.project_root / "package"
        apm_instructions = package_dir / ".apm" / "instructions"
        apm_instructions.mkdir(parents=True)

        (apm_instructions / "coding.instructions.md").write_text(
            "# Coding Instructions"
        )
        (apm_instructions / "testing.instructions.md").write_text(
            "# Testing Instructions"
        )
        (apm_instructions / "readme.md").write_text(
            "# Not an instruction"
        )  # Should not match

        instructions = self.integrator.find_instruction_files(package_dir)

        assert len(instructions) == 2
        assert all(p.name.endswith(".instructions.md") for p in instructions)

    def test_find_instruction_files_empty_when_no_directory(self):
        """Test returns empty list when .apm/instructions/ doesn't exist."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()

        instructions = self.integrator.find_instruction_files(package_dir)

        assert instructions == []

    def test_find_instruction_files_empty_when_no_files(self):
        """Test returns empty list when directory exists but has no instruction files."""
        package_dir = self.project_root / "package"
        apm_instructions = package_dir / ".apm" / "instructions"
        apm_instructions.mkdir(parents=True)

        instructions = self.integrator.find_instruction_files(package_dir)

        assert instructions == []

    # ========== find_agent_files tests ==========

    def test_find_agent_files_in_apm_agents(self):
        """Test finding agent files in .apm/agents/."""
        package_dir = self.project_root / "package"
        apm_agents = package_dir / ".apm" / "agents"
        apm_agents.mkdir(parents=True)

        (apm_agents / "reviewer.agent.md").write_text("# Reviewer Agent")
        (apm_agents / "debugger.agent.md").write_text("# Debugger Agent")
        (apm_agents / "other.md").write_text("# Not an agent")  # Should not match

        agents = self.integrator.find_agent_files(package_dir)

        assert len(agents) == 2
        assert all(p.name.endswith(".agent.md") for p in agents)

    def test_find_agent_files_empty_when_no_directory(self):
        """Test returns empty list when .apm/agents/ doesn't exist."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()

        agents = self.integrator.find_agent_files(package_dir)

        assert agents == []

    def test_find_agent_files_empty_when_no_files(self):
        """Test returns empty list when directory exists but has no agent files."""
        package_dir = self.project_root / "package"
        apm_agents = package_dir / ".apm" / "agents"
        apm_agents.mkdir(parents=True)

        agents = self.integrator.find_agent_files(package_dir)

        assert agents == []

    # ========== find_prompt_files tests ==========

    def test_find_prompt_files_in_root(self):
        """Test finding prompt files in package root."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()

        (package_dir / "design-review.prompt.md").write_text("# Design Review")
        (package_dir / "code-audit.prompt.md").write_text("# Code Audit")
        (package_dir / "readme.md").write_text("# Readme")  # Should not match

        prompts = self.integrator.find_prompt_files(package_dir)

        assert len(prompts) == 2
        assert all(p.name.endswith(".prompt.md") for p in prompts)

    def test_find_prompt_files_in_apm_prompts(self):
        """Test finding prompt files in .apm/prompts/."""
        package_dir = self.project_root / "package"
        apm_prompts = package_dir / ".apm" / "prompts"
        apm_prompts.mkdir(parents=True)

        (apm_prompts / "workflow.prompt.md").write_text("# Workflow")

        prompts = self.integrator.find_prompt_files(package_dir)

        assert len(prompts) == 1
        assert prompts[0].name == "workflow.prompt.md"

    def test_find_prompt_files_combines_root_and_apm(self):
        """Test finding prompt files from both root and .apm/prompts/."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        apm_prompts = package_dir / ".apm" / "prompts"
        apm_prompts.mkdir(parents=True)

        (package_dir / "root.prompt.md").write_text("# Root Prompt")
        (apm_prompts / "nested.prompt.md").write_text("# Nested Prompt")

        prompts = self.integrator.find_prompt_files(package_dir)

        assert len(prompts) == 2
        prompt_names = [p.name for p in prompts]
        assert "root.prompt.md" in prompt_names
        assert "nested.prompt.md" in prompt_names

    def test_find_prompt_files_empty_when_no_prompts(self):
        """Test returns empty list when no prompt files exist."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()

        prompts = self.integrator.find_prompt_files(package_dir)

        assert prompts == []

    # ========== find_context_files tests ==========

    def test_find_context_files_in_apm_context(self):
        """Test finding context files in .apm/context/."""
        package_dir = self.project_root / "package"
        apm_context = package_dir / ".apm" / "context"
        apm_context.mkdir(parents=True)

        (apm_context / "project.context.md").write_text("# Project Context")

        context_files = self.integrator.find_context_files(package_dir)

        assert len(context_files) == 1
        assert context_files[0].name == "project.context.md"

    def test_find_context_files_in_apm_memory(self):
        """Test finding memory files in .apm/memory/."""
        package_dir = self.project_root / "package"
        apm_memory = package_dir / ".apm" / "memory"
        apm_memory.mkdir(parents=True)

        (apm_memory / "history.memory.md").write_text("# History Memory")

        context_files = self.integrator.find_context_files(package_dir)

        assert len(context_files) == 1
        assert context_files[0].name == "history.memory.md"

    def test_find_context_files_combines_context_and_memory(self):
        """Test finding files from both context and memory directories."""
        package_dir = self.project_root / "package"
        apm_context = package_dir / ".apm" / "context"
        apm_memory = package_dir / ".apm" / "memory"
        apm_context.mkdir(parents=True)
        apm_memory.mkdir(parents=True)

        (apm_context / "project.context.md").write_text("# Context")
        (apm_memory / "history.memory.md").write_text("# Memory")

        context_files = self.integrator.find_context_files(package_dir)

        assert len(context_files) == 2

    # ========== integrate_package_skill tests ==========

    def _create_package_info(
        self,
        name: str = "test-pkg",
        version: str = "1.0.0",
        commit: str = "abc123",
        install_path: Path = None,
        source: str = None,
        description: str = None,
        dependency_ref: DependencyReference = None,
        package_type: PackageType = None,
        content_type: "PackageContentType" = None,
    ) -> PackageInfo:
        """Helper to create PackageInfo objects for tests.

        Args:
            package_type: Internal detection type (CLAUDE_SKILL, HYBRID, APM_PACKAGE)
            content_type: Explicit type from apm.yml (skill, hybrid, instructions, prompts)
        """
        package = APMPackage(
            name=name,
            version=version,
            package_path=install_path or self.project_root / "package",
            source=source or f"github.com/test/{name}",
            description=description,
            type=content_type,
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=commit,
            ref_name="main",
        )
        return PackageInfo(
            package=package,
            install_path=install_path or self.project_root / "package",
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dependency_ref,
            package_type=package_type,
        )

    def test_integrate_package_skill_skips_when_no_content(self):
        """Test that integration is skipped when package has no primitives."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()

        package_info = self._create_package_info(install_path=package_dir)

        result = self.integrator.integrate_package_skill(
            package_info, self.project_root
        )

        assert result.skill_created is False
        assert result.skill_updated is False
        assert result.skill_skipped is True
        assert result.skill_path is None
        assert not (package_dir / "SKILL.md").exists()

    def test_integrate_package_skill_skips_virtual_file_packages(self):
        """Test that virtual FILE packages (single files) do not generate Skills.

        Virtual file packages are individual files like owner/repo/agents/myagent.agent.md.
        They should not generate Skills because:
        1. Multiple virtual packages from the same repo would collide on skill name
        2. A single file doesn't constitute a proper skill with context

        Note: Virtual SUBDIRECTORY packages (like Claude Skills) SHOULD generate Skills.
        """
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        # Even if there's content, virtual file packages should be skipped
        (package_dir / "terraform.agent.md").write_text(
            "# Terraform Agent\nSome agent content"
        )

        # Create a virtual FILE package dependency reference
        virtual_dep_ref = DependencyReference.parse(
            "github/awesome-copilot/agents/terraform.agent.md"
        )
        assert virtual_dep_ref.is_virtual  # Sanity check
        assert virtual_dep_ref.is_virtual_file()  # This is a file, not subdirectory

        package_info = self._create_package_info(
            install_path=package_dir,
            name="terraform",
            source="github/awesome-copilot",
            dependency_ref=virtual_dep_ref,
        )

        result = self.integrator.integrate_package_skill(
            package_info, self.project_root
        )

        # Virtual FILE packages should be skipped
        assert result.skill_created is False
        assert result.skill_updated is False
        assert result.skill_skipped is True
        assert result.skill_path is None
        # No skill directory should be created
        skill_dir = self.project_root / ".github" / "skills" / "awesome-copilot"
        assert not skill_dir.exists()

    def test_integrate_package_skill_processes_virtual_subdirectory_packages(self):
        """Test that virtual SUBDIRECTORY packages (like Claude Skills) DO generate Skills.

        Subdirectory packages like ComposioHQ/awesome-claude-skills/mcp-builder are
        complete skill packages with their own content. They should generate Skills
        because they represent full packages, not individual files.
        """
        package_dir = self.project_root / "mcp-builder"
        package_dir.mkdir()
        # Create a subdirectory package with content
        (package_dir / "SKILL.md").write_text("# MCP Builder\nBuild MCP servers")
        instructions_dir = package_dir / ".apm" / "instructions"
        instructions_dir.mkdir(parents=True)
        (instructions_dir / "mcp.instructions.md").write_text(
            "---\napplyTo: '**/*'\n---\n# MCP Guidelines"
        )

        # Create a virtual SUBDIRECTORY package dependency reference
        virtual_dep_ref = DependencyReference.parse(
            "ComposioHQ/awesome-claude-skills/mcp-builder"
        )
        assert virtual_dep_ref.is_virtual  # Sanity check
        assert (
            virtual_dep_ref.is_virtual_subdirectory()
        )  # This is a subdirectory, not file

        # Has SKILL.md → CLAUDE_SKILL type
        package_info = self._create_package_info(
            install_path=package_dir,
            name="mcp-builder",
            source="ComposioHQ/awesome-claude-skills",
            dependency_ref=virtual_dep_ref,
            package_type=PackageType.CLAUDE_SKILL,
        )

        result = self.integrator.integrate_package_skill(
            package_info, self.project_root
        )

        # Virtual SUBDIRECTORY packages SHOULD generate skills
        assert result.skill_skipped is False
        assert result.skill_created is True
        assert result.skill_path is not None
        # Skill directory should be created
        assert result.skill_path.exists()

    def test_integrate_package_skill_multiple_virtual_file_packages_no_collision(self):
        """Test that multiple virtual FILE packages from same repo don't create conflicting Skills.

        This is a regression test: previously both would try to create 'awesome-copilot' skill.
        """
        # First virtual file package
        pkg1_dir = self.project_root / "pkg1"
        pkg1_dir.mkdir()
        (pkg1_dir / "jfrog-sec.agent.md").write_text("# JFrog Security Agent")

        virtual_dep1 = DependencyReference.parse(
            "github/awesome-copilot/agents/jfrog-sec.agent.md"
        )
        pkg1_info = self._create_package_info(
            install_path=pkg1_dir,
            name="jfrog-sec",
            source="github/awesome-copilot",
            dependency_ref=virtual_dep1,
        )

        # Second virtual file package from same repo
        pkg2_dir = self.project_root / "pkg2"
        pkg2_dir.mkdir()
        (pkg2_dir / "terraform.agent.md").write_text("# Terraform Agent")

        virtual_dep2 = DependencyReference.parse(
            "github/awesome-copilot/agents/terraform.agent.md"
        )
        pkg2_info = self._create_package_info(
            install_path=pkg2_dir,
            name="terraform",
            source="github/awesome-copilot",
            dependency_ref=virtual_dep2,
        )

        # Both should be skipped, no collision occurs
        result1 = self.integrator.integrate_package_skill(pkg1_info, self.project_root)
        result2 = self.integrator.integrate_package_skill(pkg2_info, self.project_root)

        assert result1.skill_skipped is True
        assert result2.skill_skipped is True

        # No skill directories should exist
        skills_dir = self.project_root / ".github" / "skills"
        assert not skills_dir.exists()

    def test_integrate_package_skill_skips_when_unchanged(self):
        """Test that SKILL.md is skipped when version and commit unchanged."""
        package_dir = self.project_root / "package"
        apm_agents = package_dir / ".apm" / "agents"
        apm_agents.mkdir(parents=True)
        (apm_agents / "helper.agent.md").write_text("# Helper")

        # Create package_info first to get the skill path
        package_info = self._create_package_info(
            version="1.0.0", commit="abc123", install_path=package_dir
        )
        skill_dir = self._get_skill_path(package_info)
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"

        # Create initial SKILL.md with same version and commit
        old_content = """---
name: test-pkg
description: Old description
metadata:
  apm_package: test-pkg@1.0.0
  apm_version: '1.0.0'
  apm_commit: abc123
  apm_installed_at: '2024-01-01T00:00:00'
  apm_content_hash: somehash
---

# Old content"""
        skill_path.write_text(old_content)

        result = self.integrator.integrate_package_skill(
            package_info, self.project_root
        )

        assert result.skill_created is False
        assert result.skill_updated is False
        assert result.skill_skipped is True

    # ========== update_gitignore_for_skills tests ==========

    def test_update_gitignore_adds_skill_patterns(self):
        """Test that gitignore is updated with skill patterns."""
        gitignore = self.project_root / ".gitignore"
        gitignore.write_text("# Existing content\napm_modules/\n")

        updated = self.integrator.update_gitignore_for_skills(self.project_root)

        assert updated is True
        content = gitignore.read_text()
        assert ".github/skills/*-apm/" in content

    def test_update_gitignore_skips_if_patterns_exist(self):
        """Test that gitignore update is skipped if patterns already exist."""
        gitignore = self.project_root / ".gitignore"
        gitignore.write_text(".github/skills/*-apm/\n# APM integrated skills\n")

        updated = self.integrator.update_gitignore_for_skills(self.project_root)

        assert updated is False

    def test_update_gitignore_creates_file_if_missing(self):
        """Test that gitignore is created if it doesn't exist."""
        updated = self.integrator.update_gitignore_for_skills(self.project_root)

        assert updated is True
        gitignore = self.project_root / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert ".github/skills/*-apm/" in content

    # ========== sync_integration tests ==========

    def test_sync_integration_returns_zero_stats(self):
        """Test that sync returns zero stats (cleanup handled by package removal)."""
        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = []

        result = self.integrator.sync_integration(apm_package, self.project_root)

        assert result == {"files_removed": 0, "errors": 0}

    def test_sync_integration_removes_orphaned_subdirectory_skill(self):
        """Test that sync removes skills for uninstalled subdirectory packages.

        This tests the full install → uninstall flow for virtual subdirectory packages
        like ComposioHQ/awesome-claude-skills/mcp-builder.
        """
        # Simulate an installed skill from a subdirectory package
        skill_name = "mcp-builder"
        skill_dir = self.project_root / ".github" / "skills" / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: mcp-builder\n---\n# MCP Builder Skill\n"
        )

        # Now simulate that this package was uninstalled (not in dependencies)
        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = []  # Empty = uninstalled

        result = self.integrator.sync_integration(apm_package, self.project_root)

        # Orphaned skill should be removed
        assert result["files_removed"] == 1
        assert not skill_dir.exists()

    def test_sync_integration_keeps_installed_subdirectory_skill(self):
        """Test that sync keeps skills for still-installed subdirectory packages."""
        # Simulate an installed skill from a subdirectory package
        skill_name = "mcp-builder"
        skill_dir = self.project_root / ".github" / "skills" / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: mcp-builder\n---\n# MCP Builder Skill\n"
        )

        # Simulate that this package is still installed
        dep_ref = DependencyReference.parse(
            "ComposioHQ/awesome-claude-skills/mcp-builder"
        )

        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = [dep_ref]

        result = self.integrator.sync_integration(apm_package, self.project_root)

        # Skill should NOT be removed
        assert result["files_removed"] == 0
        assert skill_dir.exists()


class TestSkillIntegrationResult:
    """Test SkillIntegrationResult dataclass."""

    def test_result_defaults(self):
        """Test result dataclass default values."""
        result = SkillIntegrationResult(
            skill_created=False,
            skill_updated=False,
            skill_skipped=True,
            skill_path=None,
            references_copied=0,
        )

        assert result.skill_created is False
        assert result.skill_updated is False
        assert result.skill_skipped is True
        assert result.skill_path is None
        assert result.references_copied == 0
        assert result.links_resolved == 0

    def test_result_with_values(self):
        """Test result dataclass with values."""
        skill_path = Path("/test/SKILL.md")
        result = SkillIntegrationResult(
            skill_created=True,
            skill_updated=False,
            skill_skipped=False,
            skill_path=skill_path,
            references_copied=3,
            links_resolved=5,
        )

        assert result.skill_created is True
        assert result.skill_path == skill_path
        assert result.references_copied == 3
        assert result.links_resolved == 5


class TestValidateSkillName:
    """Test skill name validation per agentskills.io spec."""

    # ========== Valid names ==========

    def test_valid_simple_lowercase(self):
        """Test valid simple lowercase name."""
        is_valid, error = validate_skill_name("mypackage")
        assert is_valid is True
        assert error == ""

    def test_valid_with_hyphens(self):
        """Test valid name with hyphens."""
        is_valid, error = validate_skill_name("my-awesome-package")
        assert is_valid is True
        assert error == ""

    def test_valid_with_numbers(self):
        """Test valid name with numbers."""
        is_valid, error = validate_skill_name("package123")
        assert is_valid is True
        assert error == ""

    def test_valid_numbers_and_hyphens(self):
        """Test valid name with numbers and hyphens."""
        is_valid, error = validate_skill_name("my-package-2")
        assert is_valid is True
        assert error == ""

    def test_valid_single_char(self):
        """Test valid single character name."""
        is_valid, error = validate_skill_name("a")
        assert is_valid is True
        assert error == ""

    def test_valid_single_number(self):
        """Test valid single number name."""
        is_valid, error = validate_skill_name("1")
        assert is_valid is True
        assert error == ""

    def test_valid_64_chars(self):
        """Test valid name at max length (64 chars)."""
        name = "a" * 64
        is_valid, error = validate_skill_name(name)
        assert is_valid is True
        assert error == ""

    def test_valid_realistic_names(self):
        """Test valid realistic skill names."""
        valid_names = [
            "mcp-builder",
            "brand-guidelines",
            "code-review",
            "gdpr-assessment",
            "python-standards",
            "react-components",
            "aws-lambda-v2",
            "openai-gpt4o",
        ]
        for name in valid_names:
            is_valid, error = validate_skill_name(name)
            assert is_valid is True, (
                f"Expected '{name}' to be valid, got error: {error}"
            )

    # ========== Invalid: Uppercase letters ==========

    def test_invalid_uppercase(self):
        """Test invalid name with uppercase letters."""
        is_valid, error = validate_skill_name("MyPackage")
        assert is_valid is False
        assert "lowercase" in error.lower()

    def test_invalid_all_uppercase(self):
        """Test invalid name with all uppercase."""
        is_valid, error = validate_skill_name("MYPACKAGE")
        assert is_valid is False
        assert "lowercase" in error.lower()

    def test_invalid_mixed_case(self):
        """Test invalid name with mixed case."""
        is_valid, error = validate_skill_name("myPackage")
        assert is_valid is False
        assert "lowercase" in error.lower()

    # ========== Invalid: Underscores ==========

    def test_invalid_underscore(self):
        """Test invalid name with underscores."""
        is_valid, error = validate_skill_name("my_package")
        assert is_valid is False
        assert "underscore" in error.lower()

    def test_invalid_multiple_underscores(self):
        """Test invalid name with multiple underscores."""
        is_valid, error = validate_skill_name("my_awesome_package")
        assert is_valid is False
        assert "underscore" in error.lower()

    # ========== Invalid: Spaces ==========

    def test_invalid_space(self):
        """Test invalid name with spaces."""
        is_valid, error = validate_skill_name("my package")
        assert is_valid is False
        assert "space" in error.lower()

    def test_invalid_multiple_spaces(self):
        """Test invalid name with multiple spaces."""
        is_valid, error = validate_skill_name("my awesome package")
        assert is_valid is False
        assert "space" in error.lower()

    # ========== Invalid: Special characters ==========

    def test_invalid_special_chars(self):
        """Test invalid name with special characters."""
        is_valid, error = validate_skill_name("my@package")
        assert is_valid is False
        assert "invalid character" in error.lower() or "alphanumeric" in error.lower()

    def test_invalid_dots(self):
        """Test invalid name with dots."""
        is_valid, error = validate_skill_name("my.package")
        assert is_valid is False
        assert "invalid character" in error.lower() or "alphanumeric" in error.lower()

    def test_invalid_slashes(self):
        """Test invalid name with slashes."""
        is_valid, error = validate_skill_name("my/package")
        assert is_valid is False
        assert "invalid character" in error.lower() or "alphanumeric" in error.lower()

    # ========== Invalid: Consecutive hyphens ==========

    def test_invalid_consecutive_hyphens(self):
        """Test invalid name with consecutive hyphens."""
        is_valid, error = validate_skill_name("my--package")
        assert is_valid is False
        assert "consecutive" in error.lower()

    def test_invalid_triple_hyphens(self):
        """Test invalid name with triple hyphens."""
        is_valid, error = validate_skill_name("my---package")
        assert is_valid is False
        assert "consecutive" in error.lower()

    def test_invalid_multiple_consecutive_groups(self):
        """Test invalid name with multiple groups of consecutive hyphens."""
        is_valid, error = validate_skill_name("my--awesome--package")
        assert is_valid is False
        assert "consecutive" in error.lower()

    # ========== Invalid: Leading/trailing hyphens ==========

    def test_invalid_leading_hyphen(self):
        """Test invalid name starting with hyphen."""
        is_valid, error = validate_skill_name("-mypackage")
        assert is_valid is False
        assert "start" in error.lower()

    def test_invalid_trailing_hyphen(self):
        """Test invalid name ending with hyphen."""
        is_valid, error = validate_skill_name("mypackage-")
        assert is_valid is False
        assert "end" in error.lower()

    def test_invalid_both_leading_trailing_hyphens(self):
        """Test invalid name with both leading and trailing hyphens."""
        is_valid, error = validate_skill_name("-mypackage-")
        assert is_valid is False
        # Either error is acceptable
        assert "start" in error.lower() or "end" in error.lower()

    def test_invalid_only_hyphen(self):
        """Test invalid name that is just a hyphen."""
        is_valid, error = validate_skill_name("-")
        assert is_valid is False
        assert "start" in error.lower()

    # ========== Invalid: Length ==========

    def test_invalid_empty_string(self):
        """Test invalid empty name."""
        is_valid, error = validate_skill_name("")
        assert is_valid is False
        assert "empty" in error.lower()

    def test_invalid_too_long(self):
        """Test invalid name exceeding 64 characters."""
        name = "a" * 65
        is_valid, error = validate_skill_name(name)
        assert is_valid is False
        assert "64" in error or "65" in error

    def test_invalid_way_too_long(self):
        """Test invalid name far exceeding limit."""
        name = "a" * 200
        is_valid, error = validate_skill_name(name)
        assert is_valid is False
        assert "64" in error or "200" in error


class TestNormalizeSkillName:
    """Test skill name normalization for creating valid names from any input."""

    # ========== Basic normalization ==========

    def test_normalize_already_valid(self):
        """Test that already valid names remain unchanged."""
        assert normalize_skill_name("my-package") == "my-package"
        assert normalize_skill_name("package123") == "package123"

    def test_normalize_uppercase_to_lowercase(self):
        """Test uppercase conversion to lowercase."""
        assert normalize_skill_name("MyPackage") == "my-package"
        assert normalize_skill_name("MYPACKAGE") == "mypackage"

    def test_normalize_camel_case(self):
        """Test camelCase conversion."""
        assert normalize_skill_name("myPackage") == "my-package"
        assert normalize_skill_name("myAwesomePackage") == "my-awesome-package"

    def test_normalize_pascal_case(self):
        """Test PascalCase conversion."""
        assert normalize_skill_name("MyPackage") == "my-package"
        assert normalize_skill_name("MyAwesomePackage") == "my-awesome-package"

    # ========== Separator normalization ==========

    def test_normalize_underscores_to_hyphens(self):
        """Test underscores converted to hyphens."""
        assert normalize_skill_name("my_package") == "my-package"
        assert normalize_skill_name("my_awesome_package") == "my-awesome-package"

    def test_normalize_spaces_to_hyphens(self):
        """Test spaces converted to hyphens."""
        assert normalize_skill_name("my package") == "my-package"
        assert normalize_skill_name("my awesome package") == "my-awesome-package"

    def test_normalize_mixed_separators(self):
        """Test mixed separators normalized."""
        assert normalize_skill_name("my_awesome package") == "my-awesome-package"

    # ========== Consecutive hyphens ==========

    def test_normalize_removes_consecutive_hyphens(self):
        """Test consecutive hyphens are collapsed."""
        assert normalize_skill_name("my--package") == "my-package"
        assert normalize_skill_name("my---package") == "my-package"

    def test_normalize_underscores_create_single_hyphen(self):
        """Test multiple underscores become single hyphen."""
        assert normalize_skill_name("my___package") == "my-package"

    # ========== Leading/trailing normalization ==========

    def test_normalize_strips_leading_hyphens(self):
        """Test leading hyphens are stripped."""
        assert normalize_skill_name("-mypackage") == "mypackage"
        assert normalize_skill_name("--mypackage") == "mypackage"

    def test_normalize_strips_trailing_hyphens(self):
        """Test trailing hyphens are stripped."""
        assert normalize_skill_name("mypackage-") == "mypackage"
        assert normalize_skill_name("mypackage--") == "mypackage"

    def test_normalize_strips_leading_underscores(self):
        """Test leading underscores are stripped after conversion."""
        assert normalize_skill_name("_mypackage") == "mypackage"

    def test_normalize_strips_trailing_underscores(self):
        """Test trailing underscores are stripped after conversion."""
        assert normalize_skill_name("mypackage_") == "mypackage"

    # ========== Special character removal ==========

    def test_normalize_removes_special_chars(self):
        """Test special characters are removed."""
        assert normalize_skill_name("my@package") == "mypackage"
        assert normalize_skill_name("my!package#name") == "mypackagename"

    def test_normalize_removes_dots(self):
        """Test dots are removed."""
        assert normalize_skill_name("my.package") == "mypackage"

    # ========== Owner/repo format ==========

    def test_normalize_extracts_repo_name(self):
        """Test owner/repo format extracts repo name."""
        assert normalize_skill_name("owner/my-package") == "my-package"
        assert normalize_skill_name("acme/compliance-rules") == "compliance-rules"

    def test_normalize_extracts_and_converts_repo_name(self):
        """Test owner/repo format with conversion needed."""
        assert normalize_skill_name("owner/MyPackage") == "my-package"
        assert normalize_skill_name("owner/my_package") == "my-package"

    # ========== Truncation ==========

    def test_normalize_truncates_to_64_chars(self):
        """Test names are truncated to 64 characters."""
        long_name = "a" * 100
        result = normalize_skill_name(long_name)
        assert len(result) == 64

    def test_normalize_truncation_preserves_content(self):
        """Test truncation preserves the start of the name."""
        long_name = "abcdefghij" * 10  # 100 chars
        result = normalize_skill_name(long_name)
        assert result == "abcdefghij" * 6 + "abcd"  # First 64 chars

    # ========== Integration: Normalized names are valid ==========

    def test_normalize_produces_valid_names(self):
        """Test that normalized names pass validation."""
        test_inputs = [
            "MyPackage",
            "my_awesome_package",
            "owner/repo",
            "My Package Name",
            "package@v1.2.3",
            "--leading-hyphens--",
            "a" * 100,
            "camelCasePackageName",
            "UPPERCASE",
        ]

        for input_name in test_inputs:
            normalized = normalize_skill_name(input_name)
            if normalized:  # Skip if normalization produces empty string
                is_valid, error = validate_skill_name(normalized)
                assert is_valid is True, (
                    f"normalize_skill_name('{input_name}') = '{normalized}' is invalid: {error}"
                )

    def test_normalize_realistic_package_names(self):
        """Test normalization of realistic package names."""
        test_cases = [
            ("microsoft/apm-sample-package", "apm-sample-package"),
            ("ComposioHQ/awesome-claude-skills", "awesome-claude-skills"),
            ("github/awesome-copilot", "awesome-copilot"),
            ("My_Awesome_Package", "my-awesome-package"),
            ("code-review", "code-review"),
        ]

        for input_name, expected in test_cases:
            result = normalize_skill_name(input_name)
            assert result == expected, (
                f"normalize_skill_name('{input_name}') = '{result}', expected '{expected}'"
            )


class TestCopySkillToTarget:
    """Test the copy_skill_to_target standalone function (T6).

    This tests direct skill copy functionality for native skills
    that already have SKILL.md files.
    """

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.apm_modules = self.project_root / "apm_modules"
        self.apm_modules.mkdir(parents=True)

    def teardown_method(self):
        """Clean up after tests."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_package_info(
        self,
        name: str = "test-pkg",
        version: str = "1.0.0",
        commit: str = "abc123",
        install_path: Path = None,
        source: str = None,
        description: str = None,
        dependency_ref: DependencyReference = None,
        pkg_type: PackageContentType = None,
        package_type: PackageType = PackageType.CLAUDE_SKILL,
    ) -> PackageInfo:
        """Helper to create PackageInfo objects for tests.

        For native skill tests, package_type defaults to CLAUDE_SKILL since
        these packages have SKILL.md and should be installed to .github/skills/.
        """
        package = APMPackage(
            name=name,
            version=version,
            package_path=install_path or self.project_root / "package",
            source=source or f"github.com/test/{name}",
            description=description,
            type=pkg_type,
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=commit,
            ref_name="main",
        )
        return PackageInfo(
            package=package,
            install_path=install_path or self.project_root / "package",
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dependency_ref,
            package_type=package_type,
        )

    # ========== Test T6: Direct copy preserves SKILL.md content exactly ==========

    def test_copy_skill_preserves_skill_md_content_exactly(self):
        """Test that direct copy preserves SKILL.md content exactly."""
        # Create a skill package with specific content
        skill_source = self.apm_modules / "owner" / "mcp-builder"
        skill_source.mkdir(parents=True)

        original_content = """---
name: mcp-builder
description: Build MCP servers with best practices
version: 1.0.0
---

# MCP Builder

This skill helps you build **Model Context Protocol** servers.

## Features

- TypeScript support
- Python support
- Automatic validation

## Usage

Use when building MCP servers or tools.
"""
        (skill_source / "SKILL.md").write_text(original_content)

        package_info = self._create_package_info(
            name="mcp-builder", install_path=skill_source, source="owner/mcp-builder"
        )

        # Copy skill to target
        github_path, _ = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None
        target_skill_md = github_path / "SKILL.md"
        assert target_skill_md.exists()

        # Read copied content
        copied_content = target_skill_md.read_text()

        # The content should be preserved exactly (verbatim copy, no mutation)
        assert "# MCP Builder" in copied_content
        assert (
            "This skill helps you build **Model Context Protocol** servers."
            in copied_content
        )
        assert "- TypeScript support" in copied_content
        assert "- Python support" in copied_content
        assert "- Automatic validation" in copied_content
        assert "Use when building MCP servers or tools." in copied_content

    # ========== Test T6: Subdirectories are copied correctly ==========

    def test_copy_skill_copies_scripts_directory(self):
        """Test that scripts/ subdirectory is copied correctly."""
        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)

        (skill_source / "SKILL.md").write_text("---\nname: my-skill\n---\n# My Skill")

        # Create scripts directory with content
        scripts_dir = skill_source / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "validate.sh").write_text("#!/bin/bash\necho 'validating...'")
        (scripts_dir / "build.py").write_text(
            "#!/usr/bin/env python3\nprint('building...')"
        )

        package_info = self._create_package_info(
            name="my-skill", install_path=skill_source
        )

        github_path, _ = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None
        assert (github_path / "scripts").exists()
        assert (github_path / "scripts" / "validate.sh").exists()
        assert (github_path / "scripts" / "build.py").exists()

        # Verify content preserved
        assert (
            "echo 'validating...'"
            in (github_path / "scripts" / "validate.sh").read_text()
        )

    def test_copy_skill_copies_references_directory(self):
        """Test that references/ subdirectory is copied correctly."""
        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)

        (skill_source / "SKILL.md").write_text("---\nname: my-skill\n---\n# My Skill")

        # Create references directory with content
        refs_dir = skill_source / "references"
        refs_dir.mkdir()
        (refs_dir / "api-spec.md").write_text("# API Specification\n\nEndpoints...")
        (refs_dir / "patterns.md").write_text("# Common Patterns\n\n...")

        package_info = self._create_package_info(
            name="my-skill", install_path=skill_source
        )

        github_path, _ = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None
        assert (github_path / "references").exists()
        assert (github_path / "references" / "api-spec.md").exists()
        assert (github_path / "references" / "patterns.md").exists()

    def test_copy_skill_copies_assets_directory(self):
        """Test that assets/ subdirectory is copied correctly."""
        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)

        (skill_source / "SKILL.md").write_text("---\nname: my-skill\n---\n# My Skill")

        # Create assets directory with content
        assets_dir = skill_source / "assets"
        assets_dir.mkdir()
        (assets_dir / "template.json").write_text('{"type": "template"}')
        (assets_dir / "example.yaml").write_text("name: example\nversion: 1.0")

        package_info = self._create_package_info(
            name="my-skill", install_path=skill_source
        )

        github_path, _ = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None
        assert (github_path / "assets").exists()
        assert (github_path / "assets" / "template.json").exists()
        assert (github_path / "assets" / "example.yaml").exists()

    def test_copy_skill_copies_all_subdirectories(self):
        """Test that all skill subdirectories are copied correctly."""
        skill_source = self.apm_modules / "owner" / "complete-skill"
        skill_source.mkdir(parents=True)

        (skill_source / "SKILL.md").write_text(
            "---\nname: complete-skill\n---\n# Complete Skill"
        )

        # Create all standard subdirectories
        (skill_source / "scripts").mkdir()
        (skill_source / "scripts" / "run.sh").write_text("#!/bin/bash")

        (skill_source / "references").mkdir()
        (skill_source / "references" / "guide.md").write_text("# Guide")

        (skill_source / "assets").mkdir()
        (skill_source / "assets" / "config.json").write_text("{}")

        # Also create a custom subdirectory (should be copied too)
        (skill_source / "examples").mkdir()
        (skill_source / "examples" / "basic.md").write_text("# Basic Example")

        package_info = self._create_package_info(
            name="complete-skill", install_path=skill_source
        )

        github_path, _ = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None
        assert (github_path / "SKILL.md").exists()
        assert (github_path / "scripts" / "run.sh").exists()
        assert (github_path / "references" / "guide.md").exists()
        assert (github_path / "assets" / "config.json").exists()
        assert (github_path / "examples" / "basic.md").exists()

    # ========== Test T6: Skill name validation is applied ==========

    def test_copy_skill_validates_skill_name(self):
        """Test that skill name is validated when copying."""
        # Create a skill with a valid name
        skill_source = self.apm_modules / "owner" / "valid-skill-name"
        skill_source.mkdir(parents=True)
        (skill_source / "SKILL.md").write_text(
            "---\nname: valid-skill-name\n---\n# Skill"
        )

        package_info = self._create_package_info(
            name="valid-skill-name", install_path=skill_source
        )

        github_path, _ = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None
        assert github_path.name == "valid-skill-name"

    def test_copy_skill_normalizes_invalid_skill_name(self):
        """Test that invalid skill names are normalized."""
        # Create a skill with an invalid name (uppercase)
        skill_source = self.apm_modules / "owner" / "MyInvalidSkillName"
        skill_source.mkdir(parents=True)
        (skill_source / "SKILL.md").write_text(
            "---\nname: MyInvalidSkillName\n---\n# Skill"
        )

        package_info = self._create_package_info(
            name="MyInvalidSkillName", install_path=skill_source
        )

        github_path, _ = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None
        # Name should be normalized to hyphen-case lowercase
        assert github_path.name == "my-invalid-skill-name"

    # ========== Test T6: Existing skill is updated on reinstall ==========

    def test_copy_skill_updates_existing_skill(self):
        """Test that existing skill is updated on reinstall (overwrite)."""
        # Create target skill directory first
        skill_dir = self.project_root / ".github" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n# OLD CONTENT")
        (skill_dir / "old-file.txt").write_text("This should be removed")

        # Create new source skill
        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)
        (skill_source / "SKILL.md").write_text(
            "---\nname: my-skill\n---\n# NEW CONTENT"
        )
        (skill_source / "new-file.txt").write_text("This is new")

        package_info = self._create_package_info(
            name="my-skill", install_path=skill_source
        )

        github_path, _ = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None
        assert github_path == skill_dir

        # Verify content is updated
        skill_content = (skill_dir / "SKILL.md").read_text()
        assert "# NEW CONTENT" in skill_content
        assert "# OLD CONTENT" not in skill_content

        # Old file should be removed, new file should exist
        assert not (skill_dir / "old-file.txt").exists()
        assert (skill_dir / "new-file.txt").exists()

    # ========== Test T6: Packages without SKILL.md are skipped ==========

    def test_copy_skill_skips_packages_without_skill_md(self):
        """Test that packages without SKILL.md are skipped."""
        # Create a package without SKILL.md (only has instructions)
        pkg_source = self.apm_modules / "owner" / "instructions-only"
        pkg_source.mkdir(parents=True)
        apm_dir = pkg_source / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        (apm_dir / "coding.instructions.md").write_text("# Coding Standards")

        package_info = self._create_package_info(
            name="instructions-only", install_path=pkg_source
        )

        github_path, claude_path = copy_skill_to_target(
            package_info, pkg_source, self.project_root
        )

        # Should return None (skipped) - both paths should be None
        assert github_path is None
        assert claude_path is None

        # No skill directory should be created
        assert not (
            self.project_root / ".github" / "skills" / "instructions-only"
        ).exists()

    # ========== Test T6: Package type routing ==========

    def test_copy_skill_respects_skill_type(self):
        """Test that packages with type='skill' are processed."""
        from apm_cli.models.apm_package import PackageContentType

        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)
        (skill_source / "SKILL.md").write_text("---\nname: my-skill\n---\n# Skill")

        package_info = self._create_package_info(
            name="my-skill",
            install_path=skill_source,
            pkg_type=PackageContentType.SKILL,
        )

        github_path, _ = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None
        assert (github_path / "SKILL.md").exists()

    def test_copy_skill_respects_hybrid_type(self):
        """Test that packages with type='hybrid' are processed."""
        from apm_cli.models.apm_package import PackageContentType

        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)
        (skill_source / "SKILL.md").write_text("---\nname: my-skill\n---\n# Skill")

        package_info = self._create_package_info(
            name="my-skill",
            install_path=skill_source,
            pkg_type=PackageContentType.HYBRID,
        )

        github_path, _ = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None
        assert (github_path / "SKILL.md").exists()

    # ========== Test T6: Creates .github/skills/ if doesn't exist ==========

    def test_copy_skill_creates_github_skills_directory(self):
        """Test that .github/skills/ is created if it doesn't exist."""
        # Start with no .github directory
        assert not (self.project_root / ".github").exists()

        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)
        (skill_source / "SKILL.md").write_text("---\nname: my-skill\n---\n# Skill")

        package_info = self._create_package_info(
            name="my-skill", install_path=skill_source
        )

        github_path, _ = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None
        assert (self.project_root / ".github" / "skills").exists()
        assert (
            self.project_root / ".github" / "skills" / "my-skill" / "SKILL.md"
        ).exists()

    # ========== Test T6: APM metadata is added for orphan detection ==========

    def test_copy_skill_preserves_source_integrity(self):
        """Test that copied SKILL.md is identical to source (no metadata injection)."""
        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)
        original_content = "---\nname: my-skill\ndescription: Test\n---\n# My Skill"
        (skill_source / "SKILL.md").write_text(original_content)

        package_info = self._create_package_info(
            name="my-skill",
            version="2.5.0",
            commit="xyz789",
            install_path=skill_source,
            source="owner/my-skill",
        )

        github_path, _ = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None

        # Copied SKILL.md must be identical to the source
        copied_content = (github_path / "SKILL.md").read_text()
        assert copied_content == original_content


class TestNativeSkillIntegration:
    """Additional tests for native skill integration via SkillIntegrator._integrate_native_skill (T6).

    These tests verify that packages with existing SKILL.md files are correctly
    copied to .github/skills/ and .claude/skills/ directories.
    """

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = SkillIntegrator()

    def teardown_method(self):
        """Clean up after tests."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_package_info(
        self,
        name: str = "test-pkg",
        version: str = "1.0.0",
        commit: str = "abc123",
        install_path: Path = None,
        source: str = None,
        dependency_ref: DependencyReference = None,
        package_type: PackageType = PackageType.CLAUDE_SKILL,
    ) -> PackageInfo:
        """Helper to create PackageInfo objects for tests.

        For native skill tests, package_type defaults to CLAUDE_SKILL since
        these packages have SKILL.md and should be installed to .github/skills/.
        """
        package = APMPackage(
            name=name,
            version=version,
            package_path=install_path or self.project_root / "package",
            source=source or f"github.com/test/{name}",
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=commit,
            ref_name="main",
        )
        return PackageInfo(
            package=package,
            install_path=install_path or self.project_root / "package",
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dependency_ref,
            package_type=package_type,
        )

    def test_native_skill_preserves_complete_structure(self):
        """Test that native skill integration preserves complete directory structure."""
        # Create a complete skill package
        package_dir = self.project_root / "complete-skill"
        package_dir.mkdir()

        # Create SKILL.md
        (package_dir / "SKILL.md").write_text("""---
name: complete-skill
description: A complete skill with all subdirectories
---
# Complete Skill

Use this skill for comprehensive guidance.
""")

        # Create scripts/
        (package_dir / "scripts").mkdir()
        (package_dir / "scripts" / "validate.sh").write_text(
            "#!/bin/bash\necho 'validating'"
        )

        # Create references/
        (package_dir / "references").mkdir()
        (package_dir / "references" / "api.md").write_text("# API Reference")

        # Create assets/
        (package_dir / "assets").mkdir()
        (package_dir / "assets" / "template.json").write_text('{"key": "value"}')

        package_info = self._create_package_info(
            name="complete-skill", install_path=package_dir
        )

        result = self.integrator.integrate_package_skill(
            package_info, self.project_root
        )

        assert result.skill_created is True
        assert result.skill_path is not None

        skill_dir = self.project_root / ".github" / "skills" / "complete-skill"

        # Verify all subdirectories are copied
        assert (skill_dir / "SKILL.md").exists()
        assert (skill_dir / "scripts" / "validate.sh").exists()
        assert (skill_dir / "references" / "api.md").exists()
        assert (skill_dir / "assets" / "template.json").exists()

        # Verify content preserved
        assert "validating" in (skill_dir / "scripts" / "validate.sh").read_text()
        assert "API Reference" in (skill_dir / "references" / "api.md").read_text()

    def test_native_skill_normalizes_uppercase_name(self):
        """Test that native skill with uppercase folder name is normalized."""
        # Create a skill with uppercase folder name
        package_dir = self.project_root / "MyUpperCaseSkill"
        package_dir.mkdir()
        (package_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n# Skill")

        package_info = self._create_package_info(
            name="MyUpperCaseSkill", install_path=package_dir
        )

        result = self.integrator.integrate_package_skill(
            package_info, self.project_root
        )

        assert result.skill_created is True

        # Skill should be installed with normalized name
        normalized_skill_dir = (
            self.project_root / ".github" / "skills" / "my-upper-case-skill"
        )
        assert normalized_skill_dir.exists()
        assert (normalized_skill_dir / "SKILL.md").exists()

    def test_native_skill_files_copied_count(self):
        """Test that references_copied accurately counts all copied files."""
        package_dir = self.project_root / "counting-skill"
        package_dir.mkdir()

        (package_dir / "SKILL.md").write_text("---\nname: counting-skill\n---\n# Skill")

        (package_dir / "scripts").mkdir()
        (package_dir / "scripts" / "a.sh").write_text("a")
        (package_dir / "scripts" / "b.sh").write_text("b")

        (package_dir / "references").mkdir()
        (package_dir / "references" / "c.md").write_text("c")

        # Total files: SKILL.md + a.sh + b.sh + c.md = 4

        package_info = self._create_package_info(
            name="counting-skill", install_path=package_dir
        )

        result = self.integrator.integrate_package_skill(
            package_info, self.project_root
        )

        assert result.skill_created is True
        assert result.references_copied == 4  # All 4 files


# =============================================================================
# T7: Claude Skills Compatibility Copy Tests
# =============================================================================


class TestClaudeSkillsCompatibilityCopy:
    """Test T7: Claude Skills compatibility copy to .claude/skills/.

    When a skill is installed to .github/skills/, it should also be copied
    to .claude/skills/ IF the .claude/ directory already exists.
    This ensures Claude Code users get skills while not polluting projects
    that don't use Claude.
    """

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.apm_modules = self.project_root / "apm_modules"
        self.apm_modules.mkdir(parents=True)
        self.integrator = SkillIntegrator()

    def teardown_method(self):
        """Clean up after tests."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_package_info(
        self,
        name: str = "test-pkg",
        version: str = "1.0.0",
        commit: str = "abc123",
        install_path: Path = None,
        source: str = None,
        dependency_ref: DependencyReference = None,
        package_type: PackageType = PackageType.CLAUDE_SKILL,
    ) -> PackageInfo:
        """Helper to create PackageInfo objects for tests.

        For skill compatibility tests, package_type defaults to CLAUDE_SKILL since
        these packages have SKILL.md and should be installed to .github/skills/.
        """
        package = APMPackage(
            name=name,
            version=version,
            package_path=install_path or self.project_root / "package",
            source=source or f"github.com/test/{name}",
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=commit,
            ref_name="main",
        )
        return PackageInfo(
            package=package,
            install_path=install_path or self.project_root / "package",
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dependency_ref,
            package_type=package_type,
        )

    # ========== Test: Skill copies to .github/skills/ only when .claude/ doesn't exist ==========

    def test_skill_copies_to_github_only_when_no_claude_dir(self):
        """Test skill copies to .github/skills/ when .claude/ doesn't exist."""
        # Ensure .claude/ does NOT exist
        assert not (self.project_root / ".claude").exists()

        # Create a native skill package
        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)
        (skill_source / "SKILL.md").write_text("---\nname: my-skill\n---\n# My Skill")

        package_info = self._create_package_info(
            name="my-skill", install_path=skill_source
        )

        result = self.integrator.integrate_package_skill(
            package_info, self.project_root
        )

        # Should create in .github/skills/
        assert result.skill_created is True
        github_skill = (
            self.project_root / ".github" / "skills" / "my-skill" / "SKILL.md"
        )
        assert github_skill.exists()

        # Should NOT create .claude/ folder
        assert not (self.project_root / ".claude").exists()

        # Should NOT create .claude/skills/
        assert not (self.project_root / ".claude" / "skills").exists()

    # ========== Test: Skill copies to BOTH when .claude/ exists ==========

    def test_skill_copies_to_both_when_claude_exists(self):
        """Test skill copies to BOTH .github/skills/ and .claude/skills/ when .claude/ exists."""
        # Create .claude/ directory (simulating a Claude Code project)
        (self.project_root / ".claude").mkdir()

        # Create a native skill package
        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)
        (skill_source / "SKILL.md").write_text(
            "---\nname: my-skill\n---\n# My Skill Content"
        )
        (skill_source / "references").mkdir()
        (skill_source / "references" / "guide.md").write_text("# Guide")

        package_info = self._create_package_info(
            name="my-skill", install_path=skill_source
        )

        result = self.integrator.integrate_package_skill(
            package_info, self.project_root
        )

        # Should create in .github/skills/
        assert result.skill_created is True
        github_skill_dir = self.project_root / ".github" / "skills" / "my-skill"
        assert github_skill_dir.exists()
        assert (github_skill_dir / "SKILL.md").exists()
        assert (github_skill_dir / "references" / "guide.md").exists()

        # Should ALSO create in .claude/skills/
        claude_skill_dir = self.project_root / ".claude" / "skills" / "my-skill"
        assert claude_skill_dir.exists()
        assert (claude_skill_dir / "SKILL.md").exists()
        assert (claude_skill_dir / "references" / "guide.md").exists()

    # ========== Test: Copies are identical ==========

    def test_copies_are_identical(self):
        """Test that .github/skills/ and .claude/skills/ copies are identical."""
        # Create .claude/ directory
        (self.project_root / ".claude").mkdir()

        # Create a native skill package with multiple files
        skill_source = self.apm_modules / "owner" / "complete-skill"
        skill_source.mkdir(parents=True)

        skill_content = """---
name: complete-skill
description: A complete skill
---

# Complete Skill

Detailed instructions here.
"""
        (skill_source / "SKILL.md").write_text(skill_content)

        (skill_source / "scripts").mkdir()
        (skill_source / "scripts" / "run.sh").write_text("#!/bin/bash\necho 'running'")

        (skill_source / "references").mkdir()
        (skill_source / "references" / "api.md").write_text("# API\n\nEndpoints...")

        (skill_source / "assets").mkdir()
        (skill_source / "assets" / "config.json").write_text('{"key": "value"}')

        package_info = self._create_package_info(
            name="complete-skill", install_path=skill_source
        )

        self.integrator.integrate_package_skill(package_info, self.project_root)

        github_skill_dir = self.project_root / ".github" / "skills" / "complete-skill"
        claude_skill_dir = self.project_root / ".claude" / "skills" / "complete-skill"

        # Compare all files
        github_files = set(
            f.relative_to(github_skill_dir)
            for f in github_skill_dir.rglob("*")
            if f.is_file()
        )
        claude_files = set(
            f.relative_to(claude_skill_dir)
            for f in claude_skill_dir.rglob("*")
            if f.is_file()
        )

        assert github_files == claude_files, "File structure should be identical"

        # Compare content of each file (except SKILL.md which may have slightly different timestamps)
        for rel_path in github_files:
            if rel_path.name != "SKILL.md":
                github_content = (github_skill_dir / rel_path).read_text()
                claude_content = (claude_skill_dir / rel_path).read_text()
                assert github_content == claude_content, (
                    f"Content of {rel_path} should be identical"
                )

        # SKILL.md should have same body content
        github_skill_body = (github_skill_dir / "SKILL.md").read_text()
        claude_skill_body = (claude_skill_dir / "SKILL.md").read_text()
        assert "# Complete Skill" in github_skill_body
        assert "# Complete Skill" in claude_skill_body
        assert "Detailed instructions here." in github_skill_body
        assert "Detailed instructions here." in claude_skill_body

    # ========== Test: Updates affect both locations ==========

    def test_updates_affect_both_locations(self):
        """Test that skill updates affect both .github/skills/ and .claude/skills/."""
        # Create .claude/ directory
        (self.project_root / ".claude").mkdir()

        # Create initial skill
        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)
        (skill_source / "SKILL.md").write_text("---\nname: my-skill\n---\n# Version 1")

        package_info_v1 = self._create_package_info(
            name="my-skill", version="1.0.0", commit="abc123", install_path=skill_source
        )

        # First install
        result1 = self.integrator.integrate_package_skill(
            package_info_v1, self.project_root
        )
        assert result1.skill_created is True

        # Verify both locations have v1 content
        github_content_v1 = (
            self.project_root / ".github" / "skills" / "my-skill" / "SKILL.md"
        ).read_text()
        claude_content_v1 = (
            self.project_root / ".claude" / "skills" / "my-skill" / "SKILL.md"
        ).read_text()
        assert "# Version 1" in github_content_v1
        assert "# Version 1" in claude_content_v1

        # Update skill source
        (skill_source / "SKILL.md").write_text("---\nname: my-skill\n---\n# Version 2")

        package_info_v2 = self._create_package_info(
            name="my-skill",
            version="2.0.0",  # New version triggers update
            commit="def456",
            install_path=skill_source,
        )

        # Second install (update)
        result2 = self.integrator.integrate_package_skill(
            package_info_v2, self.project_root
        )
        assert result2.skill_updated is True

        # Verify both locations have v2 content
        github_content_v2 = (
            self.project_root / ".github" / "skills" / "my-skill" / "SKILL.md"
        ).read_text()
        claude_content_v2 = (
            self.project_root / ".claude" / "skills" / "my-skill" / "SKILL.md"
        ).read_text()
        assert "# Version 2" in github_content_v2
        assert "# Version 2" in claude_content_v2

    # ========== Test: .claude/ not created if doesn't exist ==========

    def test_claude_dir_not_created_if_not_exists(self):
        """Test that .claude/ directory is NOT created if it doesn't exist."""
        # Ensure .claude/ does NOT exist
        assert not (self.project_root / ".claude").exists()

        # Create and install multiple skills
        for i in range(3):
            skill_source = self.apm_modules / "owner" / f"skill-{i}"
            skill_source.mkdir(parents=True)
            (skill_source / "SKILL.md").write_text(
                f"---\nname: skill-{i}\n---\n# Skill {i}"
            )

            package_info = self._create_package_info(
                name=f"skill-{i}", install_path=skill_source
            )

            self.integrator.integrate_package_skill(package_info, self.project_root)

        # .github/skills/ should have all skills
        github_skills = self.project_root / ".github" / "skills"
        assert github_skills.exists()
        assert (github_skills / "skill-0").exists()
        assert (github_skills / "skill-1").exists()
        assert (github_skills / "skill-2").exists()

        # .claude/ should NOT exist (we never created it)
        assert not (self.project_root / ".claude").exists()


class TestOpenCodeSkillsCompatibilityCopy:
    """Test OpenCode compatibility copy to .opencode/skills/."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.apm_modules = self.project_root / "apm_modules"
        self.apm_modules.mkdir(parents=True)
        self.integrator = SkillIntegrator()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_package_info(
        self,
        name: str = "test-pkg",
        version: str = "1.0.0",
        commit: str = "abc123",
        install_path: Path = None,
        source: str = None,
        dependency_ref: DependencyReference = None,
        package_type: PackageType = PackageType.CLAUDE_SKILL,
    ) -> PackageInfo:
        package = APMPackage(
            name=name,
            version=version,
            package_path=install_path or self.project_root / "package",
            source=source or f"github.com/test/{name}",
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=commit,
            ref_name="main",
        )
        return PackageInfo(
            package=package,
            install_path=install_path or self.project_root / "package",
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dependency_ref,
            package_type=package_type,
        )

    def test_skill_copies_to_github_only_when_no_opencode_dir(self):
        """Test skill copies to .github/skills/ when .opencode/ doesn't exist."""
        assert not (self.project_root / ".opencode").exists()

        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)
        (skill_source / "SKILL.md").write_text("---\nname: my-skill\n---\n# My Skill")

        package_info = self._create_package_info(
            name="my-skill",
            install_path=skill_source,
        )

        result = self.integrator.integrate_package_skill(
            package_info, self.project_root
        )

        assert result.skill_created is True
        github_skill = (
            self.project_root / ".github" / "skills" / "my-skill" / "SKILL.md"
        )
        assert github_skill.exists()
        assert not (self.project_root / ".opencode").exists()

    def test_skill_copies_to_both_when_opencode_exists(self):
        """Test skill copies to BOTH .github/skills/ and .opencode/skills/."""
        (self.project_root / ".opencode").mkdir()

        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)
        (skill_source / "SKILL.md").write_text(
            "---\nname: my-skill\n---\n# My Skill Content"
        )
        (skill_source / "references").mkdir()
        (skill_source / "references" / "guide.md").write_text("# Guide")

        package_info = self._create_package_info(
            name="my-skill",
            install_path=skill_source,
        )

        result = self.integrator.integrate_package_skill(
            package_info, self.project_root
        )

        assert result.skill_created is True
        github_skill_dir = self.project_root / ".github" / "skills" / "my-skill"
        opencode_skill_dir = self.project_root / ".opencode" / "skills" / "my-skill"
        assert github_skill_dir.exists()
        assert (github_skill_dir / "references" / "guide.md").exists()
        assert opencode_skill_dir.exists()
        assert (opencode_skill_dir / "references" / "guide.md").exists()

    # ========== Test: copy_skill_to_target returns both paths ==========

    def test_copy_skill_to_target_returns_both_paths_when_claude_exists(self):
        """Test that copy_skill_to_target returns both paths when .claude/ exists."""
        # Create .claude/ directory
        (self.project_root / ".claude").mkdir()

        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)
        (skill_source / "SKILL.md").write_text("---\nname: my-skill\n---\n# Skill")

        package_info = self._create_package_info(
            name="my-skill", install_path=skill_source
        )

        github_path, claude_path = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None
        assert claude_path is not None
        assert github_path == self.project_root / ".github" / "skills" / "my-skill"
        assert claude_path == self.project_root / ".claude" / "skills" / "my-skill"

    def test_copy_skill_to_target_returns_none_claude_when_no_claude_dir(self):
        """Test that copy_skill_to_target returns None for claude_path when .claude/ doesn't exist."""
        # Ensure .claude/ does NOT exist
        assert not (self.project_root / ".claude").exists()

        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)
        (skill_source / "SKILL.md").write_text("---\nname: my-skill\n---\n# Skill")

        package_info = self._create_package_info(
            name="my-skill", install_path=skill_source
        )

        github_path, claude_path = copy_skill_to_target(
            package_info, skill_source, self.project_root
        )

        assert github_path is not None
        assert claude_path is None

    # ========== Test: sync_integration cleans both locations ==========

    def test_sync_removes_orphans_from_both_locations(self):
        """Test that sync_integration removes orphaned skills from both locations."""
        # Create skill directories in both locations (no metadata needed)
        github_skill = self.project_root / ".github" / "skills" / "orphan-skill"
        github_skill.mkdir(parents=True)
        (github_skill / "SKILL.md").write_text("# Orphan Skill\n")

        claude_skill = self.project_root / ".claude" / "skills" / "orphan-skill"
        claude_skill.mkdir(parents=True)
        (claude_skill / "SKILL.md").write_text("# Orphan Skill\n")

        # Mock apm_package with no dependencies (orphan)
        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = []

        result = self.integrator.sync_integration(apm_package, self.project_root)

        # Both orphans should be removed
        assert result["files_removed"] == 2
        assert not github_skill.exists()
        assert not claude_skill.exists()

    def test_sync_keeps_installed_skills_in_both_locations(self):
        """Test that sync_integration keeps installed skills in both locations."""
        # Create skill directories in both locations (no metadata needed)
        skill_name = "installed-skill"

        github_skill = self.project_root / ".github" / "skills" / skill_name
        github_skill.mkdir(parents=True)
        (github_skill / "SKILL.md").write_text("# Installed Skill\n")

        claude_skill = self.project_root / ".claude" / "skills" / skill_name
        claude_skill.mkdir(parents=True)
        (claude_skill / "SKILL.md").write_text("# Installed Skill\n")

        # Mock apm_package with this dependency installed
        # "owner/installed-skill" → skill dir name "installed-skill"
        dep_ref = DependencyReference.parse("owner/installed-skill")
        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = [dep_ref]

        result = self.integrator.sync_integration(apm_package, self.project_root)

        # Nothing should be removed
        assert result["files_removed"] == 0
        assert github_skill.exists()
        assert claude_skill.exists()

    # ========== Test: Only .claude/skills/ cleaned when .claude/ exists ==========

    def test_sync_only_cleans_claude_skills_when_claude_exists(self):
        """Test that sync only cleans .claude/skills/ when .claude/ directory exists."""
        # Only .github/ exists, not .claude/
        github_skill = self.project_root / ".github" / "skills" / "orphan-skill"
        github_skill.mkdir(parents=True)
        (github_skill / "SKILL.md").write_text("# Orphan Skill\n")

        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = []

        result = self.integrator.sync_integration(apm_package, self.project_root)

        # Only the github orphan should be removed (claude doesn't exist)
        assert result["files_removed"] == 1
        assert not github_skill.exists()
        assert not (self.project_root / ".claude").exists()

    # ========== Test: APM metadata added to both copies ==========

    def test_native_skill_copied_verbatim_to_both_locations(self):
        """Test that native SKILL.md is copied verbatim (no metadata injection) to both locations."""
        # Create .claude/ directory
        (self.project_root / ".claude").mkdir()

        skill_source = self.apm_modules / "owner" / "my-skill"
        skill_source.mkdir(parents=True)
        original_content = "---\nname: my-skill\ndescription: Test\n---\n# My Skill"
        (skill_source / "SKILL.md").write_text(original_content)

        package_info = self._create_package_info(
            name="my-skill",
            version="2.0.0",
            commit="xyz789",
            install_path=skill_source,
            source="owner/my-skill",
        )

        self.integrator.integrate_package_skill(package_info, self.project_root)

        # Both copies must be identical to the source
        github_content = (
            self.project_root / ".github" / "skills" / "my-skill" / "SKILL.md"
        ).read_text()
        assert github_content == original_content

        claude_content = (
            self.project_root / ".claude" / "skills" / "my-skill" / "SKILL.md"
        ).read_text()
        assert claude_content == original_content

    # ========== T12: Additional orphan cleanup tests ==========

    def test_sync_removes_all_unknown_skill_dirs(self):
        """Test that sync removes ALL skill directories not matching installed packages.

        Uses npm-style approach: .github/skills/ is fully APM-managed.
        Any directory not matching an installed package name is removed.
        """
        # Create a skill dir not matching any installed package
        unknown_skill = self.project_root / ".github" / "skills" / "unknown-skill"
        unknown_skill.mkdir(parents=True)
        (unknown_skill / "SKILL.md").write_text(
            "---\nname: unknown\n---\n# Custom Skill\n"
        )

        # Create another with no SKILL.md
        (self.project_root / ".claude").mkdir()
        claude_unknown = self.project_root / ".claude" / "skills" / "my-workflow"
        claude_unknown.mkdir(parents=True)
        (claude_unknown / "SKILL.md").write_text(
            "---\nname: my-workflow\n---\n# Workflow\n"
        )

        # Run sync with no dependencies
        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = []

        result = self.integrator.sync_integration(apm_package, self.project_root)

        # All unknown dirs should be removed (npm-style)
        assert result["files_removed"] == 2
        assert not unknown_skill.exists()
        assert not claude_unknown.exists()

    def test_sync_removes_skill_dirs_without_skill_md(self):
        """Test that sync removes orphaned skill directories even without SKILL.md.

        Uses npm-style approach: any directory not matching an installed package
        name is removed, regardless of its contents.
        """
        # Create a skill directory without SKILL.md
        empty_skill = self.project_root / ".github" / "skills" / "empty-skill"
        empty_skill.mkdir(parents=True)
        (empty_skill / "README.md").write_text("# Some file")

        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = []

        result = self.integrator.sync_integration(apm_package, self.project_root)

        # Should be removed (not in installed set)
        assert result["files_removed"] == 1
        assert not empty_skill.exists()

    def test_sync_removes_malformed_skill_dirs(self):
        """Test that sync removes orphaned skill directories with malformed SKILL.md.

        Uses npm-style approach: directory name matching, not SKILL.md content.
        Malformed SKILL.md has no effect on orphan detection.
        """
        # Create a skill with malformed frontmatter
        malformed_skill = self.project_root / ".github" / "skills" / "malformed"
        malformed_skill.mkdir(parents=True)
        (malformed_skill / "SKILL.md").write_text("""---
invalid yaml: [this is broken
  no closing bracket
---
# Content
""")

        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = []

        result = self.integrator.sync_integration(apm_package, self.project_root)

        # Should be removed (not in installed set)
        assert result["files_removed"] == 1
        assert not malformed_skill.exists()

    def test_sync_removes_orphans_only_from_github_when_no_claude(self):
        """Test cleanup works correctly when .claude/ directory doesn't exist."""
        # Ensure .claude/ does NOT exist
        assert not (self.project_root / ".claude").exists()

        # Create an orphan skill in .github/skills/
        orphan_skill = self.project_root / ".github" / "skills" / "orphan"
        orphan_skill.mkdir(parents=True)
        (orphan_skill / "SKILL.md").write_text("# Orphan Skill\n")

        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = []

        result = self.integrator.sync_integration(apm_package, self.project_root)

        # Only github orphan should be removed
        assert result["files_removed"] == 1
        assert not orphan_skill.exists()

    def test_sync_aggregates_stats_from_both_locations(self):
        """Test that sync correctly aggregates removal stats from both locations."""
        # Create .claude/ directory
        (self.project_root / ".claude").mkdir()

        # Create orphan in .github/skills/
        github_orphan = self.project_root / ".github" / "skills" / "orphan-a"
        github_orphan.mkdir(parents=True)
        (github_orphan / "SKILL.md").write_text("# Orphan A\n")

        # Create different orphan in .claude/skills/
        claude_orphan = self.project_root / ".claude" / "skills" / "orphan-b"
        claude_orphan.mkdir(parents=True)
        (claude_orphan / "SKILL.md").write_text("# Orphan B\n")

        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = []

        result = self.integrator.sync_integration(apm_package, self.project_root)

        # Both orphans should be removed (1 from each location)
        assert result["files_removed"] == 2
        assert not github_orphan.exists()
        assert not claude_orphan.exists()


class TestSubSkillPromotion:
    """Test that sub-skills inside packages are promoted to top-level entries.

    When a package contains .apm/skills/<sub-skill>/SKILL.md, each sub-skill
    should be copied to .github/skills/<sub-skill>/ as an independent
    top-level entry so Copilot can discover it.
    """

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = SkillIntegrator()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_package_info(
        self,
        name: str = "test-pkg",
        install_path: Path = None,
        package_type: PackageType = PackageType.CLAUDE_SKILL,
    ) -> PackageInfo:
        package = APMPackage(
            name=name,
            version="1.0.0",
            package_path=install_path or self.project_root / "package",
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
            install_path=install_path or self.project_root / "package",
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            package_type=package_type,
        )

    def _create_package_with_sub_skills(self, name="parent-skill", sub_skills=None):
        """Create a package directory with a SKILL.md and sub-skills under .apm/skills/."""
        package_dir = self.project_root / name
        package_dir.mkdir()
        (package_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Parent skill\n---\n# {name}\n"
        )
        if sub_skills:
            skills_dir = package_dir / ".apm" / "skills"
            skills_dir.mkdir(parents=True)
            for sub_name in sub_skills:
                sub_dir = skills_dir / sub_name
                sub_dir.mkdir()
                (sub_dir / "SKILL.md").write_text(
                    f"---\nname: {sub_name}\ndescription: Sub-skill {sub_name}\n---\n# {sub_name}\n"
                )
        return package_dir

    def test_sub_skill_promoted_to_top_level(self):
        """Sub-skills under .apm/skills/ should be copied to .github/skills/ as top-level entries."""
        package_dir = self._create_package_with_sub_skills(
            "modernisation", sub_skills=["azure-naming"]
        )
        pkg_info = self._create_package_info(
            name="modernisation", install_path=package_dir
        )

        self.integrator.integrate_package_skill(pkg_info, self.project_root)

        # Parent skill exists
        assert (
            self.project_root / ".github" / "skills" / "modernisation" / "SKILL.md"
        ).exists()
        # .apm/ excluded from parent copy to avoid redundant storage
        assert not (
            self.project_root / ".github" / "skills" / "modernisation" / ".apm"
        ).exists()
        # Sub-skill promoted to top level
        assert (
            self.project_root / ".github" / "skills" / "azure-naming" / "SKILL.md"
        ).exists()
        content = (
            self.project_root / ".github" / "skills" / "azure-naming" / "SKILL.md"
        ).read_text()
        assert "azure-naming" in content

    def test_multiple_sub_skills_promoted(self):
        """All sub-skills in the package should be promoted."""
        package_dir = self._create_package_with_sub_skills(
            "my-package", sub_skills=["skill-a", "skill-b", "skill-c"]
        )
        pkg_info = self._create_package_info(
            name="my-package", install_path=package_dir
        )

        self.integrator.integrate_package_skill(pkg_info, self.project_root)

        for sub in ["skill-a", "skill-b", "skill-c"]:
            assert (
                self.project_root / ".github" / "skills" / sub / "SKILL.md"
            ).exists()

    def test_sub_skill_without_skill_md_not_promoted(self):
        """Directories under .apm/skills/ without SKILL.md should be ignored."""
        package_dir = self._create_package_with_sub_skills(
            "pkg", sub_skills=["valid-sub"]
        )
        # Add a directory without SKILL.md
        (package_dir / ".apm" / "skills" / "no-skill-md").mkdir()
        (package_dir / ".apm" / "skills" / "no-skill-md" / "README.md").write_text(
            "# Not a skill"
        )

        pkg_info = self._create_package_info(name="pkg", install_path=package_dir)
        self.integrator.integrate_package_skill(pkg_info, self.project_root)

        assert (
            self.project_root / ".github" / "skills" / "valid-sub" / "SKILL.md"
        ).exists()
        assert not (self.project_root / ".github" / "skills" / "no-skill-md").exists()

    def test_sub_skill_name_collision_overwrites_with_warning(self):
        """If a promoted sub-skill name clashes with an existing skill, it overwrites and warns."""
        # Pre-existing skill at top level
        existing = self.project_root / ".github" / "skills" / "azure-naming"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("# Old content")

        package_dir = self._create_package_with_sub_skills(
            "modernisation", sub_skills=["azure-naming"]
        )
        pkg_info = self._create_package_info(
            name="modernisation", install_path=package_dir
        )

        with patch("apm_cli.cli._rich_warning") as mock_warning:
            self.integrator.integrate_package_skill(pkg_info, self.project_root)

        # Warning should have been emitted about the collision
        mock_warning.assert_called_once()
        assert "azure-naming" in mock_warning.call_args[0][0]
        assert "modernisation" in mock_warning.call_args[0][0]

        # Should be overwritten with sub-skill content
        content = (
            self.project_root / ".github" / "skills" / "azure-naming" / "SKILL.md"
        ).read_text()
        assert "Sub-skill azure-naming" in content
        assert "Old content" not in content

    def test_sub_skill_promoted_to_claude_skills(self):
        """Sub-skills should also be promoted under .claude/skills/ when .claude/ exists."""
        (self.project_root / ".claude").mkdir()
        package_dir = self._create_package_with_sub_skills(
            "modernisation", sub_skills=["azure-naming"]
        )
        pkg_info = self._create_package_info(
            name="modernisation", install_path=package_dir
        )

        self.integrator.integrate_package_skill(pkg_info, self.project_root)

        assert (
            self.project_root / ".github" / "skills" / "azure-naming" / "SKILL.md"
        ).exists()
        assert (
            self.project_root / ".claude" / "skills" / "azure-naming" / "SKILL.md"
        ).exists()

    def test_sub_skill_name_normalization(self):
        """Sub-skills with invalid names should be normalized before promotion."""
        package_dir = self.project_root / "my-package"
        package_dir.mkdir()
        (package_dir / "SKILL.md").write_text("---\nname: my-package\n---\n# Parent")
        skills_dir = package_dir / ".apm" / "skills"
        skills_dir.mkdir(parents=True)
        # Create sub-skill with invalid name (uppercase + underscores)
        bad_name_dir = skills_dir / "My_Azure_Skill"
        bad_name_dir.mkdir()
        (bad_name_dir / "SKILL.md").write_text(
            "---\nname: My_Azure_Skill\n---\n# Bad name"
        )

        pkg_info = self._create_package_info(
            name="my-package", install_path=package_dir
        )
        self.integrator.integrate_package_skill(pkg_info, self.project_root)

        # Should be normalized to lowercase-hyphenated
        assert not (
            self.project_root / ".github" / "skills" / "My_Azure_Skill"
        ).exists()
        assert (
            self.project_root / ".github" / "skills" / "my-azure-skill" / "SKILL.md"
        ).exists()

    def test_package_without_sub_skills_unchanged(self):
        """Packages without .apm/skills/ subdirectory should work as before."""
        package_dir = self.project_root / "simple-skill"
        package_dir.mkdir()
        (package_dir / "SKILL.md").write_text("---\nname: simple-skill\n---\n# Simple")

        pkg_info = self._create_package_info(
            name="simple-skill", install_path=package_dir
        )
        result = self.integrator.integrate_package_skill(pkg_info, self.project_root)

        assert result.skill_created is True
        assert (
            self.project_root / ".github" / "skills" / "simple-skill" / "SKILL.md"
        ).exists()
        skills = list((self.project_root / ".github" / "skills").iterdir())
        assert len(skills) == 1

    def test_sync_integration_preserves_promoted_sub_skills(self):
        """sync_integration should not orphan promoted sub-skills."""
        # Set up installed package structure in apm_modules
        apm_modules = self.project_root / "apm_modules"
        owner_dir = apm_modules / "testorg" / "agent-library" / "modernisation"
        owner_dir.mkdir(parents=True)
        (owner_dir / "apm.yml").write_text("name: modernisation\nversion: 1.0.0\n")
        (owner_dir / "SKILL.md").write_text("---\nname: modernisation\n---\n# Parent")
        sub_dir = owner_dir / ".apm" / "skills" / "azure-naming"
        sub_dir.mkdir(parents=True)
        (sub_dir / "SKILL.md").write_text("---\nname: azure-naming\n---\n# Sub")

        # Create the promoted skills in .github/skills/
        for name in ["modernisation", "azure-naming"]:
            d = self.project_root / ".github" / "skills" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"# {name}")

        # Mock the dependency
        dep = DependencyReference.parse("testorg/agent-library/modernisation")
        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = [dep]

        result = self.integrator.sync_integration(apm_package, self.project_root)

        # Neither should be removed
        assert result["files_removed"] == 0
        assert (self.project_root / ".github" / "skills" / "modernisation").exists()
        assert (self.project_root / ".github" / "skills" / "azure-naming").exists()


class TestSubSkillPromotionForNonSkillPackages:
    """Test that sub-skills under .apm/skills/ are promoted even when the
    parent package is type INSTRUCTIONS (no top-level SKILL.md)."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = SkillIntegrator()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_instructions_package(self, name="sample-package", sub_skills=None):
        """Create a package WITHOUT SKILL.md (INSTRUCTIONS type) that ships sub-skills."""
        package_dir = self.project_root / name
        package_dir.mkdir()
        (package_dir / "apm.yml").write_text(
            f"name: {name}\nversion: 1.0.0\ndescription: test\n"
        )
        # Add .apm/instructions/ so it's a valid package
        instr_dir = package_dir / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "design-standards.instructions.md").write_text("# Standards\n")
        if sub_skills:
            skills_dir = package_dir / ".apm" / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            for sub_name in sub_skills:
                sub_dir = skills_dir / sub_name
                sub_dir.mkdir()
                (sub_dir / "SKILL.md").write_text(
                    f"---\nname: {sub_name}\ndescription: Sub-skill {sub_name}\n---\n# {sub_name}\n"
                )
        return package_dir

    def _create_package_info(self, name, install_path):
        package = APMPackage(
            name=name,
            version="1.0.0",
            package_path=install_path,
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
            install_path=install_path,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            package_type=PackageType.APM_PACKAGE,
        )

    def test_sub_skills_promoted_from_instructions_package(self):
        """Sub-skills should be promoted even from INSTRUCTIONS-type packages."""
        package_dir = self._create_instructions_package(
            "sample-package", sub_skills=["style-checker"]
        )
        pkg_info = self._create_package_info("sample-package", package_dir)

        result = self.integrator.integrate_package_skill(pkg_info, self.project_root)

        # Package itself should NOT become a skill (INSTRUCTIONS type)
        assert result.skill_created is False
        assert result.skill_skipped is True
        # But sub-skills should be promoted
        assert result.sub_skills_promoted == 1
        assert (
            self.project_root / ".github" / "skills" / "style-checker" / "SKILL.md"
        ).exists()

    def test_multiple_sub_skills_promoted_from_instructions_package(self):
        """All sub-skills should be promoted from INSTRUCTIONS-type packages."""
        package_dir = self._create_instructions_package(
            "sample-package", sub_skills=["skill-a", "skill-b"]
        )
        pkg_info = self._create_package_info("sample-package", package_dir)

        result = self.integrator.integrate_package_skill(pkg_info, self.project_root)

        assert result.sub_skills_promoted == 2
        assert (
            self.project_root / ".github" / "skills" / "skill-a" / "SKILL.md"
        ).exists()
        assert (
            self.project_root / ".github" / "skills" / "skill-b" / "SKILL.md"
        ).exists()

    def test_no_sub_skills_returns_zero(self):
        """Packages without .apm/skills/ should return sub_skills_promoted=0."""
        package_dir = self._create_instructions_package(
            "sample-package", sub_skills=None
        )
        pkg_info = self._create_package_info("sample-package", package_dir)

        result = self.integrator.integrate_package_skill(pkg_info, self.project_root)

        assert result.sub_skills_promoted == 0
        assert not (self.project_root / ".github" / "skills").exists()

    def test_sub_skills_promoted_to_claude_when_claude_exists(self):
        """Sub-skills from INSTRUCTIONS packages should also go to .claude/skills/ if .claude/ exists."""
        (self.project_root / ".claude").mkdir()
        package_dir = self._create_instructions_package(
            "sample-package", sub_skills=["style-checker"]
        )
        pkg_info = self._create_package_info("sample-package", package_dir)

        result = self.integrator.integrate_package_skill(pkg_info, self.project_root)

        assert result.sub_skills_promoted == 1
        assert (
            self.project_root / ".github" / "skills" / "style-checker" / "SKILL.md"
        ).exists()
        assert (
            self.project_root / ".claude" / "skills" / "style-checker" / "SKILL.md"
        ).exists()

    def test_sync_removes_orphaned_promoted_sub_skills(self):
        """When a package is uninstalled, its promoted sub-skills should be cleaned up."""
        # Create the promoted sub-skill as if it had been installed
        style_checker = self.project_root / ".github" / "skills" / "style-checker"
        style_checker.mkdir(parents=True)
        (style_checker / "SKILL.md").write_text("# style-checker")

        # Simulate an empty apm.yml (package was uninstalled)
        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = []

        result = self.integrator.sync_integration(apm_package, self.project_root)

        assert result["files_removed"] == 1
        assert not style_checker.exists()

    def test_sync_preserves_promoted_sub_skills_when_package_installed(self):
        """When a package is still installed, its promoted sub-skills should be preserved."""
        # Create apm_modules with the package and its sub-skills
        apm_modules = self.project_root / "apm_modules"
        owner_dir = apm_modules / "microsoft" / "apm-sample-package"
        owner_dir.mkdir(parents=True)
        (owner_dir / "apm.yml").write_text("name: apm-sample-package\nversion: 1.0.0\n")
        sub_dir = owner_dir / ".apm" / "skills" / "style-checker"
        sub_dir.mkdir(parents=True)
        (sub_dir / "SKILL.md").write_text("# style-checker")

        # Create the promoted sub-skill in .github/skills/
        style_checker = self.project_root / ".github" / "skills" / "style-checker"
        style_checker.mkdir(parents=True)
        (style_checker / "SKILL.md").write_text("# style-checker")

        dep = DependencyReference.parse("microsoft/apm-sample-package")
        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = [dep]

        result = self.integrator.sync_integration(apm_package, self.project_root)

        assert result["files_removed"] == 0
        assert style_checker.exists()
