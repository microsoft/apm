"""Tests for sync_integration nuke-and-regenerate behavior.

These tests verify that sync_integration correctly removes all APM-managed
files (identified by the -apm suffix) for clean regeneration from the lockfile.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock

from apm_cli.integration import AgentIntegrator, PromptIntegrator


class TestSyncIntegrationURLNormalization:
    """Test sync_integration URL normalization for multiple packages."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.prompt_integrator = PromptIntegrator()
        self.agent_integrator = AgentIntegrator()

    def teardown_method(self):
        """Clean up after tests."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sync_removes_all_apm_prompt_files(self):
        """Test that sync removes all *-apm.prompt.md files (nuke approach)."""
        github_prompts = self.project_root / ".github" / "prompts"
        github_prompts.mkdir(parents=True)

        # Create integrated prompts from multiple packages
        (github_prompts / "compliance-audit-apm.prompt.md").write_text("# Compliance Audit")
        (github_prompts / "design-review-apm.prompt.md").write_text("# Design Review")
        (github_prompts / "breakdown-plan-apm.prompt.md").write_text("# Breakdown Plan")

        apm_package = Mock()

        # Run sync - nuke approach removes all
        result = self.prompt_integrator.sync_integration(apm_package, self.project_root)

        assert not (github_prompts / "design-review-apm.prompt.md").exists()
        assert not (github_prompts / "compliance-audit-apm.prompt.md").exists()
        assert not (github_prompts / "breakdown-plan-apm.prompt.md").exists()
        assert result["files_removed"] == 3
        assert result["errors"] == 0

    def test_sync_preserves_non_apm_prompt_files(self):
        """Test that sync only removes *-apm.prompt.md files, not other files."""
        github_prompts = self.project_root / ".github" / "prompts"
        github_prompts.mkdir(parents=True)

        # APM files (should be removed)
        (github_prompts / "test-apm.prompt.md").write_text("# APM prompt")

        # Non-APM files (should be preserved)
        (github_prompts / "my-custom.prompt.md").write_text("# Custom prompt")
        (github_prompts / "readme.md").write_text("# Readme")

        apm_package = Mock()

        result = self.prompt_integrator.sync_integration(apm_package, self.project_root)

        assert result["files_removed"] == 1
        assert not (github_prompts / "test-apm.prompt.md").exists()
        assert (github_prompts / "my-custom.prompt.md").exists()
        assert (github_prompts / "readme.md").exists()

    def test_sync_nuke_removes_all_agent_files(self):
        """Test that sync removes ALL *-apm.agent.md files (nuke-and-regenerate)."""
        github_agents = self.project_root / ".github" / "agents"
        github_agents.mkdir(parents=True)

        # Create integrated agents from multiple packages
        (github_agents / "compliance-agent-apm.agent.md").write_text("# Compliance Agent")
        (github_agents / "design-agent-apm.agent.md").write_text("# Design Agent")
        # Non-APM file should survive
        (github_agents / "my-custom.agent.md").write_text("# My Custom Agent")

        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = []

        # Run sync
        result = self.agent_integrator.sync_integration(apm_package, self.project_root)

        # All -apm files removed
        assert not (github_agents / "compliance-agent-apm.agent.md").exists()
        assert not (github_agents / "design-agent-apm.agent.md").exists()
        # Non-APM file preserved
        assert (github_agents / "my-custom.agent.md").exists()
        assert result["files_removed"] == 2
        assert result["errors"] == 0

    def test_sync_nuke_removes_all_prompt_files(self):
        """Test that sync removes all *-apm.prompt.md files regardless of packages."""
        github_prompts = self.project_root / ".github" / "prompts"
        github_prompts.mkdir(parents=True)

        # Create prompts from 3 packages
        packages = ["pkg-a", "pkg-b", "pkg-c"]
        for pkg_name in packages:
            (github_prompts / f"{pkg_name}-apm.prompt.md").write_text(f"# Prompt from {pkg_name}")

        apm_package = Mock()

        result = self.prompt_integrator.sync_integration(apm_package, self.project_root)

        # All APM files removed (nuke approach)
        for pkg_name in packages:
            assert not (github_prompts / f"{pkg_name}-apm.prompt.md").exists()
        assert result["files_removed"] == 3

    def test_sync_nuke_preserves_non_apm_files(self):
        """Test that nuke approach doesn't remove non-APM files."""
        github_prompts = self.project_root / ".github" / "prompts"
        github_prompts.mkdir(parents=True)

        # User's custom prompt (no -apm suffix)
        (github_prompts / "my-custom.prompt.md").write_text("# Custom prompt")

        # APM-integrated prompt
        (github_prompts / "test-apm.prompt.md").write_text("# APM Prompt")

        apm_package = Mock()

        result = self.prompt_integrator.sync_integration(apm_package, self.project_root)

        assert (github_prompts / "my-custom.prompt.md").exists(), "Custom file should remain"
        assert not (github_prompts / "test-apm.prompt.md").exists(), "APM file should be removed"
        assert result["files_removed"] == 1
