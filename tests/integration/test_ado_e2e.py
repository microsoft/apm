"""
E2E tests for Azure DevOps package support.

These tests require explicit ``-m live`` selection and the ADO_APM_PAT
environment variable, then make real network calls to an Azure DevOps
acceptance fixture.
"""

import os
import shlex
import subprocess
import tempfile  # noqa: F401
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
import yaml

pytestmark = [
    pytest.mark.live,
    pytest.mark.requires_ado_pat,
]

_DEFAULT_ADO_TEST_REPO = "dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules"
_ADO_TEST_REPO = os.environ.get("APM_TEST_ADO_REPO", _DEFAULT_ADO_TEST_REPO).rstrip("/")


def _ado_repo_coordinates(repo: str) -> tuple[str, str, str]:
    """Return the org, project, and repository from an ADO Git URL."""
    parsed = urlparse(repo if "://" in repo else f"https://{repo}")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
        git_index = parts.index("_git")
        if parsed.hostname != "dev.azure.com" or git_index != 2:
            raise ValueError
        return parts[0], parts[1], parts[git_index + 1].removesuffix(".git")
    except (IndexError, ValueError) as error:
        raise ValueError(
            "APM_TEST_ADO_REPO must use dev.azure.com/org/project/_git/repo"
        ) from error


ADO_ORG, ADO_PROJECT, ADO_REPO = _ado_repo_coordinates(_ADO_TEST_REPO)
_ADO_VIRTUAL_PACKAGE = f"{_ADO_TEST_REPO}/gdpr-assessment.prompt.md"


def run_apm_command(
    apm_binary_path: Path, cmd: str, cwd: Path, timeout: int = 60
) -> subprocess.CompletedProcess:
    """Run an APM CLI command and return the result."""
    result = subprocess.run(
        [str(apm_binary_path), *shlex.split(cmd)],
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
    ADO_TEST_REPO = _ADO_TEST_REPO

    def test_install_ado_package(self, tmp_path, apm_binary_path: Path):
        """Install a real ADO package and verify directory structure."""
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        # Initialize project
        apm_yml = project_dir / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "target": "copilot",
                    "dependencies": {"apm": [], "mcp": []},
                }
            )
        )

        # Install ADO package
        result = run_apm_command(apm_binary_path, f'install "{self.ADO_TEST_REPO}"', project_dir)
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Verify 3-level directory structure
        apm_modules = project_dir / "apm_modules"
        assert apm_modules.exists(), "apm_modules/ not created"

        expected_path = apm_modules / ADO_ORG / ADO_PROJECT / ADO_REPO
        assert expected_path.exists(), f"Expected 3-level path not found: {expected_path}"

        # Verify package content
        assert (expected_path / "apm.yml").exists() or (expected_path / ".apm").exists()


class TestADODepsAndPrune:
    """Test deps list and prune with ADO packages."""

    ADO_TEST_REPO = _ADO_TEST_REPO

    def test_deps_list_shows_correct_path(self, tmp_path, apm_binary_path: Path):
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
                    "target": "copilot",
                    "dependencies": {"apm": [self.ADO_TEST_REPO], "mcp": []},
                }
            )
        )

        run_apm_command(apm_binary_path, "install", project_dir, timeout=120)

        # Run deps list
        result = run_apm_command(apm_binary_path, "deps list", project_dir)
        assert result.returncode == 0, f"deps list failed: {result.stderr}"

        # Should show 3-level path (may be truncated with ...) and azure-devops source
        assert f"{ADO_ORG}/{ADO_PROJECT}" in result.stdout
        assert "azure-devops" in result.stdout
        assert "orphaned" not in result.stdout.lower()

    def test_prune_no_false_positives(self, tmp_path, apm_binary_path: Path):
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
                    "target": "copilot",
                    "dependencies": {"apm": [self.ADO_TEST_REPO], "mcp": []},
                }
            )
        )

        run_apm_command(apm_binary_path, "install", project_dir, timeout=120)

        # Run prune --dry-run
        result = run_apm_command(apm_binary_path, "prune --dry-run", project_dir)
        assert result.returncode == 0, f"prune failed: {result.stderr}"

        # Should report clean
        assert "No orphaned packages found" in result.stdout or "clean" in result.stdout.lower()


