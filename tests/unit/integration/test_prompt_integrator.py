"""Tests for prompt integration functionality."""

import os  # noqa: F401
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch  # noqa: F401

import pytest  # noqa: F401

from apm_cli.integration import PromptIntegrator
from apm_cli.models.apm_package import APMPackage, GitReferenceType, PackageInfo, ResolvedReference


class TestPromptIntegrator:
    """Test prompt integration logic."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = PromptIntegrator()

    def teardown_method(self):
        """Clean up after tests."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_should_integrate_always_returns_true(self):
        """Test integration is always enabled (zero-config approach)."""
        # No .github/ directory needed
        assert self.integrator.should_integrate(self.project_root) == True  # noqa: E712

        # Even with .github/ present
        github_dir = self.project_root / ".github"
        github_dir.mkdir()
        assert self.integrator.should_integrate(self.project_root) == True  # noqa: E712

    def test_find_prompt_files_in_root(self):
        """Test finding .prompt.md files in package root."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()

        # Create test prompt files
        (package_dir / "test1.prompt.md").write_text("# Test 1")
        (package_dir / "test2.prompt.md").write_text("# Test 2")
        (package_dir / "readme.md").write_text("# Readme")  # Should not be found

        prompts = self.integrator.find_prompt_files(package_dir)
        assert len(prompts) == 2
        assert all(p.name.endswith(".prompt.md") for p in prompts)

    def test_find_prompt_files_in_apm_prompts(self):
        """Test finding .prompt.md files in .apm/prompts/."""
        package_dir = self.project_root / "package"
        apm_prompts = package_dir / ".apm" / "prompts"
        apm_prompts.mkdir(parents=True)

        (apm_prompts / "workflow.prompt.md").write_text("# Workflow")

        prompts = self.integrator.find_prompt_files(package_dir)
        assert len(prompts) == 1
        assert prompts[0].name == "workflow.prompt.md"

    def test_copy_prompt_verbatim(self):
        """Test copying prompt file verbatim without metadata injection."""
        source = self.project_root / "source.prompt.md"
        target = self.project_root / "target.prompt.md"

        source_content = "# Test Prompt\n\nSome prompt content."
        source.write_text(source_content)

        self.integrator.copy_prompt(source, target)

        target_content = target.read_text()
        assert target_content == source_content
        # No metadata injected
        assert "apm:" not in target_content
        assert "version:" not in target_content

    def test_copy_prompt_preserves_existing_frontmatter(self):
        """Test that existing frontmatter in source is preserved verbatim."""
        source = self.project_root / "source.prompt.md"
        target = self.project_root / "target.prompt.md"

        source_content = """---
title: My Prompt
description: A test prompt
---

# Test Prompt

