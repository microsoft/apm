"""Placement strategy free-functions for the context optimiser.

Extracted from :mod:`placement` to keep that module under 400 lines.
These are module-level free functions that accept ``self``
(a :class:`~.class_.ContextOptimizer` instance) as their first positional
argument; they are called from :mod:`class_` as
``_placement_strategies._fn(self, ...)``.
"""

from __future__ import annotations

import builtins
from pathlib import Path

from ...primitives.models import Instruction
from .class_ import PlacementCandidate

set = builtins.set
list = builtins.list
dict = builtins.dict


def _optimize_single_point_placement(
    self,
    matching_directories: builtins.set[Path],
    instruction: Instruction,
    verbose: bool = False,
) -> builtins.list[Path]:
    """Optimize placement for low distribution patterns (< 0.3 ratio).

    Strategy: Ensure mandatory coverage constraint first, then optimize for minimal pollution.
    Coverage guarantee takes priority over efficiency optimization.
    """
    candidates = self._generate_all_candidates(matching_directories, instruction)

    if not candidates:
        return [self.base_dir]

    # CRITICAL: Mandatory coverage constraint - filter candidates that provide complete coverage
    coverage_candidates = []
    for candidate in candidates:
        # Verify this placement can provide hierarchical coverage for ALL matching directories
        covered_directories = self._calculate_hierarchical_coverage(
            [candidate.directory], matching_directories
        )
        if covered_directories == matching_directories:
            # This candidate satisfies the mandatory coverage constraint
            coverage_candidates.append(candidate)

    # If no single candidate provides complete coverage, find minimal coverage placement
    if not coverage_candidates:
        minimal_coverage = self._find_minimal_coverage_placement(matching_directories)
        if minimal_coverage:
            return [minimal_coverage]
        else:
            # Ultimate fallback to root to guarantee coverage
            return [self.base_dir]

    # Among coverage-compliant candidates, select the one with best efficiency/pollution ratio
    best_candidate = max(
        coverage_candidates, key=lambda c: c.coverage_efficiency - c.pollution_score
    )

    return [best_candidate.directory]


def _optimize_distributed_placement(
    self,
    matching_directories: builtins.set[Path],
    instruction: Instruction,
    verbose: bool = False,
) -> builtins.list[Path]:
    """Optimize placement for high distribution patterns (> 0.7 ratio).

    Strategy: Place at root to minimize duplication while maintaining accessibility.
    """
    return [self.base_dir]


