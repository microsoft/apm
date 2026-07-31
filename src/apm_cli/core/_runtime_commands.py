"""_RuntimeCommandsMixin — runtime-command builder methods for ScriptRunner.

Extracted from script_runner.py (Strangler Stage 2, #1078).
Composed into ScriptRunner via ``class ScriptRunner(_RuntimeCommandsMixin)``.

Rule B: ``_detect_installed_runtime`` references ``find_runtime_binary`` which
tests patch at ``apm_cli.core.script_runner.find_runtime_binary``.  The method
uses a function-level late import to route through the origin module so patches
are intercepted correctly.
"""

import re
from pathlib import Path


class _RuntimeCommandsMixin:
    """Mixin carrying the runtime-command builder cluster for ScriptRunner."""

    # ------------------------------------------------------------------
    # Command transformation helpers
    # ------------------------------------------------------------------

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
        import apm_cli.core.script_runner as _sr

        runtime_commands = _sr.runtime_names()

        # Try matching with env-var prefix (e.g. "ENV=val codex args file.prompt.md")
        for runtime_cmd in runtime_commands:
            runtime_pattern = f" {runtime_cmd} "
            if runtime_pattern in command and re.search(re.escape(prompt_file), command):
                parts = command.split(runtime_pattern, 1)
                potential_env_part = parts[0]
                runtime_part = runtime_cmd + " " + parts[1]

                if "=" in potential_env_part and not potential_env_part.startswith(runtime_cmd):
                    result = self._parse_and_build_runtime_command(
                        runtime_cmd,
                        runtime_part,
                        prompt_file,
                        env_prefix=potential_env_part,
                    )
                    if result is not None:
                        return result

        # Try individual runtime patterns without environment variables
        for runtime_cmd in runtime_commands:
            if re.search(r"^" + runtime_cmd + r"\s+.*" + re.escape(prompt_file), command):
                result = self._parse_and_build_runtime_command(
                    runtime_cmd,
                    command,
                    prompt_file,
                )
                if result is not None:
                    return result

        # Handle bare "file.prompt.md" -> "codex exec" (default to codex)
        if command.strip() == prompt_file:
            return "codex exec"

        # Fallback: just replace file path with compiled path (for non-runtime commands)
        return command.replace(prompt_file, compiled_path)

    def _parse_and_build_runtime_command(
        self,
        runtime_cmd: str,
        command_part: str,
        prompt_file: str,
        env_prefix: str = None,  # noqa: RUF013
    ) -> str | None:
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

        import apm_cli.core.script_runner as _sr

        descriptor = _sr.get_runtime_descriptor(runtime_cmd)
        builder = (
            getattr(self, descriptor.script_builder)
            if descriptor.script_builder is not None
            else None
        )
        if builder:
            return builder(args_before, args_after, env_prefix)
        return None

    def _build_codex_command(
        self,
        args_before: str,
        args_after: str,
        env_prefix: str | None = None,
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
        self,
        args_before: str,
        args_after: str,
        env_prefix: str | None = None,
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
        self,
        args_before: str,
        args_after: str,
        env_prefix: str | None = None,
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
        self,
        args_before: str,
        args_after: str,
        env_prefix: str | None = None,
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
            cleaned_args = re.sub(r"(^|\s)-p(?=\s|$)", "", args_before).strip()
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
        import apm_cli.core.script_runner as _sr

        for runtime_name in _sr.runtime_names():
            if re.search(rf"(?:^|\s){re.escape(runtime_name)}(?:\s|$)", command_lower):
                return runtime_name
        return "unknown"

    def _generate_runtime_command(self, runtime: str, prompt_file: Path) -> str:
        """Generate appropriate runtime command with proper defaults.

        Args:
            runtime: Name of runtime (copilot or codex)
            prompt_file: Path to the prompt file

        Returns:
            Full command string with runtime-specific defaults
        """
        import apm_cli.core.script_runner as _sr

        try:
            descriptor = _sr.get_runtime_descriptor(runtime)
        except ValueError:
            raise ValueError(f"Unsupported runtime: {runtime}") from None
        if descriptor.default_command is None:
            raise ValueError(f"Unsupported runtime: {runtime}")
        return descriptor.default_command.format(prompt_file=prompt_file)

    def _detect_installed_runtime(self) -> str:
        """Detect installed runtime with priority order.

        Priority: copilot > codex > gemini > error

        Rule B: ``find_runtime_binary`` is patched at
        ``apm_cli.core.script_runner.find_runtime_binary`` in tests;
        route through the origin module via a function-level import.

        Returns:
            Name of detected runtime

        Raises:
            RuntimeError: If no compatible runtime is found
        """
        import apm_cli.core.script_runner as _sr

        executable_descriptors = [
            descriptor
            for descriptor in _sr.runtime_descriptors()
            if descriptor.default_command is not None
        ]
        for descriptor in executable_descriptors:
            if _sr.find_runtime_binary(descriptor.binary):
                return descriptor.name
        setup_lines = "\n".join(
            f"  apm runtime setup {descriptor.name}" for descriptor in executable_descriptors
        )
        raise RuntimeError(
            f"No compatible runtime found.\nInstall a supported runtime with one of:\n{setup_lines}"
        )
