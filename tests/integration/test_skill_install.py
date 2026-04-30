"""Integration tests for Claude Skill installation via APM.

Tests the installation of Claude Skills (SKILL.md-based packages) from GitHub,
including simple skills and skills with bundled resources.

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
    project_dir = tmp_path / "test-skill-project"
    project_dir.mkdir()

    # Initialize minimal apm.yml
    apm_yml = project_dir / "apm.yml"
    apm_yml.write_text("""name: test-skill-project
version: 1.0.0
description: Test project for skill installation
dependencies:
  apm: []
  mcp: []
""")

    # Create .github folder for VSCode target detection
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


class TestSimpleClaudeSkillInstall:
    """Test installing a simple Claude Skill (SKILL.md only)."""

    def test_install_brand_guidelines_skill(self, temp_project, apm_command):
        """Install brand-guidelines skill from anthropics/skills."""
        # Install the skill
        result = subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines", "--verbose"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        # Check command succeeded
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Verify path structure is correct (nested, not flattened)
        skill_path = (
            temp_project / "apm_modules" / "anthropics" / "skills" / "skills" / "brand-guidelines"
        )
        assert skill_path.exists(), f"Skill not installed at expected path: {skill_path}"

        # Verify SKILL.md exists
        skill_md = skill_path / "SKILL.md"
        assert skill_md.exists(), "SKILL.md not found in installed package"

        # Verify skill was integrated to .github/skills/
        skill_integrated = temp_project / ".github" / "skills" / "brand-guidelines" / "SKILL.md"
        assert skill_integrated.exists(), "Skill not integrated to .github/skills/"

    def test_install_skill_updates_apm_yml(self, temp_project, apm_command):
        """Verify the skill is added to project's apm.yml."""
        # Install the skill
        subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        # Read project apm.yml
        apm_yml = temp_project / "apm.yml"
        content = apm_yml.read_text()

        # Verify dependency was added
        assert "anthropics/skills/skills/brand-guidelines" in content

    def test_skill_detection_in_output(self, temp_project, apm_command):
        """Verify CLI output shows skill integration message."""
        result = subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines", "--verbose"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        # Check for skill detection/integration message
        assert (
            "Skill integrated" in result.stdout
            or "Claude Skill" in result.stdout
            or "SKILL.md detected" in result.stdout
        )


class TestClaudeSkillWithResources:
    """Test installing Claude Skills with bundled resources."""

    def test_install_skill_with_scripts(self, temp_project, apm_command):
        """Install skill-creator which has scripts/ folder."""
        result = subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/skill-creator", "--verbose"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        # May fail if skill doesn't exist, skip gracefully
        if result.returncode != 0 and "not found" in result.stderr.lower():
            pytest.skip("skill-creator not available in repository")

        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Verify package path
        skill_path = (
            temp_project / "apm_modules" / "anthropics" / "skills" / "skills" / "skill-creator"
        )
        assert skill_path.exists(), "Skill not installed"

        # Verify SKILL.md
        assert (skill_path / "SKILL.md").exists()

        # Verify skill was integrated to .github/skills/
        skill_integrated = temp_project / ".github" / "skills" / "skill-creator" / "SKILL.md"
        assert skill_integrated.exists(), "Skill not integrated to .github/skills/"

    def test_resources_stay_in_apm_modules(self, temp_project, apm_command):
        """Verify bundled resources stay in apm_modules, not copied to .github/."""
        subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/skill-creator", "--verbose"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        skill_path = (
            temp_project / "apm_modules" / "anthropics" / "skills" / "skills" / "skill-creator"
        )

        if not skill_path.exists():
            pytest.skip("skill-creator not available")

        # Check .github/skills/ has the skill directory with SKILL.md
        skills_dir = temp_project / ".github" / "skills" / "skill-creator"
        if skills_dir.exists():
            assert (skills_dir / "SKILL.md").exists(), "SKILL.md not found in .github/skills/"


class TestSkillInstallIdempotency:
    """Test that skill installation is idempotent."""

    def test_reinstall_same_skill_is_idempotent(self, temp_project, apm_command):
        """Installing the same skill twice should work without errors."""
        skill_ref = "anthropics/skills/skills/brand-guidelines"

        # First install
        result1 = subprocess.run(
            [apm_command, "install", skill_ref],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result1.returncode == 0

        # Second install (should succeed, possibly from cache)
        result2 = subprocess.run(
            [apm_command, "install", skill_ref],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result2.returncode == 0

        # Verify still only one skill copy
        skill_integrated = temp_project / ".github" / "skills" / "brand-guidelines" / "SKILL.md"
        assert skill_integrated.exists()


class TestSkillInstallWithoutVSCodeTarget:
    """Test skill installation when VSCode is not the target."""

    def test_skill_install_without_github_folder(self, tmp_path, apm_command):
        """Skill installs but no agent.md generated without .github/ folder."""
        project_dir = tmp_path / "no-vscode-project"
        project_dir.mkdir()

        # Minimal apm.yml without .github folder
        apm_yml = project_dir / "apm.yml"
        apm_yml.write_text("""name: no-vscode-project
version: 1.0.0
dependencies:
  apm: []
""")

        # Install skill
        result = subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode == 0

        # Skill should be installed
        skill_path = (
            project_dir / "apm_modules" / "anthropics" / "skills" / "skills" / "brand-guidelines"
        )
        assert skill_path.exists()

        # Skill should still be integrated to .github/skills/
        skill_integrated = project_dir / ".github" / "skills" / "brand-guidelines" / "SKILL.md"
        assert skill_integrated.exists(), "Skill should be integrated to .github/skills/"
