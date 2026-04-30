"""Test for sibling directory coverage issue.

This reproduces the bug where instructions are placed in one directory
but need to cover files in sibling directories, requiring a common ancestor.

Real-world example: Pattern **/*.{tsx,jsx} matching both:
- frontend/components/Header.tsx
- src/components/ContactForm.tsx

The instruction gets placed in frontend/components/AGENTS.md but
src/components/ContactForm.tsx cannot inherit from it.
"""

import tempfile
from pathlib import Path

import pytest

from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.primitives.models import Instruction


class TestSiblingDirectoryCoverage:
    """Test coverage for files in sibling directories."""

    @pytest.fixture
    def sibling_directory_project(self):
        """Create a project with TSX files in sibling directories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create the exact structure from corporate-website
            (temp_path / "frontend" / "components").mkdir(parents=True)
            (temp_path / "src" / "components").mkdir(parents=True)
            (temp_path / "tests").mkdir(parents=True)

            # Create the TSX files
            (temp_path / "frontend" / "components" / "Header.tsx").write_text("// Header component")
            (temp_path / "src" / "components" / "ContactForm.tsx").write_text(
                "// ContactForm component"
            )
            (temp_path / "tests" / "ContactForm.test.tsx").write_text("// Test file")

            yield temp_path

    def test_sibling_directory_coverage_failure(self, sibling_directory_project):
        """Test that reproduces the coverage gap for sibling directories.

        This test reproduces the real corporate-website issue where an instruction
        gets placed in one sibling directory but cannot cover files in other siblings.
        """
        optimizer = ContextOptimizer(str(sibling_directory_project))

        # Create instruction that matches files in sibling directories
        react_instruction = Instruction(
            name="react-components-local",
            file_path=Path("local-react.md"),
            description="Local React component guidelines",
            apply_to="**/*.{tsx,jsx}",  # Matches files in frontend/components/ AND src/components/
            content="Local React guidelines",
        )

        # Get optimized placement
        placement_map = optimizer.optimize_instruction_placement([react_instruction])

        # Debug: Print the placement for analysis
        print(
            f"\nDEBUG: Placement map: {[(str(k), [i.name for i in v]) for k, v in placement_map.items()]}"
        )

        # Test BOTH sibling directories that should be covered
        sibling_directories = [
            sibling_directory_project / "frontend" / "components",
            sibling_directory_project / "src" / "components",
        ]

        # CRITICAL TEST: Both sibling directories must be able to inherit the instruction
        for sibling_dir in sibling_directories:
            inheritance_chain = optimizer._get_inheritance_chain(sibling_dir)

            has_coverage = False
            for dir_in_chain in inheritance_chain:
                if dir_in_chain in placement_map:
                    for instruction in placement_map[dir_in_chain]:
                        if (
                            instruction.name == "react-components-local"
                            and optimizer._is_instruction_relevant(instruction, sibling_dir)
                        ):
                            has_coverage = True
                            break
                if has_coverage:
                    break

            assert has_coverage, (
                f"COVERAGE GAP: Directory {sibling_dir} cannot access react-components-local instruction\n"
                f"Inheritance chain: {[str(p) for p in inheritance_chain]}\n"
                f"Placement map: {[(str(k), [i.name for i in v]) for k, v in placement_map.items()]}\n"
                f"This violates the mandatory coverage constraint: all matching directories must have access"
            )

    def test_minimal_coverage_for_sibling_directories(self, sibling_directory_project):
        """Test that the fix places instructions at common ancestor for sibling coverage."""
        optimizer = ContextOptimizer(str(sibling_directory_project))

        react_instruction = Instruction(
            name="react-components",
            file_path=Path("react.md"),
            description="React component guidelines",
            apply_to="**/*.{tsx,jsx}",
            content="React guidelines",
        )

        placement_map = optimizer.optimize_instruction_placement([react_instruction])

        # The instruction should be placed at the root (common ancestor)
        # to ensure all sibling directories can inherit it
        root_dir = sibling_directory_project  # noqa: F841

        # Verify the instruction is accessible from root or a high enough level
        # that covers all sibling directories
        directories_with_tsx = [
            sibling_directory_project / "frontend" / "components",
            sibling_directory_project / "src" / "components",
            sibling_directory_project / "tests",
        ]

        for tsx_dir in directories_with_tsx:
            inheritance_chain = optimizer._get_inheritance_chain(tsx_dir)

            has_coverage = False
            for dir_in_chain in inheritance_chain:
                if dir_in_chain in placement_map:
                    for instruction in placement_map[dir_in_chain]:
                        if (
                            instruction.name == "react-components"
                            and optimizer._is_instruction_relevant(instruction, tsx_dir)
                        ):
                            has_coverage = True
                            break
                if has_coverage:
                    break

            assert has_coverage, (
                f"FIXED COVERAGE: {tsx_dir} should be able to inherit react-components\n"
                f"Inheritance chain: {[str(p) for p in inheritance_chain]}\n"
                f"Placement map: {[(str(k), [i.name for i in v]) for k, v in placement_map.items()]}"
            )

    def test_corporate_website_exact_reproduction(self):
        """Test that reproduces the exact corporate-website scenario."""
        # This would be run against the actual corporate-website structure
        # when we have the fix in place
        pass
