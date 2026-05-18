"""Distributed AGENTS.md compilation system following the Minimal Context Principle.

This module implements hierarchical directory-based distribution to generate multiple
AGENTS.md files across a project's directory structure, following the AGENTS.md standard
for nested agent context files.

Data models live in ``_dc_models``, orphan-management helpers in ``_dc_orphans``, and
content-generation / stats helpers in ``_dc_content``.  All public names are re-exported
from this module so external imports remain unchanged.
"""

from __future__ import annotations

import builtins
from collections import defaultdict
from pathlib import Path

from ..output.formatters import CompilationFormatter
from ..output.models import CompilationResults
from ..primitives.models import Instruction, PrimitiveCollection
from . import _dc_generation as _dcg
from ._dc_content import compile_distributed_stats, generate_agents_content, validate_coverage
from ._dc_models import CompilationResult, DirectoryMap, PlacementResult
from ._dc_orphans import (
    cleanup_orphaned_files,
    find_orphaned_agents_files,
    generate_orphan_warnings,
)
from .context_optimizer import ContextOptimizer
from .link_resolver import UnifiedLinkResolver

# CRITICAL: Shadow Click commands to prevent namespace collision
set = builtins.set
list = builtins.list
dict = builtins.dict

# Re-export public names so callers importing from this module are unaffected.
__all__ = [
    "CompilationResult",
    "DirectoryMap",
    "DistributedAgentsCompiler",
    "PlacementResult",
]


