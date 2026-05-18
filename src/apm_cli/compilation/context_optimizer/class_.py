"""Context Optimizer for APM distributed compilation system.

This module implements the Context Optimization Engine that minimizes
irrelevant context loaded by agents working in specific directories,
following the Minimal Context Principle.
"""

import builtins
from dataclasses import dataclass, field
from pathlib import Path

from ...output.models import (
    CompilationResults,
    OptimizationDecision,
    OptimizationStats,
)
from ...primitives.models import Instruction
from ...utils.exclude import validate_exclude_patterns

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


class ContextOptimizer:
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
        return _timing.enable_timing(self, verbose)

    def _time_phase(self, phase_name: str, operation_func, *args, **kwargs):
        return _timing._time_phase(self, phase_name, operation_func, *args, **kwargs)

    def _cached_glob(self, pattern: str) -> builtins.list[str]:
        return _glob_cache._cached_glob(self, pattern)

    def _get_all_files(self) -> builtins.list[Path]:
        return _glob_cache._get_all_files(self)

    def optimize_instruction_placement(
        self,
        instructions: builtins.list[Instruction],
        verbose: bool = False,
        enable_timing: bool = False,
    ) -> builtins.dict[Path, builtins.list[Instruction]]:
        return _placement.optimize_instruction_placement(self, instructions, verbose, enable_timing)

    def analyze_context_inheritance(
        self,
        working_directory: Path,
        placement_map: builtins.dict[Path, builtins.list[Instruction]],
    ) -> InheritanceAnalysis:
        return _analysis.analyze_context_inheritance(self, working_directory, placement_map)

    def get_optimization_stats(
        self, placement_map: builtins.dict[Path, builtins.list[Instruction]]
    ) -> OptimizationStats:
        return _analysis.get_optimization_stats(self, placement_map)

    def get_compilation_results(
        self,
        placement_map: builtins.dict[Path, builtins.list[Instruction]],
        is_dry_run: bool = False,
    ) -> CompilationResults:
        return _analysis.get_compilation_results(self, placement_map, is_dry_run)

    def _analyze_project_structure(self) -> None:
        return _analysis._analyze_project_structure(self)

    def _should_exclude_subdir(self, path: Path) -> bool:
        return _analysis._should_exclude_subdir(self, path)

    def _should_exclude_path(self, path: Path) -> bool:
        return _analysis._should_exclude_path(self, path)

    def _find_optimal_placements(
        self, instruction: Instruction, verbose: bool = False
    ) -> builtins.list[Path]:
        return _placement._find_optimal_placements(self, instruction, verbose)

    def _solve_placement_optimization(
        self, instruction: Instruction, verbose: bool = False
    ) -> builtins.list[Path]:
        return _placement._solve_placement_optimization(self, instruction, verbose)

    def _extract_intended_directory_from_pattern(self, pattern: str) -> Path | None:
        return _analysis._extract_intended_directory_from_pattern(self, pattern)

    def _expand_glob_pattern(self, pattern: str) -> builtins.list[str]:
        return _analysis._expand_glob_pattern(self, pattern)

    def _file_matches_pattern(self, file_path: Path, pattern: str) -> bool:
        return _analysis._file_matches_pattern(self, file_path, pattern)

    def _find_matching_directories(self, pattern: str) -> builtins.set[Path]:
        return _analysis._find_matching_directories(self, pattern)

    def _calculate_inheritance_pollution(self, directory: Path, pattern: str) -> float:
        return _analysis._calculate_inheritance_pollution(self, directory, pattern)

    def _calculate_distribution_score(self, matching_directories: builtins.set[Path]) -> float:
        return _analysis._calculate_distribution_score(self, matching_directories)

    def _optimize_single_point_placement(
        self,
        matching_directories: builtins.set[Path],
        instruction: Instruction,
        verbose: bool = False,
    ) -> builtins.list[Path]:
        return _placement_strategies._optimize_single_point_placement(
            self, matching_directories, instruction, verbose
        )

    def _optimize_distributed_placement(
        self,
        matching_directories: builtins.set[Path],
        instruction: Instruction,
        verbose: bool = False,
    ) -> builtins.list[Path]:
        return _placement_strategies._optimize_distributed_placement(
            self, matching_directories, instruction, verbose
        )

    def _optimize_selective_placement(
        self,
        matching_directories: builtins.set[Path],
        instruction: Instruction,
        verbose: bool = False,
    ) -> builtins.list[Path]:
        return _placement_strategies._optimize_selective_placement(
            self, matching_directories, instruction, verbose
        )

    def _generate_all_candidates(
        self, matching_directories: builtins.set[Path], instruction: Instruction
    ) -> builtins.list[PlacementCandidate]:
        return _placement_strategies._generate_all_candidates(
            self, matching_directories, instruction
        )

    def _find_minimal_coverage_placement(
        self, matching_directories: builtins.set[Path]
    ) -> Path | None:
        return _placement_strategies._find_minimal_coverage_placement(self, matching_directories)

    def _calculate_hierarchical_coverage(
        self, placements: builtins.list[Path], target_directories: builtins.set[Path]
    ) -> builtins.set[Path]:
        return _analysis._calculate_hierarchical_coverage(self, placements, target_directories)

    def _is_hierarchically_covered(self, target_dir: Path, placement_dir: Path) -> bool:
        return _analysis._is_hierarchically_covered(self, target_dir, placement_dir)

    def _calculate_coverage_efficiency(self, directory: Path, pattern: str) -> float:
        return _analysis._calculate_coverage_efficiency(self, directory, pattern)

    def _calculate_pollution_minimization(self, directory: Path, pattern: str) -> float:
        return _analysis._calculate_pollution_minimization(self, directory, pattern)

    def _calculate_maintenance_locality(self, directory: Path, pattern: str) -> float:
        return _analysis._calculate_maintenance_locality(self, directory, pattern)

    def _select_clean_separation_placements(
        self, candidates: builtins.list[PlacementCandidate], pattern: str
    ) -> builtins.list[Path]:
        return _placement_strategies._select_clean_separation_placements(self, candidates, pattern)

    def _get_inheritance_chain(self, working_directory: Path) -> builtins.list[Path]:
        return _analysis._get_inheritance_chain(self, working_directory)

    def _is_child_directory(self, child: Path, parent: Path) -> bool:
        return _analysis._is_child_directory(self, child, parent)

    def _is_instruction_relevant(self, instruction: Instruction, working_directory: Path) -> bool:
        return _analysis._is_instruction_relevant(self, instruction, working_directory)

    # Debug print methods removed - replaced by structured data collection
    # for professional output formatting via CompilationResults


from . import _placement_strategies
from . import analysis as _analysis
from . import glob_cache as _glob_cache
from . import placement as _placement
from . import timing as _timing
