"""Unit tests for orphan detection in agent and prompt integrators.

These tests validate the fix for orphan detection with virtual packages.
The fix ensures that orphan detection compares full dependency strings
instead of just base repo URLs.
"""

import tempfile
from pathlib import Path

from src.apm_cli.integration.agent_integrator import AgentIntegrator
from src.apm_cli.integration.prompt_integrator import PromptIntegrator
from src.apm_cli.models.apm_package import APMPackage, DependencyReference


def create_test_integrated_file(path: Path, source_repo: str, source_dependency: str = None):  # noqa: RUF013
    """Create a test integrated file with APM metadata.

    Args:
        path: Path to create the file at
        source_repo: The source_repo value (e.g., "owner/repo")
        source_dependency: The full dependency string (e.g., "owner/repo/collections/name")
                          If None, uses old format without source_dependency field
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if source_dependency:
        # New format with source_dependency
        content = f"""---
apm:
  source: test-package
  source_repo: {source_repo}
  source_dependency: {source_dependency}
  version: 1.0.0
  commit: abc123
  content_hash: deadbeef
---
# Test content
"""
    else:
        # Old format without source_dependency
        content = f"""---
apm:
  source: test-package
  source_repo: {source_repo}
  version: 1.0.0
  commit: abc123
  content_hash: deadbeef