Some prompt content."""
        source.write_text(source_content)

        self.integrator.copy_prompt(source, target)

        target_content = target.read_text()
        assert target_content == source_content
        # No APM metadata added
        assert "apm:" not in target_content

    def test_get_target_filename(self):
        """Test target filename generation with clean naming (no suffix)."""
        source = Path("/package/accessibility-audit.prompt.md")
        package_name = "microsoft/apm-sample-package"

        target = self.integrator.get_target_filename(source, package_name)
        # Clean naming: original filename preserved
        assert target == "accessibility-audit.prompt.md"

    def test_integrate_package_prompts_creates_directory(self):
        """Test that integration creates .github/prompts/ if missing."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "test.prompt.md").write_text("# Test")

        github_dir = self.project_root / ".github"
        github_dir.mkdir()

        package = APMPackage(name="test-pkg", version="1.0.0", package_path=package_dir)
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        )
        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
        )

        result = self.integrator.integrate_package_prompts(package_info, self.project_root)

        assert result.files_integrated == 1
        assert (self.project_root / ".github" / "prompts").exists()

    def test_integrate_always_overwrites_existing_files(self):
        """Test that integration always overwrites existing files."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        (package_dir / "test.prompt.md").write_text("# New Content")

        github_prompts = self.project_root / ".github" / "prompts"
        github_prompts.mkdir(parents=True)

        # Pre-create the target file with old content
        (github_prompts / "test.prompt.md").write_text("# Old Content")

        package = APMPackage(
            name="test-pkg",
            version="1.0.0",
            package_path=package_dir,
            source="github.com/test/repo",
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        )
        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=resolved_ref,
            installed_at="2024-01-01T00:00:00",
        )

        result = self.integrator.integrate_package_prompts(package_info, self.project_root)

        # Always counts as integrated (overwrite)
        assert result.files_integrated == 1
        assert result.files_updated == 0
        assert result.files_skipped == 0

        # Verify content was overwritten
        content = (github_prompts / "test.prompt.md").read_text()
        assert "# New Content" in content
        assert "# Old Content" not in content

    def test_integrate_copies_verbatim_no_metadata(self):
        """Test that integration copies files verbatim without metadata."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()
        source_content = "# Test Content\n\nSome content here."
        (package_dir / "test.prompt.md").write_text(source_content)

        github_prompts = self.project_root / ".github" / "prompts"
        github_prompts.mkdir(parents=True)

        package = APMPackage(
            name="test-pkg",
            version="1.0.0",
            package_path=package_dir,
            source="github.com/test/repo",
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        )
        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=resolved_ref,
            installed_at="2024-11-13T10:00:00",
        )

        result = self.integrator.integrate_package_prompts(package_info, self.project_root)

        assert result.files_integrated == 1
        assert result.files_updated == 0
        assert result.files_skipped == 0

        # Verify file is copied verbatim - no metadata
        target_file = github_prompts / "test.prompt.md"
        content = target_file.read_text()
        assert content == source_content
        assert "apm:" not in content
        assert "---" not in content

    def test_integrate_multiple_files(self):
        """Test integration with multiple prompt files."""
        package_dir = self.project_root / "package"
        package_dir.mkdir()

        (package_dir / "file1.prompt.md").write_text("# File 1")
        (package_dir / "file2.prompt.md").write_text("# File 2")
        (package_dir / "file3.prompt.md").write_text("# File 3")

        github_prompts = self.project_root / ".github" / "prompts"
        github_prompts.mkdir(parents=True)

        # Pre-create one existing file to test overwrite
        (github_prompts / "file2.prompt.md").write_text("# Old File 2")

        package = APMPackage(
            name="test-pkg",
            version="2.0.0",
            package_path=package_dir,
            source="github.com/test/repo",
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="def456",
            ref_name="main",
        )
        package_info = PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=resolved_ref,
            installed_at="2024-11-13T11:00:00",
        )

        result = self.integrator.integrate_package_prompts(package_info, self.project_root)

        # All 3 files are integrated (always overwrite)
        assert result.files_integrated == 3
        assert result.files_updated == 0
        assert result.files_skipped == 0

        # Verify all files exist with correct content
        assert (github_prompts / "file1.prompt.md").exists()
        assert (github_prompts / "file2.prompt.md").read_text() == "# File 2"
        assert (github_prompts / "file3.prompt.md").exists()

    # ========== Sync Integration Tests (Nuke-and-Regenerate) ==========

    def test_sync_integration_removes_all_apm_files(self):
        """Test that sync removes all *-apm.prompt.md files."""
        github_prompts = self.project_root / ".github" / "prompts"
        github_prompts.mkdir(parents=True)

        # Create multiple APM-managed prompt files
        (github_prompts / "design-review-apm.prompt.md").write_text("# Design Review")
        (github_prompts / "compliance-audit-apm.prompt.md").write_text("# Compliance Audit")

        apm_package = Mock()

        result = self.integrator.sync_integration(apm_package, self.project_root)

        assert result["files_removed"] == 2
        assert not (github_prompts / "design-review-apm.prompt.md").exists()
        assert not (github_prompts / "compliance-audit-apm.prompt.md").exists()

    def test_sync_integration_preserves_non_apm_files(self):
        """Test that sync does not remove files without -apm suffix."""
        github_prompts = self.project_root / ".github" / "prompts"
        github_prompts.mkdir(parents=True)

        # Create both APM and non-APM files
        (github_prompts / "test-apm.prompt.md").write_text("# APM prompt")
        (github_prompts / "my-custom.prompt.md").write_text("# Custom prompt")
        (github_prompts / "readme.md").write_text("# Readme")

        apm_package = Mock()

        result = self.integrator.sync_integration(apm_package, self.project_root)

        assert result["files_removed"] == 1
        assert not (github_prompts / "test-apm.prompt.md").exists()
        assert (github_prompts / "my-custom.prompt.md").exists()
        assert (github_prompts / "readme.md").exists()

    def test_sync_integration_handles_missing_prompts_dir(self):
        """Test that sync gracefully handles missing .github/prompts/ directory."""
        apm_package = Mock()

        # Should not raise exception
        result = self.integrator.sync_integration(apm_package, self.project_root)

        assert result["files_removed"] == 0
        assert result["errors"] == 0

    def test_sync_integration_ignores_apm_package_param(self):
        """Test that sync removes all APM files regardless of installed packages."""
        github_prompts = self.project_root / ".github" / "prompts"
        github_prompts.mkdir(parents=True)

        (github_prompts / "design-review-apm.prompt.md").write_text("# Design Review")

        # Even with matching dependencies, sync removes everything
        from apm_cli.models.apm_package import DependencyReference

        apm_package = Mock()
        apm_package.get_apm_dependencies.return_value = [
            DependencyReference(repo_url="microsoft/apm-sample-package", reference="main")
        ]

        result = self.integrator.sync_integration(apm_package, self.project_root)

        assert result["files_removed"] == 1
        assert not (github_prompts / "design-review-apm.prompt.md").exists()


