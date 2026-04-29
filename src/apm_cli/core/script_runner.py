"""Script runner for APM NPM-like script execution."""

import os
import re
import shutil
import subprocess
import sys
import time
import yaml
from pathlib import Path
from typing import Dict, Optional

from .token_manager import setup_runtime_environment
from ..output.script_formatters import ScriptExecutionFormatter


class ScriptRunner:
    """Executes APM scripts with auto-compilation of .prompt.md files."""

    def __init__(self, compiler=None, use_color: bool = True):
        """Initialize script runner with optional compiler.

        Args:
            compiler: Optional prompt compiler instance
            use_color: Whether to use colored output
        """
        self.compiler = compiler or PromptCompiler()
        self.formatter = ScriptExecutionFormatter(use_color=use_color)

    def run_script(self, script_name: str, params: Dict[str, str]) -> bool:
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
                print(f"  [i]  Creating minimal apm.yml for zero-config execution...")
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
                    print(f"\n* Package installed and ready to run\n")
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
        error_msg += f"\nTo find available prompts, check:\n"
        error_msg += f"  - Local: .apm/prompts/, .github/prompts/, or project root\n"
        error_msg += f"  - Dependencies: apm_modules/*/.apm/prompts/\n"
        error_msg += f"\nOr install a prompt package:\n"
        error_msg += f"  apm install <owner>/<repo>/path/to/prompt.prompt.md\n"

        raise RuntimeError(error_msg)

    def _execute_script_command(self, command: str, params: Dict[str, str]) -> bool:
        """Execute a script command (from apm.yml or auto-generated).

        This is the existing run_script logic, extracted for reuse.

        Args:
            command: Script command to execute
            params: Parameters for compilation and script execution

        Returns:
            bool: True if script executed successfully
        """

        # Auto-compile any .prompt.md files in the command
        compiled_command, compiled_prompt_files, runtime_content = (
            self._auto_compile_prompts(command, params)
        )

        # Show compilation progress if needed
        if compiled_prompt_files:
            compilation_lines = self.formatter.format_compilation_progress(
                compiled_prompt_files
            )
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

            # Show environment setup if relevant
            env_vars_set = []
            if "GITHUB_TOKEN" in env and env["GITHUB_TOKEN"]:
                env_vars_set.append("GITHUB_TOKEN")
            if "GITHUB_APM_PAT" in env and env["GITHUB_APM_PAT"]:
                env_vars_set.append("GITHUB_APM_PAT")

            if env_vars_set:
                env_lines = self.formatter.format_environment_setup(
                    runtime, env_vars_set
                )
                for line in env_lines:
                    print(line)

            # Track execution time
            start_time = time.time()

            # Check if this command needs subprocess execution (has compiled content)
            if runtime_content is not None:
                # Use argument list approach for all runtimes to avoid shell parsing issues
                result = self._execute_runtime_command(
                    compiled_command, runtime_content, env
                )
            else:
                # Use regular shell execution for other commands
                # (shell=True works cross-platform: bash on Unix, cmd.exe on Windows)
                result = subprocess.run(
                    compiled_command, shell=True, check=True, env=env
                )

            execution_time = time.time() - start_time

            # Show success message
            success_lines = self.formatter.format_execution_success(
                runtime, execution_time
            )
            for line in success_lines:
                print(line)

            return result.returncode == 0

        except subprocess.CalledProcessError as e:
            execution_time = time.time() - start_time

            # Show error message
            error_lines = self.formatter.format_execution_error(runtime, e.returncode)
            for line in error_lines:
                print(line)

            raise RuntimeError(f"Script execution failed with exit code {e.returncode}")

    def list_scripts(self) -> Dict[str, str]:
        """List all available scripts from apm.yml.

        Returns:
            Dict mapping script names to their commands
        """
        config = self._load_config()
        return config.get("scripts", {}) if config else {}

    def _load_config(self) -> Optional[Dict]:
        """Load apm.yml from current directory."""
        config_path = Path("apm.yml")
        if not config_path.exists():
            return None

        from ..utils.yaml_io import load_yaml
        return load_yaml(config_path)

    def _auto_compile_prompts(
        self, command: str, params: Dict[str, str]
    ) -> tuple[str, list[str], str]:
        """Auto-compile .prompt.md files and transform runtime commands.

        Args:
            command: Original script command
            params: Parameters for compilation

        Returns:
            Tuple of (compiled_command, list_of_compiled_prompt_files, runtime_content_or_none)
        """
        # Find all .prompt.md files in the command using regex
        prompt_files = re.findall(r"(\S+\.prompt\.md)", command)
        compiled_prompt_files = []
        runtime_content = None

        compiled_command = command
        for prompt_file in prompt_files:
            # Compile the prompt file with current params
            compiled_path = self.compiler.compile(prompt_file, params)
            compiled_prompt_files.append(prompt_file)

            # Read the compiled content
            with open(compiled_path, "r", encoding="utf-8") as f:
                compiled_content = f.read().strip()

            # Check if this is a runtime command before transformation
            is_runtime_cmd = any(
                re.search(r"(?:^|\s)" + runtime + r"(?:\s|$)", command)
                for runtime in ["copilot", "codex", "llm", "gemini"]
            ) and re.search(re.escape(prompt_file), command)

            # Transform command based on runtime pattern
            compiled_command = self._transform_runtime_command(
                compiled_command, prompt_file, compiled_content, compiled_path
            )

            # Store content for runtime commands that need subprocess execution
            if is_runtime_cmd:
                runtime_content = compiled_content

        return compiled_command, compiled_prompt_files, runtime_content

    def _transform_runtime_command(
        self, command: str, prompt_file: str, compiled_content: str, compiled_path: str
    ) -> str:
        """Transform runtime commands to their proper execution format.

        Dispatches to per-runtime builders after extracting arguments
        around the prompt file reference.

        Args:
            command: Original command
            prompt_file: Original .prompt.md file path
            compiled_content: Compiled prompt content as string
            compiled_path: Path to compiled .txt file

        Returns:
            Transformed command for proper runtime execution
        """
        # Handle environment variables prefix (e.g., "ENV1=val1 ENV2=val2 codex [args] file.prompt.md")
        # More robust approach: split by runtime commands to separate env vars from command
        runtime_commands = ["codex", "copilot", "llm", "gemini"]

        # Try matching with env-var prefix (e.g. "ENV=val codex args file.prompt.md")
        for runtime_cmd in runtime_commands:
            runtime_pattern = f" {runtime_cmd} "
            if runtime_pattern in command and re.search(
                re.escape(prompt_file), command
            ):
                parts = command.split(runtime_pattern, 1)
                potential_env_part = parts[0]
                runtime_part = runtime_cmd + " " + parts[1]

                if "=" in potential_env_part and not potential_env_part.startswith(
                    runtime_cmd
                ):
                    result = self._parse_and_build_runtime_command(
                        runtime_cmd, runtime_part, prompt_file,
                        env_prefix=potential_env_part,
                    )
                    if result is not None:
                        return result

        # Try individual runtime patterns without environment variables
        for runtime_cmd in runtime_commands:
            if re.search(
                r"^" + runtime_cmd + r"\s+.*" + re.escape(prompt_file), command
            ):
                result = self._parse_and_build_runtime_command(
                    runtime_cmd, command, prompt_file,
                )
                if result is not None:
                    return result

        # Handle bare "file.prompt.md" -> "codex exec" (default to codex)
        if command.strip() == prompt_file:
            return "codex exec"

        # Fallback: just replace file path with compiled path (for non-runtime commands)
        return command.replace(prompt_file, compiled_path)

    def _parse_and_build_runtime_command(
        self, runtime_cmd: str, command_part: str, prompt_file: str,
        env_prefix: str = None,
    ) -> Optional[str]:
        """Parse arguments around the prompt file and delegate to a per-runtime builder.

        Args:
            runtime_cmd: Runtime name (codex, copilot, llm, or gemini)
            command_part: The command portion containing the runtime invocation
            prompt_file: The .prompt.md filename to strip
            env_prefix: Optional environment variable prefix (e.g. "DEBUG=1")

        Returns:
            Transformed command string, or None if the pattern does not match
        """
        match = re.search(
            f"{runtime_cmd}\\s+(.*?)(" + re.escape(prompt_file) + r")(.*?)$",
            command_part,
        )
        if not match:
            return None

        args_before = match.group(1).strip()
        args_after = match.group(3).strip()

        # In the env-var path, non-codex runtimes strip -p flags (matches
        # original behaviour where copilot and llm shared an else branch).
        if env_prefix is not None and runtime_cmd != "codex":
            args_before = args_before.replace("-p", "").strip()

        builders = {
            "codex": self._build_codex_command,
            "copilot": self._build_copilot_command,
            "llm": self._build_llm_command,
            "gemini": self._build_gemini_command,
        }
        builder = builders.get(runtime_cmd)
        if builder:
            return builder(args_before, args_after, env_prefix)
        return None

    def _build_codex_command(
        self, args_before: str, args_after: str, env_prefix: Optional[str] = None,
    ) -> str:
        """Build a codex command from parsed arguments.

        Args:
            args_before: Arguments that appeared before the prompt file
            args_after: Arguments that appeared after the prompt file
            env_prefix: Optional environment variable prefix

        Returns:
            Assembled codex command string
        """
        prefix = f"{env_prefix} " if env_prefix else ""
        result = f"{prefix}codex exec"
        if args_before:
            result += f" {args_before}"
        if args_after:
            result += f" {args_after}"
        return result

    def _build_copilot_command(
        self, args_before: str, args_after: str, env_prefix: Optional[str] = None,
    ) -> str:
        """Build a copilot command from parsed arguments.

        Removes any existing -p flag since content is passed separately
        during execution.

        Args:
            args_before: Arguments that appeared before the prompt file
            args_after: Arguments that appeared after the prompt file
            env_prefix: Optional environment variable prefix

        Returns:
            Assembled copilot command string
        """
        prefix = f"{env_prefix} " if env_prefix else ""
        result = f"{prefix}copilot"
        if args_before:
            # Remove any existing -p flag since we handle it in execution
            cleaned_args = args_before.replace("-p", "").strip()
            if cleaned_args:
                result += f" {cleaned_args}"
        if args_after:
            result += f" {args_after}"
        return result

    def _build_llm_command(
        self, args_before: str, args_after: str, env_prefix: Optional[str] = None,
    ) -> str:
        """Build an llm command from parsed arguments.

        Args:
            args_before: Arguments that appeared before the prompt file
            args_after: Arguments that appeared after the prompt file
            env_prefix: Optional environment variable prefix

        Returns:
            Assembled llm command string
        """
        prefix = f"{env_prefix} " if env_prefix else ""
        result = f"{prefix}llm"
        if args_before:
            result += f" {args_before}"
        if args_after:
            result += f" {args_after}"
        return result

    def _build_gemini_command(
        self, args_before: str, args_after: str, env_prefix: Optional[str] = None,
    ) -> str:
        """Build a gemini command from parsed arguments.

        Args:
            args_before: Arguments that appeared before the prompt file
            args_after: Arguments that appeared after the prompt file
            env_prefix: Optional environment variable prefix

        Returns:
            Assembled gemini command string
        """
        prefix = f"{env_prefix} " if env_prefix else ""
        result = f"{prefix}gemini"
        if args_before:
            cleaned_args = re.sub(r'(^|\s)-p(?=\s|$)', '', args_before).strip()
            if cleaned_args:
                result += f" {cleaned_args}"
        if args_after:
            result += f" {args_after}"
        return result

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

        # Parse the command into arguments
        if sys.platform == "win32":
            # On Windows, use posix=False to preserve Windows quoting semantics
            # (e.g., paths with spaces, quoted arguments like --model "gpt-4o mini")
            args = shlex.split(command.strip(), posix=False)
        else:
            args = shlex.split(command.strip())

        # Handle environment variables at the beginning of the command
        # Extract environment variables (key=value pairs) from the beginning of args
        env_vars = env.copy()  # Start with existing environment
        actual_command_args = []

        for arg in args:
            if "=" in arg and not actual_command_args:
                # This looks like an environment variable and we haven't started the actual command yet
                key, value = arg.split("=", 1)
                # Validate environment variable name with restrictive pattern
                # Only allow uppercase letters, numbers, and underscores, starting with letter or underscore
                if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", key):
                    env_vars[key] = value
                    continue
            # Once we hit a non-env-var argument, everything else is part of the command
            actual_command_args.append(arg)

        # Determine how to pass content based on runtime
        runtime = self._detect_runtime(" ".join(actual_command_args))

        if runtime == "copilot":
            # Copilot uses -p flag
            actual_command_args.extend(["-p", content])
        elif runtime == "codex":
            # Codex exec expects content as the last argument
            actual_command_args.append(content)
        elif runtime == "llm":
            # LLM expects content as argument
            actual_command_args.append(content)
        elif runtime == "gemini":
            # Gemini uses -p flag for prompt content
            actual_command_args.extend(["-p", content])
        else:
            # Default: assume content as last argument
            actual_command_args.append(content)

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
                env_lines = self.formatter.format_environment_setup(
                    "command", extracted_env_vars
                )
                for line in env_lines:
                    print(line)

        # Execute using argument list (no shell interpretation) with updated environment
        # On Windows, resolve the executable via shutil.which() so that shell
        # wrappers like copilot.cmd / copilot.ps1 are found without shell=True.
        if sys.platform == "win32" and actual_command_args:
            resolved = shutil.which(actual_command_args[0])
            if resolved:
                actual_command_args[0] = resolved
        return subprocess.run(actual_command_args, check=True, env=env_vars)

    def _discover_prompt_file(self, name: str) -> Optional[Path]:
        """Discover prompt files by name across local and dependencies.

        Supports both simple names and qualified paths:
        - Simple: "code-review" -> searches everywhere
        - Qualified: "github/awesome-copilot/code-review" -> searches specific package

        Search order for simple names:
        1. Local root: ./{name}.prompt.md
        2. Local prompts: .apm/prompts/{name}.prompt.md
        3. GitHub convention: .github/prompts/{name}.prompt.md
        4. Dependencies: apm_modules/**/.apm/prompts/{name}.prompt.md
        5. Dependencies root: apm_modules/**/{name}.prompt.md

        Args:
            name: Script/prompt name or qualified path (owner/repo/name)

        Returns:
            Path to discovered prompt file, or None if not found

        Raises:
            RuntimeError: If multiple prompts found with same name (collision)
        """
        # Check if this is a qualified path (contains /)
        if "/" in name:
            return self._discover_qualified_prompt(name)

        # Ensure name doesn't already have .prompt.md extension
        if name.endswith(".prompt.md"):
            search_name = name
        else:
            search_name = f"{name}.prompt.md"

        # 1. Check local paths first (highest priority)
        local_search_paths = [
            Path(search_name),  # Local root
            Path(f".apm/prompts/{search_name}"),  # APM prompts dir
            Path(f".github/prompts/{search_name}"),  # GitHub convention
        ]

        for path in local_search_paths:
            if path.exists() and not path.is_symlink():
                return path

        # 2. Search in dependencies and detect collisions
        apm_modules = Path("apm_modules")
        if apm_modules.exists():
            # Collect ALL .prompt.md matches to detect collisions
            raw_matches = list(apm_modules.rglob(search_name))

            # Also search for SKILL.md in directories matching the name
            for skill_dir in apm_modules.rglob(name):
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        raw_matches.append(skill_file)

            # Filter out symlinks
            matches = [m for m in raw_matches if not m.is_symlink()]

            if len(matches) == 0:
                return None
            elif len(matches) == 1:
                return matches[0]
            else:
                # Multiple matches - collision detected!
                self._handle_prompt_collision(name, matches)

        return None

    def _discover_qualified_prompt(self, qualified_path: str) -> Optional[Path]:
        """Discover prompt using qualified path (owner/repo/name format).

        Args:
            qualified_path: Qualified path like "github/awesome-copilot/code-review"

        Returns:
            Path to discovered prompt file, or None if not found
        """
        # Parse qualified path: owner/repo/name or owner/repo-name/name
        parts = qualified_path.split("/")

        if len(parts) < 2:
            return None

        # Extract prompt name (last part)
        prompt_name = parts[-1]
        if not prompt_name.endswith(".prompt.md"):
            prompt_name = f"{prompt_name}.prompt.md"

        # Build possible package directory patterns
        # Could be: owner/repo or owner/repo-promptname (virtual packages)
        apm_modules = Path("apm_modules")
        if not apm_modules.exists():
            return None

        # Try to find matching package directory
        owner = parts[0]

        # Check if owner directory exists
        owner_dir = apm_modules / owner
        if not owner_dir.exists():
            return None

        # For subdirectory packages (skills), check for SKILL.md first
        # e.g., github/awesome-copilot/skills/architecture-blueprint-generator
        # installs to apm_modules/github/awesome-copilot/skills/architecture-blueprint-generator/SKILL.md
        if len(parts) >= 3:
            subdir_path = apm_modules.joinpath(*parts)
            skill_file = subdir_path / "SKILL.md"
            if skill_file.exists():
                return skill_file

        # Search within this owner's packages for .prompt.md files
        for pkg_dir in owner_dir.iterdir():
            if not pkg_dir.is_dir():
                continue

            # Try to find the prompt in this package
            for prompt_path in pkg_dir.rglob(prompt_name):
                # Verify this matches the qualified path structure
                if self._matches_qualified_path(prompt_path, qualified_path):
                    return prompt_path

        return None

    def _matches_qualified_path(self, prompt_path: Path, qualified_path: str) -> bool:
        """Check if a prompt path matches the qualified path specification.

        Args:
            prompt_path: Actual path to prompt file
            qualified_path: User-specified qualified path

        Returns:
            True if paths match
        """
        # For now, just check if the qualified path components appear in the prompt path
        # This is a simple heuristic that works for most cases
        path_str = str(prompt_path)
        qualified_parts = qualified_path.split("/")

        # Check if owner is in the path
        if qualified_parts[0] not in path_str:
            return False

        # Check if prompt name matches
        prompt_name = qualified_parts[-1]
        if not prompt_name.endswith(".prompt.md"):
            prompt_name = f"{prompt_name}.prompt.md"

        return prompt_path.name == prompt_name

    def _handle_prompt_collision(self, name: str, matches: list[Path]) -> None:
        """Handle collision when multiple prompts found with same name.

        Args:
            name: Prompt name that has collisions
            matches: List of matching prompt paths

        Raises:
            RuntimeError: Always raises with helpful disambiguation message
        """
        # Build helpful error message
        error_msg = f"Multiple prompts found for '{name}':\n"

        # List all matches with their package paths
        for match in matches:
            # Extract package identifier from path
            path_parts = match.parts
            if "apm_modules" in path_parts:
                idx = path_parts.index("apm_modules")
                if idx + 2 < len(path_parts):
                    owner = path_parts[idx + 1]
                    pkg = path_parts[idx + 2]
                    error_msg += f"  - {owner}/{pkg} ({match})\n"
                else:
                    error_msg += f"  - {match}\n"
            else:
                error_msg += f"  - {match}\n"

        error_msg += f"\nPlease specify using qualified path:\n"

        # Suggest qualified paths based on matches
        for match in matches:
            path_parts = match.parts
            if "apm_modules" in path_parts:
                idx = path_parts.index("apm_modules")
                if idx + 2 < len(path_parts):
                    owner = path_parts[idx + 1]
                    pkg = path_parts[idx + 2]
                    error_msg += f"  apm run {owner}/{pkg}/{name}\n"

        error_msg += f"\nOr add an explicit script to apm.yml:\n"
        error_msg += f"  scripts:\n"
        error_msg += f'    my-{name}: "copilot -p <path-to-preferred-prompt>"\n'

        raise RuntimeError(error_msg)

    def _is_virtual_package_reference(self, name: str) -> bool:
        """Check if a name looks like a virtual package reference.

        Virtual packages have format:
        - owner/repo/path/to/file.prompt.md (virtual file)
        - owner/repo/collections/name (virtual collection)
        - owner/repo/skills/name (virtual subdirectory/skill)

        Args:
            name: Name to check

        Returns:
            True if this looks like a virtual package reference
        """
        # Must have at least one slash
        if "/" not in name:
            return False

        from ..models.apm_package import DependencyReference

        try:
            dep_ref = DependencyReference.parse(name)
            return dep_ref.is_virtual
        except Exception:
            return False

    def _auto_install_virtual_package(self, package_ref: str) -> bool:
        """Auto-install a virtual package.

        Handles three types of virtual packages:
        - Virtual files: owner/repo/prompts/file.prompt.md
        - Virtual collections: owner/repo/collections/name
        - Virtual subdirectories (skills): owner/repo/skills/name

        Args:
            package_ref: Virtual package reference

        Returns:
            True if installation succeeded, False otherwise
        """
        try:
            from ..models.apm_package import DependencyReference
            from ..deps.github_downloader import GitHubPackageDownloader

            # Parse the reference as-is  -- no extension guessing
            dep_ref = DependencyReference.parse(package_ref)

            if not dep_ref.is_virtual:
                return False

            # Ensure apm_modules exists
            apm_modules = Path("apm_modules")
            apm_modules.mkdir(parents=True, exist_ok=True)

            # Use the canonical install path from the dependency reference
            target_path = dep_ref.get_install_path(apm_modules)

            # Check if already installed
            if target_path.exists():
                print(f"  [i]  Package already installed at {target_path}")
                return True

            # Download the virtual package
            downloader = GitHubPackageDownloader()

            print(f"   Downloading from {dep_ref.to_github_url()}")

            if dep_ref.is_virtual_collection():
                package_info = downloader.download_virtual_collection_package(
                    dep_ref, target_path
                )
            elif dep_ref.is_virtual_subdirectory():
                package_info = downloader.download_subdirectory_package(
                    dep_ref, target_path
                )
            else:
                package_info = downloader.download_virtual_file_package(
                    dep_ref, target_path
                )

            # PackageInfo has a 'package' attribute which is an APMPackage
            print(
                f"  [+] Installed {package_info.package.name} v{package_info.package.version}"
            )

            # Update apm.yml to include this dependency
            self._add_dependency_to_config(package_ref)

            return True

        except Exception as e:
            print(f"  [x] Auto-install failed: {e}")
            return False

    def _add_dependency_to_config(self, package_ref: str) -> None:
        """Add a virtual package dependency to apm.yml.

        Args:
            package_ref: Virtual package reference to add
        """
        config_path = Path("apm.yml")

        # Skip if apm.yml doesn't exist (e.g., in test environments)
        if not config_path.exists():
            return

        # Load current config
        from ..utils.yaml_io import load_yaml, dump_yaml
        config = load_yaml(config_path) or {}

        # Ensure dependencies.apm section exists
        if "dependencies" not in config:
            config["dependencies"] = {}
        if "apm" not in config["dependencies"]:
            config["dependencies"]["apm"] = []

        # Add the dependency if not already present
        if package_ref not in config["dependencies"]["apm"]:
            config["dependencies"]["apm"].append(package_ref)

            # Write back to file
            dump_yaml(config, config_path)

            print(f"  [i]  Added {package_ref} to apm.yml dependencies")

    def _create_minimal_config(self) -> None:
        """Create a minimal apm.yml for zero-config usage.

        This enables running virtual packages without apm init.
        """
        minimal_config = {
            "name": Path.cwd().name,
            "version": "1.0.0",
            "description": "Auto-generated for zero-config virtual package execution",
        }

        from ..utils.yaml_io import dump_yaml
        dump_yaml(minimal_config, "apm.yml")

        print(f"  [i]  Created minimal apm.yml for zero-config execution")

    def _detect_installed_runtime(self) -> str:
        """Detect installed runtime with priority order.

        Priority: copilot > codex > gemini > error

        Returns:
            Name of detected runtime

        Raises:
            RuntimeError: If no compatible runtime is found
        """
        import shutil

        if shutil.which("copilot"):
            return "copilot"
        elif shutil.which("codex"):
            return "codex"
        elif shutil.which("gemini"):
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


