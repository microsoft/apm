"""Mixin: placement-optimisation solver methods for ContextOptimizer.

Extracted from context_optimizer.ContextOptimizer to stay under the 800-line
guardrail (Strangler Stage 2 / issue #1078).

Rule B routing
--------------
``Path`` is patched at ``apm_cli.compilation.context_optimizer.Path`` in tests
(specifically ``Path.resolve``).  Any moved method that constructs ``Path(...)``
does so via a function-level late import so the mock is picked up at call time:

    from apm_cli.compilation import context_optimizer as _co
    _co.Path(...)
"""

from __future__ import annotations

import builtins

from ..output.models import OptimizationDecision, PlacementStrategy
from ..primitives.models import Instruction
from ..utils.paths import portable_relpath


class _PlacementSolverMixin:
    """Mixin: mathematical placement-optimisation solver for ContextOptimizer."""

    def _find_optimal_placements(
        self, instruction: Instruction, verbose: bool = False
    ) -> builtins.list:
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
    ) -> builtins.list:
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
            name = getattr(instruction, "name", None) or instruction.file_path.stem

            if intended_dir:
                # Place in the intended directory (e.g., docs/ for docs/**/*.md)
                placement = intended_dir
                reasoning = f"No matching files found, placed in intended directory '{portable_relpath(intended_dir, self.base_dir)}'"
                self._warnings.append(
                    f"applyTo for '{name}' matched no files - placing in '{portable_relpath(intended_dir, self.base_dir)}'"
                )
            else:
                # Fallback to root for global patterns
                placement = self.base_dir
                reasoning = "No matching files found, fallback to root placement"
                self._warnings.append(
                    f"applyTo for '{name}' matched no files - placing at project root"
                )

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
            placements = self._optimize_selective_placement(
                matching_directories, instruction, verbose
            )
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

    def _optimize_single_point_placement(
        self,
        matching_directories: builtins.set,
        instruction: Instruction,
        verbose: bool = False,
    ) -> builtins.list:
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
        matching_directories: builtins.set,
        instruction: Instruction,
        verbose: bool = False,
    ) -> builtins.list:
        """Optimize placement for high distribution patterns (> 0.7 ratio).

        Strategy: Place at root to minimize duplication while maintaining accessibility.
        """
        return [self.base_dir]

    def _optimize_selective_placement(
        self,
        matching_directories: builtins.set,
        instruction: Instruction,
        verbose: bool = False,
    ) -> builtins.list:
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
        self, matching_directories: builtins.set, instruction: Instruction
    ) -> builtins.list:
        """Generate all placement candidates with optimization scores.

        This includes both matching directories AND their common ancestors to ensure
        the mandatory coverage constraint can be satisfied.
        """
        candidates = []
        pattern = instruction.apply_to

        # Collect all potential placement directories:
        # 1. The matching directories themselves
        # 2. Their common ancestors (for coverage guarantee)
        potential_directories = builtins.set(matching_directories)

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

            # PlacementCandidate lives in context_optimizer; import lazily to avoid cycle
            from .context_optimizer import PlacementCandidate

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

    def _find_minimal_coverage_placement(self, matching_directories: builtins.set):
        """Find the highest directory that can provide hierarchical coverage for all matching directories.

        Args:
            matching_directories: Directories that contain files matching the pattern

        Returns:
            Path to the minimal covering directory, or None if no single placement works
        """
        if not matching_directories:
            return None

        # Convert to relative paths for easier analysis
        relative_dirs = [
            d.resolve().relative_to(self.base_dir.resolve()) for d in matching_directories
        ]

        # Find the lowest common ancestor that covers all directories
        if len(relative_dirs) == 1:
            # Single directory - we can place instruction in that directory or any parent
            return next(iter(matching_directories))

        # Find common path prefix for all directories
        common_parts = []
        min_depth = min(len(d.parts) for d in relative_dirs)

        for i in range(min_depth):
            parts_at_level = [d.parts[i] for d in relative_dirs]
            if len(builtins.set(parts_at_level)) == 1:
                # All directories share this path component
                common_parts.append(parts_at_level[0])
            else:
                break

        if common_parts:
            # Found common ancestor.
            # Rule B: Path is patched at context_optimizer.Path in tests.
            from apm_cli.compilation import context_optimizer as _co

            common_ancestor = self.base_dir / _co.Path(*common_parts)
            return common_ancestor
        else:
            # No common ancestor beyond root - place at root
            return self.base_dir

    def _calculate_hierarchical_coverage(
        self, placements: builtins.list, target_directories: builtins.set
    ) -> builtins.set:
        """Calculate which target directories are covered by the given placements through hierarchical inheritance.

        Args:
            placements: List of directories where AGENTS.md files will be placed
            target_directories: Directories that need to be covered

        Returns:
            Set of target directories that are covered by the placements
        """
        covered = builtins.set()

        for target in target_directories:
            for placement in placements:
                if self._is_hierarchically_covered(target, placement):
                    covered.add(target)
                    break

        return covered

    def _is_hierarchically_covered(self, target_dir, placement_dir) -> bool:
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

    def _calculate_coverage_efficiency(self, directory, pattern: str) -> float:
        """Calculate how well placement covers actual usage."""
        analysis = self._directory_cache[directory]
        return analysis.get_relevance_score(pattern)

    def _calculate_pollution_minimization(self, directory, pattern: str) -> float:
        """Calculate pollution score (higher = more pollution)."""
        return self._calculate_inheritance_pollution(directory, pattern)

    def _calculate_maintenance_locality(self, directory, pattern: str) -> float:
        """Calculate maintenance locality score."""
        # Simple heuristic: prefer directories with more related files
        analysis = self._directory_cache[directory]
        pattern_matches = analysis.pattern_matches.get(pattern, 0)

        if analysis.total_files == 0:
            return 0.0

        return min(1.0, pattern_matches / analysis.total_files)

    def _select_clean_separation_placements(
        self, candidates: builtins.list, pattern: str
    ) -> builtins.list:
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
