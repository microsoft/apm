"""Unit tests for runnable prompts feature."""

import os
import shutil  # noqa: F401
import tempfile  # noqa: F401
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch  # noqa: F401

import pytest

from apm_cli.core.script_runner import ScriptRunner


@pytest.fixture(autouse=True)
def preserve_cwd():
    """Fixture to preserve and restore CWD for all tests."""
    try:
        original = os.getcwd()
    except (FileNotFoundError, OSError):
        # If we can't get CWD, use a safe default
        original = Path(__file__).parent.parent
        os.chdir(original)

    yield

    try:
        os.chdir(original)
    except (FileNotFoundError, OSError):
        # If original dir was deleted, change to project root
        try:
            os.chdir(Path(__file__).parent.parent)
        except (FileNotFoundError, OSError):
            # Last resort: home directory
            os.chdir(Path.home())


class TestPromptDiscovery:
    """Test prompt file discovery logic."""

    def test_discover_prompt_file_local_root(self, tmp_path):
        """Test discovery of prompt in project root."""
        # Setup: Create temp prompt file
        prompt_file = tmp_path / "test.prompt.md"
        prompt_file.write_text("---\n---\nTest prompt")

        # Change to temp directory
        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("test")

        assert result is not None
        assert result.name == "test.prompt.md"
        assert result.exists()

    def test_discover_prompt_file_local_apm_dir(self, tmp_path):
        """Test discovery in .apm/prompts/."""
        # Setup: Create .apm/prompts/test.prompt.md
        prompts_dir = tmp_path / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)
        prompt_file = prompts_dir / "test.prompt.md"
        prompt_file.write_text("---\n---\nTest prompt")

        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("test")

        assert result is not None
        assert result.name == "test.prompt.md"
        assert ".apm/prompts" in str(result).replace("\\", "/")

    def test_discover_prompt_file_github_dir(self, tmp_path):
        """Test discovery in .github/prompts/."""
        # Setup: Create .github/prompts/test.prompt.md
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        prompt_file = prompts_dir / "test.prompt.md"
        prompt_file.write_text("---\n---\nTest prompt")

        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("test")

        assert result is not None
        assert result.name == "test.prompt.md"
        assert ".github/prompts" in str(result).replace("\\", "/")

    def test_discover_prompt_file_dependencies(self, tmp_path):
        """Test discovery in apm_modules/."""
        # Setup: Create apm_modules/org/pkg/.apm/prompts/test.prompt.md
        dep_dir = tmp_path / "apm_modules" / "org" / "pkg" / ".apm" / "prompts"
        dep_dir.mkdir(parents=True)
        prompt_file = dep_dir / "test.prompt.md"
        prompt_file.write_text("---\n---\nTest prompt from dependency")

        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("test")

        assert result is not None
        assert result.name == "test.prompt.md"
        assert "apm_modules" in str(result)

    def test_discover_prompt_file_not_found(self, tmp_path):
        """Test behavior when prompt not found."""
        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("nonexistent")

        assert result is None

    def test_discover_prompt_precedence(self, tmp_path):
        """Test that local prompts take precedence over dependencies."""
        # Setup: Create both local and dependency versions
        local_prompt = tmp_path / "test.prompt.md"
        local_prompt.write_text("---\n---\nLocal version")

        dep_dir = tmp_path / "apm_modules" / "org" / "pkg" / ".apm" / "prompts"
        dep_dir.mkdir(parents=True)
        dep_prompt = dep_dir / "test.prompt.md"
        dep_prompt.write_text("---\n---\nDependency version")

        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("test")

        assert result is not None
        # Check that it's the local version (not in apm_modules)
        assert "apm_modules" not in str(result)
        assert result.name == "test.prompt.md"

    def test_discover_with_extension(self, tmp_path):
        """Test discovery when name already includes .prompt.md extension."""
        prompt_file = tmp_path / "test.prompt.md"
        prompt_file.write_text("---\n---\nTest prompt")

        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("test.prompt.md")

        assert result is not None
        assert result.name == "test.prompt.md"

    def test_discover_multiple_dependencies_same_filename(self, tmp_path):
        """Test collision detection when multiple dependencies have same filename.

        This tests the name collision scenario where two different repos
        have prompts with the same filename. The implementation now detects
        this and raises an error with helpful disambiguation options.
        """
        # Setup: Create two different packages with same prompt filename
        dep1_dir = (
            tmp_path / "apm_modules" / "github" / "test-repo-code-review" / ".apm" / "prompts"
        )
        dep1_dir.mkdir(parents=True)
        dep1_prompt = dep1_dir / "code-review.prompt.md"
        dep1_prompt.write_text("---\n---\nGitHub Copilot code review")

        dep2_dir = tmp_path / "apm_modules" / "acme" / "dev-tools-code-review" / ".apm" / "prompts"
        dep2_dir.mkdir(parents=True)
        dep2_prompt = dep2_dir / "code-review.prompt.md"
        dep2_prompt.write_text("---\n---\nAcme dev tools code review")

        os.chdir(tmp_path)
        runner = ScriptRunner()

        # Should raise error about collision
        with pytest.raises(RuntimeError) as exc_info:
            runner._discover_prompt_file("code-review")

        error_msg = str(exc_info.value)
        assert "Multiple prompts found for 'code-review'" in error_msg
        assert "github/test-repo-code-review" in error_msg
        assert "acme/dev-tools-code-review" in error_msg
        assert "apm run" in error_msg
        assert "qualified path" in error_msg

    def test_discover_collision_local_wins(self, tmp_path):
        """Test that local prompt takes precedence even with name collisions in dependencies."""
        # Setup: Create local prompt and two dependency prompts with same name
        local_prompt = tmp_path / "code-review.prompt.md"
        local_prompt.write_text("---\n---\nLocal code review")

        dep1_dir = (
            tmp_path / "apm_modules" / "github" / "test-repo-code-review" / ".apm" / "prompts"
        )
        dep1_dir.mkdir(parents=True)
        dep1_prompt = dep1_dir / "code-review.prompt.md"
        dep1_prompt.write_text("---\n---\nGitHub Copilot code review")

        dep2_dir = tmp_path / "apm_modules" / "acme" / "dev-tools-code-review" / ".apm" / "prompts"
        dep2_dir.mkdir(parents=True)
        dep2_prompt = dep2_dir / "code-review.prompt.md"
        dep2_prompt.write_text("---\n---\nAcme dev tools code review")

        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("code-review")

        # Local should always win
        assert result is not None
        assert result.name == "code-review.prompt.md"
        assert "apm_modules" not in str(result)
        assert str(result) == "code-review.prompt.md"

    def test_discover_virtual_package_naming_convention(self, tmp_path):
        """Test discovery works with virtual package directory naming.

        Virtual packages use format: {repo-name}-{filename-without-extension}
        Example: github/test-repo/prompts/architecture-blueprint-generator.prompt.md
        → Directory: github/test-repo-architecture-blueprint-generator/
        """
        # Setup: Create virtual package structure as it would be installed
        virt_pkg_dir = (
            tmp_path
            / "apm_modules"
            / "github"
            / "test-repo-architecture-blueprint-generator"
            / ".apm"
            / "prompts"
        )
        virt_pkg_dir.mkdir(parents=True)
        prompt_file = virt_pkg_dir / "architecture-blueprint-generator.prompt.md"
        prompt_file.write_text("---\n---\nArchitecture blueprint generator")

        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("architecture-blueprint-generator")

        assert result is not None
        assert result.name == "architecture-blueprint-generator.prompt.md"
        assert "test-repo-architecture-blueprint-generator" in str(result)

    def test_discover_multiple_virtual_packages_different_repos_same_filename(self, tmp_path):
        """Test collision between virtual packages from different repos with same filename.

        This is the critical collision scenario:
        - github/test-repo/prompts/code-review.prompt.md
        - acme/dev-tools/prompts/code-review.prompt.md

        Both install as virtual packages with different directory names but same prompt filename.
        Now properly detects collision and provides disambiguation.
        """
        # Setup: Two virtual packages from different repos
        github_pkg = (
            tmp_path / "apm_modules" / "github" / "test-repo-code-review" / ".apm" / "prompts"
        )
        github_pkg.mkdir(parents=True)
        github_prompt = github_pkg / "code-review.prompt.md"
        github_prompt.write_text("---\n---\nGitHub version")

        acme_pkg = tmp_path / "apm_modules" / "acme" / "dev-tools-code-review" / ".apm" / "prompts"
        acme_pkg.mkdir(parents=True)
        acme_prompt = acme_pkg / "code-review.prompt.md"
        acme_prompt.write_text("---\n---\nAcme version")

        os.chdir(tmp_path)
        runner = ScriptRunner()

        # Should detect collision and raise error
        with pytest.raises(RuntimeError) as exc_info:
            runner._discover_prompt_file("code-review")

        error_msg = str(exc_info.value)
        assert "Multiple prompts found" in error_msg
        assert "github/test-repo-code-review" in error_msg
        assert "acme/dev-tools-code-review" in error_msg

    def test_discover_qualified_path_github(self, tmp_path):
        """Test discovery using qualified path for GitHub package."""
        # Setup: Two virtual packages with same prompt name
        github_pkg = (
            tmp_path / "apm_modules" / "github" / "test-repo-code-review" / ".apm" / "prompts"
        )
        github_pkg.mkdir(parents=True)
        github_prompt = github_pkg / "code-review.prompt.md"
        github_prompt.write_text("---\n---\nGitHub version")

        acme_pkg = tmp_path / "apm_modules" / "acme" / "dev-tools-code-review" / ".apm" / "prompts"
        acme_pkg.mkdir(parents=True)
        acme_prompt = acme_pkg / "code-review.prompt.md"
        acme_prompt.write_text("---\n---\nAcme version")

        os.chdir(tmp_path)
        runner = ScriptRunner()

        # Use qualified path to specify which one
        result = runner._discover_prompt_file("github/test-repo-code-review/code-review")

        assert result is not None
        assert result.name == "code-review.prompt.md"
        assert "github" in str(result)
        assert "test-repo-code-review" in str(result)

    def test_discover_qualified_path_acme(self, tmp_path):
        """Test discovery using qualified path for Acme package."""
        # Setup: Two virtual packages with same prompt name
        github_pkg = (
            tmp_path / "apm_modules" / "github" / "test-repo-code-review" / ".apm" / "prompts"
        )
        github_pkg.mkdir(parents=True)
        github_prompt = github_pkg / "code-review.prompt.md"
        github_prompt.write_text("---\n---\nGitHub version")

        acme_pkg = tmp_path / "apm_modules" / "acme" / "dev-tools-code-review" / ".apm" / "prompts"
        acme_pkg.mkdir(parents=True)
        acme_prompt = acme_pkg / "code-review.prompt.md"
        acme_prompt.write_text("---\n---\nAcme version")

        os.chdir(tmp_path)
        runner = ScriptRunner()

        # Use qualified path to specify the Acme version
        result = runner._discover_prompt_file("acme/dev-tools-code-review/code-review")

        assert result is not None
        assert result.name == "code-review.prompt.md"
        assert "acme" in str(result)
        assert "dev-tools-code-review" in str(result)

    def test_discover_qualified_path_not_found(self, tmp_path):
        """Test qualified path returns None when package doesn't exist."""
        os.chdir(tmp_path)
        runner = ScriptRunner()

        result = runner._discover_prompt_file("nonexistent/package/prompt")

        assert result is None


