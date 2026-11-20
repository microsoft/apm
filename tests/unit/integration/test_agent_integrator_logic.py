"""Tests for agent integration functionality (pure logic cases)."""

from pathlib import Path

import pytest

from apm_cli.integration import AgentIntegrator

pytestmark = pytest.mark.unit


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
        source = Path("default.agent.md")
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


# ==================================================================
# Windsurf no longer exposes an 'agents' primitive (it deploys SKILL.md
# directly via the 'skills' primitive). Tests that exercised the
# agents -> SKILL.md transformer (_write_windsurf_agent_skill) and the
# integrate_agents_for_target(windsurf, ...) path were removed when
# the primitive was dropped to fix the uninstall cleanup bug.
# ==================================================================
