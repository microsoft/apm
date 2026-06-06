"""Mixin: orphan-file handling methods for DistributedAgentsCompiler.

Extracted from distributed_compiler.DistributedAgentsCompiler to stay under
the 800-line guardrail (Strangler Stage 2 / issue #1078).

No Rule B routing is required: none of the methods here reference the
module-level names patched by tests (build_attributed_instructions,
UnifiedLinkResolver, ContextOptimizer, CompilationFormatter, CompilationResults).
"""

from __future__ import annotations

import builtins

from ..utils.paths import portable_relpath


class _DistributedOrphansMixin:
    """Mixin: orphan AGENTS.md detection/cleanup and coverage validation."""

    def _find_orphaned_agents_files(self, generated_paths: builtins.list) -> builtins.list:
        """Find existing AGENTS.md files that weren't generated in the current compilation.

        Args:
            generated_paths (List[Path]): List of AGENTS.md files generated in current run.

        Returns:
            List[Path]: List of orphaned AGENTS.md files that should be cleaned up.
        """
        orphaned_files = []
        generated_set = builtins.set(generated_paths)

        # Find all existing AGENTS.md files in the project
        for agents_file in self.base_dir.rglob("AGENTS.md"):
            # Skip files that are outside our project or in special directories
            try:
                relative_path = agents_file.resolve().relative_to(self.base_dir.resolve())

                # Skip files in certain directories that shouldn't be cleaned
                skip_dirs = {
                    ".git",
                    ".apm",
                    "node_modules",
                    "__pycache__",
                    ".pytest_cache",
                    "apm_modules",
                }
                if any(part in skip_dirs for part in relative_path.parts):
                    continue

                # If this existing file wasn't generated in current run, it's orphaned
                if agents_file not in generated_set:
                    orphaned_files.append(agents_file)

            except ValueError:
                # File is outside base_dir, skip it
                continue

        return orphaned_files

    def _generate_orphan_warnings(self, orphaned_files: builtins.list) -> builtins.list:
        """Generate warning messages for orphaned AGENTS.md files.

        Args:
            orphaned_files (List[Path]): List of orphaned files to warn about.

        Returns:
            List[str]: List of warning messages.
        """
        warning_messages = []

        if not orphaned_files:
            return warning_messages

        # Professional warning format with readable list for multiple files
        if len(orphaned_files) == 1:
            rel_path = portable_relpath(orphaned_files[0], self.base_dir)
            warning_messages.append(
                f"Orphaned AGENTS.md found: {rel_path} - run 'apm compile --clean' to remove"
            )
        else:
            # For multiple files, create a single multi-line warning message
            file_list = []
            for file_path in orphaned_files[:5]:  # Show first 5
                rel_path = portable_relpath(file_path, self.base_dir)
                file_list.append(f"  * {rel_path}")
            if len(orphaned_files) > 5:
                file_list.append(f"  * ...and {len(orphaned_files) - 5} more")

            # Create one cohesive warning message
            files_text = "\n".join(file_list)
            warning_messages.append(
                f"Found {len(orphaned_files)} orphaned AGENTS.md files:\n{files_text}\n  Run 'apm compile --clean' to remove orphaned files"
            )

        return warning_messages

    def _cleanup_orphaned_files(
        self, orphaned_files: builtins.list, dry_run: bool = False
    ) -> builtins.list:
        """Actually remove orphaned AGENTS.md files.

        Args:
            orphaned_files (List[Path]): List of orphaned files to remove.
            dry_run (bool): If True, don't actually remove files, just report what would be removed.

        Returns:
            List[str]: List of cleanup status messages.
        """
        cleanup_messages = []

        if not orphaned_files:
            return cleanup_messages

        if dry_run:
            # In dry-run mode, just report what would be cleaned
            cleanup_messages.append(
                f"Would clean up {len(orphaned_files)} orphaned AGENTS.md files"
            )
            for file_path in orphaned_files:
                rel_path = portable_relpath(file_path, self.base_dir)
                cleanup_messages.append(f"  * {rel_path}")
        else:
            # Actually perform the cleanup
            cleanup_messages.append(f"Cleaning up {len(orphaned_files)} orphaned AGENTS.md files")
            for file_path in orphaned_files:
                try:
                    rel_path = portable_relpath(file_path, self.base_dir)
                    file_path.unlink()
                    cleanup_messages.append(f"  + Removed {rel_path}")
                except Exception as e:
                    cleanup_messages.append(f"  x Failed to remove {rel_path}: {e!s}")

        return cleanup_messages

    def _validate_coverage(
        self,
        placements: builtins.list,
        all_instructions: builtins.list,
    ) -> builtins.list:
        """Validate that all instructions are covered by placements.

        Args:
            placements (List[PlacementResult]): Generated placements.
            all_instructions (List[Instruction]): All available instructions.

        Returns:
            List[str]: List of coverage warnings.
        """
        warnings = []
        placed_instructions = builtins.set()

        for placement in placements:
            placed_instructions.update(str(inst.file_path) for inst in placement.instructions)

        all_instruction_paths = builtins.set(str(inst.file_path) for inst in all_instructions)

        missing_instructions = all_instruction_paths - placed_instructions
        if missing_instructions:
            warnings.append(
                f"Instructions not placed in any AGENTS.md: {', '.join(missing_instructions)}"
            )

        return warnings
