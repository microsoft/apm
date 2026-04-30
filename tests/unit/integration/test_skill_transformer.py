"""Tests for skill transformer functionality.

Note: The SkillTransformer class is used internally by SkillIntegrator for
skill name normalization and formatting. Skills are NO LONGER transformed
to .agent.md files - they go directly to .github/skills/ as native skills.
See skill-strategy.md for architectural rationale (T5).
"""

import shutil
import tempfile
from pathlib import Path

from apm_cli.integration.skill_transformer import SkillTransformer, to_hyphen_case
from apm_cli.primitives.models import Skill


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

    def test_with_underscores(self):
        """Test underscore replacement."""
        assert to_hyphen_case("my_package") == "my-package"

    def test_with_spaces(self):
        """Test space replacement."""
        assert to_hyphen_case("Brand Guidelines") == "brand-guidelines"

    def test_mixed_separators(self):
        """Test mixed underscores and camelCase."""
        assert to_hyphen_case("my_AwesomePackage") == "my-awesome-package"

    def test_removes_invalid_characters(self):
        """Test removal of invalid characters."""
        assert to_hyphen_case("my@package!name") == "mypackagename"

    def test_removes_consecutive_hyphens(self):
        """Test consecutive hyphens are collapsed."""
        assert to_hyphen_case("my--package") == "my-package"

    def test_strips_leading_trailing_hyphens(self):
        """Test leading/trailing hyphens are stripped."""
        assert to_hyphen_case("-mypackage-") == "mypackage"


class TestSkillTransformer:
    """Test SkillTransformer class.

    Note: The SkillTransformer is kept for backwards compatibility and internal use.
    The transform_to_agent() method is deprecated but still functional for testing
    purposes. In production, skills go directly to .github/skills/ via SkillIntegrator.
    """

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.transformer = SkillTransformer()

    def teardown_method(self):
        """Clean up after tests."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_transform_to_agent_creates_directory(self):
        """Test that transform_to_agent creates .github/agents/ directory."""
        skill = Skill(
            name="Test Skill",
            file_path=Path("/fake/SKILL.md"),
            description="A test skill",
            content="# Test Skill\n\nThis is a test skill.",
            source="local",
        )

        result = self.transformer.transform_to_agent(skill, self.project_root)

        assert result is not None
        assert (self.project_root / ".github" / "agents").exists()

    def test_transform_to_agent_creates_agent_file(self):
        """Test that transform_to_agent creates the agent file."""
        skill = Skill(
            name="Brand Guidelines",
            file_path=Path("/fake/SKILL.md"),
            description="Corporate brand guidelines",
            content="# Brand Guidelines\n\nFollow these guidelines.",
            source="local",
        )

        result = self.transformer.transform_to_agent(skill, self.project_root)

        assert result is not None
        assert result.exists()
        assert result.name == "brand-guidelines.agent.md"

    def test_transform_to_agent_file_content(self):
        """Test that the generated agent file has correct content."""
        skill = Skill(
            name="Brand Guidelines",
            file_path=Path("/fake/SKILL.md"),
            description="Corporate brand guidelines",
            content="# Brand Guidelines\n\nFollow these guidelines.",
            source="local",
        )

        result = self.transformer.transform_to_agent(skill, self.project_root)
        content = result.read_text()

        # Check frontmatter
        assert "---" in content
        assert "name: Brand Guidelines" in content
        assert "description: Corporate brand guidelines" in content

        # Check body content
        assert "# Brand Guidelines" in content
        assert "Follow these guidelines." in content

    def test_transform_to_agent_with_dependency_source(self):
        """Test that source attribution is included for dependency skills."""
        skill = Skill(
            name="Compliance Rules",
            file_path=Path("/fake/SKILL.md"),
            description="Compliance rules",
            content="# Compliance\n\nFollow these rules.",
            source="dependency:owner/repo",
        )

        result = self.transformer.transform_to_agent(skill, self.project_root)
        content = result.read_text()

        assert "<!-- Source: dependency:owner/repo -->" in content

    def test_transform_to_agent_dry_run(self):
        """Test that dry_run returns path but doesn't write file."""
        skill = Skill(
            name="Test Skill",
            file_path=Path("/fake/SKILL.md"),
            description="A test skill",
            content="# Test",
            source="local",
        )

        result = self.transformer.transform_to_agent(skill, self.project_root, dry_run=True)

        assert result is not None
        assert result.name == "test-skill.agent.md"
        assert not result.exists()

    def test_get_agent_name(self):
        """Test get_agent_name method."""
        skill = Skill(
            name="Brand Guidelines",
            file_path=Path("/fake/SKILL.md"),
            description="",
            content="",
            source="local",
        )

        result = self.transformer.get_agent_name(skill)

        assert result == "brand-guidelines"

    def test_transform_complex_skill_name(self):
        """Test transformation with complex skill name."""
        skill = Skill(
            name="My Awesome SKILL v2",
            file_path=Path("/fake/SKILL.md"),
            description="An awesome skill",
            content="# Content",
            source="local",
        )

        result = self.transformer.transform_to_agent(skill, self.project_root)

        assert result is not None
        # Should normalize the name
        assert result.name == "my-awesome-skill-v2.agent.md"


# NOTE: TestAgentIntegratorSkillSupport class has been REMOVED as part of T5.
#
# The find_skill_file() method was removed from AgentIntegrator because:
# - Skills are NO LONGER transformed to .agent.md files
# - Skills now go directly to .github/skills/ via SkillIntegrator
# - See skill-strategy.md for the full architectural rationale