class PromptCompiler:
    """Compiles .prompt.md files with parameter substitution."""

    DEFAULT_COMPILED_DIR = Path(".apm/compiled")

    def __init__(self):
        """Initialize compiler."""
        self.compiled_dir = self.DEFAULT_COMPILED_DIR

    def compile(self, prompt_file: str, params: Dict[str, str]) -> str:
        """Compile a .prompt.md file with parameter substitution.

        Args:
            prompt_file: Path to the .prompt.md file
            params: Parameters to substitute

        Returns:
            Path to the compiled file
        """
        # Resolve the prompt file path - check local first, then dependencies
        prompt_path = self._resolve_prompt_file(prompt_file)

        # Now ensure compiled directory exists
        self.compiled_dir.mkdir(parents=True, exist_ok=True)

        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Parse frontmatter and content
        if content.startswith("---"):
            # Split frontmatter and content
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1].strip()
                main_content = parts[2].strip()
            else:
                main_content = content
        else:
            main_content = content

        # Substitute parameters in content
        compiled_content = self._substitute_parameters(main_content, params)

        # Generate output file path
        output_name = prompt_path.stem.replace(".prompt", "") + ".txt"
        output_path = self.compiled_dir / output_name

        # Write compiled content
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(compiled_content)

        return str(output_path)

    def _resolve_prompt_file(self, prompt_file: str) -> Path:
        """Resolve prompt file path, checking local directory first, then common directories, then dependencies.

        Symlinks are rejected outright to prevent traversal attacks.

        Args:
            prompt_file: Relative path to the .prompt.md file

        Returns:
            Path: Resolved path to the prompt file

        Raises:
            FileNotFoundError: If prompt file is not found or is a symlink
        """
        prompt_path = Path(prompt_file)

        # First check if it exists in current directory (local)
        if prompt_path.exists():
            if prompt_path.is_symlink():
                raise FileNotFoundError(
                    f"Prompt file '{prompt_file}' is a symlink. "
                    f"Symlinks are not allowed for security reasons."
                )
            return prompt_path

        # Check in common project directories
        common_dirs = [".github/prompts", ".apm/prompts"]
        for common_dir in common_dirs:
            common_path = Path(common_dir) / prompt_file
            if common_path.exists() and not common_path.is_symlink():
                return common_path

        # Search dependencies — scan directory tree once to avoid double walk
        apm_modules_dir = Path("apm_modules")
        dep_dirs = self._collect_dependency_dirs(apm_modules_dir)

        for _org_name, _repo_name, repo_dir in dep_dirs:
            dep_prompt_path = repo_dir / prompt_file
            if dep_prompt_path.exists() and not dep_prompt_path.is_symlink():
                return dep_prompt_path

            for subdir in ["prompts", ".", "workflows"]:
                sub_prompt_path = repo_dir / subdir / prompt_file
                if sub_prompt_path.exists() and not sub_prompt_path.is_symlink():
                    return sub_prompt_path

        # Build error using already-collected directories (no second walk)
        self._raise_prompt_not_found(prompt_file, prompt_path, dep_dirs)

    def _collect_dependency_dirs(self, apm_modules_dir: Path) -> list:
        """Collect (org_name, repo_name, repo_dir) tuples from apm_modules.

        Walks the two-level directory tree once so callers can iterate
        without repeated filesystem scans.

        Args:
            apm_modules_dir: Path to the apm_modules directory

        Returns:
            List of (org_name, repo_name, repo_dir) tuples
        """
        if not apm_modules_dir.exists():
            return []
        result = []
        for org_dir in apm_modules_dir.iterdir():
            if org_dir.is_dir() and not org_dir.name.startswith("."):
                for repo_dir in org_dir.iterdir():
                    if repo_dir.is_dir() and not repo_dir.name.startswith("."):
                        result.append((org_dir.name, repo_dir.name, repo_dir))
        return result

    def _raise_prompt_not_found(
        self, prompt_file: str, prompt_path: Path, dep_dirs: list,
    ) -> None:
        """Build and raise a helpful FileNotFoundError for a missing prompt.

        Args:
            prompt_file: Original prompt file reference
            prompt_path: Local Path that was checked
            dep_dirs: Pre-collected dependency directory tuples

        Raises:
            FileNotFoundError: Always — with a message listing searched locations
        """
        searched_locations = [
            f"Local: {prompt_path}",
            f"GitHub prompts: .github/prompts/{prompt_file}",
            f"APM prompts: .apm/prompts/{prompt_file}",
        ]

        if dep_dirs:
            searched_locations.append("Dependencies:")
            for org_name, repo_name, _repo_dir in dep_dirs:
                searched_locations.append(
                    f"  - {org_name}/{repo_name}/{prompt_file}"
                )

        raise FileNotFoundError(
            f"Prompt file '{prompt_file}' not found.\n"
            f"Searched in:\n"
            + "\n".join(searched_locations)
            + f"\n\nTip: Run 'apm install' to ensure dependencies are installed."
        )

    def _substitute_parameters(self, content: str, params: Dict[str, str]) -> str:
        """Substitute parameters in content.

        Args:
            content: Content to process
            params: Parameters to substitute

        Returns:
            Content with parameters substituted
        """
        result = content
        for key, value in params.items():
            # Replace ${input:key} placeholders
            placeholder = f"${{input:{key}}}"
            result = result.replace(placeholder, str(value))
        return result
