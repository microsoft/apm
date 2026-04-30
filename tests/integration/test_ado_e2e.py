"""
E2E tests for Azure DevOps package support.

These tests require the ADO_APM_PAT environment variable to be set
and make real network calls to Azure DevOps repositories.

Skip these tests if ADO_APM_PAT is not available.
"""

import os
import shutil
import subprocess
import sys
import tempfile  # noqa: F401
from pathlib import Path

import pytest
import yaml

# Skip all tests in this module if ADO_APM_PAT is not set
pytestmark = pytest.mark.skipif(
    not os.getenv("ADO_APM_PAT"), reason="ADO_APM_PAT environment variable not set"
)


def run_apm_command(cmd: str, cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run an APM CLI command and return the result."""
    # Prefer binary on PATH (CI uses the PR artifact there)
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        apm_path = apm_on_path
    # Fallback to local dev venv
    elif sys.platform == "win32":
        apm_path = Path(__file__).parent.parent.parent / ".venv" / "Scripts" / "apm.exe"
    else:
        apm_path = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"

    full_cmd = f"{apm_path} {cmd}"
    result = subprocess.run(
        full_cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ},
        encoding="utf-8",
        errors="replace",
    )
    return result


class TestADOInstall:
    """Test installing ADO packages."""

    # Test ADO repository - must be accessible with ADO_APM_PAT
    ADO_TEST_REPO = "dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules"

    def test_install_ado_package(self, tmp_path):
        """Install a real ADO package and verify directory structure."""
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        # Initialize project
        apm_yml = project_dir / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {"name": "test-project", "version": "1.0.0", "dependencies": {"apm": [], "mcp": []}}
            )
        )

        # Install ADO package
        result = run_apm_command(f'install "{self.ADO_TEST_REPO}"', project_dir)
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Verify 3-level directory structure
        apm_modules = project_dir / "apm_modules"
        assert apm_modules.exists(), "apm_modules/ not created"

        # Should be: apm_modules/dmeppiel-org/market-js-app/compliance-rules/
        expected_path = apm_modules / "dmeppiel-org" / "market-js-app" / "compliance-rules"
        assert expected_path.exists(), f"Expected 3-level path not found: {expected_path}"

        # Verify package content
        assert (expected_path / "apm.yml").exists() or (expected_path / ".apm").exists()


class TestADODepsAndPrune:
    """Test deps list and prune with ADO packages."""

    ADO_TEST_REPO = "dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules"

    def test_deps_list_shows_correct_path(self, tmp_path):
        """deps list should show full 3-level path for ADO packages."""
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        # Initialize and install
        apm_yml = project_dir / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": [self.ADO_TEST_REPO], "mcp": []},
                }
            )
        )

        run_apm_command("install", project_dir, timeout=120)

        # Run deps list
        result = run_apm_command("deps list", project_dir)
        assert result.returncode == 0, f"deps list failed: {result.stderr}"

        # Should show 3-level path (may be truncated with ...) and azure-devops source
        assert "dmeppiel-org/market-js-app" in result.stdout
        assert "azure-devops" in result.stdout
        assert "orphaned" not in result.stdout.lower()

    def test_prune_no_false_positives(self, tmp_path):
        """prune should not flag properly installed ADO packages as orphaned."""
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        # Initialize and install
        apm_yml = project_dir / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": [self.ADO_TEST_REPO], "mcp": []},
                }
            )
        )

        run_apm_command("install", project_dir, timeout=120)

        # Run prune --dry-run
        result = run_apm_command("prune --dry-run", project_dir)
        assert result.returncode == 0, f"prune failed: {result.stderr}"

        # Should report clean
        assert "No orphaned packages found" in result.stdout or "clean" in result.stdout.lower()


class TestADOCompile:
    """Test compilation with ADO dependencies."""

    ADO_TEST_REPO = "dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules"

    def test_compile_generates_agents_md(self, tmp_path):
        """Compile should generate AGENTS.md from ADO dependencies."""
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        # Initialize and install
        apm_yml = project_dir / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": [self.ADO_TEST_REPO], "mcp": []},
                }
            )
        )

        run_apm_command("install", project_dir, timeout=120)

        # Run compile
        result = run_apm_command("compile --verbose", project_dir)
        assert result.returncode == 0, f"compile failed: {result.stderr}"

        # Should not show orphan warnings
        assert "orphan" not in result.stdout.lower() or "0 orphan" in result.stdout.lower()

        # AGENTS.md should be generated
        agents_md = project_dir / "AGENTS.md"
        assert agents_md.exists(), "AGENTS.md not generated"


class TestADOVirtualPackage:
    """Test virtual package (single file) installation from ADO."""

    ADO_VIRTUAL_PACKAGE = (
        "dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules/gdpr-assessment.prompt.md"
    )

    def test_install_virtual_package(self, tmp_path):
        """Install a single file (virtual package) from ADO repo."""
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        # Initialize
        apm_yml = project_dir / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {"name": "test-project", "version": "1.0.0", "dependencies": {"apm": [], "mcp": []}}
            )
        )

        # Install virtual package
        result = run_apm_command(f'install "{self.ADO_VIRTUAL_PACKAGE}"', project_dir, timeout=120)
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Verify 3-level virtual package path
        apm_modules = project_dir / "apm_modules"
        # Virtual package name: compliance-rules-gdpr-assessment
        expected_path = (
            apm_modules / "dmeppiel-org" / "market-js-app" / "compliance-rules-gdpr-assessment"
        )
        assert expected_path.exists(), f"Expected virtual package path not found: {expected_path}"

    def test_virtual_package_not_orphaned(self, tmp_path):
        """Virtual packages should not be flagged as orphaned."""
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        # Initialize and install virtual package
        apm_yml = project_dir / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": [self.ADO_VIRTUAL_PACKAGE], "mcp": []},
                }
            )
        )

        run_apm_command("install", project_dir, timeout=120)

        # deps list should show it correctly
        result = run_apm_command("deps list", project_dir)
        assert "orphaned" not in result.stdout.lower() or "0 orphan" in result.stdout.lower()

        # prune should report clean
        result = run_apm_command("prune --dry-run", project_dir)
        assert "No orphaned packages found" in result.stdout or "clean" in result.stdout.lower()


class TestMixedDependencies:
    """Test mixed GitHub and ADO dependencies."""

    GITHUB_PACKAGE = "microsoft/apm-sample-package"
    ADO_PACKAGE = "dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules"

    def test_mixed_install(self, tmp_path):
        """Both GitHub and ADO packages should install correctly."""
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        # Initialize with both dependencies
        apm_yml = project_dir / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": [self.GITHUB_PACKAGE, self.ADO_PACKAGE], "mcp": []},
                }
            )
        )

        # Install all
        result = run_apm_command("install", project_dir, timeout=180)
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Verify both structures
        apm_modules = project_dir / "apm_modules"

        # GitHub: 2-level
        github_path = apm_modules / "microsoft" / "apm-sample-package"
        assert github_path.exists(), f"GitHub package not found: {github_path}"

        # ADO: 3-level
        ado_path = apm_modules / "dmeppiel-org" / "market-js-app" / "compliance-rules"
        assert ado_path.exists(), f"ADO package not found: {ado_path}"

    def test_mixed_deps_list(self, tmp_path):
        """deps list should show correct sources for mixed dependencies."""
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        # Initialize with both dependencies
        apm_yml = project_dir / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": [self.GITHUB_PACKAGE, self.ADO_PACKAGE], "mcp": []},
                }
            )
        )

        run_apm_command("install", project_dir, timeout=180)

        # deps list should show both correctly
        result = run_apm_command("deps list", project_dir)
        assert result.returncode == 0

        # Check sources are correct
        assert "github" in result.stdout.lower()
        assert "azure-devops" in result.stdout.lower()

        # No orphans
        lines = result.stdout.lower()
        # Either no orphan warning or explicitly 0 orphaned
        assert "orphaned" not in lines or "0 orphan" in lines

    def test_mixed_prune_no_false_positives(self, tmp_path):
        """prune should handle both GitHub and ADO packages correctly."""
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        # Initialize with both dependencies
        apm_yml = project_dir / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": [self.GITHUB_PACKAGE, self.ADO_PACKAGE], "mcp": []},
                }
            )
        )

        run_apm_command("install", project_dir, timeout=180)

        # prune should report clean
        result = run_apm_command("prune --dry-run", project_dir)
        assert result.returncode == 0
        assert "No orphaned packages found" in result.stdout or "clean" in result.stdout.lower()
