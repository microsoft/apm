"""Command integration functionality for APM packages.

Integrates .prompt.md files into runtime command directories during install,
mirroring how PromptIntegrator handles .github/prompts/.
"""

from pathlib import Path
from typing import List, Dict
from dataclasses import dataclass
import frontmatter

from apm_cli.compilation.link_resolver import UnifiedLinkResolver


@dataclass
class CommandIntegrationResult:
    """Result of command integration operation."""

    files_integrated: int
    files_updated: int
    files_skipped: int
    target_paths: List[Path]
    gitignore_updated: bool
    links_resolved: int = 0


class CommandIntegrator:
    """Handles integration of APM package prompts into command directories.

    Transforms .prompt.md files into runtime custom commands during package
    installation, following the same pattern as PromptIntegrator.
    """

    def __init__(self):
        """Initialize the command integrator."""
        self.link_resolver = None  # Lazy init when needed

    def should_integrate(self, project_root: Path) -> bool:
        """Check if command integration should be performed.

        Args:
            project_root: Root directory of the project

        Returns:
            bool: Always True - integration happens automatically
        """
        return True

    def _resolve_commands_dirs(self, project_root: Path) -> List[Path]:
        """Resolve target command directories based on project integrations.

        - .claude/commands when .claude exists
        - .opencode/commands when .opencode exists
        - legacy fallback to .claude/commands when neither root exists
        """
        command_dirs: List[Path] = []

        if (project_root / ".claude").exists():
            command_dirs.append(project_root / ".claude" / "commands")
        if (project_root / ".opencode").exists():
            command_dirs.append(project_root / ".opencode" / "commands")

        if not command_dirs:
            command_dirs.append(project_root / ".claude" / "commands")

        return command_dirs

    def find_prompt_files(self, package_path: Path) -> List[Path]:
        """Find all .prompt.md files in a package.

        Searches in:
        - Package root directory
        - .apm/prompts/ subdirectory

        Args:
            package_path: Path to the package directory

        Returns:
            List[Path]: List of absolute paths to .prompt.md files
        """
        prompt_files = []

        # Search in package root
        if package_path.exists():
            prompt_files.extend(package_path.glob("*.prompt.md"))

        # Search in .apm/prompts/
        apm_prompts = package_path / ".apm" / "prompts"
        if apm_prompts.exists():
            prompt_files.extend(apm_prompts.glob("*.prompt.md"))

        return prompt_files

    def _transform_prompt_to_command(self, source: Path) -> tuple:
        """Transform a .prompt.md file into command format.

        Args:
            source: Path to the .prompt.md file

        Returns:
            Tuple[str, frontmatter.Post, List[str]]: (command_name, post, warnings)
        """
        warnings: List[str] = []

        post = frontmatter.load(str(source))

        # Extract command name from filename
        filename = source.name
        if filename.endswith(".prompt.md"):
            command_name = filename[: -len(".prompt.md")]
        else:
            command_name = source.stem

        # Build command frontmatter (preserve existing runtime-compatible fields)
        claude_metadata = {}

        # Map APM frontmatter to Claude frontmatter
        if "description" in post.metadata:
            claude_metadata["description"] = post.metadata["description"]

        if "allowed-tools" in post.metadata:
            claude_metadata["allowed-tools"] = post.metadata["allowed-tools"]
        elif "allowedTools" in post.metadata:
            claude_metadata["allowed-tools"] = post.metadata["allowedTools"]

        if "model" in post.metadata:
            claude_metadata["model"] = post.metadata["model"]

        if "argument-hint" in post.metadata:
            claude_metadata["argument-hint"] = post.metadata["argument-hint"]
        elif "argumentHint" in post.metadata:
            claude_metadata["argument-hint"] = post.metadata["argumentHint"]

        # Create new post with Claude metadata
        new_post = frontmatter.Post(post.content)
        new_post.metadata = claude_metadata

        return (command_name, new_post, warnings)

    def integrate_command(
        self, source: Path, target: Path, package_info, original_path: Path
    ) -> int:
        """Integrate a prompt file as a command (verbatim copy with format conversion).

        Args:
            source: Source .prompt.md file path
            target: Target command file path in runtime commands dir
            package_info: PackageInfo object with package metadata
            original_path: Original path to the prompt file

        Returns:
            int: Number of links resolved
        """
        # Transform to command format
        command_name, post, warnings = self._transform_prompt_to_command(source)

        # Resolve context links in content
        links_resolved = 0
        if self.link_resolver:
            import re

            original_content = post.content
            resolved_content = self.link_resolver.resolve_links_for_installation(
                content=post.content, source_file=source, target_file=target
            )
            post.content = resolved_content
            if resolved_content != original_content:
                link_pattern = re.compile(r"\]\(([^)]+)\)")
                original_links = set(link_pattern.findall(original_content))
                resolved_links = set(link_pattern.findall(resolved_content))
                links_resolved = len(original_links - resolved_links)

        # Ensure target directory exists
        target.parent.mkdir(parents=True, exist_ok=True)

        # Write the command file
        with open(target, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

        return links_resolved

    def integrate_package_commands(
        self, package_info, project_root: Path
    ) -> CommandIntegrationResult:
        """Integrate all prompt files from a package as runtime commands.

        Args:
            package_info: PackageInfo object with package metadata and install path
            project_root: Root directory of the project

        Returns:
            CommandIntegrationResult: Result of integration
        """
        commands_dirs = self._resolve_commands_dirs(project_root)

        prompt_files = self.find_prompt_files(package_info.install_path)

        if not prompt_files:
            return CommandIntegrationResult(
                files_integrated=0,
                files_updated=0,
                files_skipped=0,
                target_paths=[],
                gitignore_updated=False,
                links_resolved=0,
            )

        # Initialize link resolver if needed
        if self.link_resolver is None:
            self.link_resolver = UnifiedLinkResolver(project_root)

        files_integrated = 0
        target_paths = []
        total_links_resolved = 0

        for prompt_file in prompt_files:
            # Generate command name with package suffix for uniqueness
            filename = prompt_file.name
            if filename.endswith(".prompt.md"):
                base_name = filename[: -len(".prompt.md")]
            else:
                base_name = prompt_file.stem

            # Add -apm suffix to distinguish from local prompts
            command_name = f"{base_name}-apm"
            for commands_dir in commands_dirs:
                target_path = commands_dir / f"{command_name}.md"

                # Always overwrite
                links_resolved = self.integrate_command(
                    prompt_file, target_path, package_info, prompt_file
                )
                files_integrated += 1
                total_links_resolved += links_resolved
                target_paths.append(target_path)

        # Update .gitignore
        gitignore_updated = self._update_gitignore(project_root)

        return CommandIntegrationResult(
            files_integrated=files_integrated,
            files_updated=0,
            files_skipped=0,
            target_paths=target_paths,
            gitignore_updated=gitignore_updated,
            links_resolved=total_links_resolved,
        )

    def _update_gitignore(self, project_root: Path) -> bool:
        """Add runtime command patterns to .gitignore if needed.

        Args:
            project_root: Root directory of the project

        Returns:
            bool: True if .gitignore was updated
        """
        gitignore_path = project_root / ".gitignore"
        command_dirs = self._resolve_commands_dirs(project_root)

        patterns = []
        for commands_dir in command_dirs:
            if ".claude" in commands_dir.parts:
                patterns.append(".claude/commands/*-apm.md")
            elif ".opencode" in commands_dir.parts:
                patterns.append(".opencode/commands/*-apm.md")

        # de-duplicate while keeping order
        patterns = list(dict.fromkeys(patterns))

        existing_content = ""
        if gitignore_path.exists():
            existing_content = gitignore_path.read_text()

        # Check if all patterns already exist
        if patterns and all(pattern in existing_content for pattern in patterns):
            return False

        lines_to_add = ["# APM-generated commands", *patterns]
        new_content = (
            existing_content.rstrip() + "\n\n" + "\n".join(lines_to_add) + "\n"
        )
        gitignore_path.write_text(new_content)
        return True

    def sync_integration(self, apm_package, project_root: Path) -> Dict:
        """Remove all APM-managed command files for clean regeneration.

        Args:
            apm_package: APMPackage (unused, kept for interface compatibility)
            project_root: Root directory of the project

        Returns:
            Dict with cleanup stats: {'files_removed': int, 'errors': int}
        """
        stats = {"files_removed": 0, "errors": 0}

        command_dirs = [
            project_root / ".claude" / "commands",
            project_root / ".opencode" / "commands",
        ]

        for commands_dir in command_dirs:
            if not commands_dir.exists():
                continue

            for cmd_file in commands_dir.glob("*-apm.md"):
                try:
                    cmd_file.unlink()
                    stats["files_removed"] += 1
                except Exception:
                    stats["errors"] += 1

        return stats

    def remove_package_commands(self, package_name: str, project_root: Path) -> int:
        """Remove all APM-managed command files.

        Args:
            package_name: Name of the package (unused, all -apm files are removed)
            project_root: Root directory of the project

        Returns:
            int: Number of files removed
        """
        files_removed = 0

        for commands_dir in (
            project_root / ".claude" / "commands",
            project_root / ".opencode" / "commands",
        ):
            if not commands_dir.exists():
                continue

            for cmd_file in commands_dir.glob("*-apm.md"):
                try:
                    cmd_file.unlink()
                    files_removed += 1
                except Exception:
                    pass

        return files_removed
