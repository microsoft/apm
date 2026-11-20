"""Tests for prompt integration functionality (pure logic cases)."""

import os  # noqa: F401
from pathlib import Path
from unittest.mock import Mock, patch  # noqa: F401

import pytest

from apm_cli.integration import PromptIntegrator

pytestmark = pytest.mark.unit


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
