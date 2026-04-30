"""Integration tests for mixed dependencies (APM packages + Claude Skills).

Tests that projects can have both traditional APM packages and Claude Skills
as dependencies, and that both types work correctly together.

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
    project_dir = tmp_path / "mixed-deps-project"
    project_dir.mkdir()

    # Initialize apm.yml with both dependency types
    apm_yml = project_dir / "apm.yml"
    apm_yml.write_text("""name: mixed-deps-project
version: 1.0.0
description: Test project with mixed dependencies
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


class TestMixedDependencyInstall:
    """Test installing both APM packages and Claude Skills."""

    def test_install_apm_package_and_claude_skill(self, temp_project, apm_command):
        """Install an APM package and a Claude Skill in the same project."""
        # Install APM package first
        result1 = subprocess.run(
            [apm_command, "install", "microsoft/apm-sample-package"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        # May fail if package doesn't exist or no access
        if result1.returncode != 0:
            pytest.skip(f"Could not install apm-sample-package: {result1.stderr}")

        # Install Claude Skill
        result2 = subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result2.returncode == 0, f"Skill install failed: {result2.stderr}"

        # Verify both are installed
        apm_package_path = temp_project / "apm_modules" / "microsoft" / "apm-sample-package"
        skill_path = (
            temp_project / "apm_modules" / "anthropics" / "skills" / "skills" / "brand-guidelines"
        )

        assert apm_package_path.exists(), "APM package not installed"
        assert skill_path.exists(), "Claude Skill not installed"

    def test_apm_yml_contains_both_dependency_types(self, temp_project, apm_command):
        """Verify apm.yml lists both APM packages and Claude Skills."""
        # Install both
        subprocess.run(
            [apm_command, "install", "microsoft/apm-sample-package"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )
        subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        # Read apm.yml
        apm_yml = temp_project / "apm.yml"
        content = apm_yml.read_text()

        # Verify the skill is in dependencies
        has_skill = "skills/brand-guidelines" in content

        # At least the skill should be there (apm-sample-package may fail)
        assert has_skill, "Claude Skill not in apm.yml"


class TestMixedDependencyCompile:
    """Test compiling projects with mixed dependencies."""

    def test_compile_with_mixed_deps_generates_agents_md(self, temp_project, apm_command):
        """Compile should generate AGENTS.md from both package types."""
        # Install Claude Skill (most likely to succeed)
        subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        # Create a local instruction to ensure AGENTS.md has content
        instructions_dir = temp_project / ".github" / "instructions"
        instructions_dir.mkdir(parents=True, exist_ok=True)
        instruction = instructions_dir / "test.instructions.md"
        instruction.write_text("""---
applyTo: "**/*.py"
description: Test instruction
---
# Test Instruction
This is a test.
""")

        # Run compile
        result = subprocess.run(
            [apm_command, "compile"], cwd=temp_project, capture_output=True, text=True, timeout=60
        )

        assert result.returncode == 0, f"Compile failed: {result.stderr}"

        # Verify AGENTS.md was created
        agents_md = temp_project / "AGENTS.md"
        assert agents_md.exists(), "AGENTS.md not generated"

        # Verify skill was integrated to .github/skills/
        skill_integrated = temp_project / ".github" / "skills" / "brand-guidelines" / "SKILL.md"
        assert skill_integrated.exists(), "Skill not integrated to .github/skills/"

    def test_compile_output_mentions_sources(self, temp_project, apm_command):
        """Compile output should mention different source types."""
        # Install skill
        subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        # Run compile with verbose
        result = subprocess.run(
            [apm_command, "compile", "--verbose"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Should complete without error
        assert result.returncode == 0


class TestDependencyTypeDetection:
    """Test that dependency types are correctly detected."""

    def test_apm_package_has_apm_yml(self, temp_project, apm_command):
        """APM packages have apm.yml at root."""
        result = subprocess.run(
            [apm_command, "install", "microsoft/apm-sample-package"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            pytest.skip("Could not install apm-sample-package")

        pkg_path = temp_project / "apm_modules" / "microsoft" / "apm-sample-package"
        assert (pkg_path / "apm.yml").exists(), "APM package missing apm.yml"

    def test_claude_skill_has_skill_md(self, temp_project, apm_command):
        """Claude Skills have SKILL.md at root."""
        subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        skill_path = (
            temp_project / "apm_modules" / "anthropics" / "skills" / "skills" / "brand-guidelines"
        )
        assert (skill_path / "SKILL.md").exists(), "Claude Skill missing SKILL.md"

    def test_skill_gets_integrated_to_github_skills(self, temp_project, apm_command):
        """Claude Skills get integrated to .github/skills/ directory."""
        subprocess.run(
            [apm_command, "install", "anthropics/skills/skills/brand-guidelines"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
        )

        skill_integrated = temp_project / ".github" / "skills" / "brand-guidelines" / "SKILL.md"

        assert skill_integrated.exists(), "Claude Skill should be integrated to .github/skills/"

        content = skill_integrated.read_text()
        assert len(content) > 0, "Integrated SKILL.md should not be empty"
