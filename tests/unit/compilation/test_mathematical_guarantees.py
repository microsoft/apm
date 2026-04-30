"""Tests for mathematical guarantees and coverage constraints."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest  # noqa: F401

from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.primitives.models import Instruction


class TestMathematicalGuarantees:
    """Test suite for mathematical foundations and coverage guarantees."""

    def setup_method(self):
        """Set up test environment with a simple project structure."""
        self.temp_dir = tempfile.mkdtemp()
        self.base_path = Path(self.temp_dir)

        # Create simple directory structure
        (self.base_path / "src").mkdir()
        (self.base_path / "src" / "components").mkdir()
        (self.base_path / "tests").mkdir()

        # Create some files
        (self.base_path / "src" / "main.py").touch()
        (self.base_path / "src" / "components" / "widget.py").touch()
        (self.base_path / "tests" / "test_main.py").touch()

        self.optimizer = ContextOptimizer(self.base_path)

    def create_mock_instruction(self, pattern: str, apply_to: str = "**/*.py"):
        """Create a mock instruction for testing."""
        instruction = MagicMock(spec=Instruction)
        instruction.pattern = pattern
        instruction.apply_to = apply_to
        instruction.content = f"Test instruction for {pattern}"
        instruction.file_path = self.base_path / "test.instructions.md"
        return instruction

    def test_coverage_guarantee_basic(self):
        """Test that every file can access applicable instructions."""
        # Create instruction that should apply to all Python files
        instruction = self.create_mock_instruction("**/*.py")
        instructions = [instruction]

        # Run optimization
        placement_map = self.optimizer.optimize_instruction_placement(instructions)

        # Verify that placement exists
        assert len(placement_map) > 0, "No placement generated"

        # For coverage guarantee, instruction should be accessible to all matching files
        total_instructions = sum(len(instr_list) for instr_list in placement_map.values())
        assert total_instructions >= 1, "Coverage guarantee violated - no instructions placed"

    def test_hierarchical_inheritance_chain(self):
        """Test that files can inherit instructions from parent directories."""
        instruction = self.create_mock_instruction("**/*.py")
        instructions = [instruction]

        # Run optimization
        placement_map = self.optimizer.optimize_instruction_placement(instructions)  # noqa: F841

        # Test inheritance chain for deep file
        deep_file = self.base_path / "src" / "components" / "widget.py"
        inheritance_chain = self.optimizer._get_inheritance_chain(deep_file.parent)

        # Verify chain includes path up to root
        assert len(inheritance_chain) >= 2, "Inheritance chain too short"
        # Use resolved paths for consistent comparison
        assert inheritance_chain[0].resolve() == deep_file.parent.resolve()  # src/components
        assert self.base_path.resolve() in [p.resolve() for p in inheritance_chain], (
            "Root not in inheritance chain"
        )

    def test_no_data_loss_constraint(self):
        """Test that no instructions are lost during optimization."""
        instructions = [
            self.create_mock_instruction("src/**/*.py"),
            self.create_mock_instruction("tests/**/*.py"),
            self.create_mock_instruction("**/*.py"),
        ]

        # Run optimization
        placement_map = self.optimizer.optimize_instruction_placement(instructions)

        # Count total instructions placed
        total_placed = sum(len(instr_list) for instr_list in placement_map.values())

        # Verify no data loss
        assert total_placed == len(instructions), (
            f"Data loss detected: {len(instructions)} input, {total_placed} placed"
        )

    def test_coverage_first_over_efficiency(self):
        """Test that coverage guarantee takes priority over efficiency."""
        # Create instruction with very specific pattern that would be more "efficient"
        # if placed locally, but might violate coverage for edge cases
        instruction = self.create_mock_instruction("**/*.py")
        instructions = [instruction]

        # Run optimization
        placement_map = self.optimizer.optimize_instruction_placement(instructions)

        # Even if efficiency is low, coverage must be guaranteed
        # This means at minimum, instruction should be accessible to all matching files
        total_instructions = sum(len(instr_list) for instr_list in placement_map.values())
        assert total_instructions >= 1, "Coverage guarantee violated"

        # The instruction should be placed somewhere that guarantees coverage
        # (either at optimal location or root fallback)
        placed_locations = list(placement_map.keys())
        assert len(placed_locations) > 0, "No placement locations found"

    def test_pattern_matching_coverage(self):
        """Test that pattern matching correctly identifies applicable files."""
        # Test specific pattern
        src_instruction = self.create_mock_instruction("src/**/*.py")
        instructions = [src_instruction]

        # Run optimization
        placement_map = self.optimizer.optimize_instruction_placement(instructions)

        # Should place instruction in a location accessible to src files
        total_placed = sum(len(instr_list) for instr_list in placement_map.values())
        assert total_placed == 1, "Pattern-specific instruction not placed correctly"

    def test_constraint_satisfaction_over_optimization(self):
        """Test that mathematical constraints are satisfied even if not optimal."""
        # Create multiple instructions with overlapping patterns
        instructions = [
            self.create_mock_instruction("**/*.py"),
            self.create_mock_instruction("src/**/*.py"),
            self.create_mock_instruction("tests/**/*.py"),
        ]

        # Run optimization
        placement_map = self.optimizer.optimize_instruction_placement(instructions)

        # Verify constraint: ∀instruction → ∃placement
        for instruction in instructions:
            found = False
            for instr_list in placement_map.values():
                if instruction in instr_list:
                    found = True
                    break
            assert found, f"Constraint violation: instruction {instruction.pattern} not placed"

    def test_root_fallback_for_coverage(self):
        """Test that root placement is used when necessary for coverage guarantee."""
        # Create instruction with broad pattern that might need root placement
        global_instruction = self.create_mock_instruction("**/*.py")
        instructions = [global_instruction]

        # Run optimization
        placement_map = self.optimizer.optimize_instruction_placement(instructions)

        # Verify instruction is placed somewhere that provides coverage
        assert len(placement_map) > 0, "No placement generated"

        # Check that the placement satisfies coverage requirement
        total_instructions = sum(len(instr_list) for instr_list in placement_map.values())
        assert total_instructions >= 1, "Coverage not guaranteed"


class TestCoverageEdgeCases:
    """Test edge cases for coverage guarantee."""

    def setup_method(self):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.base_path = Path(self.temp_dir)

        # Create complex directory structure
        (self.base_path / "src" / "api" / "v1").mkdir(parents=True)
        (self.base_path / "src" / "components" / "forms").mkdir(parents=True)
        (self.base_path / "tests" / "integration" / "api").mkdir(parents=True)

        # Create files at different depths
        (self.base_path / "src" / "main.py").touch()
        (self.base_path / "src" / "api" / "v1" / "endpoints.py").touch()
        (self.base_path / "src" / "components" / "forms" / "validation.py").touch()
        (self.base_path / "tests" / "integration" / "api" / "test_endpoints.py").touch()

        self.optimizer = ContextOptimizer(self.base_path)

    def create_mock_instruction(self, pattern: str, apply_to: str = "**/*.py"):
        """Create a mock instruction for testing."""
        instruction = MagicMock(spec=Instruction)
        instruction.pattern = pattern
        instruction.apply_to = apply_to
        instruction.content = f"Test instruction for {pattern}"
        instruction.file_path = self.base_path / "test.instructions.md"
        return instruction

    def test_deep_directory_coverage(self):
        """Test that files in deep directories maintain coverage."""
        instruction = self.create_mock_instruction("**/*.py")
        instructions = [instruction]

        # Run optimization
        placement_map = self.optimizer.optimize_instruction_placement(instructions)

        # Verify deep files can access instructions
        deep_file = self.base_path / "src" / "api" / "v1" / "endpoints.py"
        inheritance_chain = self.optimizer._get_inheritance_chain(deep_file.parent)

        # Check that inheritance chain allows access to placed instructions
        can_access = False
        for directory in inheritance_chain:
            if directory in placement_map:
                can_access = True
                break

        assert can_access, "Deep directory cannot access instructions via inheritance"

    def test_multiple_pattern_overlap_coverage(self):
        """Test coverage when multiple patterns overlap."""
        instructions = [
            self.create_mock_instruction("**/*.py"),
            self.create_mock_instruction("src/**/*.py"),
            self.create_mock_instruction("tests/**/*.py"),
            self.create_mock_instruction("src/api/**/*.py"),
        ]

        # Run optimization
        placement_map = self.optimizer.optimize_instruction_placement(instructions)

        # Verify all instructions are placed (no data loss)
        total_placed = sum(len(instr_list) for instr_list in placement_map.values())
        assert total_placed == len(instructions), "Some instructions lost in overlap scenario"

    def test_pattern_specificity_maintains_coverage(self):
        """Test that specific patterns don't break general coverage."""
        instructions = [
            self.create_mock_instruction("**/*.py"),  # General
            self.create_mock_instruction("src/components/forms/**/*.py"),  # Very specific
        ]

        # Run optimization
        placement_map = self.optimizer.optimize_instruction_placement(instructions)

        # Both instructions should be placed
        total_placed = sum(len(instr_list) for instr_list in placement_map.values())
        assert total_placed == 2, "Pattern specificity caused instruction loss"

        # Files should still have access via inheritance
        specific_file = self.base_path / "src" / "components" / "forms" / "validation.py"
        inheritance_chain = self.optimizer._get_inheritance_chain(specific_file.parent)

        # Should be able to access instructions
        assert len(inheritance_chain) >= 3, "Inheritance chain too short for specific pattern"
