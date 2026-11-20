"""Unit tests for script runner functionality (filesystem, process and CLI boundary cases)."""

import os
import shutil  # noqa: F401
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.core.script_runner import PromptCompiler, ScriptRunner

pytestmark = pytest.mark.component


class TestScriptRunner:
    """Test ScriptRunner functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.script_runner = ScriptRunner()
        self.compiled_content = "You are a helpful assistant. Say hello to TestUser!"
        self.compiled_path = ".apm/compiled/hello-world.txt"

    @patch("subprocess.run")
    @patch("apm_cli.core.script_runner.find_runtime_binary", return_value=None)
    @patch("apm_cli.core.script_runner.setup_runtime_environment")
    def test_execute_runtime_command_with_env_vars(
        self, mock_setup_env, mock_find_runtime, mock_subprocess
    ):
        """Test runtime command execution with environment variables."""
        mock_setup_env.return_value = {"EXISTING_VAR": "value"}
        mock_subprocess.return_value.returncode = 0

        # Test command with environment variable prefix
        command = "RUST_LOG=debug codex exec --skip-git-repo-check"
        content = "test content"
        env = {"EXISTING_VAR": "value"}

        result = self.script_runner._execute_runtime_command(command, content, env)  # noqa: F841

        # Verify subprocess was called with correct arguments and environment
        mock_subprocess.assert_called_once()
        args, kwargs = mock_subprocess.call_args

        # Check command arguments (should not include environment variable)
        called_args = args[0]
        assert called_args == ["codex", "exec", "--skip-git-repo-check", content]

        # Check environment variables were properly set
        called_env = kwargs["env"]
        assert called_env["RUST_LOG"] == "debug"
        assert called_env["EXISTING_VAR"] == "value"  # Existing env should be preserved

    @patch("subprocess.run")
    @patch("apm_cli.core.script_runner.find_runtime_binary", return_value=None)
    @patch("apm_cli.core.script_runner.setup_runtime_environment")
    def test_execute_runtime_command_multiple_env_vars(
        self, mock_setup_env, mock_find_runtime, mock_subprocess
    ):
        """Test runtime command execution with multiple environment variables."""
        mock_setup_env.return_value = {}
        mock_subprocess.return_value.returncode = 0

        # Test command with multiple environment variables
        command = "DEBUG=1 VERBOSE=true llm --model gpt-4"
        content = "test content"
        env = {}

        result = self.script_runner._execute_runtime_command(command, content, env)  # noqa: F841

        # Verify subprocess was called with correct arguments and environment
        mock_subprocess.assert_called_once()
        args, kwargs = mock_subprocess.call_args

        # Check command arguments (should not include environment variables)
        called_args = args[0]
        assert called_args == ["llm", "--model", "gpt-4", content]

        # Check environment variables were properly set
        called_env = kwargs["env"]
        assert called_env["DEBUG"] == "1"
        assert called_env["VERBOSE"] == "true"


class TestPromptCompilerDependencyDiscovery:
    """Test PromptCompiler dependency discovery functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.compiler = PromptCompiler()

    def test_resolve_prompt_file_local_exists(self):
        """Test resolving prompt file when it exists locally."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Change to temp directory for test
            original_cwd = Path.cwd()
            try:
                import os

                os.chdir(tmpdir)

                # Create local prompt file
                prompt_file = Path("hello-world.prompt.md")
                prompt_file.write_text("Hello World!")

                result = self.compiler._resolve_prompt_file("hello-world.prompt.md")
                assert result == prompt_file
            finally:
                os.chdir(original_cwd)

    def test_resolve_prompt_file_dependency_root(self):
        """Test resolving prompt file from dependency root directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = Path.cwd()
            try:
                import os

                os.chdir(tmpdir)

                # Create apm_modules structure with org/repo hierarchy
                dep_dir = Path("apm_modules/microsoft/apm-sample-package")
                dep_dir.mkdir(parents=True)

                # Create prompt file in dependency root
                dep_prompt = dep_dir / "hello-world.prompt.md"
                dep_prompt.write_text("Hello from dependency!")

                result = self.compiler._resolve_prompt_file("hello-world.prompt.md")
                assert result == dep_prompt
            finally:
                os.chdir(original_cwd)

    def test_resolve_prompt_file_dependency_subdirectory(self):
        """Test resolving prompt file from dependency subdirectory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = Path.cwd()
            try:
                import os

                os.chdir(tmpdir)

                # Create apm_modules structure
                dep_dir = Path("apm_modules/design-guidelines")
                dep_dir.mkdir(parents=True)

                # Create prompt file in prompts subdirectory
                prompts_dir = dep_dir / "prompts"
                prompts_dir.mkdir()
                dep_prompt = prompts_dir / "hello-world.prompt.md"
                dep_prompt.write_text("Hello from dependency prompts!")

                result = self.compiler._resolve_prompt_file("hello-world.prompt.md")
                assert result == dep_prompt
            finally:
                os.chdir(original_cwd)

    def test_resolve_prompt_file_multiple_dependencies(self):
        """Test resolving prompt file with multiple dependencies (first match wins)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = Path.cwd()
            try:
                import os

                os.chdir(tmpdir)

                # Create multiple dependency directories with org/repo structure
                compliance_dir = Path("apm_modules/acme/compliance-rules")
                compliance_dir.mkdir(parents=True)
                design_dir = Path("apm_modules/microsoft/apm-sample-package")
                design_dir.mkdir(parents=True)

                # Create prompt files in both (first one found should win)
                compliance_prompt = compliance_dir / "hello-world.prompt.md"
                compliance_prompt.write_text("Hello from compliance!")
                design_prompt = design_dir / "hello-world.prompt.md"
                design_prompt.write_text("Hello from design!")

                result = self.compiler._resolve_prompt_file("hello-world.prompt.md")
                # Should return one of the matches (doesn't matter which since both exist)
                assert result in [compliance_prompt, design_prompt]
                assert result.exists()
                assert result.read_text().startswith("Hello from")
            finally:
                os.chdir(original_cwd)

    def test_resolve_prompt_file_no_apm_modules(self):
        """Test resolving prompt file when apm_modules directory doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = Path.cwd()
            try:
                import os

                os.chdir(tmpdir)

                # No apm_modules directory exists
                with pytest.raises(FileNotFoundError) as exc_info:
                    self.compiler._resolve_prompt_file("hello-world.prompt.md")

                error_msg = str(exc_info.value)
                assert "Prompt file 'hello-world.prompt.md' not found" in error_msg
                assert "Local: hello-world.prompt.md" in error_msg
                assert "Run 'apm install'" in error_msg
            finally:
                os.chdir(original_cwd)

    def test_resolve_prompt_file_not_found_anywhere(self):
        """Test resolving prompt file when it's not found anywhere."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = Path.cwd()
            try:
                import os

                os.chdir(tmpdir)

                # Create apm_modules with dependencies but no prompt files
                compliance_dir = Path("apm_modules/acme/compliance-rules")
                compliance_dir.mkdir(parents=True)
                design_dir = Path("apm_modules/microsoft/apm-sample-package")
                design_dir.mkdir(parents=True)

                with pytest.raises(FileNotFoundError) as exc_info:
                    self.compiler._resolve_prompt_file("hello-world.prompt.md")

                error_msg = str(exc_info.value)
                assert "Prompt file 'hello-world.prompt.md' not found" in error_msg
                assert "Local: hello-world.prompt.md" in error_msg
                assert "Dependencies:" in error_msg
                assert "acme/compliance-rules/hello-world.prompt.md" in error_msg
                assert "microsoft/apm-sample-package/hello-world.prompt.md" in error_msg
            finally:
                os.chdir(original_cwd)

    def test_resolve_prompt_file_local_takes_precedence(self):
        """Test that local file takes precedence over dependency files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = Path.cwd()
            try:
                import os

                os.chdir(tmpdir)

                # Create local prompt file
                local_prompt = Path("hello-world.prompt.md")
                local_prompt.write_text("Hello from local!")

                # Create dependency with same file
                dep_dir = Path("apm_modules/microsoft/apm-sample-package")
                dep_dir.mkdir(parents=True)
                dep_prompt = dep_dir / "hello-world.prompt.md"
                dep_prompt.write_text("Hello from dependency!")

                result = self.compiler._resolve_prompt_file("hello-world.prompt.md")
                # Local should take precedence
                assert result == local_prompt
            finally:
                os.chdir(original_cwd)


class TestScriptRunnerAutoInstall:
    """Test ScriptRunner auto-install functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.script_runner = ScriptRunner()

    def test_discover_qualified_prompt_finds_skill_md(self):
        """Test that _discover_qualified_prompt finds SKILL.md for subdirectory packages."""
        with tempfile.TemporaryDirectory() as temp_dir:
            original_dir = os.getcwd()
            os.chdir(temp_dir)
            try:
                # Create subdirectory skill package structure
                skill_dir = Path(
                    "apm_modules/github/awesome-copilot/skills/architecture-blueprint-generator"
                )
                skill_dir.mkdir(parents=True)
                skill_file = skill_dir / "SKILL.md"
                skill_file.write_text("# Architecture Blueprint Generator Skill")

                result = self.script_runner._discover_qualified_prompt(
                    "github/awesome-copilot/skills/architecture-blueprint-generator"
                )

                assert result is not None
                assert result.name == "SKILL.md"
            finally:
                os.chdir(original_dir)

    def test_discover_simple_name_finds_skill_md(self):
        """Test that _discover_prompt_file finds SKILL.md by simple name."""
        with tempfile.TemporaryDirectory() as temp_dir:
            original_dir = os.getcwd()
            os.chdir(temp_dir)
            try:
                # Create subdirectory skill package installed in apm_modules
                skill_dir = Path(
                    "apm_modules/github/awesome-copilot/skills/architecture-blueprint-generator"
                )
                skill_dir.mkdir(parents=True)
                skill_file = skill_dir / "SKILL.md"
                skill_file.write_text("# Architecture Blueprint Generator Skill")

                result = self.script_runner._discover_prompt_file(
                    "architecture-blueprint-generator"
                )

                assert result is not None
                assert result.name == "SKILL.md"
            finally:
                os.chdir(original_dir)
