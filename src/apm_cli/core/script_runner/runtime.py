"""Script runner for APM NPM-like script execution."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from ..token_manager import setup_runtime_environment
from ._command_builders import (  # noqa: F401
    _auto_compile_prompts,
    _build_codex_command,
    _build_copilot_command,
    _build_gemini_command,
    _build_llm_command,
    _parse_and_build_runtime_command,
    _transform_runtime_command,
)


def run_script(self, script_name: str, params: dict[str, str]) -> bool:
    """Run a script from apm.yml with parameter substitution.

    Execution priority:
    1. Explicit scripts in apm.yml (takes precedence)
    2. Auto-discovered prompt files (fallback)
    3. Error if not found

    Args:
        script_name: Name of the script to run
        params: Parameters for compilation and script execution

    Returns:
        bool: True if script executed successfully
    """
    # Display script execution header
    header_lines = self.formatter.format_script_header(script_name, params)
    for line in header_lines:
        print(line)

    # Check if this is a virtual package (before loading config)
    is_virtual_package = self._is_virtual_package_reference(script_name)

    # Load apm.yml configuration (or create minimal one for virtual packages)
    config = self._load_config()
    if not config:
        if is_virtual_package:
            # Create minimal config for zero-config virtual package execution
            print("  [i]  Creating minimal apm.yml for zero-config execution...")
            self._create_minimal_config()
            config = self._load_config()
        else:
            raise RuntimeError("No apm.yml found in current directory")

    # 1. Check explicit scripts first (existing behavior - highest priority)
    scripts = config.get("scripts", {})
    if script_name in scripts:
        command = scripts[script_name]
        return self._execute_script_command(command, params)

    # 2. Auto-discover prompt file (fallback)
    discovered_prompt = self._discover_prompt_file(script_name)

    if discovered_prompt:
        # Print discovery message early to allow E2E tests to validate
        # This message appears before runtime detection, which may fail in test environments
        print(f"[i] Auto-discovered: {discovered_prompt.as_posix()}")

        # Detect runtime and generate command
        runtime = self._detect_installed_runtime()
        command = self._generate_runtime_command(runtime, discovered_prompt)

        # Execute with existing logic
        return self._execute_script_command(command, params)

    # 2.5 Try auto-install if it looks like a virtual package reference
    if self._is_virtual_package_reference(script_name):
        print(f"\n Auto-installing virtual package: {script_name}")
        if self._auto_install_virtual_package(script_name):
            # Retry discovery after install
            discovered_prompt = self._discover_prompt_file(script_name)
            if discovered_prompt:
                # Signal successful install before attempting runtime detection
                # This allows E2E tests to validate auto-install without requiring runtime
                print("\n* Package installed and ready to run\n")
                runtime = self._detect_installed_runtime()
                command = self._generate_runtime_command(runtime, discovered_prompt)
                return self._execute_script_command(command, params)
            else:
                raise RuntimeError(
                    f"Package installed successfully but prompt not found.\n"
                    f"The package may not contain the expected prompt file.\n"
                    f"Check {Path('apm_modules')} for installed files."
                )

    # 3. Not found anywhere
    available = ", ".join(scripts.keys()) if scripts else "none"

    # Build helpful error message
    error_msg = f"Script or prompt '{script_name}' not found.\n"
    error_msg += f"Available scripts in apm.yml: {available}\n"
    error_msg += "\nTo find available prompts, check:\n"
    error_msg += "  - Local: .apm/prompts/, .github/prompts/, or project root\n"
    error_msg += "  - Dependencies: apm_modules/*/.apm/prompts/\n"
    error_msg += "\nOr install a prompt package:\n"
    error_msg += "  apm install <owner>/<repo>/path/to/prompt.prompt.md\n"

    raise RuntimeError(error_msg)


def _show_env_setup_if_relevant(self, env: dict, runtime: str) -> None:
    """Show environment setup info if relevant tokens are set."""
    env_vars_set = []
    if env.get("GITHUB_TOKEN"):
        env_vars_set.append("GITHUB_TOKEN")
    if env.get("GITHUB_APM_PAT"):
        env_vars_set.append("GITHUB_APM_PAT")
    if env_vars_set:
        env_lines = self.formatter.format_environment_setup(runtime, env_vars_set)
        for line in env_lines:
            print(line)


def _extract_env_vars_from_args(args: list, env: dict) -> tuple[dict, list]:
    """Split a parsed arg list into (env_vars dict, actual_command_args list)."""
    env_vars = env.copy()
    actual_command_args = []
    for arg in args:
        if "=" in arg and not actual_command_args:
            key, value = arg.split("=", 1)
            if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", key):
                env_vars[key] = value
                continue
        actual_command_args.append(arg)
    return env_vars, actual_command_args


def _append_content_for_runtime(actual_args: list, runtime: str, content: str) -> list:
    """Return a copy of actual_args with content appended using runtime conventions."""
    result = list(actual_args)
    if runtime in ("copilot", "gemini"):
        result.extend(["-p", content])
    else:
        result.append(content)
    return result


def _execute_script_command(self, command: str, params: dict[str, str]) -> bool:
    """Execute a script command (from apm.yml or auto-generated).

    This is the existing run_script logic, extracted for reuse.

    Args:
        command: Script command to execute
        params: Parameters for compilation and script execution

    Returns:
        bool: True if script executed successfully
    """

    # Auto-compile any .prompt.md files in the command
    compiled_command, compiled_prompt_files, runtime_content = self._auto_compile_prompts(
        command, params
    )

    # Show compilation progress if needed
    if compiled_prompt_files:
        compilation_lines = self.formatter.format_compilation_progress(compiled_prompt_files)
        for line in compilation_lines:
            print(line)

    # Detect runtime and show execution details
    runtime = self._detect_runtime(compiled_command)

    # Execute the final command
    if runtime_content is not None:
        # Show runtime execution details
        execution_lines = self.formatter.format_runtime_execution(
            runtime, compiled_command, len(runtime_content)
        )
        for line in execution_lines:
            print(line)

        # Show content preview
        preview_lines = self.formatter.format_content_preview(runtime_content)
        for line in preview_lines:
            print(line)

    try:
        # Set up GitHub token environment for all runtimes using centralized manager
        env = setup_runtime_environment(os.environ.copy())

        _show_env_setup_if_relevant(self, env, runtime)

        # Track execution time
        start_time = time.time()

        # Check if this command needs subprocess execution (has compiled content)
        if runtime_content is not None:
            # Use argument list approach for all runtimes to avoid shell parsing issues
            result = self._execute_runtime_command(compiled_command, runtime_content, env)
        else:
            # Use regular shell execution for other commands
            # (shell=True works cross-platform: bash on Unix, cmd.exe on Windows)
            result = subprocess.run(compiled_command, shell=True, check=True, env=env)

        execution_time = time.time() - start_time

        # Show success message
        success_lines = self.formatter.format_execution_success(runtime, execution_time)
        for line in success_lines:
            print(line)

        return result.returncode == 0

    except subprocess.CalledProcessError as e:
        execution_time = time.time() - start_time

        # Show error message
        error_lines = self.formatter.format_execution_error(runtime, e.returncode)
        for line in error_lines:
            print(line)

        raise RuntimeError(f"Script execution failed with exit code {e.returncode}")  # noqa: B904


def _detect_runtime(self, command: str) -> str:
    """Detect which runtime is being used in the command.

    Args:
        command: The command to analyze

    Returns:
        Name of the detected runtime (copilot, codex, llm, gemini, or unknown)
    """
    command_lower = command.lower().strip()
    if re.search(r"(?:^|\s)copilot(?:\s|$)", command_lower):
        return "copilot"
    elif re.search(r"(?:^|\s)codex(?:\s|$)", command_lower):
        return "codex"
    elif re.search(r"(?:^|\s)llm(?:\s|$)", command_lower):
        return "llm"
    elif re.search(r"(?:^|\s)gemini(?:\s|$)", command_lower):
        return "gemini"
    else:
        return "unknown"


def _execute_runtime_command(
    self, command: str, content: str, env: dict
) -> subprocess.CompletedProcess:
    """Execute a runtime command using subprocess argument list to avoid shell parsing issues.

    Args:
        command: The simplified runtime command (without content)
        content: The compiled prompt content to pass to the runtime
        env: Environment variables

    Returns:
        subprocess.CompletedProcess: The result of the command execution
    """
    import shlex

    package_module = sys.modules[__package__]

    # Parse the command into arguments
    if package_module.sys.platform == "win32":
        # On Windows, use posix=False to preserve Windows quoting semantics
        # (e.g., paths with spaces, quoted arguments like --model "gpt-4o mini")
        args = shlex.split(command.strip(), posix=False)
    else:
        args = shlex.split(command.strip())

    # Handle environment variables at the beginning of the command
    env_vars, actual_command_args = _extract_env_vars_from_args(args, env)

    # Determine how to pass content based on runtime
    runtime = self._detect_runtime(" ".join(actual_command_args))
    actual_command_args = _append_content_for_runtime(actual_command_args, runtime, content)

    # Show subprocess details for debugging
    subprocess_lines = self.formatter.format_subprocess_details(
        actual_command_args[:-1], len(content)
    )
    for line in subprocess_lines:
        print(line)

    # Show environment variables if any were extracted
    if len(env_vars) > len(env):
        extracted_env_vars = []
        for key, value in env_vars.items():
            if key not in env:
                extracted_env_vars.append(f"{key}={value}")
        if extracted_env_vars:
            env_lines = self.formatter.format_environment_setup("command", extracted_env_vars)
            for line in env_lines:
                print(line)

    # Resolve the executable via find_runtime_binary so that
    # APM-managed runtimes and shell wrappers (copilot.cmd) are found without shell=True.
    if actual_command_args:
        resolved = package_module.find_runtime_binary(actual_command_args[0])
        if resolved:
            actual_command_args[0] = resolved
    return package_module.subprocess.run(actual_command_args, check=True, env=env_vars)


def _detect_installed_runtime(self) -> str:
    """Detect installed runtime with priority order.

    Priority: copilot > codex > gemini > error

    Returns:
        Name of detected runtime

    Raises:
        RuntimeError: If no compatible runtime is found
    """
    package_module = sys.modules[__package__]
    if package_module.find_runtime_binary("copilot"):
        return "copilot"
    elif package_module.find_runtime_binary("codex"):
        return "codex"
    elif package_module.find_runtime_binary("gemini"):
        return "gemini"
    else:
        raise RuntimeError(
            "No compatible runtime found.\n"
            "Install GitHub Copilot CLI with:\n"
            "  apm runtime setup copilot\n"
            "Or install Codex CLI with:\n"
            "  apm runtime setup codex\n"
            "Or install Gemini CLI with:\n"
            "  apm runtime setup gemini"
        )


def _generate_runtime_command(self, runtime: str, prompt_file: Path) -> str:
    """Generate appropriate runtime command with proper defaults.

    Args:
        runtime: Name of runtime (copilot or codex)
        prompt_file: Path to the prompt file

    Returns:
        Full command string with runtime-specific defaults
    """
    if runtime == "copilot":
        return f"copilot --log-level all --log-dir copilot-logs --allow-all-tools -p {prompt_file}"
    elif runtime == "codex":
        return f"codex -s workspace-write --skip-git-repo-check {prompt_file}"
    elif runtime == "gemini":
        return f"gemini -p {prompt_file}"
    else:
        raise ValueError(f"Unsupported runtime: {runtime}")