def _optimize_selective_placement(
    self,
    matching_directories: builtins.set[Path],
    instruction: Instruction,
    verbose: bool = False,
) -> builtins.list[Path]:
    """Optimize placement for medium distribution patterns (0.3-0.7 ratio).

    Strategy: Ensure hierarchical coverage - all matching files must be able
    to inherit the instruction through the hierarchical AGENTS.md system.
    """
    # First check if we can achieve complete coverage with a single high-level placement
    coverage_placement = self._find_minimal_coverage_placement(matching_directories)
    if coverage_placement:
        return [coverage_placement]

    # If single placement doesn't work, use multi-placement strategy
    candidates = self._generate_all_candidates(matching_directories, instruction)

    if not candidates:
        return [self.base_dir]

    # Filter for high-relevance candidates (top 20% or relevance > 0.8)
    high_relevance_threshold = max(
        0.8,
        sorted([c.coverage_efficiency for c in candidates], reverse=True)[
            max(0, len(candidates) // 5)
        ],
    )

    high_relevance_candidates = [
        c for c in candidates if c.coverage_efficiency >= high_relevance_threshold
    ]

    if not high_relevance_candidates:
        # Fallback: use best candidate
        high_relevance_candidates = [max(candidates, key=lambda c: c.total_score)]

    optimal_placements = [c.directory for c in high_relevance_candidates]

    # CRITICAL: Verify hierarchical coverage
    covered_directories = self._calculate_hierarchical_coverage(
        optimal_placements, matching_directories
    )
    uncovered_directories = matching_directories - covered_directories

    if uncovered_directories:
        # Coverage violation! Find minimal placement that covers everything
        minimal_coverage = self._find_minimal_coverage_placement(matching_directories)
        if minimal_coverage:
            return [minimal_coverage]
        else:
            # Fallback to root to ensure no coverage gaps
            return [self.base_dir]

    return optimal_placements


def _generate_all_candidates(
    self, matching_directories: builtins.set[Path], instruction: Instruction
) -> builtins.list[PlacementCandidate]:
    """Generate all placement candidates with optimization scores.

    This includes both matching directories AND their common ancestors to ensure
    the mandatory coverage constraint can be satisfied.
    """
    candidates = []
    pattern = instruction.apply_to

    # Collect all potential placement directories:
    # 1. The matching directories themselves
    # 2. Their common ancestors (for coverage guarantee)
    potential_directories = set(matching_directories)

    # Add common ancestor directories to ensure coverage options exist
    if len(matching_directories) > 1:
        # Find common ancestors that could provide coverage
        common_ancestor = self._find_minimal_coverage_placement(matching_directories)
        if common_ancestor:
            potential_directories.add(common_ancestor)

        # Also add any intermediate directories in the inheritance chains
        for directory in matching_directories:
            chain = self._get_inheritance_chain(directory)
            # Add intermediate directories that could provide coverage
            for intermediate in chain:
                if intermediate != directory and intermediate in self._directory_cache:
                    potential_directories.add(intermediate)

    # Generate candidates for all potential directories
    for directory in sorted(potential_directories):
        if directory not in self._directory_cache:
            continue

        analysis = self._directory_cache[directory]

        # Calculate the three optimization objectives
        coverage_efficiency = self._calculate_coverage_efficiency(directory, pattern)
        pollution_score = self._calculate_pollution_minimization(directory, pattern)
        maintenance_locality = self._calculate_maintenance_locality(directory, pattern)

        # Apply depth penalty for excessive nesting
        depth_penalty = max(0, (analysis.depth - 3) * self.DEPTH_PENALTY_FACTOR)

        # Calculate total objective function score
        total_score = (
            coverage_efficiency * self.COVERAGE_EFFICIENCY_WEIGHT
            + (1.0 - pollution_score) * self.POLLUTION_MINIMIZATION_WEIGHT
            + maintenance_locality * self.MAINTENANCE_LOCALITY_WEIGHT
            - depth_penalty
        )

        candidate = PlacementCandidate(
            instruction=instruction,
            directory=directory,
            direct_relevance=coverage_efficiency,  # Legacy field
            inheritance_pollution=pollution_score,  # Legacy field
            depth_specificity=analysis.depth * 0.1,  # Legacy field
            total_score=0.0,  # Temporary value, will be overwritten
        )

        # Add new optimization fields
        candidate.coverage_efficiency = coverage_efficiency
        candidate.pollution_score = pollution_score
        candidate.maintenance_locality = maintenance_locality

        # Set the mathematical optimization score (after __post_init__ has run)
        candidate.total_score = total_score

        candidates.append(candidate)

    return candidates


def _find_minimal_coverage_placement(self, matching_directories: builtins.set[Path]) -> Path | None:
    """Find the highest directory that can provide hierarchical coverage for all matching directories.

    Args:
        matching_directories: Directories that contain files matching the pattern

    Returns:
        Path to the minimal covering directory, or None if no single placement works
    """
    if not matching_directories:
        return None

    # Convert to relative paths for easier analysis
    relative_dirs = [d.resolve().relative_to(self.base_dir.resolve()) for d in matching_directories]

    # Find the lowest common ancestor that covers all directories
    if len(relative_dirs) == 1:
        # Single directory - we can place instruction in that directory or any parent
        return list(matching_directories)[0]

    # Find common path prefix for all directories
    common_parts = []
    min_depth = min(len(d.parts) for d in relative_dirs)

    for i in range(min_depth):
        parts_at_level = [d.parts[i] for d in relative_dirs]
        if len(set(parts_at_level)) == 1:
            # All directories share this path component
            common_parts.append(parts_at_level[0])
        else:
            break

    if common_parts:
        # Found common ancestor
        common_ancestor = self.base_dir / Path(*common_parts)
        return common_ancestor
    else:
        # No common ancestor beyond root - place at root
        return self.base_dir


def _select_clean_separation_placements(
    self, candidates: builtins.list[PlacementCandidate], pattern: str
) -> builtins.list[Path]:
    """Select placements that provide clean separation of concerns.

    Args:
        candidates (List[PlacementCandidate]): Sorted placement candidates.
        pattern (str): Instruction pattern.

    Returns:
        List[Path]: List of directories for clean separation.
    """
    # Look for distinct clusters of files
    clusters = []

    for candidate in candidates:
        # Check if this directory is isolated (not a parent/child of others)
        is_isolated = True

        for other in candidates:
            if candidate.directory == other.directory:
                continue

            if self._is_child_directory(
                candidate.directory, other.directory
            ) or self._is_child_directory(other.directory, candidate.directory):
                is_isolated = False
                break

        if is_isolated and candidate.direct_relevance >= 0.1:  # Use fixed threshold
            clusters.append(candidate.directory)

    # If we found clean clusters, use them
    if len(clusters) > 1:
        return clusters

    # Otherwise, return single best placement
    return []