class TestADOCompile:
    """Test compilation with ADO dependencies."""

    ADO_TEST_REPO = _ADO_TEST_REPO

    def test_compile_generates_agents_md(self, tmp_path, apm_binary_path: Path):
        """Compile should keep ADO Copilot instructions outside AGENTS.md."""
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        # Initialize and install
        apm_yml = project_dir / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "target": "copilot",
                    "dependencies": {"apm": [self.ADO_TEST_REPO], "mcp": []},
                }
            )
        )

        run_apm_command(apm_binary_path, "install", project_dir, timeout=120)

        # Run compile
        result = run_apm_command(apm_binary_path, "compile --verbose", project_dir)
        assert result.returncode == 0, f"compile failed: {result.stderr}"

        # Should not show orphan warnings
        assert "orphan" not in result.stdout.lower() or "0 orphan" in result.stdout.lower()

        # Copilot compile suppresses empty AGENTS.md shells when installed
        # instructions already live under .github/instructions/.
        agents_md = project_dir / "AGENTS.md"
        github_instructions = project_dir / ".github" / "instructions"
        assert not agents_md.exists(), (
            "AGENTS.md should not be generated for Copilot-only instructions"
        )
        assert github_instructions.is_dir(), ".github/instructions not generated"
        assert list(github_instructions.glob("*.md")), "No Copilot instruction files generated"


class TestADOVirtualPackage:
    """Test virtual package (single file) installation from ADO."""

    ADO_VIRTUAL_PACKAGE = _ADO_VIRTUAL_PACKAGE

    def test_install_virtual_package(self, tmp_path, apm_binary_path: Path):
        """Install a single file (virtual package) from ADO repo."""
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        # Initialize
        apm_yml = project_dir / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "target": "copilot",
                    "dependencies": {"apm": [], "mcp": []},
                }
            )
        )

        # Install virtual package
        result = run_apm_command(
            apm_binary_path, f'install "{self.ADO_VIRTUAL_PACKAGE}"', project_dir, timeout=120
        )
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Verify 3-level virtual package path
        apm_modules = project_dir / "apm_modules"
        expected_path = apm_modules / ADO_ORG / ADO_PROJECT / f"{ADO_REPO}-gdpr-assessment"
        assert expected_path.exists(), f"Expected virtual package path not found: {expected_path}"

    def test_virtual_package_not_orphaned(self, tmp_path, apm_binary_path: Path):
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
                    "target": "copilot",
                    "dependencies": {"apm": [self.ADO_VIRTUAL_PACKAGE], "mcp": []},
                }
            )
        )

        run_apm_command(apm_binary_path, "install", project_dir, timeout=120)

        # deps list should show it correctly
        result = run_apm_command(apm_binary_path, "deps list", project_dir)
        assert "orphaned" not in result.stdout.lower() or "0 orphan" in result.stdout.lower()

        # prune should report clean
        result = run_apm_command(apm_binary_path, "prune --dry-run", project_dir)
        assert "No orphaned packages found" in result.stdout or "clean" in result.stdout.lower()


class TestMixedDependencies:
    """Test mixed GitHub and ADO dependencies."""

    GITHUB_PACKAGE = "microsoft/apm-sample-package"
    ADO_PACKAGE = _ADO_TEST_REPO

    def test_mixed_install(self, tmp_path, apm_binary_path: Path):
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
                    "target": "copilot",
                    "dependencies": {"apm": [self.GITHUB_PACKAGE, self.ADO_PACKAGE], "mcp": []},
                }
            )
        )

        # Install all
        result = run_apm_command(apm_binary_path, "install", project_dir, timeout=180)
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Verify both structures
        apm_modules = project_dir / "apm_modules"

        # GitHub: 2-level
        github_path = apm_modules / "microsoft" / "apm-sample-package"
        assert github_path.exists(), f"GitHub package not found: {github_path}"

        # ADO: 3-level
        ado_path = apm_modules / ADO_ORG / ADO_PROJECT / ADO_REPO
        assert ado_path.exists(), f"ADO package not found: {ado_path}"

    def test_mixed_deps_list(self, tmp_path, apm_binary_path: Path):
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
                    "target": "copilot",
                    "dependencies": {"apm": [self.GITHUB_PACKAGE, self.ADO_PACKAGE], "mcp": []},
                }
            )
        )

        run_apm_command(apm_binary_path, "install", project_dir, timeout=180)

        # deps list should show both correctly
        result = run_apm_command(apm_binary_path, "deps list", project_dir)
        assert result.returncode == 0

        # Check sources are correct
        assert "github" in result.stdout.lower()
        assert "azure-devops" in result.stdout.lower()

        # No orphans
        lines = result.stdout.lower()
        # Either no orphan warning or explicitly 0 orphaned
        assert "orphaned" not in lines or "0 orphan" in lines

    def test_mixed_prune_no_false_positives(self, tmp_path, apm_binary_path: Path):
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
                    "target": "copilot",
                    "dependencies": {"apm": [self.GITHUB_PACKAGE, self.ADO_PACKAGE], "mcp": []},
                }
            )
        )

        run_apm_command(apm_binary_path, "install", project_dir, timeout=180)

        # prune should report clean
        result = run_apm_command(apm_binary_path, "prune --dry-run", project_dir)
        assert result.returncode == 0
        assert "No orphaned packages found" in result.stdout or "clean" in result.stdout.lower()