class TestRuntimeDetection:
    """Test runtime detection logic."""

    @patch("shutil.which")
    def test_detect_installed_runtime_copilot(self, mock_which):
        """Test runtime detection when copilot is installed."""
        mock_which.side_effect = lambda cmd: "/path/to/copilot" if cmd == "copilot" else None

        runner = ScriptRunner()
        result = runner._detect_installed_runtime()

        assert result == "copilot"

    @patch("shutil.which")
    def test_detect_installed_runtime_codex_fallback(self, mock_which):
        """Test runtime detection falls back to codex."""

        def which_side_effect(cmd):
            if cmd == "copilot":
                return None
            elif cmd == "codex":
                return "/path/to/codex"
            return None

        mock_which.side_effect = which_side_effect

        runner = ScriptRunner()
        result = runner._detect_installed_runtime()

        assert result == "codex"

    @patch("shutil.which")
    def test_detect_installed_runtime_none(self, mock_which):
        """Test error when no runtime found."""
        mock_which.return_value = None

        runner = ScriptRunner()

        with pytest.raises(RuntimeError) as exc_info:
            runner._detect_installed_runtime()

        assert "No compatible runtime found" in str(exc_info.value)
        assert "apm runtime setup copilot" in str(exc_info.value)


class TestCommandGeneration:
    """Test runtime command generation."""

    def test_generate_runtime_command_copilot(self):
        """Test command generation for Copilot CLI."""
        runner = ScriptRunner()
        result = runner._generate_runtime_command("copilot", Path("test.prompt.md"))

        assert (
            result
            == "copilot --log-level all --log-dir copilot-logs --allow-all-tools -p test.prompt.md"
        )

    def test_generate_runtime_command_codex(self):
        """Test command generation for Codex CLI."""
        runner = ScriptRunner()
        result = runner._generate_runtime_command("codex", Path("test.prompt.md"))

        assert result == "codex -s workspace-write --skip-git-repo-check test.prompt.md"

    def test_generate_runtime_command_unsupported(self):
        """Test error for unsupported runtime."""
        runner = ScriptRunner()

        with pytest.raises(ValueError) as exc_info:
            runner._generate_runtime_command("unknown", Path("test.prompt.md"))

        assert "Unsupported runtime: unknown" in str(exc_info.value)


