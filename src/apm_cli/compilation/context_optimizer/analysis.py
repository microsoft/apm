"""Context Optimizer for APM distributed compilation system.

This module implements the Context Optimization Engine that minimizes
irrelevant context loaded by agents working in specific directories,
following the Minimal Context Principle.
"""

import builtins
import os
import time
from pathlib import Path

from ...output.models import (
    CompilationResults,
    OptimizationStats,
    PlacementSummary,
    ProjectAnalysis,
)
from ...primitives.models import Instruction
from ...utils.paths import portable_relpath
from .class_ import DirectoryAnalysis, InheritanceAnalysis

set = builtins.set
list = builtins.list
dict = builtins.dict


def analyze_context_inheritance(
    self,
    working_directory: Path,
    placement_map: builtins.dict[Path, builtins.list[Instruction]],
) -> InheritanceAnalysis:
    """Analyze context inheritance chain for a working directory.

    Args:
        working_directory (Path): Directory where agent is working.
        placement_map (Dict[Path, List[Instruction]]): Current placement mapping.

    Returns:
        InheritanceAnalysis: Analysis of inheritance efficiency.
    """
    inheritance_chain = self._get_inheritance_chain(working_directory)

    total_context = 0
    relevant_context = 0

    for directory in inheritance_chain:
        if directory in placement_map:
            instructions = placement_map[directory]
            total_context += len(instructions)

            # Count relevant instructions for working directory
            for instruction in instructions:
                if self._is_instruction_relevant(instruction, working_directory):
                    relevant_context += 1

    pollution_score = 1.0 - (relevant_context / total_context) if total_context > 0 else 0.0

    return InheritanceAnalysis(
        working_directory=working_directory,
        inheritance_chain=inheritance_chain,
        total_context_load=total_context,
        relevant_context_load=relevant_context,
        pollution_score=pollution_score,
    )


def get_optimization_stats(
    self, placement_map: builtins.dict[Path, builtins.list[Instruction]]
) -> OptimizationStats:
    """Calculate optimization statistics for the placement map."""
    if not placement_map:
        return OptimizationStats(
            average_context_efficiency=0.0,
            total_agents_files=0,
            directories_analyzed=len(self._directory_cache),
        )

    # Calculate average context efficiency across all directories with files
    all_directories = set(self._directory_cache.keys())
    efficiency_scores = []

    for directory in all_directories:
        if self._directory_cache[directory].total_files > 0:
            inheritance = self.analyze_context_inheritance(directory, placement_map)
            efficiency_scores.append(inheritance.get_efficiency_ratio())

    average_efficiency = (
        sum(efficiency_scores) / len(efficiency_scores) if efficiency_scores else 0.0
    )

    return OptimizationStats(
        average_context_efficiency=average_efficiency,
        total_agents_files=len(placement_map),
        directories_analyzed=len(self._directory_cache),
    )


def get_compilation_results(
    self,
    placement_map: builtins.dict[Path, builtins.list[Instruction]],
    is_dry_run: bool = False,
) -> CompilationResults:
    """Generate comprehensive compilation results for output formatting.

    Args:
        placement_map: Final instruction placement mapping.
        is_dry_run: Whether this is a dry run.

    Returns:
        CompilationResults with all analysis data.
    """
    # Calculate generation time
    generation_time_ms = None
    if self._start_time is not None:
        generation_time_ms = int((time.time() - self._start_time) * 1000)

    # Create project analysis
    file_types = set()
    total_files = 0

    for analysis in self._directory_cache.values():
        file_types.update(analysis.file_types)
        total_files += analysis.total_files

    # Check for constitution
    from ..constitution import find_constitution

    constitution_path = find_constitution(Path(self.base_dir))
    constitution_detected = constitution_path.exists()

    project_analysis = ProjectAnalysis(
        directories_scanned=len(self._directory_cache),
        files_analyzed=total_files,
        file_types_detected=file_types,
        instruction_patterns_detected=len(self._optimization_decisions),
        max_depth=max((a.depth for a in self._directory_cache.values()), default=0),
        constitution_detected=constitution_detected,
        constitution_path=portable_relpath(constitution_path, self.base_dir)
        if constitution_detected
        else None,
    )

    # Create placement summaries
    placement_summaries = []

    # Special case: if no instructions but constitution exists, create root placement
    if not placement_map and constitution_detected:
        # Create a root placement for constitution-only projects
        root_sources = {"constitution.md"}
        summary = PlacementSummary(
            path=Path(self.base_dir),
            instruction_count=0,
            source_count=len(root_sources),
            sources=list(root_sources),
        )
        placement_summaries.append(summary)
    else:
        # Normal case: create summaries for each placement in the map
        for directory, instructions in placement_map.items():
            # Count unique sources
            sources = set()
            for instruction in instructions:
                if hasattr(instruction, "source_file") and instruction.source_file:
                    sources.add(instruction.source_file)
                elif hasattr(instruction, "source") and instruction.source:
                    sources.add(str(instruction.source))

            # Add constitution as a source if it exists and will be injected
            if constitution_detected:
                sources.add("constitution.md")

            summary = PlacementSummary(
                path=directory,
                instruction_count=len(instructions),
                source_count=len(sources),
                sources=list(sources),
            )
            placement_summaries.append(summary)

    # Get optimization statistics
    optimization_stats = self.get_optimization_stats(placement_map)
    optimization_stats.generation_time_ms = generation_time_ms

    return CompilationResults(
        project_analysis=project_analysis,
        optimization_decisions=self._optimization_decisions.copy(),
        placement_summaries=placement_summaries,
        optimization_stats=optimization_stats,
        warnings=self._warnings.copy(),
        errors=self._errors.copy(),
        is_dry_run=is_dry_run,
    )


