"""PromptCompiler — compile .prompt.md files with parameter substitution.

Extracted from script_runner.py (Strangler Stage 2, #1078).
Re-exported from apm_cli.core.script_runner as ``PromptCompiler``.
"""

from pathlib import Path


class PromptCompiler:
    """Compiles .prompt.md files with parameter substitution."""

    DEFAULT_COMPILED_DIR = Path(".apm/compiled")

    def __init__(self):
        """Initialize compiler."""
        self.compiled_dir = self.DEFAULT_COMPILED_DIR

    def compile(self, prompt_file: str, params: dict[str, str]) -> str:
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

        with open(prompt_path, encoding="utf-8") as f:
            content = f.read()

        # Parse frontmatter and content
        if content.startswith("---"):
            # Split frontmatter and content
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1].strip()  # noqa: F841
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
        self,
        prompt_file: str,
        prompt_path: Path,
        dep_dirs: list,
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
                searched_locations.append(f"  - {org_name}/{repo_name}/{prompt_file}")

        raise FileNotFoundError(
            f"Prompt file '{prompt_file}' not found.\n"
            f"Searched in:\n"
            + "\n".join(searched_locations)
            + f"\n\nTip: Run 'apm install' to ensure dependencies are installed."  # noqa: F541
        )

    def _substitute_parameters(self, content: str, params: dict[str, str]) -> str:
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
