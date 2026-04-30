"""Integration tests for runnable prompts feature."""

import os
import subprocess  # noqa: F401
import tempfile  # noqa: F401
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def preserve_cwd():
    """Fixture to preserve and restore CWD for all tests."""
    try:
        original = os.getcwd()
    except (FileNotFoundError, OSError):
        # If we can't get CWD, use a safe default
        original = Path(__file__).parent.parent.parent
        os.chdir(original)

    yield

    try:
        os.chdir(original)
    except (FileNotFoundError, OSError):
        # If original dir was deleted, change to project root
        try:
            os.chdir(Path(__file__).parent.parent.parent)
        except (FileNotFoundError, OSError):
            # Last resort: home directory
            os.chdir(Path.home())


class TestRunnablePromptsIntegration:
    """Integration tests for install and run workflow."""

    def test_local_prompt_immediate_run(self, tmp_path):
        """Test creating and running a local prompt immediately."""
        # Setup: Create minimal apm.yml
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("""
name: test-project
version: 1.0.0
""")

        # Create a simple local prompt
        prompt_file = tmp_path / "hello.prompt.md"
        prompt_file.write_text("""---
description: Simple hello world prompt
---
Say hello to ${input:name}!
""")

        os.chdir(tmp_path)

        # Import here to ensure we're in the right directory
        from unittest.mock import patch

        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()

        # Mock actual command execution
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            # Mock runtime detection to return copilot
            with patch("shutil.which") as mock_which:
                mock_which.side_effect = lambda cmd: (
                    "/usr/bin/copilot" if cmd == "copilot" else None
                )

                # Run the prompt
                result = runner.run_script("hello", {"name": "World"})

                assert result is True
                # Verify subprocess was called
                assert mock_run.called

    def test_dependency_prompt_discovery(self, tmp_path):
        """Test discovering and running prompts from installed dependencies."""
        # Setup: Create apm.yml with dependency
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("""
name: test-project
version: 1.0.0
dependencies:
  apm:
    - github/test-package
""")

        # Simulate installed dependency with prompt
        dep_dir = tmp_path / "apm_modules" / "github" / "test-package" / ".apm" / "prompts"
        dep_dir.mkdir(parents=True)
        prompt_file = dep_dir / "analyze.prompt.md"
        prompt_file.write_text("""---
description: Analysis prompt from dependency
---
Analyze this code.
""")

        os.chdir(tmp_path)

        from unittest.mock import patch  # noqa: F401

        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()

        # Discover the prompt
        discovered = runner._discover_prompt_file("analyze")

        assert discovered is not None
        assert "apm_modules" in str(discovered)
        assert discovered.name == "analyze.prompt.md"

    def test_local_prompt_precedence_over_dependency(self, tmp_path):
        """Test that local prompts override dependency prompts."""
        # Setup: Create apm.yml
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("""
name: test-project
version: 1.0.0
""")

        # Create local prompt
        local_prompt = tmp_path / "test.prompt.md"
        local_prompt.write_text("---\n---\nLocal version")

        # Create dependency prompt
        dep_dir = tmp_path / "apm_modules" / "org" / "pkg" / ".apm" / "prompts"
        dep_dir.mkdir(parents=True)
        dep_prompt = dep_dir / "test.prompt.md"
        dep_prompt.write_text("---\n---\nDependency version")

        os.chdir(tmp_path)

        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        discovered = runner._discover_prompt_file("test")

        assert discovered is not None
        # Verify it's the local version (not from apm_modules)
        assert "apm_modules" not in str(discovered)
        assert discovered.read_text() == "---\n---\nLocal version"

    def test_explicit_script_precedence_over_discovery(self, tmp_path):
        """Test that apm.yml scripts take precedence over auto-discovery."""
        # Setup: Create apm.yml with explicit script
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("""
name: test-project
scripts:
  test: "echo 'explicit script command'"
""")

        # Create prompt with same name
        prompt_file = tmp_path / "test.prompt.md"
        prompt_file.write_text("---\n---\nThis should not be used")

        os.chdir(tmp_path)

        from unittest.mock import patch

        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()

        # Mock execution
        with patch.object(runner, "_execute_script_command", return_value=True) as mock_exec:
            runner.run_script("test", {})

            # Verify explicit script was used, not auto-discovery
            call_args = mock_exec.call_args[0]
            assert call_args[0] == "echo 'explicit script command'"
            assert "copilot" not in call_args[0]

    def test_copilot_command_defaults(self, tmp_path):
        """Test that Copilot CLI uses correct default flags."""
        # Setup
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("name: test-project\n")

        prompt_file = tmp_path / "test.prompt.md"
        prompt_file.write_text("---\n---\nTest prompt")

        os.chdir(tmp_path)

        from unittest.mock import patch

        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()

        # Mock runtime detection
        with patch("shutil.which") as mock_which:
            mock_which.side_effect = lambda cmd: "/usr/bin/copilot" if cmd == "copilot" else None

            # Get the generated command
            command = runner._generate_runtime_command("copilot", Path("test.prompt.md"))

            # Verify all required flags are present
            assert "--log-level all" in command
            assert "--log-dir copilot-logs" in command
            assert "--allow-all-tools" in command
            assert "-p" in command
            assert "test.prompt.md" in command

    def test_codex_command_defaults(self, tmp_path):
        """Test that Codex CLI uses correct default command."""
        # Setup
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("name: test-project\n")

        prompt_file = tmp_path / "test.prompt.md"
        prompt_file.write_text("---\n---\nTest prompt")

        os.chdir(tmp_path)

        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()

        # Get the generated command
        command = runner._generate_runtime_command("codex", Path("test.prompt.md"))

        # Verify codex command with default flags
        assert command == "codex -s workspace-write --skip-git-repo-check test.prompt.md"

    def test_no_runtime_error_message(self, tmp_path):
        """Test helpful error when no runtime installed."""
        # Setup
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("name: test-project\n")

        prompt_file = tmp_path / "test.prompt.md"
        prompt_file.write_text("---\n---\nTest prompt")

        os.chdir(tmp_path)

        from unittest.mock import patch

        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()

        # Mock no runtimes installed
        with patch("shutil.which") as mock_which:
            mock_which.return_value = None

            # Try to run script
            with pytest.raises(RuntimeError) as exc_info:
                runner.run_script("test", {})

            error_msg = str(exc_info.value)
            assert "No compatible runtime found" in error_msg
            assert "apm runtime setup copilot" in error_msg

    def test_virtual_package_prompt_workflow(self, tmp_path):
        """Test complete workflow: virtual package installed -> prompt discovered -> run."""
        # Setup: Simulate virtual package installation
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("""
name: test-project
dependencies:
  apm:
    - owner/test-repo/prompts/architecture-blueprint-generator.prompt.md
""")

        # Simulate installed virtual package structure
        virtual_pkg_dir = (
            tmp_path
            / "apm_modules"
            / "owner"
            / "test-repo-architecture-blueprint-generator"
            / ".apm"
            / "prompts"
        )
        virtual_pkg_dir.mkdir(parents=True)
        prompt_file = virtual_pkg_dir / "architecture-blueprint-generator.prompt.md"
        prompt_file.write_text("""---
description: Generate architecture blueprint
---
Create a comprehensive architecture blueprint for this project.
""")

        os.chdir(tmp_path)

        from unittest.mock import patch

        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()

        # Discover the prompt
        discovered = runner._discover_prompt_file("architecture-blueprint-generator")

        assert discovered is not None
        assert discovered.name == "architecture-blueprint-generator.prompt.md"

        # Mock execution to verify command generation
        with patch("shutil.which") as mock_which:
            mock_which.side_effect = lambda cmd: "/usr/bin/copilot" if cmd == "copilot" else None

            command = runner._generate_runtime_command("copilot", discovered)
            assert "copilot" in command
            assert "architecture-blueprint-generator.prompt.md" in command
            assert "--log-level all" in command