from ._scoring import (  # noqa: E402, F401
    _calculate_coverage_efficiency,
    _calculate_distribution_score,
    _calculate_hierarchical_coverage,
    _calculate_inheritance_pollution,
    _calculate_maintenance_locality,
    _calculate_pollution_minimization,
    _is_hierarchically_covered,
)


def _get_inheritance_chain(self, working_directory: Path) -> builtins.list[Path]:
    """Get inheritance chain from working directory to root.

    Args:
        working_directory (Path): Starting directory.

    Returns:
        List[Path]: Inheritance chain (most specific to root).
    """
    cached = self._inheritance_cache.get(working_directory)
    if cached is not None:
        return cached

    chain = []
    # Resolve the starting directory to ensure consistent path comparison
    try:
        current = working_directory.resolve()
    except (OSError, ValueError):
        current = working_directory.absolute()

    seen_paths = set()  # Track visited paths to prevent infinite loops

    # Build chain from working directory up to (and including) base_dir
    while current not in seen_paths:
        seen_paths.add(current)
        chain.append(current)

        # Stop at base_dir
        if current == self.base_dir:
            break

        # Stop if we can't go higher or hit filesystem root
        try:
            parent = current.parent
            if parent == current:  # We've hit filesystem root
                break
            current = parent
        except (OSError, ValueError):
            break

    self._inheritance_cache[working_directory] = chain
    return chain


def _is_child_directory(self, child: Path, parent: Path) -> bool:
    """Check if child is a subdirectory of parent.

    Args:
        child (Path): Potential child directory.
        parent (Path): Potential parent directory.

    Returns:
        bool: True if child is subdirectory of parent.
    """
    try:
        child.resolve().relative_to(parent.resolve())
        return child.resolve() != parent.resolve()
    except ValueError:
        return False


def _is_instruction_relevant(self, instruction: Instruction, working_directory: Path) -> bool:
    """Check if instruction is relevant for the working directory.

    Args:
        instruction (Instruction): Instruction to check.
        working_directory (Path): Directory where agent is working.

    Returns:
        bool: True if instruction is relevant.
    """
    if not instruction.apply_to:
        return True  # Global instructions are always relevant

    pattern = instruction.apply_to

    # Resolve working directory to handle path inconsistencies
    try:
        resolved_working_dir = working_directory.resolve()
    except (OSError, ValueError):
        resolved_working_dir = working_directory.absolute()

    # Check if working directory has files matching the pattern
    analysis = self._directory_cache.get(resolved_working_dir)
    if not analysis:
        return False

    # If pattern already analyzed, use cached result
    if pattern in analysis.pattern_matches:
        return analysis.pattern_matches[pattern] > 0

    # Otherwise, analyze this specific directory for the pattern
    # Only check direct files in this directory (not subdirectories for simplicity)
    matching_files = 0

    try:
        for file in os.listdir(resolved_working_dir):
            if file.startswith("."):
                continue

            file_path = resolved_working_dir / file
            if file_path.is_file():
                if self._file_matches_pattern(file_path, pattern):
                    matching_files += 1
    except (OSError, PermissionError):
        # Handle case where directory doesn't exist or can't be read
        pass

    # Cache the result
    analysis.pattern_matches[pattern] = matching_files

    return matching_files > 0


# ---------------------------------------------------------------------------
# Re-exports from sibling private modules
#
# ``class_.py`` imports this module as ``_analysis`` and accesses every
# function via attribute lookup (e.g. ``_analysis._analyze_project_structure``).
# Importing the moved functions here keeps that interface intact without
# requiring any changes to ``class_.py``.
# ---------------------------------------------------------------------------
from ._matching import (  # noqa: E402, F401
    _expand_glob_pattern,
    _extract_intended_directory_from_pattern,
    _file_matches_pattern,
    _find_matching_directories,
)
from ._traversal import (  # noqa: E402, F401
    _analyze_project_structure,
    _should_exclude_path,
    _should_exclude_subdir,
)
