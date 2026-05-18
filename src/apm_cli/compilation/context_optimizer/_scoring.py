"""Scoring functions extracted from analysis.py."""

from __future__ import annotations

import builtins
from pathlib import Path

set = builtins.set
list = builtins.list
dict = builtins.dict


def _calculate_inheritance_pollution(self, directory: Path, pattern: str) -> float:
    """Calculate inheritance pollution score for placing instruction at directory.

    Args:
        directory (Path): Candidate placement directory.
        pattern (str): Instruction pattern.

    Returns:
        float: Pollution score (higher = more pollution).
    """
    pollution_score = 0.0

    # Optimization: Only check direct children instead of all directories
    # This prevents O(n2) complexity with unlimited depth analysis
    try:
        direct_children = [
            child
            for child in directory.iterdir()
            if child.is_dir() and child in self._directory_cache
        ]

        # Check only direct child directories for pollution
        for child_dir in direct_children:
            analysis = self._directory_cache[child_dir]

            # If child has no matching files, this creates pollution
            child_relevance = analysis.get_relevance_score(pattern)
            if child_relevance == 0.0:
                pollution_score += 0.5  # Strong pollution penalty
            elif child_relevance < 0.1:  # Weak relevance threshold
                pollution_score += 0.2  # Weak pollution penalty
    except (OSError, PermissionError):
        # Skip directories we can't read
        pass

    return pollution_score


def _calculate_distribution_score(self, matching_directories: builtins.set[Path]) -> float:
    """Calculate distribution score with diversity factor.

    Args:
        matching_directories: Set of directories with pattern matches.

    Returns:
        float: Distribution score accounting for spread and depth diversity.
    """
    total_dirs_with_files = len([d for d in self._directory_cache.values() if d.total_files > 0])
    if total_dirs_with_files == 0:
        return 0.0

    base_ratio = len(matching_directories) / total_dirs_with_files

    # Calculate diversity factor based on depth distribution
    depths = [self._directory_cache[d].depth for d in matching_directories]
    if not depths:
        return base_ratio

    depth_variance = sum((d - sum(depths) / len(depths)) ** 2 for d in depths) / len(depths)
    diversity_factor = 1.0 + (depth_variance * self.DIVERSITY_FACTOR_BASE)

    return base_ratio * diversity_factor


def _calculate_hierarchical_coverage(
    self, placements: builtins.list[Path], target_directories: builtins.set[Path]
) -> builtins.set[Path]:
    """Calculate which target directories are covered by the given placements through hierarchical inheritance.

    Args:
        placements: List of directories where AGENTS.md files will be placed
        target_directories: Directories that need to be covered

    Returns:
        Set of target directories that are covered by the placements
    """
    covered = set()

    for target in target_directories:
        for placement in placements:
            if self._is_hierarchically_covered(target, placement):
                covered.add(target)
                break

    return covered


def _is_hierarchically_covered(self, target_dir: Path, placement_dir: Path) -> bool:
    """Check if target_dir can inherit instructions from placement_dir through hierarchy.

    This is true if placement_dir is target_dir itself or any parent of target_dir.
    """
    try:
        # Check if target is the same as placement or is a subdirectory of placement
        target_dir.resolve().relative_to(placement_dir.resolve())
        return True
    except ValueError:
        # target_dir is not under placement_dir
        return False


def _calculate_coverage_efficiency(self, directory: Path, pattern: str) -> float:
    """Calculate how well placement covers actual usage."""
    analysis = self._directory_cache[directory]
    return analysis.get_relevance_score(pattern)


def _calculate_pollution_minimization(self, directory: Path, pattern: str) -> float:
    """Calculate pollution score (higher = more pollution)."""
    return self._calculate_inheritance_pollution(directory, pattern)


def _calculate_maintenance_locality(self, directory: Path, pattern: str) -> float:
    """Calculate maintenance locality score."""
    # Simple heuristic: prefer directories with more related files
    analysis = self._directory_cache[directory]
    pattern_matches = analysis.pattern_matches.get(pattern, 0)

    if analysis.total_files == 0:
        return 0.0

    return min(1.0, pattern_matches / analysis.total_files)
