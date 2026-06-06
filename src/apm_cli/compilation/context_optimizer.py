"""Context Optimizer for APM distributed compilation system.

This module implements the Context Optimization Engine that minimizes
irrelevant context loaded by agents working in specific directories,
following the Minimal Context Principle.
"""

import builtins
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..output.models import (
    CompilationResults,
    OptimizationDecision,
    OptimizationStats,
    PlacementStrategy,
    PlacementSummary,
    ProjectAnalysis,
)
from ..primitives.models import Instruction
from ..utils.exclude import matches_glob, should_exclude, validate_exclude_patterns
from ..utils.paths import portable_relpath
from ._pattern_matcher import _PatternMatcherMixin
from ._placement_solver import _PlacementSolverMixin

# CRITICAL: Shadow Click commands to prevent namespace collision
# When this module is imported during 'apm compile', Click's active context
# can cause set/list/dict to resolve to Click commands instead of builtins
set = builtins.set
list = builtins.list
dict = builtins.dict

# Default directory names excluded from compilation scanning.
# Shared across _analyze_project_structure, _should_exclude_subdir, and _get_all_files.
DEFAULT_EXCLUDED_DIRNAMES = frozenset(
    {
        "node_modules",
        "__pycache__",
        ".git",
        "dist",
        "build",
        "apm_modules",
    }
)


@dataclass
class DirectoryAnalysis:
    """Analysis of a directory's file distribution and patterns."""

    directory: Path
    depth: int
    total_files: int
    pattern_matches: builtins.dict[str, int] = field(default_factory=dict)  # pattern -> count
    file_types: builtins.set[str] = field(default_factory=set)

    def get_relevance_score(self, pattern: str) -> float:
        """Calculate relevance score for a pattern in this directory."""
        if self.total_files == 0:
            return 0.0
        matches = self.pattern_matches.get(pattern, 0)
        return matches / self.total_files


@dataclass
class InheritanceAnalysis:
    """Analysis of context inheritance chain for a working directory."""

    working_directory: Path
    inheritance_chain: builtins.list[Path]  # From most specific to root
    total_context_load: int = 0
    relevant_context_load: int = 0
    pollution_score: float = 0.0

    def get_efficiency_ratio(self) -> float:
        """Calculate context efficiency ratio."""
        if self.total_context_load == 0:
            return 1.0
        return self.relevant_context_load / self.total_context_load


@dataclass
class PlacementCandidate:
    """Candidate placement for an instruction with optimization scores."""

    instruction: Instruction
    directory: Path
    direct_relevance: float
    inheritance_pollution: float
    depth_specificity: float
    total_score: float

    def __post_init__(self):
        """Calculate total optimization score."""
        self.total_score = (
            self.direct_relevance * 1.0  # Direct relevance weight
            + -self.inheritance_pollution * 0.5  # Pollution penalty
            + self.depth_specificity * 0.1  # Depth bonus
        )


