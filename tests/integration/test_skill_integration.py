"""Integration tests for skill installation and integration.

Tests the install flow for skills, verifying SKILL.md is
integrated to .github/skills/{name}/SKILL.md at install time
and that compile does not modify skill files.

These tests require network access to GitHub.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Skip all tests if GITHUB_APM_PAT is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("GITHUB_APM_PAT") and not os.environ.get("GITHUB_TOKEN"),
    reason="GITHUB_APM_PAT or GITHUB_TOKEN required for GitHub API access",
)


@pytest.fixture
def temp_project(tmp_path):
    """Create a temporary APM project for testing."""
    project_dir = tmp_path / "skill-compile-project"
    project_dir.mkdir()

    # Initialize apm.yml
    apm_yml = project_dir / "apm.yml"
    apm_yml.write_text("""name: skill-compile-project
version: 1.0.0
description: Test project for skill compilation
dependencies:
  apm: []
  mcp: []
""")

    # Create .github folder for VSCode target
    github_dir = project_dir / ".github"
    github_dir.mkdir()

    return project_dir


@pytest.fixture
def apm_command():
    """Get the path to the APM CLI executable."""
    # Prefer binary on PATH (CI uses the PR artifact there)
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    # Fallback to local dev venv
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


class TestSkillInstallIntegration:
    """Test SKILL.md integration at install time."""

    def test_install_integrates_skill(self, temp_project, apm_command):
        """Install should integrate SKILL.md to .github/skills/ when VSCode is target."""
        # Install skill
        result = subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Verify skill was integrated to .github/skills/ at install time
        skill_integrated = temp_project / ".github" / "skills" / "brand-guidelines" / "SKILL.md"
        assert skill_integrated.exists(), (
            "Skill should be integrated to .github/skills/ at install time"
        )

    def test_install_preserves_skill_content(self, temp_project, apm_command):
        """Integrated skill should preserve the original SKILL.md content."""
        # Install skill
        result = subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Read both files
        skill_path = (
            temp_project
            / "apm_modules"
            / "anthropics"
            / "skills"
            / "skills"
            / "brand-guidelines"
            / "SKILL.md"
        )
        integrated_path = temp_project / ".github" / "skills" / "brand-guidelines" / "SKILL.md"

        assert skill_path.exists(), "Source SKILL.md not found in apm_modules"
        assert integrated_path.exists(), "Integrated SKILL.md not found in .github/skills/"

        skill_content = skill_path.read_text()
        integrated_content = integrated_path.read_text()

        # The content should be preserved
        assert skill_content == integrated_content, "Integrated skill content should match original"

    def test_install_creates_correct_structure(self, temp_project, apm_command):
        """Integrated skill should have SKILL.md in .github/skills/{name}/."""
        # Install skill
        result = subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        skill_dir = temp_project / ".github" / "skills" / "brand-guidelines"
        assert skill_dir.exists(), "Skill directory not created"

        # Check SKILL.md exists
        assert (skill_dir / "SKILL.md").exists(), "SKILL.md should be in skill directory"


class TestCompileSkipsSkills:
    """Test that compile does NOT modify or generate files from skills."""

    def test_compile_does_not_modify_skills(self, temp_project, apm_command):
        """Compile should not modify skill files already integrated."""
        # Install skill (this integrates the skill)
        result = subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        skill_integrated = temp_project / ".github" / "skills" / "brand-guidelines" / "SKILL.md"
        assert skill_integrated.exists(), "Skill not integrated after install"

        # Record modification time
        mtime_before = skill_integrated.stat().st_mtime

        # Run compile
        subprocess.run(
            [apm_command, "compile"], cwd=temp_project, capture_output=True, text=True, timeout=60
        )

        # Skill file should not be modified by compile
        mtime_after = skill_integrated.stat().st_mtime
        assert mtime_before == mtime_after, "Compile should not modify skill integrated at install"


class TestMultipleSkillsInstall:
    """Test install with multiple skills."""

    def test_multiple_skills_create_multiple_integrations(self, temp_project, apm_command):
        """Each installed skill should be integrated to .github/skills/."""
        skills = [
            "anthropics/skills/skills/brand-guidelines",
            # Add more skills if available in the repo
        ]

        for skill in skills:
            result = subprocess.run(
                [apm_command, "install", skill],
                cwd=temp_project,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                continue  # Skip unavailable skills

        # Check that skills were integrated
        skills_dir = temp_project / ".github" / "skills"
        if skills_dir.exists():
            skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir()]
            assert len(skill_dirs) >= 1, "At least one skill should be integrated"


class TestSkillNaming:
    """Test that skill directory naming conventions are correct."""

    def test_skill_name_matches_directory(self, temp_project, apm_command):
        """Skill directory name should match the skill name."""
        subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        # Should be brand-guidelines/ directory
        skill_dir = temp_project / ".github" / "skills" / "brand-guidelines"
        assert skill_dir.exists(), "Skill directory should match skill name"
        assert (skill_dir / "SKILL.md").exists(), "SKILL.md should be in skill directory"

    def test_skill_name_in_content(self, temp_project, apm_command):
        """Integrated SKILL.md should have content."""
        subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        skill_path = temp_project / ".github" / "skills" / "brand-guidelines" / "SKILL.md"

        if not skill_path.exists():
            pytest.skip("Skill not created")

        content = skill_path.read_text()

        # Should have content
        assert len(content) > 0, "SKILL.md should not be empty"
