"""Integration tests for prompt collision handling."""

import os
from pathlib import Path

import pytest

from apm_cli.core.script_runner import ScriptRunner


@pytest.fixture(autouse=True)
def preserve_cwd():
    """Fixture to preserve and restore CWD for all tests."""
    try:
        original = os.getcwd()
    except (FileNotFoundError, OSError):
        original = Path(__file__).parent.parent
        os.chdir(original)

    yield

    try:
        os.chdir(original)
    except (FileNotFoundError, OSError):
        try:
            os.chdir(Path(__file__).parent.parent)
        except (FileNotFoundError, OSError):
            os.chdir(Path.home())


class TestCollisionIntegration:
    """Integration tests for real-world collision scenarios."""

    def test_collision_detection_with_helpful_error(self, tmp_path):
        """Test that collision detection provides helpful error message.

        This simulates the real scenario where a user has installed:
        - owner/test-repo/prompts/code-review.prompt.md
        - acme/dev-tools/prompts/code-review.prompt.md

        And tries to run: apm run code-review
        """
        # Setup: Create realistic virtual package structure
        github_pkg = (
            tmp_path / "apm_modules" / "github" / "test-repo-code-review" / ".apm" / "prompts"
        )
        github_pkg.mkdir(parents=True)
        (github_pkg / "code-review.prompt.md").write_text("---\n---\nGitHub Copilot code review")

        acme_pkg = tmp_path / "apm_modules" / "acme" / "dev-tools-code-review" / ".apm" / "prompts"
        acme_pkg.mkdir(parents=True)
        (acme_pkg / "code-review.prompt.md").write_text("---\n---\nAcme dev tools code review")

        os.chdir(tmp_path)
        runner = ScriptRunner()

        # Attempt to discover without qualification
        with pytest.raises(RuntimeError) as exc_info:
            runner._discover_prompt_file("code-review")

        error_msg = str(exc_info.value)

        # Verify error message is helpful
        assert "Multiple prompts found for 'code-review'" in error_msg
        assert "github/test-repo-code-review" in error_msg
        assert "acme/dev-tools-code-review" in error_msg
        assert "Please specify using qualified path" in error_msg
        assert "apm run" in error_msg
        assert "explicit script to apm.yml" in error_msg

    def test_qualified_path_resolves_collision(self, tmp_path):
        """Test that using qualified path resolves collision.

        User can disambiguate by using:
        - apm run github/test-repo-code-review/code-review
        - apm run acme/dev-tools-code-review/code-review
        """
        # Setup
        github_pkg = (
            tmp_path / "apm_modules" / "github" / "test-repo-code-review" / ".apm" / "prompts"
        )
        github_pkg.mkdir(parents=True)
        (github_pkg / "code-review.prompt.md").write_text("---\n---\nGitHub version")

        acme_pkg = tmp_path / "apm_modules" / "acme" / "dev-tools-code-review" / ".apm" / "prompts"
        acme_pkg.mkdir(parents=True)
        (acme_pkg / "code-review.prompt.md").write_text("---\n---\nAcme version")

        os.chdir(tmp_path)
        runner = ScriptRunner()

        # Test GitHub qualified path
        github_result = runner._discover_prompt_file("github/test-repo-code-review/code-review")
        assert github_result is not None
        assert "github" in str(github_result)
        assert "test-repo-code-review" in str(github_result)

        # Test Acme qualified path
        acme_result = runner._discover_prompt_file("acme/dev-tools-code-review/code-review")
        assert acme_result is not None
        assert "acme" in str(acme_result)
        assert "dev-tools-code-review" in str(acme_result)

        # Verify they're different files
        assert str(github_result) != str(acme_result)

    def test_no_collision_with_single_dependency(self, tmp_path):
        """Test that single dependency works without requiring qualified path."""
        # Setup: Only one package with the prompt
        github_pkg = (
            tmp_path / "apm_modules" / "github" / "test-repo-code-review" / ".apm" / "prompts"
        )
        github_pkg.mkdir(parents=True)
        (github_pkg / "code-review.prompt.md").write_text("---\n---\nGitHub version")

        os.chdir(tmp_path)
        runner = ScriptRunner()

        # Should work with simple name (no collision)
        result = runner._discover_prompt_file("code-review")
        assert result is not None
        assert result.name == "code-review.prompt.md"

    def test_local_overrides_all_dependencies_no_collision(self, tmp_path):
        """Test that local prompt always wins without collision detection."""
        # Setup: Local + two dependencies with same name
        (tmp_path / "code-review.prompt.md").write_text("---\n---\nLocal version")

        github_pkg = (
            tmp_path / "apm_modules" / "github" / "test-repo-code-review" / ".apm" / "prompts"
        )
        github_pkg.mkdir(parents=True)
        (github_pkg / "code-review.prompt.md").write_text("---\n---\nGitHub version")

        acme_pkg = tmp_path / "apm_modules" / "acme" / "dev-tools-code-review" / ".apm" / "prompts"
        acme_pkg.mkdir(parents=True)
        (acme_pkg / "code-review.prompt.md").write_text("---\n---\nAcme version")

        os.chdir(tmp_path)
        runner = ScriptRunner()

        # Local should win without collision error
        result = runner._discover_prompt_file("code-review")
        assert result is not None
        assert "apm_modules" not in str(result)
        assert str(result) == "code-review.prompt.md"