class TestPromptSuffixPattern:
    """Test clean naming pattern edge cases."""

    def setup_method(self):
        """Set up test fixtures."""
        self.integrator = PromptIntegrator()

    def test_clean_naming_simple_filename(self):
        """Test clean naming with simple filename."""
        source = Path("test.prompt.md")
        result = self.integrator.get_target_filename(source, "pkg")
        assert result == "test.prompt.md"

    def test_clean_naming_hyphenated_filename(self):
        """Test clean naming with hyphenated filename."""
        source = Path("design-review.prompt.md")
        result = self.integrator.get_target_filename(source, "pkg")
        assert result == "design-review.prompt.md"

    def test_clean_naming_multi_part_filename(self):
        """Test clean naming with multi-part filename."""
        source = Path("accessibility-audit-wcag.prompt.md")
        result = self.integrator.get_target_filename(source, "pkg")
        assert result == "accessibility-audit-wcag.prompt.md"

    def test_clean_naming_preserves_original_name(self):
        """Test that original filename structure is preserved."""
        source = Path("my_custom-workflow.prompt.md")
        result = self.integrator.get_target_filename(source, "pkg")
        assert result == "my_custom-workflow.prompt.md"

    def test_gitignore_pattern_matches_suffix_files(self):
        """Test that gitignore pattern matches -apm suffix files."""
        import fnmatch

        pattern = "*-apm.prompt.md"

        # Should match
        assert fnmatch.fnmatch("design-review-apm.prompt.md", pattern)
        assert fnmatch.fnmatch("test-apm.prompt.md", pattern)
        assert fnmatch.fnmatch("a-b-c-apm.prompt.md", pattern)

        # Should NOT match
        assert not fnmatch.fnmatch("design-review.prompt.md", pattern)
        assert not fnmatch.fnmatch("apm.prompt.md", pattern)
        assert not fnmatch.fnmatch("@design-review.prompt.md", pattern)