class ContextOptimizer(_PlacementSolverMixin, _PatternMatcherMixin):
    """Context Optimization Engine for distributed AGENTS.md placement."""

    # Mathematical optimization parameters
    COVERAGE_EFFICIENCY_WEIGHT = 1.0
    POLLUTION_MINIMIZATION_WEIGHT = 0.8
    MAINTENANCE_LOCALITY_WEIGHT = 0.3
    DEPTH_PENALTY_FACTOR = 0.1
    DIVERSITY_FACTOR_BASE = 0.5

    # Distribution score thresholds for placement strategy
    LOW_DISTRIBUTION_THRESHOLD = 0.3
    HIGH_DISTRIBUTION_THRESHOLD = 0.7

    def __init__(self, base_dir: str = ".", exclude_patterns: builtins.list[str] | None = None):
        """Initialize the context optimizer.

        Args:
            base_dir (str): Base directory for optimization analysis.
            exclude_patterns (Optional[List[str]]): Glob patterns for directories to exclude.
        """
        try:
            self.base_dir = Path(base_dir).resolve()
        except (OSError, FileNotFoundError):
            self.base_dir = Path(base_dir).absolute()

        self._directory_cache: builtins.dict[Path, DirectoryAnalysis] = {}
        self._pattern_cache: builtins.dict[str, builtins.set[Path]] = {}

        # Performance optimization caches
        self._glob_cache: builtins.dict[str, builtins.list[str]] = {}
        self._glob_set_cache: builtins.dict[str, builtins.set[Path]] = {}
        self._file_list_cache: builtins.list[Path] | None = None
        self._inheritance_cache: builtins.dict[Path, builtins.list[Path]] = {}  # (#171)
        self._timing_enabled = False
        self._phase_timings: builtins.dict[str, float] = {}

        # Data collection for output formatting
        self._optimization_decisions: builtins.list[OptimizationDecision] = []
        self._warnings: builtins.list[str] = []
        self._errors: builtins.list[str] = []
        self._start_time: float | None = None

        # Configurable exclusion patterns (validated at init time)
        self._exclude_patterns = validate_exclude_patterns(exclude_patterns)

    def enable_timing(self, verbose: bool = False):
        """Enable performance timing instrumentation."""
        self._timing_enabled = verbose
        self._phase_timings.clear()

    def _time_phase(self, phase_name: str, operation_func, *args, **kwargs):
        """Time a phase of optimization and optionally log it."""
        if not self._timing_enabled:
            return operation_func(*args, **kwargs)

        start_time = time.time()
        result = operation_func(*args, **kwargs)
        duration = time.time() - start_time
        self._phase_timings[phase_name] = duration

        # Only show timing in verbose mode with professional formatting
        if self._timing_enabled and hasattr(self, "_verbose") and self._verbose:
            print(f"  {phase_name}: {duration * 1000:.1f}ms")
        return result

    def _cached_glob(self, pattern: str) -> builtins.list[str]:
        """Return project files matching ``pattern`` (``**`` recursive), cached.

        Replaces ``glob.glob(pattern, recursive=True)``, whose ``**`` follows
        directory **symlinks** and descends excluded trees like
        ``node_modules``/``dist``. On a pnpm project the symlinked ``.pnpm``
        store exposes shared packages through exponentially many paths, so a
        single ``**`` pattern made ``apm compile`` walk an effectively
        unbounded space -- pinning one core near 100% CPU with multi-GB RSS and
        never terminating.

        Instead, filter the cached project file list (:meth:`_get_all_files`,
        which walks once with :func:`os.walk` -- no symlink following -- and
        prunes excluded/hidden dirs) with the shared ``**``-aware matcher
        (:func:`apm_cli.utils.exclude.matches_glob`).
        """
        if pattern not in self._glob_cache:
            self._glob_cache[pattern] = self._safe_recursive_glob(pattern)
        return self._glob_cache[pattern]

    def _safe_recursive_glob(self, pattern: str) -> builtins.list[str]:
        """Symlink-safe, exclusion-aware ``glob(recursive=True)`` replacement:
        match the cached project file list against ``pattern`` (POSIX rel paths).
        """
        results: builtins.list[str] = []
        for path in self._get_all_files():
            rel = portable_relpath(path, self.base_dir)
            if matches_glob(rel, pattern):
                results.append(rel)
        return results

    def _get_all_files(self) -> builtins.list[Path]:
        """Get cached list of all files in project."""
        if self._file_list_cache is None:
            self._file_list_cache = []
            for root, dirs, files in os.walk(self.base_dir):
                # Skip hidden and excluded directories for performance
                # Sort to guarantee deterministic traversal order across filesystems
                dirs[:] = sorted(
                    d for d in dirs if not d.startswith(".") and d not in DEFAULT_EXCLUDED_DIRNAMES
                )
                for file in sorted(files):
                    if not file.startswith("."):
                        self._file_list_cache.append(Path(root) / file)
        return self._file_list_cache

    def optimize_instruction_placement(
        self,
        instructions: builtins.list[Instruction],
        verbose: bool = False,
        enable_timing: bool = False,
    ) -> builtins.dict[Path, builtins.list[Instruction]]:
        """Optimize placement of instructions across directories with performance timing.

        Args:
            instructions (List[Instruction]): Instructions to optimize.
            verbose (bool): Collect verbose analysis data.
            enable_timing (bool): Enable detailed timing measurements.

        Returns:
            Dict[Path, List[Instruction]]: Optimized placement mapping.
        """
        self._start_time = time.time()
        self._timing_enabled = enable_timing
        self._verbose = verbose  # Store verbose mode for timing display

        # Don't show the "timing enabled" message - it's not professional
        if enable_timing and verbose:
            self._compilation_start_time = time.time()

        self.enable_timing(verbose)
        self._optimization_decisions.clear()
        self._warnings.clear()
        self._errors.clear()

        # Phase 1: Analyze project structure
        self._time_phase("Project Analysis", self._analyze_project_structure)

        # Phase 2: Analyze each instruction for optimal placement
        placement_map: builtins.dict[Path, builtins.list[Instruction]] = defaultdict(list)

        def process_instructions():
            for instruction in instructions:
                if not instruction.apply_to:
                    # Instructions without patterns go to root
                    placement_map[self.base_dir].append(instruction)

                    # Record global instruction decision
                    # Global instructions have maximum relevance since they apply everywhere
                    global_relevance = 1.0

                    self._optimization_decisions.append(
                        OptimizationDecision(
                            instruction=instruction,
                            pattern="(global)",
                            matching_directories=1,
                            total_directories=len(self._directory_cache),
                            distribution_score=1.0,
                            strategy=PlacementStrategy.DISTRIBUTED,
                            placement_directories=[self.base_dir],
                            reasoning="Global instruction placed at project root",
                            relevance_score=global_relevance,
                        )
                    )
                    continue

                optimal_placements = self._find_optimal_placements(instruction, verbose)

                # Add instruction to optimal placement(s)
                for directory in optimal_placements:
                    placement_map[directory].append(instruction)

        self._time_phase("Instruction Processing", process_instructions)

        return dict(placement_map)

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
        from .constitution import find_constitution

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

    def _analyze_project_structure(self) -> None:
        """Analyze the project structure and cache results."""
        self._directory_cache.clear()
        self._pattern_cache.clear()  # Also clear pattern cache for deterministic behavior

        # Track visited directories to prevent infinite loops
        visited_dirs = set()

        for root, dirs, files in os.walk(self.base_dir):
            current_path = Path(root)

            # Safety check for infinite loops
            if current_path in visited_dirs:
                continue
            visited_dirs.add(current_path)

            # Calculate depth for analysis
            try:
                relative_path = current_path.resolve().relative_to(self.base_dir.resolve())
                depth = len(relative_path.parts)
            except ValueError:
                depth = 0

            # Skip hidden directories and common ignore patterns
            if any(part.startswith(".") for part in current_path.parts[len(self.base_dir.parts) :]):
                continue

            # Default hardcoded exclusions  -- match on exact path components
            if any(part in DEFAULT_EXCLUDED_DIRNAMES for part in relative_path.parts):
                continue

            # Apply configurable exclusion patterns
            if self._should_exclude_path(current_path):
                continue

            # Prune subdirectories from os.walk to avoid descending into excluded paths
            # This significantly improves performance by avoiding expensive traversal
            # Note: Modifying dirs[:] (slice assignment) is the standard Python idiom
            # to control which subdirectories os.walk will descend into
            dirs[:] = [d for d in dirs if not self._should_exclude_subdir(current_path / d)]

            # Analyze files in this directory
            total_files = len([f for f in files if not f.startswith(".")])
            if total_files == 0:
                continue

            analysis = DirectoryAnalysis(
                directory=current_path, depth=depth, total_files=total_files
            )

            # Analyze file types
            for file in files:
                if file.startswith("."):
                    continue

                file_path = current_path / file
                analysis.file_types.add(file_path.suffix)

            self._directory_cache[current_path] = analysis

    def _should_exclude_subdir(self, path: Path) -> bool:
        """Check if a subdirectory should be pruned from os.walk traversal.

        This is an optimization to avoid descending into excluded directories,
        which significantly improves performance in large monorepos.

        Args:
            path: Subdirectory path to check

        Returns:
            True if subdirectory should be pruned from traversal
        """
        # Check if the subdirectory itself matches an exclusion pattern
        if self._should_exclude_path(path):
            return True

        # Also check if subdirectory is a default exclusion
        dir_name = path.name
        if dir_name in DEFAULT_EXCLUDED_DIRNAMES:
            return True

        # Skip hidden directories
        if dir_name.startswith("."):  # noqa: SIM103
            return True

        return False

    def _should_exclude_path(self, path: Path) -> bool:
        """Check if a path matches any exclusion pattern.

        Args:
            path: Path to check against exclusion patterns

        Returns:
            True if path should be excluded, False otherwise
        """
        return should_exclude(path, self.base_dir, self._exclude_patterns)