class TestScriptExecution:
    """Test script execution with auto-discovery."""

    def test_run_script_explicit_takes_precedence(self, tmp_path):
        """Test that explicit scripts in apm.yml take precedence."""
        # Setup: apm.yml with script "test", and test.prompt.md exists
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("""
name: test-project
scripts:
  test: "echo 'explicit script'"
""")

        prompt_file = tmp_path / "test.prompt.md"
        prompt_file.write_text("---\n---\nAuto-discovered prompt")

        os.chdir(tmp_path)
        runner = ScriptRunner()

        # Mock the execution to avoid actually running commands
        with patch.object(runner, "_execute_script_command", return_value=True) as mock_exec:
            result = runner.run_script("test", {})  # noqa: F841

            # Verify explicit script was used
            mock_exec.assert_called_once()
            call_args = mock_exec.call_args[0]
            assert call_args[0] == "echo 'explicit script'"

    @patch("shutil.which")
    def test_run_script_auto_discovery_fallback(self, mock_which, tmp_path):
        """Test auto-discovery when script not in apm.yml."""
        mock_which.side_effect = lambda cmd: "/path/to/copilot" if cmd == "copilot" else None

        # Setup: apm.yml without script "test", but test.prompt.md exists
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("""
name: test-project
scripts:
  other: "echo 'other script'"
""")

        prompt_file = tmp_path / "test.prompt.md"
        prompt_file.write_text("---\n---\nAuto-discovered prompt")

        os.chdir(tmp_path)
        runner = ScriptRunner()

        # Mock the execution
        with patch.object(runner, "_execute_script_command", return_value=True) as mock_exec:
            result = runner.run_script("test", {})  # noqa: F841

            # Verify auto-discovered command was used
            mock_exec.assert_called_once()
            call_args = mock_exec.call_args[0]
            assert "copilot" in call_args[0]
            assert "test.prompt.md" in call_args[0]
            assert "--log-level all" in call_args[0]

    def test_run_script_not_found_error(self, tmp_path):
        """Test error when script/prompt not found."""
        # Setup: apm.yml with no scripts and no prompts
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("""
name: test-project
""")

        os.chdir(tmp_path)
        runner = ScriptRunner()

        with pytest.raises(RuntimeError) as exc_info:
            runner.run_script("nonexistent", {})

        error_msg = str(exc_info.value)
        assert "Script or prompt 'nonexistent' not found" in error_msg
        assert "Available scripts in apm.yml: none" in error_msg
        assert "To find available prompts, check:" in error_msg
        assert ".apm/prompts/" in error_msg
        assert "apm_modules/" in error_msg
        assert "apm install" in error_msg