class DistributedAgentsCompiler:
    """Main compiler for generating distributed AGENTS.md files."""

    def __init__(self, base_dir: str = ".", exclude_patterns: builtins.list[str] | None = None):
        """Initialize the distributed AGENTS.md compiler.

        Args:
            base_dir (str): Base directory for compilation.
            exclude_patterns (Optional[List[str]]): Glob patterns for directories to exclude.
        """
        try:
            self.base_dir = Path(base_dir).resolve()
        except (OSError, FileNotFoundError):
            self.base_dir = Path(base_dir).absolute()

        self.warnings: builtins.list[str] = []
        self.errors: builtins.list[str] = []
        self.total_files_written = 0
        self.context_optimizer = ContextOptimizer(
            str(self.base_dir), exclude_patterns=exclude_patterns
        )
        self.link_resolver = UnifiedLinkResolver(self.base_dir)
        self.output_formatter = CompilationFormatter()
        self._placement_map = None

    def _handle_orphaned_files_cleanup(
        self, orphaned_files: list, dry_run: bool, clean_orphaned: bool
    ) -> None:
        """Emit warnings for orphaned files and run cleanup if configured."""
        warning_messages = generate_orphan_warnings(orphaned_files, self.base_dir)
        if warning_messages:
            self.warnings.extend(warning_messages)
        if not dry_run and clean_orphaned:
            cleanup_messages = cleanup_orphaned_files(orphaned_files, self.base_dir, dry_run=False)
            if cleanup_messages:
                self.warnings.extend(cleanup_messages)

    def _update_contexts_referenced_stat(self, placements: list, stats: dict) -> None:
        """Scan placement files for context references and record the count."""
        try:
            all_files_to_scan = []
            for placement in placements:
                for instruction in placement.instructions:
                    all_files_to_scan.append(instruction.file_path)
                for agent in placement.agents:
                    all_files_to_scan.append(agent.file_path)
            referenced_contexts = self.link_resolver.get_referenced_contexts(all_files_to_scan)
            stats["contexts_referenced"] = len(referenced_contexts)
        except Exception:
            stats["contexts_referenced"] = 0

    def compile_distributed(
        self, primitives: PrimitiveCollection, config: dict | None = None
    ) -> CompilationResult:
        """Compile primitives into distributed AGENTS.md files.

        Args:
            primitives (PrimitiveCollection): Collection of primitives to compile.
            config (Optional[dict]): Configuration for distributed compilation.
                - clean_orphaned (bool): Remove orphaned AGENTS.md files. Default: False
                - dry_run (bool): Preview mode, don't write files. Default: False

        Returns:
            CompilationResult: Result of the distributed compilation.
        """
        self.warnings.clear()
        self.errors.clear()

        try:
            # Configuration with defaults aligned to Minimal Context Principle
            config = config or {}
            min_instructions = config.get(
                "min_instructions_per_file", 1
            )  # Default to 1 for minimal context
            source_attribution = config.get("source_attribution", True)
            debug = config.get("debug", False)
            clean_orphaned = config.get("clean_orphaned", False)
            dry_run = config.get("dry_run", False)

            # Phase 0: Context Link Resolution
            # Register all context files and compile referenced ones
            self.link_resolver.register_contexts(primitives)

            # Build list of files to scan for context references
            all_files_to_scan = []
            all_files_to_scan.extend([i.file_path for i in primitives.instructions])
            all_files_to_scan.extend([c.file_path for c in primitives.chatmodes])

            # Include installed agents/prompts from .github/
            github_agents_dir = self.base_dir / ".github" / "agents"
            github_prompts_dir = self.base_dir / ".github" / "prompts"
            if github_agents_dir.exists():
                all_files_to_scan.extend(github_agents_dir.glob("*.agent.md"))
            if github_prompts_dir.exists():
                all_files_to_scan.extend(github_prompts_dir.glob("*.prompt.md"))

            # Phase 0: Validate context references (optional - for reporting)
            if debug:
                referenced_contexts = self.link_resolver.get_referenced_contexts(all_files_to_scan)
                if referenced_contexts:
                    self.warnings.append(
                        f"Found {len(referenced_contexts)} referenced context files"
                    )

            # Phase 1: Directory structure analysis
            directory_map = self.analyze_directory_structure(primitives.instructions)

            # Phase 2: Determine optimal AGENTS.md placement
            placement_map = self.determine_agents_placement(
                primitives.instructions,
                directory_map,
                min_instructions=min_instructions,
                debug=debug,
            )

            # Phase 3: Generate distributed AGENTS.md files
            placements = self.generate_distributed_agents_files(
                placement_map, primitives, source_attribution=source_attribution
            )

            # Phase 4: Handle orphaned file cleanup
            generated_paths = [p.agents_path for p in placements]
            orphaned_files = find_orphaned_agents_files(self.base_dir, generated_paths)

            if orphaned_files:
                self._handle_orphaned_files_cleanup(orphaned_files, dry_run, clean_orphaned)

            # Phase 5: Validate coverage
            coverage_validation = validate_coverage(placements, primitives.instructions)
            if coverage_validation:
                self.warnings.extend(coverage_validation)

            # Compile statistics
            stats = compile_distributed_stats(placements, primitives, self.context_optimizer)

            # Optional: Get referenced contexts for reporting (doesn't copy)
            self._update_contexts_referenced_stat(placements, stats)

            return CompilationResult(
                success=len(self.errors) == 0,
                placements=placements,
                content_map={
                    p.agents_path: generate_agents_content(
                        p, primitives, self.link_resolver, self.base_dir
                    )
                    for p in placements
                },
                warnings=self.warnings.copy(),
                errors=self.errors.copy(),
                stats=stats,
            )

        except Exception as e:
            self.errors.append(f"Distributed compilation failed: {e!s}")
            return CompilationResult(
                success=False,
                placements=[],
                content_map={},
                warnings=self.warnings.copy(),
                errors=self.errors.copy(),
                stats={},
            )

    def analyze_directory_structure(self, instructions: builtins.list[Instruction]) -> DirectoryMap:
        """Analyze project directory structure based on instruction patterns.

        Args:
            instructions (List[Instruction]): List of instructions to analyze.

        Returns:
            DirectoryMap: Analysis of the directory structure.
        """
        directories: builtins.dict[Path, builtins.set[str]] = defaultdict(set)
        depth_map: builtins.dict[Path, int] = {}
        parent_map: builtins.dict[Path, Path | None] = {}

        # Analyze each instruction's applyTo pattern
        for instruction in instructions:
            if not instruction.apply_to:
                continue

            pattern = instruction.apply_to

            # Extract directory paths from pattern
            dirs = self._extract_directories_from_pattern(pattern)

            for dir_path in dirs:
                abs_dir = self.base_dir / dir_path
                directories[abs_dir].add(pattern)

                # Calculate depth and parent relationships
                depth = len(abs_dir.resolve().relative_to(self.base_dir.resolve()).parts)
                depth_map[abs_dir] = depth

                if depth > 0:
                    parent_dir = abs_dir.parent
                    parent_map[abs_dir] = parent_dir
                    # Ensure parent is also tracked
                    if parent_dir not in directories:
                        directories[parent_dir] = set()
                else:
                    parent_map[abs_dir] = None

        # Add base directory
        directories[self.base_dir].update(
            instruction.apply_to for instruction in instructions if instruction.apply_to
        )
        depth_map[self.base_dir] = 0
        parent_map[self.base_dir] = None

        return DirectoryMap(
            directories=dict(directories), depth_map=depth_map, parent_map=parent_map
        )

    def determine_agents_placement(
        self,
        instructions: builtins.list[Instruction],
        directory_map: DirectoryMap,
        min_instructions: int = 1,
        debug: bool = False,
    ) -> builtins.dict[Path, builtins.list[Instruction]]:
        """Determine optimal AGENTS.md file placement using Context Optimization Engine.

        Following the Minimal Context Principle and Context Optimization, creates
        focused AGENTS.md files that minimize context pollution while maximizing
        relevance for agents working in specific directories.

        Args:
            instructions (List[Instruction]): List of instructions to place.
            directory_map (DirectoryMap): Directory structure analysis.
            min_instructions (int): Minimum instructions (default 1 for minimal context).
            max_depth (int): Maximum depth for placement.

        Returns:
            Dict[Path, List[Instruction]]: Optimized mapping of directory paths to instructions.
        """
        # Use the Context Optimization Engine for intelligent placement
        optimized_placement = self.context_optimizer.optimize_instruction_placement(
            instructions,
            verbose=debug,
            enable_timing=debug,  # Enable timing when debug mode is on
        )

        # Special case: if no instructions but constitution exists, create root placement
        if not optimized_placement:
            from .constitution import find_constitution

            constitution_path = find_constitution(Path(self.base_dir))
            if constitution_path.exists():
                # Create an empty placement for the root directory to enable verbose output
                optimized_placement = {Path(self.base_dir): []}

        # Store optimization results for output formatting later
        # Update with proper dry run status in the final result
        self._placement_map = optimized_placement

        # Remove the verbose warning log - we'll show this in professional output instead

        # Filter out directories with too few instructions if specified
        if min_instructions > 1:
            filtered_placement = {}
            for dir_path, dir_instructions in optimized_placement.items():
                if len(dir_instructions) >= min_instructions or dir_path == self.base_dir:
                    filtered_placement[dir_path] = dir_instructions
                else:
                    # Move instructions to parent directory
                    parent_dir = dir_path.parent if dir_path != self.base_dir else self.base_dir
                    if parent_dir not in filtered_placement:
                        filtered_placement[parent_dir] = []
                    filtered_placement[parent_dir].extend(dir_instructions)

            return filtered_placement

        return optimized_placement

    def generate_distributed_agents_files(
        self,
        placement_map: builtins.dict[Path, builtins.list[Instruction]],
        primitives: PrimitiveCollection,
        source_attribution: bool = True,
    ) -> builtins.list[PlacementResult]:
        """Delegate to _dc_generation."""
        return _dcg.generate_distributed_agents_files(
            self, placement_map, primitives, source_attribution
        )

    def _extract_directories_from_pattern(self, pattern: str) -> builtins.list[Path]:
        """Delegate to _dc_generation."""
        return _dcg._extract_directories_from_pattern(self, pattern)

    def _find_best_directory(
        self, instruction: Instruction, directory_map: DirectoryMap, max_depth: int
    ) -> Path:
        """Delegate to _dc_generation."""
        return _dcg._find_best_directory(self, instruction, directory_map, max_depth)

    def get_compilation_results_for_display(self, is_dry_run: bool = False):
        """Return formatted optimisation results for CLI display."""
        return self.context_optimizer.get_compilation_results(
            self._placement_map or {},
            is_dry_run=is_dry_run,
        )
