"""Integration tests for auto-integration feature."""

import os  # noqa: F401
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch  # noqa: F401

import pytest

from apm_cli.integration import PromptIntegrator
from apm_cli.models.apm_package import APMPackage, GitReferenceType, PackageInfo, ResolvedReference


@pytest.mark.integration
class TestAutoIntegrationEndToEnd:
    """End-to-end tests for auto-integration during package install."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

        # Create .github directory
        self.github_dir = self.project_root / ".github"
        self.github_dir.mkdir()

    def teardown_method(self):
        """Clean up after tests."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_mock_package(self, package_name: str, prompts: list) -> Path:
        """Create a mock package with prompts."""
        package_dir = self.project_root / "apm_modules" / package_name
        package_dir.mkdir(parents=True)

        # Create apm.yml
        apm_yml = package_dir / "apm.yml"
        apm_yml.write_text(f"name: {package_name}\nversion: 1.0.0\n")

        # Create prompt files
        for prompt_name in prompts:
            prompt_file = package_dir / f"{prompt_name}.prompt.md"
            prompt_file.write_text(f"# {prompt_name}\n\nTest content")

        return package_dir

    def test_full_integration_workflow(self):
        """Test complete integration workflow."""
        # Create mock package
        package_dir = self.create_mock_package("test-package", ["workflow1", "workflow2"])

        # Create PackageInfo
        package = APMPackage(name="test-package", version="1.0.0", package_path=package_dir)
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123def456",
            ref_name="main",
        )
        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
        )

        # Run integration (auto-integration is always enabled now)
        integrator = PromptIntegrator()
        result = integrator.integrate_package_prompts(package_info, self.project_root)

        # Verify results
        assert result.files_integrated == 2

        # Check files exist (clean naming, no suffix)
        prompts_dir = self.project_root / ".github" / "prompts"
        assert (prompts_dir / "workflow1.prompt.md").exists()
        assert (prompts_dir / "workflow2.prompt.md").exists()

        # Verify content is copied verbatim (no metadata injection)
        content1 = (prompts_dir / "workflow1.prompt.md").read_text()
        assert "# workflow1" in content1
