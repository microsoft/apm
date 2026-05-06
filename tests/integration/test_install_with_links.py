"""Integration tests for link resolution during installation."""

from datetime import datetime
from textwrap import dedent

import pytest

from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.models.apm_package import APMPackage, GitReferenceType, PackageInfo, ResolvedReference


@pytest.fixture
def mock_package_with_contexts(tmp_path):
    """Create a mock package with context files and prompts that link to them."""
    # Create package structure
    package_dir = tmp_path / "apm_modules" / "company" / "standards"
    package_dir.mkdir(parents=True, exist_ok=True)

    # Create apm.yml
    apm_yml = package_dir / "apm.yml"
    apm_yml.write_text(
        dedent("""
        name: standards
        version: 1.0.0
        description: Company standards package
    """),
        encoding="utf-8",
    )

    # Create context file
    context_dir = package_dir / ".apm" / "context"
    context_dir.mkdir(parents=True, exist_ok=True)

    api_context = context_dir / "api.context.md"
    api_context.write_text(
        dedent("""
        # API Standards
        
        Our company API standards...
    """),
        encoding="utf-8",
    )

    # Create prompt that links to context
    prompts_dir = package_dir / ".apm" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    backend_prompt = prompts_dir / "backend-review.prompt.md"
    backend_prompt.write_text(
        dedent("""
        ---
        description: Review backend code
        ---
        
        # Backend Code Review
        
        Follow our [API standards](../context/api.context.md) when reviewing.
    """),
        encoding="utf-8",
    )

    # Create agent that links to context
    agents_dir = package_dir / ".apm" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    backend_agent = agents_dir / "backend-expert.agent.md"
    backend_agent.write_text(
        dedent("""
        ---
        description: Backend expert
        ---
        
        # Backend Expert
        
        I follow [API standards](../context/api.context.md) strictly.
    """),
        encoding="utf-8",
    )

    return package_dir


@pytest.fixture
def package_info(mock_package_with_contexts, tmp_path):
    """Create PackageInfo for the mock package."""
    package = APMPackage(
        name="standards",
        version="1.0.0",
        package_path=mock_package_with_contexts,
        source="company/standards",
    )

    resolved_ref = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )

    return PackageInfo(
        package=package,
        install_path=mock_package_with_contexts,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
    )


class TestInstallPromptLinkResolution:
    """Tests for link resolution when installing prompts."""

    def test_install_resolves_prompt_links(self, package_info, tmp_path):
        """Installing a package resolves links in copied prompt files."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        # Integrate prompts
        integrator = PromptIntegrator()
        result = integrator.integrate_package_prompts(package_info, project_root)

        # Check that prompt was integrated
        assert result.files_integrated == 1

        # Check that the integrated file exists
        integrated_prompt = project_root / ".github" / "prompts" / "backend-review.prompt.md"
        assert integrated_prompt.exists()

        # Read content and verify links are resolved
        content = integrated_prompt.read_text(encoding="utf-8")

        # Link should be resolved to point directly to apm_modules
        # From .github/prompts/ to apm_modules/company/standards/.apm/context/
        assert "apm_modules/company/standards/.apm/context/api.context.md" in content

    def test_install_reports_link_statistics(self, package_info, tmp_path):
        """Install command reports how many links were resolved."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        # Integrate prompts
        integrator = PromptIntegrator()
        result = integrator.integrate_package_prompts(package_info, project_root)

        # Should report links resolved
        assert result.links_resolved > 0

    def test_install_without_links_still_works(self, tmp_path):
        """Installing a package without context links doesn't break."""
        # Create package without context links
        package_dir = tmp_path / "apm_modules" / "simple" / "package"
        package_dir.mkdir(parents=True)

        # Create simple prompt without links
        prompts_dir = package_dir / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)

        simple_prompt = prompts_dir / "simple.prompt.md"
        simple_prompt.write_text(
            dedent("""
            ---
            description: Simple prompt
            ---
            
            # Simple Prompt
            
            No links here!
        """),
            encoding="utf-8",
        )

        # Create package info
        package = APMPackage(
            name="package", version="1.0.0", package_path=package_dir, source="simple/package"
        )

        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=None,
            installed_at=datetime.now().isoformat(),
        )

        # Integrate should work without errors
        project_root = tmp_path / "project"
        project_root.mkdir()

        integrator = PromptIntegrator()
        result = integrator.integrate_package_prompts(package_info, project_root)

        # Should integrate successfully
        assert result.files_integrated == 1
        # No links to resolve
        assert result.links_resolved == 0


