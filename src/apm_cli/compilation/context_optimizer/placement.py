"""Context Optimizer for APM distributed compilation system.

This module implements the Context Optimization Engine that minimizes
irrelevant context loaded by agents working in specific directories,
following the Minimal Context Principle.
"""

import builtins
import time
from collections import defaultdict
from pathlib import Path

from ...output.models import (
    OptimizationDecision,
    PlacementStrategy,
)
from ...primitives.models import Instruction
from ...utils.paths import portable_relpath
from .class_ import PlacementCandidate

set = builtins.set
list = builtins.list
dict = builtins.dict
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


def _find_optimal_placements(
    self, instruction: Instruction, verbose: bool = False
) -> builtins.list[Path]:
    """Find optimal placement(s) for an instruction using mathematical optimization.

    This implements constraint satisfaction optimization that guarantees every
    instruction gets placed at its mathematically optimal location(s).

    Args:
        instruction (Instruction): Instruction to place.
        verbose (bool): Collect verbose analysis data.

    Returns:
        List[Path]: List of optimal directory placements.
    """
    return self._solve_placement_optimization(instruction, verbose)


def _solve_placement_optimization(
    self, instruction: Instruction, verbose: bool = False
) -> builtins.list[Path]:
    """Mathematical optimization solver for instruction placement.

    Implements the mathematician's objective function:
    minimize: sum(context_pollution x directory_weight)
    subject to: for_all instruction -> exists placement

    Args:
        instruction (Instruction): Instruction to optimize placement for.
        verbose (bool): Collect verbose analysis data.

    Returns:
        List[Path]: Mathematically optimal placement(s).
    """
    pattern = instruction.apply_to

    # Find all directories with matching files
    matching_directories = self._find_matching_directories(pattern)

    if not matching_directories:
        # Smart fallback: Try to place in semantically appropriate directory
        intended_dir = self._extract_intended_directory_from_pattern(pattern)

        if intended_dir:
            # Place in the intended directory (e.g., docs/ for docs/**/*.md)
            placement = intended_dir
            reasoning = f"No matching files found, placed in intended directory '{portable_relpath(intended_dir, self.base_dir)}'"
            self._warnings.append(
                f"Pattern '{pattern}' matches no files - placing in intended directory '{portable_relpath(intended_dir, self.base_dir)}'"
            )
        else:
            # Fallback to root for global patterns
            placement = self.base_dir
            reasoning = "No matching files found, fallback to root placement"
            self._warnings.append(f"Pattern '{pattern}' matches no files - placing at project root")

        # Calculate relevance score for the fallback placement
        relevance_score = 0.0  # No matches means no relevance
        if placement in self._directory_cache:
            relevance_score = self._calculate_coverage_efficiency(placement, pattern)

        decision = OptimizationDecision(
            instruction=instruction,
            pattern=pattern,
            matching_directories=0,
            total_directories=len(self._directory_cache),
            distribution_score=0.0,
            strategy=PlacementStrategy.DISTRIBUTED,
            placement_directories=[placement],
            reasoning=reasoning,
            relevance_score=relevance_score,
        )
        self._optimization_decisions.append(decision)

        return [placement]

    # Calculate distribution score with diversity factor
    distribution_score = self._calculate_distribution_score(matching_directories)

    # Apply three-tier placement strategy based on mathematical analysis
    if distribution_score < self.LOW_DISTRIBUTION_THRESHOLD:
        # Low distribution: Single Point Placement
        strategy = PlacementStrategy.SINGLE_POINT
        placements = self._optimize_single_point_placement(
            matching_directories, instruction, verbose
        )
        reasoning = "Low distribution pattern optimized for minimal pollution"
    elif distribution_score > self.HIGH_DISTRIBUTION_THRESHOLD:
        # High distribution: Distributed Placement
        strategy = PlacementStrategy.DISTRIBUTED
        placements = self._optimize_distributed_placement(
            matching_directories, instruction, verbose
        )
        reasoning = "High distribution pattern placed at root to minimize duplication"
    else:
        # Medium distribution: Selective Multi-Placement
        strategy = PlacementStrategy.SELECTIVE_MULTI
        placements = self._optimize_selective_placement(matching_directories, instruction, verbose)
        reasoning = "Medium distribution pattern with selective high-relevance placement"

    # Calculate relevance score for the primary placement directory
    relevance_score = 0.0
    if placements:
        primary_placement = placements[0]  # Use first placement as representative
        if primary_placement in self._directory_cache:
            relevance_score = self._calculate_coverage_efficiency(primary_placement, pattern)

    # Record optimization decision
    decision = OptimizationDecision(
        instruction=instruction,
        pattern=pattern,
        matching_directories=len(matching_directories),
        total_directories=len(self._directory_cache),
        distribution_score=distribution_score,
        strategy=strategy,
        placement_directories=placements,
        reasoning=reasoning,
        relevance_score=relevance_score,
    )
    self._optimization_decisions.append(decision)

    return placements