---
# Test content
"""

    path.write_text(content)


def create_mock_apm_package(dependencies: list) -> APMPackage:
    """Create a mock APMPackage with the given dependencies."""
    parsed_deps = [DependencyReference.parse(d) for d in dependencies]
    return APMPackage(name="test-project", version="1.0.0", dependencies={"apm": parsed_deps})


class TestAgentIntegratorOrphanDetection:
    """Test nuke-and-regenerate sync in AgentIntegrator.

    AgentIntegrator now uses nuke approach: removes ALL *-apm.agent.md
    and *-apm.chatmode.md files. The caller re-integrates from installed packages.
    """

    def test_sync_removes_all_apm_agent_files(self):
        """All *-apm.agent.md files are removed regardless of install state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            agents_dir = project_root / ".github" / "agents"

            create_test_integrated_file(
                agents_dir / "test-apm.agent.md",
                source_repo="owner/repo",
                source_dependency="owner/repo",
            )

            # Even with the package installed, nuke removes all
            apm_package = create_mock_apm_package(["owner/repo"])

            integrator = AgentIntegrator()
            result = integrator.sync_integration(apm_package, project_root)

            assert result["files_removed"] == 1
            assert not (agents_dir / "test-apm.agent.md").exists()

    def test_sync_removes_multiple_apm_files(self):
        """Multiple -apm files from different packages are all removed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            agents_dir = project_root / ".github" / "agents"
            agents_dir.mkdir(parents=True)

            (agents_dir / "agent-a-apm.agent.md").write_text("# Agent A")
            (agents_dir / "agent-b-apm.agent.md").write_text("# Agent B")
            (agents_dir / "agent-c-apm.agent.md").write_text("# Agent C")

            apm_package = create_mock_apm_package([])

            integrator = AgentIntegrator()
            result = integrator.sync_integration(apm_package, project_root)

            assert result["files_removed"] == 3

    def test_sync_preserves_non_apm_files(self):
        """Files without -apm suffix are preserved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            agents_dir = project_root / ".github" / "agents"
            agents_dir.mkdir(parents=True)

            (agents_dir / "my-custom.agent.md").write_text("# Custom")
            (agents_dir / "test-apm.agent.md").write_text("# APM managed")

            apm_package = create_mock_apm_package([])

            integrator = AgentIntegrator()
            result = integrator.sync_integration(apm_package, project_root)

            assert result["files_removed"] == 1
            assert (agents_dir / "my-custom.agent.md").exists()
            assert not (agents_dir / "test-apm.agent.md").exists()

    def test_sync_removes_chatmode_files(self):
        """Legacy .chatmode.md files deployed as .agent.md are removed correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            agents_dir = project_root / ".github" / "agents"
            agents_dir.mkdir(parents=True)

            # Chatmodes are now deployed as .agent.md, so sync removes them via that pattern
            (agents_dir / "test-apm.agent.md").write_text("# Agent (was chatmode)")

            apm_package = create_mock_apm_package([])

            integrator = AgentIntegrator()
            result = integrator.sync_integration(apm_package, project_root)

            assert result["files_removed"] == 1
            assert not (agents_dir / "test-apm.agent.md").exists()


class TestPromptIntegratorOrphanDetection:
    """Test nuke-and-regenerate sync in PromptIntegrator.

    PromptIntegrator now uses nuke approach: removes ALL *-apm.prompt.md files.
    The caller re-integrates from currently installed packages.
    """

    def test_sync_removes_all_apm_files(self):
        """All *-apm.prompt.md files are removed regardless of metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            prompts_dir = project_root / ".github" / "prompts"

            create_test_integrated_file(
                prompts_dir / "test-apm.prompt.md",
                source_repo="owner/repo",
                source_dependency="owner/repo",
            )

            apm_package = create_mock_apm_package(["owner/repo"])

            integrator = PromptIntegrator()
            result = integrator.sync_integration(apm_package, project_root)

            # Nuke removes ALL apm files
            assert result["files_removed"] == 1
            assert not (prompts_dir / "test-apm.prompt.md").exists()

    def test_sync_removes_uninstalled_package(self):
        """Uninstalled package files are removed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            prompts_dir = project_root / ".github" / "prompts"

            create_test_integrated_file(
                prompts_dir / "test-apm.prompt.md",
                source_repo="owner/repo",
                source_dependency="owner/repo",
            )

            apm_package = create_mock_apm_package(["other/package"])

            integrator = PromptIntegrator()
            result = integrator.sync_integration(apm_package, project_root)

            assert result["files_removed"] == 1
            assert not (prompts_dir / "test-apm.prompt.md").exists()

    def test_sync_removes_virtual_package_files(self):
        """Virtual package files are also removed by nuke approach."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            prompts_dir = project_root / ".github" / "prompts"

            create_test_integrated_file(
                prompts_dir / "code-review-apm.prompt.md",
                source_repo="github/awesome-copilot",
                source_dependency="github/awesome-copilot/skills/review-and-refactor",
            )

            apm_package = create_mock_apm_package(
                ["github/awesome-copilot/skills/review-and-refactor"]
            )

            integrator = PromptIntegrator()
            result = integrator.sync_integration(apm_package, project_root)

            # Nuke removes everything
            assert result["files_removed"] == 1
            assert not (prompts_dir / "code-review-apm.prompt.md").exists()

    def test_sync_preserves_non_apm_suffix_files(self):
        """Files without -apm suffix are not removed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            prompts_dir = project_root / ".github" / "prompts"
            prompts_dir.mkdir(parents=True, exist_ok=True)

            # Non-APM file
            (prompts_dir / "custom.prompt.md").write_text("# Custom")
            # APM file
            create_test_integrated_file(
                prompts_dir / "test-apm.prompt.md",
                source_repo="owner/repo",
                source_dependency="owner/repo",
            )

            apm_package = create_mock_apm_package(["owner/repo"])

            integrator = PromptIntegrator()
            result = integrator.sync_integration(apm_package, project_root)

            assert result["files_removed"] == 1
            assert not (prompts_dir / "test-apm.prompt.md").exists()
            assert (prompts_dir / "custom.prompt.md").exists()


class TestMixedScenarios:
    """Test complex scenarios with multiple packages and virtual packages."""

    def test_multiple_virtual_packages_from_same_repo(self):
        """Multiple virtual packages from same repo — nuke removes all."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            agents_dir = project_root / ".github" / "agents"

            # Create agents from two different virtual collections
            create_test_integrated_file(
                agents_dir / "azure-apm.agent.md",
                source_repo="github/awesome-copilot",
                source_dependency="github/awesome-copilot/plugins/azure-cloud-development",
            )
            create_test_integrated_file(
                agents_dir / "aws-apm.agent.md",
                source_repo="github/awesome-copilot",
                source_dependency="github/awesome-copilot/plugins/testing-automation",
            )

            apm_package = create_mock_apm_package(
                ["github/awesome-copilot/plugins/azure-cloud-development"]
            )

            integrator = AgentIntegrator()
            result = integrator.sync_integration(apm_package, project_root)

            # Nuke removes ALL -apm files (caller re-integrates installed ones)
            assert result["files_removed"] == 2
            assert not (agents_dir / "azure-apm.agent.md").exists()
            assert not (agents_dir / "aws-apm.agent.md").exists()

    def test_regular_and_virtual_packages_mixed(self):
        """Mix of regular and virtual packages handled correctly by nuke approach."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            prompts_dir = project_root / ".github" / "prompts"

            # Create prompt from regular package
            create_test_integrated_file(
                prompts_dir / "regular-apm.prompt.md",
                source_repo="owner/regular-pkg",
                source_dependency="owner/regular-pkg",
            )
            # Create prompt from virtual package
            create_test_integrated_file(
                prompts_dir / "virtual-apm.prompt.md",
                source_repo="github/awesome-copilot",
                source_dependency="github/awesome-copilot/skills/create-readme",
            )

            # Mock package with both installed
            apm_package = create_mock_apm_package(
                ["owner/regular-pkg", "github/awesome-copilot/skills/create-readme"]
            )

            integrator = PromptIntegrator()
            result = integrator.sync_integration(apm_package, project_root)

            # Nuke removes ALL apm files (caller re-integrates)
            assert result["files_removed"] == 2
            assert not (prompts_dir / "regular-apm.prompt.md").exists()
            assert not (prompts_dir / "virtual-apm.prompt.md").exists()
