"""Unit tests for script runner functionality."""

import os
import shutil  # noqa: F401
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from apm_cli.core.script_runner import PromptCompiler, ScriptRunner


class TestScriptRunner:
    """Test ScriptRunner functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.script_runner = ScriptRunner()
        self.compiled_content = "You are a helpful assistant. Say hello to TestUser!"
        self.compiled_path = ".apm/compiled/hello-world.txt"

    def test_transform_runtime_command_simple_codex(self):
        """Test simple codex command transformation."""
        original = "codex hello-world.prompt.md"
        result = self.script_runner._transform_runtime_command(
            original, "hello-world.prompt.md", self.compiled_content, self.compiled_path
        )
        assert result == "codex exec"

    def test_transform_runtime_command_codex_with_flags(self):
        """Test codex command with flags before file."""
        original = "codex --skip-git-repo-check hello-world.prompt.md"
        result = self.script_runner._transform_runtime_command(
            original, "hello-world.prompt.md", self.compiled_content, self.compiled_path
        )
        assert result == "codex exec --skip-git-repo-check"

    def test_transform_runtime_command_codex_multiple_flags(self):
        """Test codex command with multiple flags before file."""
        original = "codex --verbose --skip-git-repo-check hello-world.prompt.md"
        result = self.script_runner._transform_runtime_command(
            original, "hello-world.prompt.md", self.compiled_content, self.compiled_path
        )
        assert result == "codex exec --verbose --skip-git-repo-check"

    def test_transform_runtime_command_env_var_simple(self):
        """Test environment variable with simple codex command."""
        original = "DEBUG=true codex hello-world.prompt.md"
        result = self.script_runner._transform_runtime_command(
            original, "hello-world.prompt.md", self.compiled_content, self.compiled_path
        )
        assert result == "DEBUG=true codex exec"

    def test_transform_runtime_command_env_var_with_flags(self):
        """Test environment variable with codex flags."""
        original = "DEBUG=true codex --skip-git-repo-check hello-world.prompt.md"
        result = self.script_runner._transform_runtime_command(
            original, "hello-world.prompt.md", self.compiled_content, self.compiled_path
        )
        assert result == "DEBUG=true codex exec --skip-git-repo-check"

    def test_transform_runtime_command_llm_simple(self):
        """Test simple llm command transformation."""
        original = "llm hello-world.prompt.md"
        result = self.script_runner._transform_runtime_command(
            original, "hello-world.prompt.md", self.compiled_content, self.compiled_path
        )
        assert result == "llm"

    def test_transform_runtime_command_llm_with_options(self):
        """Test llm command with options after file."""
        original = "llm hello-world.prompt.md --model gpt-4"
        result = self.script_runner._transform_runtime_command(
            original, "hello-world.prompt.md", self.compiled_content, self.compiled_path
        )
        assert result == "llm --model gpt-4"

    def test_transform_runtime_command_bare_file(self):
        """Test bare prompt file defaults to codex exec."""
        original = "hello-world.prompt.md"
        result = self.script_runner._transform_runtime_command(
            original, "hello-world.prompt.md", self.compiled_content, self.compiled_path
        )
        assert result == "codex exec"

    def test_transform_runtime_command_fallback(self):
        """Test fallback behavior for unrecognized patterns."""
        original = "unknown-command hello-world.prompt.md"
        result = self.script_runner._transform_runtime_command(
            original, "hello-world.prompt.md", self.compiled_content, self.compiled_path
        )
        assert result == f"unknown-command {self.compiled_path}"

    def test_transform_runtime_command_copilot_simple(self):
        """Test simple copilot command transformation."""
        original = "copilot hello-world.prompt.md"
        result = self.script_runner._transform_runtime_command(
            original, "hello-world.prompt.md", self.compiled_content, self.compiled_path
        )
        assert result == "copilot"

    def test_transform_runtime_command_copilot_with_flags(self):
        """Test copilot command with flags before file."""
        original = "copilot --log-level all --log-dir copilot-logs hello-world.prompt.md"
        result = self.script_runner._transform_runtime_command(
            original, "hello-world.prompt.md", self.compiled_content, self.compiled_path
        )
        assert result == "copilot --log-level all --log-dir copilot-logs"

    def test_transform_runtime_command_copilot_removes_p_flag(self):
        """Test copilot command removes existing -p flag since it's handled separately."""
        original = "copilot -p hello-world.prompt.md --log-level all"
        result = self.script_runner._transform_runtime_command(
            original, "hello-world.prompt.md", self.compiled_content, self.compiled_path
        )
        assert result == "copilot --log-level all"

    def test_detect_runtime_copilot(self):
        """Test runtime detection for copilot commands."""
        assert self.script_runner._detect_runtime("copilot --log-level all") == "copilot"

    def test_detect_runtime_codex(self):
        """Test runtime detection for codex commands."""
        assert self.script_runner._detect_runtime("codex exec --skip-git-repo-check") == "codex"

    def test_detect_runtime_llm(self):
        """Test runtime detection for llm commands."""
        assert self.script_runner._detect_runtime("llm --model gpt-4") == "llm"

    def test_detect_runtime_unknown(self):
        """Test runtime detection for unknown commands."""
        assert self.script_runner._detect_runtime("unknown-command") == "unknown"

    def test_detect_runtime_model_name_containing_codex(self):
        """codex as a substring of a model name should not be detected as the codex runtime."""
        # e.g. copilot --model gpt-5.3-codex - the runtime is copilot, not codex
        assert self.script_runner._detect_runtime("copilot --model gpt-5.3-codex") == "copilot"

    def test_detect_runtime_hyphenated_codex(self):
        """A hyphen-prefixed codex substring must not trigger codex detection."""
        assert self.script_runner._detect_runtime("run-codex-tool --flag") == "unknown"

    def test_transform_runtime_command_copilot_with_codex_model(self):
        """copilot command using --model containing 'codex' must not be mis-routed to codex runtime."""
        original = "copilot --allow-all-tools --model gpt-5.3-codex -p fix-issue.prompt.md"
        result = self.script_runner._transform_runtime_command(
            original, "fix-issue.prompt.md", self.compiled_content, self.compiled_path
        )
        # Should be treated as a copilot command, not transformed into "codex exec ..."
        assert result.startswith("copilot")
        assert "codex exec" not in result

    def test_transform_runtime_command_copilot_with_codex_model_name(self):
        """--model codex (bare word as model name) must not trigger codex runtime path."""
        original = "copilot --model codex -p fix-issue.prompt.md"
        result = self.script_runner._transform_runtime_command(
            original, "fix-issue.prompt.md", self.compiled_content, self.compiled_path
        )
        assert result.startswith("copilot")
        assert "codex exec" not in result

    @patch("subprocess.run")
    @patch("apm_cli.core.script_runner.shutil.which", return_value=None)
    @patch("apm_cli.core.script_runner.setup_runtime_environment")
    def test_execute_runtime_command_with_env_vars(
        self, mock_setup_env, mock_which, mock_subprocess
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
    @patch("apm_cli.core.script_runner.shutil.which", return_value=None)
    @patch("apm_cli.core.script_runner.setup_runtime_environment")
    def test_execute_runtime_command_multiple_env_vars(
        self, mock_setup_env, mock_which, mock_subprocess
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

    @patch("apm_cli.core.script_runner.Path.exists")
    @patch(
        "builtins.open",
        new_callable=mock_open,
        read_data="scripts:\n  start: 'codex hello.prompt.md'",
    )
    def test_list_scripts(self, mock_file, mock_exists):
        """Test listing scripts from apm.yml."""
        mock_exists.return_value = True

        scripts = self.script_runner.list_scripts()

        assert "start" in scripts
        assert scripts["start"] == "codex hello.prompt.md"


class TestPromptCompiler:
    """Test PromptCompiler functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.compiler = PromptCompiler()

    def test_substitute_parameters_simple(self):
        """Test simple parameter substitution."""
        content = "Hello ${input:name}!"
        params = {"name": "World"}

        result = self.compiler._substitute_parameters(content, params)

        assert result == "Hello World!"

    def test_substitute_parameters_multiple(self):
        """Test multiple parameter substitution."""
        content = "Service: ${input:service}, Environment: ${input:env}"
        params = {"service": "api", "env": "production"}

        result = self.compiler._substitute_parameters(content, params)

        assert result == "Service: api, Environment: production"

    def test_substitute_parameters_no_params(self):
        """Test content with no parameters to substitute."""
        content = "This is a simple prompt with no parameters."
        params = {}

        result = self.compiler._substitute_parameters(content, params)

        assert result == content

    def test_substitute_parameters_missing_param(self):
        """Test behavior when parameter is missing."""
        content = "Hello ${input:name}!"
        params = {}

        result = self.compiler._substitute_parameters(content, params)

        # Should leave placeholder unchanged when parameter is missing
        assert result == "Hello ${input:name}!"

    @patch("apm_cli.core.script_runner.Path.mkdir")
    @patch("apm_cli.core.script_runner.Path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_compile_with_frontmatter(self, mock_file, mock_exists, mock_mkdir):
        """Test compiling prompt file with frontmatter."""
        mock_exists.return_value = True

        # Mock file content with frontmatter
        file_content = """---
description: Test prompt
input:
  - name
---

# Test Prompt

Hello ${input:name}!"""

        mock_file.return_value.read.return_value = file_content

        result_path = self.compiler.compile("test.prompt.md", {"name": "World"})  # noqa: F841

        # Check that the compiled content was written correctly
        mock_file.return_value.write.assert_called_once()
        written_content = mock_file.return_value.write.call_args[0][0]
        assert "Hello World!" in written_content
        assert "---" not in written_content  # Frontmatter should be stripped

    @patch("apm_cli.core.script_runner.Path.mkdir")
    @patch("apm_cli.core.script_runner.Path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_compile_without_frontmatter(self, mock_file, mock_exists, mock_mkdir):
        """Test compiling prompt file without frontmatter."""
        mock_exists.return_value = True

        # Mock file content without frontmatter
        file_content = "Hello ${input:name}!"
        mock_file.return_value.read.return_value = file_content

        result_path = self.compiler.compile("test.prompt.md", {"name": "World"})  # noqa: F841

        # Check that the compiled content was written correctly
        mock_file.return_value.write.assert_called_once()
        written_content = mock_file.return_value.write.call_args[0][0]
        assert written_content == "Hello World!"

    @patch("apm_cli.core.script_runner.Path.exists")
    def test_compile_file_not_found(self, mock_exists):
        """Test compiling non-existent prompt file."""
        mock_exists.return_value = False

        with pytest.raises(
            FileNotFoundError,
            match="Prompt file 'nonexistent.prompt.md' not found",  # noqa: RUF043
        ):
            self.compiler.compile("nonexistent.prompt.md", {})


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

    @patch("apm_cli.core.script_runner.Path.mkdir")
    @patch("builtins.open", new_callable=mock_open)
    def test_compile_with_dependency_resolution(self, mock_file, mock_mkdir):
        """Test compile method uses dependency resolution correctly."""
        with patch.object(self.compiler, "_resolve_prompt_file") as mock_resolve:
            mock_resolve.return_value = Path(
                "apm_modules/microsoft/apm-sample-package/test.prompt.md"
            )

            file_content = "Hello ${input:name}!"
            mock_file.return_value.read.return_value = file_content

            result_path = self.compiler.compile("test.prompt.md", {"name": "World"})  # noqa: F841

            # Verify _resolve_prompt_file was called
            mock_resolve.assert_called_once_with("test.prompt.md")

            # Verify file was opened with resolved path
            mock_file.assert_called()
            opened_path = mock_file.call_args_list[0][0][0]
            assert (
                str(opened_path).replace("\\", "/")
                == "apm_modules/microsoft/apm-sample-package/test.prompt.md"
            )


class TestScriptRunnerAutoInstall:
    """Test ScriptRunner auto-install functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.script_runner = ScriptRunner()

    def test_is_virtual_package_reference_valid_file(self):
        """Test detection of valid virtual file package references."""
        # Valid virtual file package reference
        ref = "owner/test-repo/prompts/architecture-blueprint-generator.prompt.md"
        assert self.script_runner._is_virtual_package_reference(ref) is True

    def test_is_virtual_package_reference_valid_collection(self):
        """Test detection of valid virtual collection package references."""
        # Valid virtual collection package reference
        ref = "owner/test-repo/collections/project-planning"
        assert self.script_runner._is_virtual_package_reference(ref) is True

    def test_is_virtual_package_reference_regular_package(self):
        """Test detection rejects regular packages."""
        # Regular package (not virtual)
        ref = "microsoft/apm-sample-package"
        assert self.script_runner._is_virtual_package_reference(ref) is False

    def test_is_virtual_package_reference_simple_name(self):
        """Test detection rejects simple names without slashes."""
        # Simple name (not a virtual package)
        ref = "code-review"
        assert self.script_runner._is_virtual_package_reference(ref) is False

    def test_is_virtual_package_reference_invalid_format(self):
        """Test detection rejects invalid formats."""
        # Invalid format - looks like a file with unsupported extension
        ref = "owner/repo/some/invalid/path.txt"
        assert self.script_runner._is_virtual_package_reference(ref) is False

    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    @patch("apm_cli.core.script_runner.Path.mkdir")
    @patch("apm_cli.core.script_runner.Path.exists")
    def test_auto_install_virtual_package_file_success(
        self, mock_exists, mock_mkdir, mock_downloader_class
    ):
        """Test successful auto-install of virtual file package."""
        # Setup mocks
        mock_exists.return_value = False  # Package not already installed
        mock_downloader = MagicMock()
        mock_downloader_class.return_value = mock_downloader

        # Mock package info
        mock_package = MagicMock()
        mock_package.name = "test-repo-architecture-blueprint-generator"
        mock_package.version = "1.0.0"
        mock_package_info = MagicMock()
        mock_package_info.package = mock_package
        mock_downloader.download_virtual_file_package.return_value = mock_package_info

        # Test auto-install
        ref = "owner/test-repo/prompts/architecture-blueprint-generator.prompt.md"
        result = self.script_runner._auto_install_virtual_package(ref)

        assert result is True
        mock_downloader.download_virtual_file_package.assert_called_once()

    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    @patch("apm_cli.core.script_runner.Path.mkdir")
    @patch("apm_cli.core.script_runner.Path.exists")
    def test_auto_install_virtual_package_collection_success(
        self, mock_exists, mock_mkdir, mock_downloader_class
    ):
        """Test successful auto-install of virtual collection package."""
        # Setup mocks
        mock_exists.return_value = False  # Package not already installed
        mock_downloader = MagicMock()
        mock_downloader_class.return_value = mock_downloader

        # Mock package info
        mock_package = MagicMock()
        mock_package.name = "test-repo-project-planning"
        mock_package.version = "1.0.0"
        mock_package_info = MagicMock()
        mock_package_info.package = mock_package
        mock_downloader.download_virtual_collection_package.return_value = mock_package_info

        # Test auto-install
        ref = "owner/test-repo/collections/project-planning"
        result = self.script_runner._auto_install_virtual_package(ref)

        assert result is True
        mock_downloader.download_virtual_collection_package.assert_called_once()

    @patch("apm_cli.core.script_runner.Path.exists")
    def test_auto_install_virtual_package_already_installed(self, mock_exists):
        """Test auto-install skips when package already installed."""
        # Package already exists
        mock_exists.return_value = True

        ref = "owner/test-repo/prompts/architecture-blueprint-generator.prompt.md"
        result = self.script_runner._auto_install_virtual_package(ref)

        assert result is True  # Should return True (success) without downloading

    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    @patch("apm_cli.core.script_runner.Path.mkdir")
    @patch("apm_cli.core.script_runner.Path.exists")
    def test_auto_install_virtual_package_download_failure(
        self, mock_exists, mock_mkdir, mock_downloader_class
    ):
        """Test auto-install handles download failures gracefully."""
        # Setup mocks
        mock_exists.return_value = False
        mock_downloader = MagicMock()
        mock_downloader_class.return_value = mock_downloader

        # Simulate download failure
        mock_downloader.download_virtual_file_package.side_effect = RuntimeError("Download failed")

        # Test auto-install
        ref = "owner/test-repo/prompts/architecture-blueprint-generator.prompt.md"
        result = self.script_runner._auto_install_virtual_package(ref)

        assert result is False  # Should return False on failure

    def test_auto_install_virtual_package_invalid_reference(self):
        """Test auto-install rejects invalid references."""
        # Not a virtual package
        ref = "microsoft/apm-sample-package"
        result = self.script_runner._auto_install_virtual_package(ref)

        assert result is False

    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    @patch("apm_cli.core.script_runner.Path.mkdir")
    @patch("apm_cli.core.script_runner.Path.exists")
    def test_auto_install_virtual_package_subdirectory_success(
        self, mock_exists, mock_mkdir, mock_downloader_class
    ):
        """Test successful auto-install of virtual subdirectory (skill) package."""
        # Setup mocks
        mock_exists.return_value = False  # Package not already installed
        mock_downloader = MagicMock()
        mock_downloader_class.return_value = mock_downloader

        # Mock package info
        mock_package = MagicMock()
        mock_package.name = "architecture-blueprint-generator"
        mock_package.version = "1.0.0"
        mock_package_info = MagicMock()
        mock_package_info.package = mock_package
        mock_downloader.download_subdirectory_package.return_value = mock_package_info

        # Test auto-install with subdirectory reference (no .prompt.md extension)
        ref = "github/awesome-copilot/skills/architecture-blueprint-generator"
        result = self.script_runner._auto_install_virtual_package(ref)

        assert result is True
        mock_downloader.download_subdirectory_package.assert_called_once()

    @patch("apm_cli.core.script_runner.ScriptRunner._auto_install_virtual_package")
    @patch("apm_cli.core.script_runner.ScriptRunner._discover_prompt_file")
    @patch("apm_cli.core.script_runner.ScriptRunner._detect_installed_runtime")
    @patch("apm_cli.core.script_runner.ScriptRunner._execute_script_command")
    @patch("apm_cli.core.script_runner.Path.exists")
    @patch("builtins.open", new_callable=mock_open, read_data="name: test\nscripts: {}")
    def test_run_script_triggers_auto_install(
        self, mock_file, mock_exists, mock_execute, mock_runtime, mock_discover, mock_auto_install
    ):
        """Test that run_script triggers auto-install for virtual package references."""
        mock_exists.return_value = True  # apm.yml exists
        mock_discover.side_effect = [
            None,
            Path(
                "apm_modules/github/test-repo-architecture-blueprint-generator/.apm/prompts/architecture-blueprint-generator.prompt.md"
            ),
        ]
        mock_auto_install.return_value = True
        mock_runtime.return_value = "copilot"
        mock_execute.return_value = True

        ref = "owner/test-repo/prompts/architecture-blueprint-generator.prompt.md"
        result = self.script_runner.run_script(ref, {})

        # Verify auto-install was called
        mock_auto_install.assert_called_once_with(ref)
        # Verify discovery was attempted twice (before and after install)
        assert mock_discover.call_count == 2
        # Verify script was executed
        mock_execute.assert_called_once()
        assert result is True

    @patch("apm_cli.core.script_runner.ScriptRunner._auto_install_virtual_package")
    @patch("apm_cli.core.script_runner.ScriptRunner._discover_prompt_file")
    @patch("apm_cli.core.script_runner.Path.exists")
    @patch("builtins.open", new_callable=mock_open, read_data="name: test\nscripts: {}")
    def test_run_script_auto_install_failure_shows_error(
        self, mock_file, mock_exists, mock_discover, mock_auto_install
    ):
        """Test that run_script shows helpful error when auto-install fails."""
        mock_exists.return_value = True  # apm.yml exists
        mock_discover.return_value = None
        mock_auto_install.return_value = False  # Auto-install failed

        ref = "owner/test-repo/prompts/architecture-blueprint-generator.prompt.md"

        with pytest.raises(RuntimeError) as exc_info:
            self.script_runner.run_script(ref, {})

        error_msg = str(exc_info.value)
        assert "Script or prompt" in error_msg
        assert "not found" in error_msg

    @patch("apm_cli.core.script_runner.ScriptRunner._auto_install_virtual_package")
    @patch("apm_cli.core.script_runner.ScriptRunner._discover_prompt_file")
    @patch("apm_cli.core.script_runner.Path.exists")
    @patch("builtins.open", new_callable=mock_open, read_data="name: test\nscripts: {}")
    def test_run_script_skips_auto_install_for_simple_names(
        self, mock_file, mock_exists, mock_discover, mock_auto_install
    ):
        """Test that run_script doesn't trigger auto-install for simple names."""
        mock_exists.return_value = True  # apm.yml exists
        mock_discover.return_value = None

        # Simple name (not a virtual package reference)
        ref = "code-review"

        with pytest.raises(RuntimeError):
            self.script_runner.run_script(ref, {})

        # Auto-install should NOT be called for simple names
        mock_auto_install.assert_not_called()

    @patch("apm_cli.core.script_runner.ScriptRunner._discover_prompt_file")
    @patch("apm_cli.core.script_runner.ScriptRunner._detect_installed_runtime")
    @patch("apm_cli.core.script_runner.ScriptRunner._execute_script_command")
    @patch("apm_cli.core.script_runner.Path.exists")
    @patch("builtins.open", new_callable=mock_open, read_data="name: test\nscripts: {}")
    def test_run_script_uses_cached_package(
        self, mock_file, mock_exists, mock_execute, mock_runtime, mock_discover
    ):
        """Test that run_script uses already-installed package without re-downloading."""
        mock_exists.return_value = True  # apm.yml exists
        # Package already discovered (no auto-install needed)
        mock_discover.return_value = Path(
            "apm_modules/github/test-repo-architecture-blueprint-generator/.apm/prompts/architecture-blueprint-generator.prompt.md"
        )
        mock_runtime.return_value = "copilot"
        mock_execute.return_value = True

        ref = "owner/test-repo/prompts/architecture-blueprint-generator.prompt.md"
        result = self.script_runner.run_script(ref, {})

        # Verify discovery found it on first try
        mock_discover.assert_called_once()
        # Verify script was executed
        mock_execute.assert_called_once()
        assert result is True

    @patch("apm_cli.core.script_runner.ScriptRunner._auto_install_virtual_package")
    @patch("apm_cli.core.script_runner.ScriptRunner._discover_prompt_file")
    @patch("apm_cli.core.script_runner.Path.exists")
    @patch("builtins.open", new_callable=mock_open, read_data="name: test\nscripts: {}")
    def test_run_script_handles_install_success_but_no_prompt(
        self, mock_file, mock_exists, mock_discover, mock_auto_install
    ):
        """Test error when package installs successfully but prompt not found."""
        mock_exists.return_value = True  # apm.yml exists
        mock_discover.side_effect = [None, None]  # Not found before or after install
        mock_auto_install.return_value = True  # Install succeeded

        ref = "owner/test-repo/prompts/architecture-blueprint-generator.prompt.md"

        with pytest.raises(RuntimeError) as exc_info:
            self.script_runner.run_script(ref, {})

        error_msg = str(exc_info.value)
        assert "Package installed successfully but prompt not found" in error_msg
        assert "may not contain the expected prompt file" in error_msg

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


class TestExecuteRuntimeCommandWindowsResolution:
    """Test that _execute_runtime_command resolves executables on Windows."""

    def setup_method(self):
        self.runner = ScriptRunner()

    @patch("apm_cli.core.script_runner.subprocess.run")
    @patch("apm_cli.core.script_runner.shutil.which", return_value=r"C:\npm\copilot.cmd")
    @patch("apm_cli.core.script_runner.sys")
    def test_resolves_executable_on_windows(self, mock_sys, mock_which, mock_run):
        """On win32, the executable should be resolved via shutil.which."""
        mock_sys.platform = "win32"
        mock_run.return_value = MagicMock(returncode=0)

        self.runner._execute_runtime_command(
            "copilot --log-level all", "prompt content", os.environ.copy()
        )

        mock_which.assert_called_once_with("copilot")
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == r"C:\npm\copilot.cmd"

    @patch("apm_cli.core.script_runner.subprocess.run")
    @patch("apm_cli.core.script_runner.shutil.which", return_value=None)
    @patch("apm_cli.core.script_runner.sys")
    def test_keeps_original_when_which_returns_none(self, mock_sys, mock_which, mock_run):
        """If shutil.which can't find it, keep the original name."""
        mock_sys.platform = "win32"
        mock_run.return_value = MagicMock(returncode=0)

        self.runner._execute_runtime_command("copilot -p", "prompt content", os.environ.copy())

        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "copilot"

    @patch("apm_cli.core.script_runner.subprocess.run")
    @patch("apm_cli.core.script_runner.shutil.which")
    @patch("apm_cli.core.script_runner.sys")
    def test_skips_resolution_on_non_windows(self, mock_sys, mock_which, mock_run):
        """On non-Windows, shutil.which should not be called."""
        mock_sys.platform = "linux"
        mock_run.return_value = MagicMock(returncode=0)

        self.runner._execute_runtime_command("copilot -p", "prompt content", os.environ.copy())

        mock_which.assert_not_called()
