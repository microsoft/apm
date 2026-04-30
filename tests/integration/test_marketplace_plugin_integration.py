"""Integration test for plugin support.

This test verifies the complete plugin workflow:
1. Detection of plugin.json in various locations
2. Synthesis of apm.yml from plugin.json metadata
3. Artifact mapping to .apm/ structure
4. Package validation and error handling
"""

import json
import shutil
from pathlib import Path
import pytest
from datetime import datetime

from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from src.apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    PackageType,
    ResolvedReference,
    validate_apm_package,
)


class TestPluginIntegration:
    """Test complete plugin integration."""
    
    def test_plugin_detection_and_synthesis(self, tmp_path):
        """Test that plugin.json is detected and apm.yml is synthesized (root location)."""
        plugin_dir = tmp_path / "test-plugin"
        plugin_dir.mkdir()

        # Create plugin.json (version is optional per spec)
        plugin_json = {
            "name": "Test Plugin",
            "description": "A test plugin",
            "author": {"name": "Test Author"},
            "license": "MIT",
            "tags": ["testing"]
        }

        with open(plugin_dir / "plugin.json", "w") as f:
            json.dump(plugin_json, f)
        
        # Create some plugin artifacts
        (plugin_dir / "commands").mkdir()
        (plugin_dir / "commands" / "test.md").write_text("# Test Command")
        
        # Run validation
        result = validate_apm_package(plugin_dir)
        
        # Verify detection
        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result.package is not None
        assert result.package.name == "Test Plugin"
        assert result.package.version == "0.0.0"  # defaults when absent
        
        # Verify synthesized apm.yml exists
        apm_yml_path = plugin_dir / "apm.yml"
        assert apm_yml_path.exists()
        
        # Verify .apm directory was created
        apm_dir = plugin_dir / ".apm"
        assert apm_dir.exists()
    
    def test_github_copilot_plugin_format(self, tmp_path):
        """Test that .github/plugin/plugin.json format is detected."""
        plugin_dir = tmp_path / "copilot-plugin"
        plugin_dir.mkdir()
        
        # Create .github/plugin/plugin.json (GitHub Copilot format)
        github_plugin_dir = plugin_dir / ".github" / "plugin"
        github_plugin_dir.mkdir(parents=True)
        
        plugin_json = {
            "name": "GitHub Copilot Plugin",
            "version": "2.0.0",
            "description": "A GitHub Copilot plugin"
        }
        
        with open(github_plugin_dir / "plugin.json", "w") as f:
            json.dump(plugin_json, f)
        
        # Create primitives at repository root
        (plugin_dir / "agents").mkdir()
        (plugin_dir / "agents" / "test.agent.md").write_text("# Test Agent")
        
        # Run validation
        result = validate_apm_package(plugin_dir)
        
        # Verify detection
        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result.package is not None
        assert result.package.name == "GitHub Copilot Plugin"
        assert result.package.version == "2.0.0"
    
    def test_claude_plugin_format(self, tmp_path):
        """Test that .claude-plugin/plugin.json format is detected."""
        plugin_dir = tmp_path / "claude-plugin"
        plugin_dir.mkdir()
        
        # Create .claude-plugin/plugin.json (Claude format)
        claude_plugin_dir = plugin_dir / ".claude-plugin"
        claude_plugin_dir.mkdir(parents=True)
        
        plugin_json = {
            "name": "Claude Plugin",
            "version": "3.0.0",
            "description": "A Claude plugin"
        }
        
        with open(claude_plugin_dir / "plugin.json", "w") as f:
            json.dump(plugin_json, f)
        
        # Create primitives at repository root
        (plugin_dir / "skills").mkdir()
        skill_dir = plugin_dir / "skills" / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Test Skill")
        
        # Run validation
        result = validate_apm_package(plugin_dir)
        
        # Verify detection
        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result.package is not None
        assert result.package.name == "Claude Plugin"
        assert result.package.version == "3.0.0"
    
    def test_plugin_location_priority(self, tmp_path):
        """Test that plugin.json is found via deterministic 3-location check."""
        # Test 1: Root plugin.json takes priority
        plugin_dir = tmp_path / "priority-test"
        plugin_dir.mkdir()
        
        with open(plugin_dir / "plugin.json", "w") as f:
            json.dump({"name": "Root Plugin", "version": "1.0.0", "description": "Root"}, f)
        
        # Create in .claude-plugin/
        (plugin_dir / ".claude-plugin").mkdir()
        with open(plugin_dir / ".claude-plugin" / "plugin.json", "w") as f:
            json.dump({"name": "Claude Plugin", "version": "3.0.0", "description": "Claude"}, f)
        
        # Create in .github/plugin/
        (plugin_dir / ".github" / "plugin").mkdir(parents=True)
        with open(plugin_dir / ".github" / "plugin" / "plugin.json", "w") as f:
            json.dump({"name": "GitHub Plugin", "version": "4.0.0", "description": "GitHub"}, f)
        
        # Root should win
        result = validate_apm_package(plugin_dir)
        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result.package is not None
        assert result.package.name == "Root Plugin"
        assert result.package.version == "1.0.0"
        
        # Test 2: .github/plugin/ is found when no root plugin.json
        plugin_dir2 = tmp_path / "github-test"
        plugin_dir2.mkdir()
        (plugin_dir2 / ".github" / "plugin").mkdir(parents=True)
        with open(plugin_dir2 / ".github" / "plugin" / "plugin.json", "w") as f:
            json.dump({"name": "GitHub Plugin", "version": "2.0.0", "description": "GitHub"}, f)
            
        result2 = validate_apm_package(plugin_dir2)
        assert result2.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result2.package.name == "GitHub Plugin"
        assert result2.package.version == "2.0.0"
        
        # Test 3: .claude-plugin/ is found when no root plugin.json
        plugin_dir3 = tmp_path / "claude-test"
        plugin_dir3.mkdir()
        (plugin_dir3 / ".claude-plugin").mkdir()
        with open(plugin_dir3 / ".claude-plugin" / "plugin.json", "w") as f:
            json.dump({"name": "Claude Plugin", "version": "3.0.0", "description": "Claude"}, f)
            
        result3 = validate_apm_package(plugin_dir3)
        assert result3.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result3.package.name == "Claude Plugin"
        assert result3.package.version == "3.0.0"
    
    def test_plugin_detection_and_structure_mapping(self, tmp_path):
        """Test that a plugin is detected and mapped correctly using fixtures."""
        # Use the mock plugin fixture
        fixture_path = Path(__file__).parent.parent / "fixtures" / "mock-marketplace-plugin"
        
        if not fixture_path.exists():
            pytest.skip("Mock marketplace plugin fixture not available")
        
        plugin_dir = tmp_path / "mock-marketplace-plugin"
        shutil.copytree(fixture_path, plugin_dir)

        # Validate the plugin package
        result = validate_apm_package(plugin_dir)
        
        # Verify package type detection
        assert result.package_type == PackageType.MARKETPLACE_PLUGIN, \
            f"Expected MARKETPLACE_PLUGIN, got {result.package_type}"
        
        # Verify no errors
        assert result.is_valid, f"Package validation failed: {result.errors}"
        
        # Verify package was created
        assert result.package is not None, "Package should be created"
        assert result.package.name == "Mock Marketplace Plugin"
        assert result.package.version == "1.0.0"
        assert result.package.description == "A test marketplace plugin for APM integration testing"
        
        # Verify apm.yml was synthesized
        apm_yml_path = plugin_dir / "apm.yml"
        assert apm_yml_path.exists(), "apm.yml should be synthesized"
        
        # Verify .apm directory structure was created
        apm_dir = plugin_dir / ".apm"
        assert apm_dir.exists(), ".apm directory should exist"
        
        # Verify artifact mapping
        agents_dir = apm_dir / "agents"
        assert agents_dir.exists(), "agents/ should be mapped to .apm/agents/"
        assert (agents_dir / "test-agent.agent.md").exists(), "Agent file should be mapped"
        
        skills_dir = apm_dir / "skills"
        assert skills_dir.exists(), "skills/ should be mapped to .apm/skills/"
        assert (skills_dir / "test-skill" / "SKILL.md").exists(), "Skill should be mapped"
        
        prompts_dir = apm_dir / "prompts"
        assert prompts_dir.exists(), "commands/ should be mapped to .apm/prompts/"
        assert (prompts_dir / "test-command.prompt.md").exists(), "Command should be mapped to prompts"
    
    def test_plugin_with_dependencies(self, tmp_path):
        """Test plugin with dependencies are handled correctly."""
        plugin_dir = tmp_path / "plugin-with-deps"
        plugin_dir.mkdir()
        
        # Create plugin.json with dependencies
        plugin_json = plugin_dir / "plugin.json"
        plugin_json.write_text("""
{
  "name": "Plugin With Dependencies",
  "version": "2.0.0",
  "description": "A plugin with dependencies",
  "author": {"name": "Test Author"},
  "dependencies": [
    "owner/dependency-package",
    "another/required-package#v1.0"
  ]
}
""")
        
        # Validate
        result = validate_apm_package(plugin_dir)
        
        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result.is_valid
        assert result.package is not None
        
        # Verify dependencies are in apm.yml
        apm_yml = plugin_dir / "apm.yml"
        assert apm_yml.exists()
        
        content = apm_yml.read_text()
        assert "dependencies:" in content
        assert "owner/dependency-package" in content
        assert "another/required-package#v1.0" in content
    
    def test_plugin_metadata_preservation(self, tmp_path):
        """Test that all plugin metadata is preserved in apm.yml."""
        plugin_dir = tmp_path / "metadata-plugin"
        plugin_dir.mkdir()
        
        # Create plugin.json with all metadata fields
        plugin_json = plugin_dir / "plugin.json"
        plugin_json.write_text("""
{
  "name": "Full Metadata Plugin",
  "version": "1.5.0",
  "description": "A plugin with complete metadata",
  "author": {"name": "APM Contributors", "email": "apm@microsoft.com"},
  "license": "Apache-2.0",
  "repository": "microsoft/apm-plugin",
  "homepage": "https://apm.dev/plugins/test",
  "tags": ["ai", "agents", "testing"]
}
""")
        
        # Validate
        result = validate_apm_package(plugin_dir)
        
        assert result.is_valid
        package = result.package
        
        # Verify all metadata
        assert package.name == "Full Metadata Plugin"
        assert package.version == "1.5.0"
        assert package.description == "A plugin with complete metadata"
        assert package.author == "APM Contributors"  # extracted from author.name
        assert package.license == "Apache-2.0"
        
        # Read apm.yml and verify fields
        apm_yml = (plugin_dir / "apm.yml").read_text()
        assert "repository: microsoft/apm-plugin" in apm_yml
        assert "homepage: https://apm.dev/plugins/test" in apm_yml
        assert "tags:" in apm_yml
        assert "ai" in apm_yml
        assert "agents" in apm_yml
    
    def test_invalid_plugin_json(self, tmp_path):
        """Test that malformed plugin.json (invalid JSON syntax) is handled gracefully."""
        plugin_dir = tmp_path / "invalid-plugin"
        plugin_dir.mkdir()

        # Write syntactically invalid JSON
        plugin_json = plugin_dir / "plugin.json"
        plugin_json.write_text("{ this is not valid json }")

        # Validate — the parser should fall back to dir-name defaults and succeed
        result = validate_apm_package(plugin_dir)
        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        # name derived from directory name
        assert result.package is not None
        assert result.package.name == "invalid-plugin"
    
    def test_plugin_without_artifacts(self, tmp_path):
        """Test plugin with only plugin.json and no artifacts."""
        plugin_dir = tmp_path / "minimal-plugin"
        plugin_dir.mkdir()
        
        # Create minimal plugin.json
        plugin_json = plugin_dir / "plugin.json"
        plugin_json.write_text("""
{
  "name": "Minimal Plugin",
  "version": "0.1.0",
  "description": "A minimal plugin"
}
""")
        
        # Validate
        result = validate_apm_package(plugin_dir)
        
        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result.is_valid
        assert result.package is not None
        
        # .apm directory should still be created even if empty
        apm_dir = plugin_dir / ".apm"
        assert apm_dir.exists()

    def test_plugin_without_plugin_json(self, tmp_path):
        """A directory with .claude-plugin/ dir but no plugin.json is still a Claude plugin."""
        plugin_dir = tmp_path / "no-manifest-plugin"
        plugin_dir.mkdir()

        # .claude-plugin/ directory acts as plugin manifest marker
        (plugin_dir / ".claude-plugin").mkdir()
        (plugin_dir / "commands").mkdir()
        (plugin_dir / "commands" / "do-something.md").write_text("# Do Something")
        (plugin_dir / "agents").mkdir()
        (plugin_dir / "agents" / "helper.agent.md").write_text("# Helper")

        result = validate_apm_package(plugin_dir)

        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result.is_valid
        assert result.package is not None
        # Name derived from directory name
        assert result.package.name == "no-manifest-plugin"
        assert result.package.version == "0.0.0"

    def test_mcp_json_copied_through(self, tmp_path):
        """MCP plugins: .mcp.json must be present in .apm/ after normalization."""
        plugin_dir = tmp_path / "mcp-plugin"
        plugin_dir.mkdir()

        mcp_config = {"mcpServers": {"my-server": {"command": "node", "args": ["index.js"]}}}
        (plugin_dir / ".mcp.json").write_text(json.dumps(mcp_config))
        # plugin.json is the manifest marker
        (plugin_dir / "plugin.json").write_text(json.dumps({"name": "mcp-plugin"}))
        (plugin_dir / "commands").mkdir()
        (plugin_dir / "commands" / "run.md").write_text("# Run")

        result = validate_apm_package(plugin_dir)

        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result.is_valid
        assert (plugin_dir / ".apm" / ".mcp.json").exists(), ".mcp.json must be copied to .apm/"

    def test_plugin_integrator_deployment(self, tmp_path):
        """Plugin install should populate .github/.claude targets consumed by editors."""
        fixture_path = Path(__file__).parent.parent / "fixtures" / "mock-marketplace-plugin"
        plugin_dir = tmp_path / "installed-plugin"
        shutil.copytree(fixture_path, plugin_dir)

        # Normalize plugin.json into apm.yml + .apm/
        validation = validate_apm_package(plugin_dir)
        assert validation.is_valid
        assert validation.package_type == PackageType.MARKETPLACE_PLUGIN

        package = validation.package
        assert isinstance(package, APMPackage)

        package_info = PackageInfo(
            package=package,
            install_path=plugin_dir,
            resolved_reference=ResolvedReference(
                original_ref="main",
                ref_type=GitReferenceType.BRANCH,
                resolved_commit="abcdef1234567890",
                ref_name="main",
            ),
            installed_at=datetime.now().isoformat(),
            package_type=validation.package_type,
        )

        project_root = tmp_path / "project"
        project_root.mkdir()

        prompt_result = PromptIntegrator().integrate_package_prompts(package_info, project_root)
        agent_result = AgentIntegrator().integrate_package_agents(package_info, project_root)
        skill_result = SkillIntegrator().integrate_package_skill(package_info, project_root)
        claude_agent_result = AgentIntegrator().integrate_package_agents_claude(package_info, project_root)
        command_result = CommandIntegrator().integrate_package_commands(package_info, project_root)

        # VS Code / Copilot pickup locations
        assert prompt_result.files_integrated == 1
        assert (project_root / ".github" / "prompts" / "test-command.prompt.md").exists()

        assert agent_result.files_integrated == 1
        assert (project_root / ".github" / "agents" / "test-agent.agent.md").exists()

        assert skill_result.skill_created or skill_result.skill_skipped
        assert (project_root / ".github" / "skills" / "test-skill" / "SKILL.md").exists()

        # Claude/Copilot-compatible locations produced during install path
        assert claude_agent_result.files_integrated == 1
        assert (project_root / ".claude" / "agents" / "test-agent.md").exists()

        assert command_result.files_integrated == 1
        assert (project_root / ".claude" / "commands" / "test-command.md").exists()
