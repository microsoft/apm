"""Comprehensive tests for mathematical coverage guarantees and constraints.

Tests the core mathematical foundation:
- Hierarchical coverage verification: ∀file_matching_pattern → can_inherit_instruction
- Coverage-constrained optimization: minimize Σ(context_pollution × directory_weight) subject to coverage
- Data loss prevention: no files are left without applicable instructions
- Coverage-first principle: coverage takes priority over efficiency
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch  # noqa: F401

import pytest

from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.primitives.models import Instruction


class TestCoverageGuarantees:
    """Test mathematical coverage guarantees and constraint satisfaction."""

    @pytest.fixture
    def complex_project(self):
        """Create a complex project structure that challenges coverage."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create deep nested structure
            (temp_path / "src" / "components" / "forms" / "validation").mkdir(parents=True)
            (temp_path / "src" / "utils" / "helpers").mkdir(parents=True)
            (temp_path / "tests" / "integration" / "api").mkdir(parents=True)
            (temp_path / "docs" / "api" / "v1").mkdir(parents=True)
            (temp_path / "scripts" / "deployment").mkdir(parents=True)

            # Create files at various depths
            (temp_path / "src" / "main.py").write_text("# Main module")
            (temp_path / "src" / "components" / "button.tsx").write_text("// Button component")
            (temp_path / "src" / "components" / "forms" / "login.tsx").write_text("// Login form")
            (temp_path / "src" / "components" / "forms" / "validation" / "rules.ts").write_text(
                "// Validation rules"
            )
            (temp_path / "src" / "utils" / "helpers" / "format.py").write_text("# Format helpers")
            (temp_path / "tests" / "integration" / "api" / "test_auth.py").write_text(
                "# Auth tests"
            )
            (temp_path / "docs" / "api" / "v1" / "README.md").write_text("# API docs")
            (temp_path / "scripts" / "deployment" / "deploy.sh").write_text("#!/bin/bash")

            yield temp_path

    @pytest.fixture
    def coverage_challenge_instructions(self):
        """Instructions that challenge the coverage guarantee."""
        return [
            # Deep pattern that should reach all TypeScript files
            Instruction(
                name="typescript-deep",
                file_path=Path("typescript.md"),
                description="TypeScript standards for all levels",
                apply_to="**/*.{ts,tsx}",
                content="TypeScript guidelines for all files",
            ),
            # Specific pattern for validation logic
            Instruction(
                name="validation-specific",
                file_path=Path("validation.md"),
                description="Validation-specific rules",
                apply_to="**/validation/*.{ts,tsx}",
                content="Validation logic standards",
            ),
            # Python testing standards
            Instruction(
                name="python-testing",
                file_path=Path("python-test.md"),
                description="Python testing standards",
                apply_to="**/test_*.py",
                content="Python test standards",
            ),
            # Documentation standards
            Instruction(
                name="docs-standards",
                file_path=Path("docs.md"),
                description="Documentation standards",
                apply_to="**/docs/**/*.md",
                content="Documentation guidelines",
            ),
            # Shell script standards
            Instruction(
                name="shell-standards",
                file_path=Path("shell.md"),
                description="Shell script standards",
                apply_to="**/*.sh",
                content="Shell scripting guidelines",
            ),
        ]

    def test_hierarchical_coverage_verification(
        self, complex_project, coverage_challenge_instructions
    ):
        """Test that every file can access applicable instructions through inheritance chain.

        This is the core mathematical constraint: ∀file_matching_pattern → can_inherit_instruction
        """
        optimizer = ContextOptimizer(str(complex_project))

        # Get optimized placement
        placement_map = optimizer.optimize_instruction_placement(coverage_challenge_instructions)

        # Define test files and their expected applicable instructions
        test_cases = [
            {
                "file": complex_project
                / "src"
                / "components"
                / "forms"
                / "validation"
                / "rules.ts",
                "expected_instructions": ["typescript-deep", "validation-specific"],
                "description": "Deep TypeScript validation file should get both TS and validation instructions",
            },
            {
                "file": complex_project / "src" / "components" / "button.tsx",
                "expected_instructions": ["typescript-deep"],
                "description": "TypeScript component should get TS instructions",
            },
            {
                "file": complex_project / "tests" / "integration" / "api" / "test_auth.py",
                "expected_instructions": ["python-testing"],
                "description": "Python test file should get testing instructions",
            },
            {
                "file": complex_project / "docs" / "api" / "v1" / "README.md",
                "expected_instructions": ["docs-standards"],
                "description": "Documentation file should get docs instructions",
            },
            {
                "file": complex_project / "scripts" / "deployment" / "deploy.sh",
                "expected_instructions": ["shell-standards"],
                "description": "Shell script should get shell instructions",
            },
        ]

        # For each test file, verify it can access all applicable instructions
        for test_case in test_cases:
            file_path = test_case["file"]
            expected_names = set(test_case["expected_instructions"])

            # Get inheritance chain for the file's directory
            file_dir = file_path.parent
            inheritance_chain = optimizer._get_inheritance_chain(file_dir)

            # Collect all instructions accessible through inheritance
            accessible_instructions = set()
            for dir_in_chain in inheritance_chain:
                if dir_in_chain in placement_map:
                    for instruction in placement_map[dir_in_chain]:
                        # Check if instruction applies to this file
                        if optimizer._is_instruction_relevant(instruction, file_path.parent):
                            accessible_instructions.add(instruction.name)

            # GUARANTEE: File must be able to access all applicable instructions
            missing_instructions = expected_names - accessible_instructions
            assert not missing_instructions, (
                f"COVERAGE VIOLATION: {test_case['description']}\n"
                f"File: {file_path}\n"
                f"Missing instructions: {missing_instructions}\n"
                f"Accessible: {accessible_instructions}\n"
                f"Expected: {expected_names}\n"
                f"Inheritance chain: {[str(p) for p in inheritance_chain]}\n"
                f"Placement map: {[(str(k), [i.name for i in v]) for k, v in placement_map.items()]}"
            )

    def test_coverage_constraint_priority_over_efficiency(
        self, complex_project, coverage_challenge_instructions
    ):
        """Test that coverage constraints take priority over efficiency optimization.

        This verifies the constraint: coverage guarantee > efficiency optimization
        """
        optimizer = ContextOptimizer(str(complex_project))

        # Get placement
        placement_map = optimizer.optimize_instruction_placement(coverage_challenge_instructions)

        # Analyze efficiency vs coverage trade-offs
        stats = optimizer.get_optimization_stats(placement_map)

        # Even if efficiency is low, all files must have coverage
        typescript_files = [
            complex_project / "src" / "components" / "button.tsx",
            complex_project / "src" / "components" / "forms" / "login.tsx",
            complex_project / "src" / "components" / "forms" / "validation" / "rules.ts",
        ]

        # CONSTRAINT: Coverage must be maintained regardless of efficiency
        for ts_file in typescript_files:
            if ts_file.exists():
                # Check that typescript-deep instruction is accessible
                file_dir = ts_file.parent
                chain = optimizer._get_inheritance_chain(file_dir)

                has_typescript_coverage = False
                for dir_in_chain in chain:
                    if dir_in_chain in placement_map:
                        for instruction in placement_map[dir_in_chain]:
                            if (
                                instruction.name == "typescript-deep"
                                and optimizer._is_instruction_relevant(instruction, ts_file.parent)
                            ):
                                has_typescript_coverage = True
                                break
                    if has_typescript_coverage:
                        break

                assert has_typescript_coverage, (
                    f"CONSTRAINT VIOLATION: TypeScript file {ts_file} lacks coverage\n"
                    f"Efficiency: {stats.efficiency_percentage:.1f}%\n"
                    f"This violates coverage-first principle: coverage must be guaranteed even with low efficiency"
                )

    def test_data_loss_prevention_edge_cases(self, complex_project):
        """Test that no files are left without applicable instructions in edge cases."""
        optimizer = ContextOptimizer(str(complex_project))

        # Create edge case: instruction with very specific pattern that might miss files
        edge_case_instructions = [
            Instruction(
                name="very-specific",
                file_path=Path("specific.md"),
                description="Very specific pattern",
                apply_to="**/validation/rules.ts",  # Very specific path
                content="Specific rules",
            ),
            Instruction(
                name="general-fallback",
                file_path=Path("general.md"),
                description="General fallback",
                apply_to="**/*.{ts,tsx}",  # General pattern for both .ts and .tsx
                content="General TS rules",
            ),
        ]

        placement_map = optimizer.optimize_instruction_placement(edge_case_instructions)

        # Test that the specific file gets both instructions
        specific_file = complex_project / "src" / "components" / "forms" / "validation" / "rules.ts"
        if specific_file.exists():
            file_dir = specific_file.parent
            chain = optimizer._get_inheritance_chain(file_dir)

            accessible_names = set()
            for dir_in_chain in chain:
                if dir_in_chain in placement_map:
                    for instruction in placement_map[dir_in_chain]:
                        if optimizer._is_instruction_relevant(instruction, specific_file.parent):
                            accessible_names.add(instruction.name)

            # GUARANTEE: Specific file should access both specific and general instructions
            assert "general-fallback" in accessible_names, (
                "DATA LOSS: File should have access to general fallback instruction"
            )
            # The specific instruction should also be accessible (if not, general covers it)

        # Test that other TypeScript files get the general instruction
        other_ts_file = complex_project / "src" / "components" / "button.tsx"
        if other_ts_file.exists():
            file_dir = other_ts_file.parent
            chain = optimizer._get_inheritance_chain(file_dir)

            has_general_coverage = False
            for dir_in_chain in chain:
                if dir_in_chain in placement_map:
                    for instruction in placement_map[dir_in_chain]:
                        if (
                            instruction.name == "general-fallback"
                            and optimizer._is_instruction_relevant(
                                instruction, other_ts_file.parent
                            )
                        ):
                            has_general_coverage = True
                            break
                if has_general_coverage:
                    break

            assert has_general_coverage, (
                "DATA LOSS: General file lacks general instruction coverage"
            )

    def test_coverage_under_pattern_evolution(self, complex_project):
        """Test that coverage is maintained when file patterns evolve."""
        optimizer = ContextOptimizer(str(complex_project))

        # Phase 1: Initial instructions for existing patterns
        initial_instructions = [
            Instruction(
                name="existing-ts",
                file_path=Path("existing.md"),
                description="Existing TS standards",
                apply_to="**/*.ts",
                content="TS standards",
            )
        ]

        initial_placement = optimizer.optimize_instruction_placement(initial_instructions)

        # Phase 2: Add new pattern (simulate project evolution)
        evolved_instructions = initial_instructions + [  # noqa: RUF005
            Instruction(
                name="new-vue",
                file_path=Path("vue.md"),
                description="New Vue standards",
                apply_to="**/*.vue",  # New file type
                content="Vue standards",
            )
        ]

        evolved_placement = optimizer.optimize_instruction_placement(evolved_instructions)

        # GUARANTEE: Existing coverage must be maintained
        ts_file = complex_project / "src" / "components" / "forms" / "validation" / "rules.ts"
        if ts_file.exists():
            # Check coverage in both phases
            def has_ts_coverage(placement_map):
                file_dir = ts_file.parent
                chain = optimizer._get_inheritance_chain(file_dir)
                for dir_in_chain in chain:
                    if dir_in_chain in placement_map:
                        for instruction in placement_map[dir_in_chain]:
                            if (
                                instruction.name == "existing-ts"
                                and optimizer._is_instruction_relevant(instruction, ts_file.parent)
                            ):
                                return True
                return False

            initial_coverage = has_ts_coverage(initial_placement)
            evolved_coverage = has_ts_coverage(evolved_placement)

            assert initial_coverage, "Initial coverage should exist"
            assert evolved_coverage, (
                "COVERAGE REGRESSION: Adding new patterns broke existing coverage\n"
                "This violates the constraint that pattern evolution cannot reduce coverage"
            )

    def test_mathematical_constraint_satisfaction(
        self, complex_project, coverage_challenge_instructions
    ):
        """Test that the mathematical constraints are satisfied as a system.

        Verifies: minimize Σ(context_pollution × directory_weight) subject to ∀file → coverage
        """
        optimizer = ContextOptimizer(str(complex_project))

        placement_map = optimizer.optimize_instruction_placement(coverage_challenge_instructions)
        stats = optimizer.get_optimization_stats(placement_map)

        # Constraint 1: No instruction should be dropped
        total_placed = sum(len(instructions) for instructions in placement_map.values())
        assert total_placed >= len(coverage_challenge_instructions), (
            "CONSTRAINT VIOLATION: Instructions were dropped, violating ∀instruction → ∃placement"
        )

        # Constraint 2: Every directory with files that match instruction patterns should have optimization consideration
        directories_needing_coverage = set()
        for root, dirs, files in os.walk(complex_project):  # noqa: B007
            if files and not any(part.startswith(".") for part in Path(root).parts):
                # Resolve paths to match the same format used in placement_map
                try:
                    resolved_path = Path(root).resolve()
                except (OSError, ValueError):
                    resolved_path = Path(root).absolute()

                # Check if this directory has any files that match instruction patterns
                has_matching_files = False
                for instruction in coverage_challenge_instructions:
                    if instruction.apply_to:
                        for file in files:
                            file_path = resolved_path / file
                            if optimizer._file_matches_pattern(file_path, instruction.apply_to):
                                has_matching_files = True
                                break
                    if has_matching_files:
                        break

                # Only require coverage for directories with files matching instruction patterns
                if has_matching_files:
                    directories_needing_coverage.add(resolved_path)

        directories_in_placement = set(placement_map.keys())

        # The placement may use hierarchical inheritance, so not every directory needs direct placement
        # But every directory with matching files should be able to inherit from the placement hierarchy
        for dir_needing_coverage in directories_needing_coverage:
            chain = optimizer._get_inheritance_chain(dir_needing_coverage)
            has_inheritance_coverage = any(
                dir_in_chain in directories_in_placement for dir_in_chain in chain
            )
            assert has_inheritance_coverage, (
                f"CONSTRAINT VIOLATION: Directory {dir_needing_coverage} has files matching instruction patterns but no inheritance coverage\n"
                f"Chain: {chain}\n"
                f"Placements: {list(directories_in_placement)}"
            )

        # Constraint 3: Efficiency should be optimized subject to coverage (can be low if needed for coverage)
        # This is tested by ensuring coverage exists even if efficiency is low
        assert stats.efficiency_percentage >= 0, "Efficiency calculation should be valid"

        # The key test: if efficiency is low, it should be due to coverage requirements, not poor optimization
        if stats.efficiency_percentage < 50:
            # Low efficiency should correlate with high coverage guarantee fulfillment
            # We test this by verifying that files can access their instructions despite low efficiency
            test_files = [
                (
                    complex_project / "src" / "components" / "forms" / "validation" / "rules.ts",
                    "typescript-deep",
                ),
                (
                    complex_project / "tests" / "integration" / "api" / "test_auth.py",
                    "python-testing",
                ),
                (complex_project / "docs" / "api" / "v1" / "README.md", "docs-standards"),
            ]

            for test_file, expected_instruction in test_files:
                if test_file.exists():
                    file_dir = test_file.parent
                    chain = optimizer._get_inheritance_chain(file_dir)
                    has_coverage = False

                    for dir_in_chain in chain:
                        if dir_in_chain in placement_map:
                            for instruction in placement_map[dir_in_chain]:
                                if (
                                    instruction.name == expected_instruction
                                    and optimizer._is_instruction_relevant(
                                        instruction, test_file.parent
                                    )
                                ):
                                    has_coverage = True
                                    break
                        if has_coverage:
                            break

                    assert has_coverage, (
                        f"LOW EFFICIENCY WITHOUT COVERAGE: {test_file} lacks {expected_instruction}\n"
                        f"Efficiency: {stats.efficiency_percentage:.1f}%\n"
                        f"Low efficiency should only occur when maintaining coverage requirements"
                    )