class TestInstallAgentLinkResolution:
    """Tests for link resolution when installing agents."""

    def test_install_resolves_agent_links(self, package_info, tmp_path):
        """Installing a package resolves links in copied agent files."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        # Integrate agents
        integrator = AgentIntegrator()
        result = integrator.integrate_package_agents(package_info, project_root)

        # Check that agent was integrated
        assert result.files_integrated == 1

        # Check that the integrated file exists
        integrated_agent = project_root / ".github" / "agents" / "backend-expert.agent.md"
        assert integrated_agent.exists()

        # Read content and verify links are resolved
        content = integrated_agent.read_text(encoding="utf-8")

        # Link should be resolved to point directly to apm_modules
        # From .github/agents/ to apm_modules/company/standards/.apm/context/
        assert "apm_modules/company/standards/.apm/context/api.context.md" in content

    def test_install_agent_reports_link_statistics(self, package_info, tmp_path):
        """Install command reports how many links were resolved in agents."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        # Integrate agents
        integrator = AgentIntegrator()
        result = integrator.integrate_package_agents(package_info, project_root)

        # Should report links resolved
        assert result.links_resolved > 0


class TestInstallEdgeCases:
    """Tests for edge cases during installation."""

    def test_missing_context_preserves_link(self, tmp_path):
        """If context file doesn't exist, preserve original link."""
        # Create package with prompt that links to non-existent context
        package_dir = tmp_path / "apm_modules" / "broken" / "package"
        package_dir.mkdir(parents=True)

        prompts_dir = package_dir / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)

        broken_prompt = prompts_dir / "broken.prompt.md"
        broken_prompt.write_text(
            dedent("""
            ---
            description: Broken prompt
            ---
            
            # Broken Prompt
            
            See [missing context](../../context/missing.context.md)
        """),
            encoding="utf-8",
        )

        # Create package info
        package = APMPackage(
            name="package", version="1.0.0", package_path=package_dir, source="broken/package"
        )

        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=None,
            installed_at=datetime.now().isoformat(),
        )

        # Integrate
        project_root = tmp_path / "project"
        project_root.mkdir()

        integrator = PromptIntegrator()
        result = integrator.integrate_package_prompts(package_info, project_root)

        # Should integrate successfully (not fail)
        assert result.files_integrated == 1

        # Read integrated file
        integrated = project_root / ".github" / "prompts" / "broken.prompt.md"
        content = integrated.read_text(encoding="utf-8")

        # Link should be preserved (broken but documented)
        assert "missing.context.md" in content

    def test_empty_prompt_file(self, tmp_path):
        """Handle empty prompt files gracefully."""
        # Create package with empty prompt
        package_dir = tmp_path / "apm_modules" / "empty" / "package"
        package_dir.mkdir(parents=True)

        prompts_dir = package_dir / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)

        empty_prompt = prompts_dir / "empty.prompt.md"
        empty_prompt.write_text("", encoding="utf-8")

        # Create package info
        package = APMPackage(
            name="package", version="1.0.0", package_path=package_dir, source="empty/package"
        )

        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=None,
            installed_at=datetime.now().isoformat(),
        )

        # Integrate should not crash
        project_root = tmp_path / "project"
        project_root.mkdir()

        integrator = PromptIntegrator()
        result = integrator.integrate_package_prompts(package_info, project_root)

        # Should integrate successfully
        assert result.files_integrated == 1
        assert result.links_resolved == 0

    def test_no_contexts_in_package(self, tmp_path):
        """If package has no context files, link resolution is skipped."""
        # Create package with prompt but no contexts
        package_dir = tmp_path / "apm_modules" / "nocontext" / "package"
        package_dir.mkdir(parents=True)

        prompts_dir = package_dir / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)

        prompt = prompts_dir / "test.prompt.md"
        prompt.write_text(
            dedent("""
            ---
            description: Test prompt
            ---
            
            # Test
            
            Just a test, no context links.
        """),
            encoding="utf-8",
        )

        # Create package info
        package = APMPackage(
            name="package", version="1.0.0", package_path=package_dir, source="nocontext/package"
        )

        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=None,
            installed_at=datetime.now().isoformat(),
        )

        # Integrate should work
        project_root = tmp_path / "project"
        project_root.mkdir()

        integrator = PromptIntegrator()
        result = integrator.integrate_package_prompts(package_info, project_root)

        # Should integrate successfully
        assert result.files_integrated == 1
        # No links resolved (no contexts discovered)
        assert result.links_resolved == 0
