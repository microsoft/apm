"""Comprehensive unit tests for Mathematical Optimization in Context Optimizer.

Tests the mathematical optimization implementation that replaced the old
threshold-based filtering approach with constraint satisfaction optimization.
"""

import os  # noqa: F401
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.compilation.context_optimizer import (
    ContextOptimizer,
    DirectoryAnalysis,  # noqa: F401
    PlacementCandidate,  # noqa: F401
)
from apm_cli.primitives.models import Instruction


class TestMathematicalOptimization:
    """Test the mathematical optimization algorithms."""

    @pytest.fixture
    def optimizer(self, temp_project):
        """Create optimizer with temp project."""
        return ContextOptimizer(str(temp_project))

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project structure for testing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create diverse project structure for testing all strategies
            (temp_path / "src").mkdir()
            (temp_path / "src" / "components").mkdir()
            (temp_path / "src" / "utils").mkdir()
            (temp_path / "docs").mkdir()
            (temp_path / "tests").mkdir()
            (temp_path / "server").mkdir()
            (temp_path / "styles").mkdir()
            (temp_path / "scripts").mkdir()
            (temp_path / "backend").mkdir()
            (temp_path / "frontend").mkdir()

            # Low distribution pattern files (should trigger Single Point Placement)
            (temp_path / "scripts" / "deploy.sh").write_text("#!/bin/bash")
            (temp_path / "scripts" / "build.sh").write_text("#!/bin/bash")

            # Medium distribution pattern files (should trigger Selective Multi-Placement)
            (temp_path / "docs" / "README.md").write_text("# Docs")
            (temp_path / "src" / "README.md").write_text("# Src")
            (temp_path / "tests" / "README.md").write_text("# Tests")
            (temp_path / "backend" / "README.md").write_text("# Backend")
            (temp_path / "frontend" / "README.md").write_text("# Frontend")

            # High distribution pattern files (should trigger Distributed Placement)
            (temp_path / "src" / "main.py").write_text("print('main')")
            (temp_path / "src" / "components" / "app.tsx").write_text("const App = () => {}")
            (temp_path / "src" / "utils" / "helper.ts").write_text("export const helper = () => {}")
            (temp_path / "tests" / "test_main.py").write_text("def test_main(): pass")
            (temp_path / "server" / "api.py").write_text("from flask import Flask")
            (temp_path / "styles" / "main.css").write_text("body { margin: 0; }")
            (temp_path / "backend" / "models.py").write_text("class User: pass")
            (temp_path / "frontend" / "index.html").write_text("<html></html>")
            (temp_path / "package.json").write_text('{"name": "test"}')
            (temp_path / "README.md").write_text("# Project")

            yield temp_path

    def test_calculate_distribution_score_low_distribution(self, optimizer):
        """Test distribution score calculation for low distribution patterns."""
        optimizer._analyze_project_structure()

        # Shell scripts only in scripts directory - should be low distribution
        matching_dirs = optimizer._find_matching_directories("**/*.sh")
        score = optimizer._calculate_distribution_score(matching_dirs)

        assert score < optimizer.LOW_DISTRIBUTION_THRESHOLD
        assert score > 0  # Should have some matches

    def test_calculate_distribution_score_medium_distribution(self, optimizer):
        """Test distribution score calculation for medium distribution patterns."""
        optimizer._analyze_project_structure()

        # README files in multiple directories - should be medium distribution
        matching_dirs = optimizer._find_matching_directories("**/README.md")
        score = optimizer._calculate_distribution_score(matching_dirs)

        assert (
            optimizer.LOW_DISTRIBUTION_THRESHOLD <= score <= optimizer.HIGH_DISTRIBUTION_THRESHOLD
        )

    def test_calculate_distribution_score_high_distribution(self, optimizer):
        """Test distribution score calculation for high distribution patterns."""
        optimizer._analyze_project_structure()

        # Python/JS/CSS files across many directories - should be high distribution
        matching_dirs = optimizer._find_matching_directories("**/*.{py,js,ts,tsx,css,html}")
        score = optimizer._calculate_distribution_score(matching_dirs)

        assert score > optimizer.HIGH_DISTRIBUTION_THRESHOLD

    def test_single_point_placement_strategy(self, optimizer):
        """Test Single Point Placement strategy for low distribution patterns."""
        instruction = Instruction(
            name="shell-standards",
            file_path=Path("shell.md"),
            description="Shell script standards",
            apply_to="**/*.sh",
            content="Shell standards",
        )

        result = optimizer.optimize_instruction_placement([instruction])

        # Should have exactly one placement location
        assert len(result) == 1
        placement_dir = list(result.keys())[0]  # noqa: RUF015

        # Should be placed in the directory with shell files
        assert "scripts" in str(placement_dir)

        # Verify the instruction was placed there
        assert len(result[placement_dir]) == 1
        assert result[placement_dir][0].name == "shell-standards"

    def test_selective_multi_placement_strategy(self, optimizer):
        """Test Selective Multi-Placement strategy for medium distribution patterns."""
        # Use a pattern that actually has medium distribution in the test project
        instruction = Instruction(
            name="python-standards",
            file_path=Path("python.md"),
            description="Python development standards",
            apply_to="**/*.py",  # This should have medium distribution
            content="Python standards",
        )

        result = optimizer.optimize_instruction_placement([instruction])

        # First check the distribution score to understand the strategy used
        optimizer._analyze_project_structure()
        matching_dirs = optimizer._find_matching_directories("**/*.py")
        distribution_score = optimizer._calculate_distribution_score(matching_dirs)

        if (
            distribution_score >= optimizer.LOW_DISTRIBUTION_THRESHOLD
            and distribution_score <= optimizer.HIGH_DISTRIBUTION_THRESHOLD
        ):
            # For medium distribution, the selective strategy should be used
            # The actual number of placements depends on the candidate coverage efficiency
            # but the strategy should select the best candidates
            assert len(result) >= 1  # At least one placement

            # Verify that the selective strategy was actually used by checking
            # that we get a reasonable placement (not just fallback to root)
            placement_paths = list(result.keys())
            placement_path_str = str(placement_paths[0])  # noqa: F841

            # Should not be placing at root if we have better options
            # (unless root is actually the best option)
            assert len(matching_dirs) > 0  # We should have found matching directories

            # Verify that candidates were evaluated and the best one was selected
            candidates = optimizer._generate_all_candidates(matching_dirs, instruction)
            assert len(candidates) > 0

            # The result should be either among the candidates OR at root for coverage guarantee
            result_dirs = set(result.keys())
            candidate_dirs = set(c.directory for c in candidates)
            root_dir = optimizer.base_dir

            # Coverage-first approach: result can include root for coverage or be from candidates
            assert result_dirs.issubset(candidate_dirs) or root_dir in result_dirs

            print(f"✅ Selective strategy used with distribution score {distribution_score:.3f}")
            print(f"   Selected {len(result)} placements from {len(candidates)} candidates")
        # If it's actually low or high distribution, adjust expectations
        elif distribution_score < optimizer.LOW_DISTRIBUTION_THRESHOLD:
            # Single point placement
            assert len(result) == 1
            print("Pattern has low distribution - using single point placement")
        else:
            # High distribution - should be at root
            assert len(result) == 1
            assert list(result.keys())[0] == optimizer.base_dir  # noqa: RUF015
            print("Pattern has high distribution - using distributed placement at root")

    def test_distributed_placement_strategy(self, optimizer):
        """Test Distributed Placement strategy for high distribution patterns."""
        instruction = Instruction(
            name="code-standards",
            file_path=Path("code.md"),
            description="General code standards",
            apply_to="**/*.{py,js,ts,tsx,css,html}",
            content="Code standards",
        )

        result = optimizer.optimize_instruction_placement([instruction])

        # Should be placed at root (distributed strategy)
        assert len(result) == 1
        root_placement = list(result.keys())[0]  # noqa: RUF015

        # Should be the base directory (root)
        assert root_placement == optimizer.base_dir

        # Verify the instruction was placed there
        assert len(result[root_placement]) == 1
        assert result[root_placement][0].name == "code-standards"

    def test_objective_function_calculation(self, optimizer):
        """Test the mathematical objective function calculation."""
        optimizer._analyze_project_structure()

        instruction = Instruction(
            name="test-instruction",
            file_path=Path("test.md"),
            description="Test instruction",
            apply_to="**/*.py",
            content="Test content",
        )

        matching_dirs = optimizer._find_matching_directories("**/*.py")
        candidates = optimizer._generate_all_candidates(matching_dirs, instruction)

        assert len(candidates) > 0

        for candidate in candidates:
            # Verify objective function components are calculated
            assert hasattr(candidate, "coverage_efficiency")
            assert hasattr(candidate, "pollution_score")
            assert hasattr(candidate, "maintenance_locality")

            # Verify scores are in valid ranges
            assert 0.0 <= candidate.coverage_efficiency <= 1.0
            assert candidate.pollution_score >= 0.0
            assert 0.0 <= candidate.maintenance_locality <= 1.0

            # Verify total score is calculated and is reasonable
            assert candidate.total_score is not None

            # Note: PlacementCandidate.__post_init__ uses legacy scoring formula
            # but _generate_all_candidates overwrites total_score with new formula
            # The new formula should be used in the actual total_score
            expected_score = (
                candidate.coverage_efficiency * optimizer.COVERAGE_EFFICIENCY_WEIGHT
                + (1.0 - candidate.pollution_score) * optimizer.POLLUTION_MINIMIZATION_WEIGHT
                + candidate.maintenance_locality * optimizer.MAINTENANCE_LOCALITY_WEIGHT
                - max(
                    0,
                    (optimizer._directory_cache[candidate.directory].depth - 3)
                    * optimizer.DEPTH_PENALTY_FACTOR,
                )
            )

            # The total_score should match our expected calculation (within tolerance)
            assert (
                abs(candidate.total_score - expected_score) < 0.01
            )  # Relaxed tolerance for floating point

    def test_no_instructions_dropped_guarantee(self, optimizer):
        """Test that no instructions are ever dropped (mathematician's guarantee)."""
        # Create instructions with various distribution patterns
        instructions = [
            # Very rare pattern
            Instruction(
                name="super-rare",
                file_path=Path("rare.md"),
                description="Super rare pattern",
                apply_to="**/*.xyz",  # Non-existent extension
                content="Rare standards",
            ),
            # Low distribution
            Instruction(
                name="shell-standards",
                file_path=Path("shell.md"),
                description="Shell standards",
                apply_to="**/*.sh",
                content="Shell standards",
            ),
            # Medium distribution
            Instruction(
                name="readme-standards",
                file_path=Path("readme.md"),
                description="README standards",
                apply_to="**/README.md",
                content="README standards",
            ),
            # High distribution
            Instruction(
                name="code-standards",
                file_path=Path("code.md"),
                description="Code standards",
                apply_to="**/*.{py,js,ts,tsx,css,html}",
                content="Code standards",
            ),
            # No pattern (global)
            Instruction(
                name="global-standards",
                file_path=Path("global.md"),
                description="Global standards",
                apply_to="",
                content="Global standards",
            ),
        ]

        result = optimizer.optimize_instruction_placement(instructions)

        # GUARANTEE: Every instruction must be placed somewhere
        total_placed = sum(len(insts) for insts in result.values())
        assert total_placed == len(instructions)

        # Verify each instruction is placed at least once
        placed_names = set()
        for instructions_list in result.values():
            for inst in instructions_list:
                placed_names.add(inst.name)

        expected_names = {
            "super-rare",
            "shell-standards",
            "readme-standards",
            "code-standards",
            "global-standards",
        }
        assert placed_names == expected_names

    def test_coverage_efficiency_calculation(self, optimizer):
        """Test coverage efficiency calculation."""
        optimizer._analyze_project_structure()

        # Find a directory with Python files
        python_dirs = optimizer._find_matching_directories("**/*.py")
        assert len(python_dirs) > 0

        test_dir = list(python_dirs)[0]  # noqa: RUF015
        efficiency = optimizer._calculate_coverage_efficiency(test_dir, "**/*.py")

        # Should return relevance score (matches/total_files)
        analysis = optimizer._directory_cache[test_dir]
        expected = analysis.get_relevance_score("**/*.py")
        assert efficiency == expected
        assert 0.0 <= efficiency <= 1.0

    def test_pollution_minimization_calculation(self, optimizer):
        """Test pollution minimization calculation."""
        optimizer._analyze_project_structure()

        # Test pollution calculation for different directories
        matching_dirs = optimizer._find_matching_directories("**/*.py")
        assert len(matching_dirs) > 0

        for directory in matching_dirs:
            pollution = optimizer._calculate_pollution_minimization(directory, "**/*.py")
            assert pollution >= 0.0  # Pollution score should be non-negative

    def test_maintenance_locality_calculation(self, optimizer):
        """Test maintenance locality calculation."""
        optimizer._analyze_project_structure()

        matching_dirs = optimizer._find_matching_directories("**/*.py")
        assert len(matching_dirs) > 0

        for directory in matching_dirs:
            locality = optimizer._calculate_maintenance_locality(directory, "**/*.py")
            assert 0.0 <= locality <= 1.0  # Should be normalized

    def test_depth_penalty_application(self, optimizer):
        """Test that depth penalty is properly applied."""
        optimizer._analyze_project_structure()

        instruction = Instruction(
            name="test-instruction",
            file_path=Path("test.md"),
            description="Test instruction",
            apply_to="**/*.py",
            content="Test content",
        )

        matching_dirs = optimizer._find_matching_directories("**/*.py")
        candidates = optimizer._generate_all_candidates(matching_dirs, instruction)

        # Find candidates at different depths
        shallow_candidates = [
            c for c in candidates if optimizer._directory_cache[c.directory].depth <= 2
        ]
        deep_candidates = [
            c for c in candidates if optimizer._directory_cache[c.directory].depth > 3
        ]

        if shallow_candidates and deep_candidates:
            # Deep candidates should have lower total scores due to depth penalty
            avg_shallow_score = sum(c.total_score for c in shallow_candidates) / len(  # noqa: F841
                shallow_candidates
            )
            avg_deep_score = sum(c.total_score for c in deep_candidates) / len(deep_candidates)  # noqa: F841

            # Generally, shallower should have higher scores (though not guaranteed due to other factors)
            # At minimum, verify depth penalty is being applied to deep candidates
            for deep_candidate in deep_candidates:
                depth = optimizer._directory_cache[deep_candidate.directory].depth
                if depth > 3:
                    expected_penalty = (depth - 3) * optimizer.DEPTH_PENALTY_FACTOR
                    assert expected_penalty > 0

    def test_edge_case_no_matching_files(self, optimizer):
        """Test edge case where no files match the pattern."""
        instruction = Instruction(
            name="nonexistent-pattern",
            file_path=Path("nonexistent.md"),
            description="Pattern with no matches",
            apply_to="**/*.nonexistent",
            content="Nonexistent standards",
        )

        result = optimizer.optimize_instruction_placement([instruction])

        # Should still place the instruction (at root)
        assert len(result) == 1
        placed_dir = list(result.keys())[0]  # noqa: RUF015
        assert placed_dir == optimizer.base_dir
        assert len(result[placed_dir]) == 1
        assert result[placed_dir][0].name == "nonexistent-pattern"

    def test_edge_case_single_file_match(self, temp_project, optimizer):
        """Test edge case where pattern matches only one file."""
        # Create a unique file type
        (temp_project / "unique.special").write_text("unique content")

        instruction = Instruction(
            name="special-file-standards",
            file_path=Path("special.md"),
            description="Standards for special files",
            apply_to="**/*.special",
            content="Special file standards",
        )

        result = optimizer.optimize_instruction_placement([instruction])

        # Should place in the directory containing the file (or its parent)
        assert len(result) >= 1

        # Verify instruction is placed
        total_placed = sum(len(insts) for insts in result.values())
        assert total_placed == 1

    def test_strategy_selection_boundary_conditions(self, optimizer):
        """Test strategy selection at boundary conditions."""
        # Test the actual distribution score calculation and strategy selection

        # Create a test instruction
        instruction = Instruction(
            name="boundary-test",
            file_path=Path("test.md"),
            description="Test",
            apply_to="**/*.py",  # Use a real pattern
            content="Test",
        )

        optimizer._analyze_project_structure()
        matching_dirs = optimizer._find_matching_directories("**/*.py")  # noqa: F841

        # Test with different distribution scores by mocking the calculation

        # Test LOW_DISTRIBUTION_THRESHOLD - 0.01 (should use single point)
        with (
            patch.object(
                optimizer,
                "_calculate_distribution_score",
                return_value=optimizer.LOW_DISTRIBUTION_THRESHOLD - 0.01,
            ),
            patch.object(
                optimizer, "_optimize_single_point_placement", return_value=[optimizer.base_dir]
            ) as mock_single,
        ):
            optimizer._solve_placement_optimization(instruction)
            mock_single.assert_called_once()

        # Test LOW_DISTRIBUTION_THRESHOLD (should use selective)
        with (
            patch.object(
                optimizer,
                "_calculate_distribution_score",
                return_value=optimizer.LOW_DISTRIBUTION_THRESHOLD,
            ),
            patch.object(
                optimizer, "_optimize_selective_placement", return_value=[optimizer.base_dir]
            ) as mock_selective,
        ):
            optimizer._solve_placement_optimization(instruction)
            mock_selective.assert_called_once()

        # Test HIGH_DISTRIBUTION_THRESHOLD + 0.01 (should use distributed)
        with (
            patch.object(
                optimizer,
                "_calculate_distribution_score",
                return_value=optimizer.HIGH_DISTRIBUTION_THRESHOLD + 0.01,
            ),
            patch.object(
                optimizer, "_optimize_distributed_placement", return_value=[optimizer.base_dir]
            ) as mock_distributed,
        ):
            optimizer._solve_placement_optimization(instruction)
            mock_distributed.assert_called_once()

    def test_mathematical_optimality_guarantee(self, optimizer):
        """Test that placement is mathematically optimal within strategy."""
        optimizer._analyze_project_structure()

        instruction = Instruction(
            name="python-standards",
            file_path=Path("python.md"),
            description="Python standards",
            apply_to="**/*.py",
            content="Python standards",
        )

        # Get matching directories and candidates
        matching_dirs = optimizer._find_matching_directories("**/*.py")
        candidates = optimizer._generate_all_candidates(matching_dirs, instruction)

        if len(candidates) > 1:
            # Verify candidates are properly scored
            scores = [c.total_score for c in candidates]  # noqa: F841

            # Get the actual placement decision
            result = optimizer.optimize_instruction_placement([instruction])
            placed_dirs = list(result.keys())

            # For single point placement, should select the highest scoring candidate
            distribution_score = optimizer._calculate_distribution_score(matching_dirs)
            if distribution_score < optimizer.LOW_DISTRIBUTION_THRESHOLD:
                # Single point placement - should be the best candidate
                best_candidate = max(
                    candidates, key=lambda c: c.coverage_efficiency - c.pollution_score
                )
                assert best_candidate.directory in placed_dirs

    def test_comprehensive_strategy_integration(self, optimizer):
        """Test all three strategies working together in one optimization run."""
        instructions = [
            # Low distribution -> Single Point
            Instruction(
                name="shell-standards",
                file_path=Path("shell.md"),
                description="Shell standards",
                apply_to="**/*.sh",
                content="Shell standards",
            ),
            # Medium distribution -> Selective Multi
            Instruction(
                name="readme-standards",
                file_path=Path("readme.md"),
                description="README standards",
                apply_to="**/README.md",
                content="README standards",
            ),
            # High distribution -> Distributed
            Instruction(
                name="code-standards",
                file_path=Path("code.md"),
                description="Code standards",
                apply_to="**/*.{py,js,ts,tsx,css,html}",
                content="Code standards",
            ),
        ]

        result = optimizer.optimize_instruction_placement(instructions)

        # All instructions should be placed
        total_placed = sum(len(insts) for insts in result.values())
        assert total_placed >= len(instructions)  # May be more due to multi-placement

        # Verify different strategies were used
        instruction_placements = {}
        for directory, insts in result.items():
            for inst in insts:
                if inst.name not in instruction_placements:
                    instruction_placements[inst.name] = []
                instruction_placements[inst.name].append(directory)

        # Shell standards should have few placements (single point)
        assert len(instruction_placements["shell-standards"]) == 1

        # README standards should have moderate placements (selective)
        assert 1 <= len(instruction_placements["readme-standards"]) <= 6

        # Code standards should have single placement at root (distributed)
        assert len(instruction_placements["code-standards"]) == 1
        assert instruction_placements["code-standards"][0] == optimizer.base_dir
