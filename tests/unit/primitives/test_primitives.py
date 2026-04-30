"""Unit tests for primitives parser and models."""

import os
import tempfile
import unittest
from pathlib import Path

from apm_cli.primitives.discovery import discover_primitives, find_primitive_files
from apm_cli.primitives.models import (
    Chatmode,
    Context,
    Instruction,
    PrimitiveCollection,
    PrimitiveConflict,
    Skill,
)
from apm_cli.primitives.parser import (
    _extract_primitive_name,
    parse_primitive_file,
    validate_primitive,  # noqa: F401
)


class TestPrimitiveModels(unittest.TestCase):
    """Test cases for primitive data models."""

    def test_chatmode_validation(self):
        """Test chatmode validation."""
        # Valid chatmode
        chatmode = Chatmode(
            name="test-chatmode",
            file_path=Path("test.chatmode.md"),
            description="Test chatmode",
            apply_to="**/*.py",
            content="# Test content",
            author="Test Author",
        )
        self.assertEqual(chatmode.validate(), [])

        # Missing description
        chatmode_no_desc = Chatmode(
            name="test",
            file_path=Path("test.chatmode.md"),
            description="",
            apply_to=None,
            content="# Test content",
        )
        errors = chatmode_no_desc.validate()
        self.assertEqual(len(errors), 1)
        self.assertIn("description", errors[0])

        # Empty content
        chatmode_no_content = Chatmode(
            name="test",
            file_path=Path("test.chatmode.md"),
            description="Test",
            apply_to=None,
            content="",
        )
        errors = chatmode_no_content.validate()
        self.assertEqual(len(errors), 1)
        self.assertIn("content", errors[0])

    def test_instruction_validation(self):
        """Test instruction validation."""
        # Valid instruction
        instruction = Instruction(
            name="test-instruction",
            file_path=Path("test.instructions.md"),
            description="Test instruction",
            apply_to="**/*.{ts,tsx}",
            content="# Test instruction content",
        )
        self.assertEqual(instruction.validate(), [])

        # Missing applyTo (instruction will apply globally)
        instruction_no_apply = Instruction(
            name="test",
            file_path=Path("test.instructions.md"),
            description="Test",
            apply_to="",
            content="# Test content",
        )
        errors = instruction_no_apply.validate()
        self.assertEqual(len(errors), 1)
        self.assertIn("applyTo", errors[0])

    def test_context_validation(self):
        """Test context validation."""
        # Valid context
        context = Context(
            name="test-context",
            file_path=Path("test.context.md"),
            content="# Test context content",
        )
        self.assertEqual(context.validate(), [])

        # Empty content
        context_no_content = Context(name="test", file_path=Path("test.context.md"), content="")
        errors = context_no_content.validate()
        self.assertEqual(len(errors), 1)
        self.assertIn("content", errors[0])

    def test_primitive_collection(self):
        """Test primitive collection functionality."""
        collection = PrimitiveCollection()

        # Test empty collection
        self.assertEqual(collection.count(), 0)
        self.assertEqual(len(collection.all_primitives()), 0)

        # Add primitives
        chatmode = Chatmode("test", Path("test.chatmode.md"), "desc", None, "content")
        instruction = Instruction(
            "test", Path("test.instructions.md"), "desc", "**/*.py", "content"
        )
        context = Context("test", Path("test.context.md"), "content")

        collection.add_primitive(chatmode)
        collection.add_primitive(instruction)
        collection.add_primitive(context)

        # Test counts
        self.assertEqual(collection.count(), 3)
        self.assertEqual(len(collection.chatmodes), 1)
        self.assertEqual(len(collection.instructions), 1)
        self.assertEqual(len(collection.contexts), 1)
        self.assertEqual(len(collection.all_primitives()), 3)

    def test_instruction_validation_missing_description(self):
        """Test instruction validation with missing description."""
        instruction = Instruction(
            name="test",
            file_path=Path("test.instructions.md"),
            description="",
            apply_to="**/*.py",
            content="# content",
        )
        errors = instruction.validate()
        self.assertIn("description", errors[0])

    def test_instruction_validation_empty_content(self):
        """Test instruction validation with empty content."""
        instruction = Instruction(
            name="test",
            file_path=Path("test.instructions.md"),
            description="desc",
            apply_to="**/*.py",
            content="   ",
        )
        errors = instruction.validate()
        self.assertEqual(len(errors), 1)
        self.assertIn("content", errors[0])

    def test_instruction_validation_multiple_errors(self):
        """Test instruction validation returns all errors."""
        instruction = Instruction(
            name="test",
            file_path=Path("test.instructions.md"),
            description="",
            apply_to="",
            content="",
        )
        errors = instruction.validate()
        self.assertEqual(len(errors), 3)

    def test_skill_validation_valid(self):
        """Test valid Skill passes validation."""
        skill = Skill(
            name="my-skill",
            file_path=Path("SKILL.md"),
            description="A helpful skill",
            content="# Skill content",
        )
        self.assertEqual(skill.validate(), [])

    def test_skill_validation_missing_name(self):
        """Test Skill validation with empty name."""
        skill = Skill(name="", file_path=Path("SKILL.md"), description="desc", content="content")
        errors = skill.validate()
        self.assertIn("name", errors[0])

    def test_skill_validation_missing_description(self):
        """Test Skill validation with missing description."""
        skill = Skill(name="test", file_path=Path("SKILL.md"), description="", content="content")
        errors = skill.validate()
        self.assertEqual(len(errors), 1)
        self.assertIn("description", errors[0])

    def test_skill_validation_empty_content(self):
        """Test Skill validation with empty content."""
        skill = Skill(name="test", file_path=Path("SKILL.md"), description="desc", content="")
        errors = skill.validate()
        self.assertEqual(len(errors), 1)
        self.assertIn("content", errors[0])

    def test_skill_validation_all_errors(self):
        """Test Skill validation reports all errors."""
        skill = Skill(name="", file_path=Path("SKILL.md"), description="", content="")
        errors = skill.validate()
        self.assertEqual(len(errors), 3)

    def test_primitive_conflict_str(self):
        """Test PrimitiveConflict string representation."""
        conflict = PrimitiveConflict(
            primitive_name="my-chatmode",
            primitive_type="chatmode",
            winning_source="local",
            losing_sources=["dependency:pkg-a", "dependency:pkg-b"],
            file_path=Path(".github/chatmodes/my-chatmode.chatmode.md"),
        )
        result = str(conflict)
        self.assertIn("chatmode", result)
        self.assertIn("my-chatmode", result)
        self.assertIn("local", result)
        self.assertIn("dependency:pkg-a", result)

    def test_primitive_collection_add_skill(self):
        """Test adding Skill primitives to collection."""
        collection = PrimitiveCollection()
        skill = Skill(
            name="guide",
            file_path=Path("SKILL.md"),
            description="desc",
            content="content",
        )
        collection.add_primitive(skill)
        self.assertEqual(len(collection.skills), 1)
        self.assertEqual(collection.count(), 1)

    def test_primitive_collection_conflict_local_wins_over_dependency(self):
        """Test that local primitive replaces dependency when names conflict."""
        collection = PrimitiveCollection()
        dep_chatmode = Chatmode(
            name="assistant",
            file_path=Path("dep.chatmode.md"),
            description="dep version",
            apply_to=None,
            content="dep content",
            source="dependency:pkg-a",
        )
        local_chatmode = Chatmode(
            name="assistant",
            file_path=Path("local.chatmode.md"),
            description="local version",
            apply_to=None,
            content="local content",
            source="local",
        )
        collection.add_primitive(dep_chatmode)
        collection.add_primitive(local_chatmode)

        # Local should win - only one chatmode in collection
        self.assertEqual(len(collection.chatmodes), 1)
        self.assertEqual(collection.chatmodes[0].source, "local")
        # Conflict should be recorded
        self.assertTrue(collection.has_conflicts())
        self.assertEqual(len(collection.conflicts), 1)
        self.assertEqual(collection.conflicts[0].winning_source, "local")

    def test_primitive_collection_local_not_replaced_by_dependency(self):
        """Test that local primitive is NOT replaced when dependency arrives later."""
        collection = PrimitiveCollection()
        local_chatmode = Chatmode(
            name="assistant",
            file_path=Path("local.chatmode.md"),
            description="local version",
            apply_to=None,
            content="local content",
            source="local",
        )
        dep_chatmode = Chatmode(
            name="assistant",
            file_path=Path("dep.chatmode.md"),
            description="dep version",
            apply_to=None,
            content="dep content",
            source="dependency:pkg-a",
        )
        collection.add_primitive(local_chatmode)
        collection.add_primitive(dep_chatmode)

        # Local should remain
        self.assertEqual(len(collection.chatmodes), 1)
        self.assertEqual(collection.chatmodes[0].source, "local")
        self.assertTrue(collection.has_conflicts())

    def test_primitive_collection_first_dependency_wins(self):
        """Test that first dependency is kept when two dependencies conflict."""
        collection = PrimitiveCollection()
        dep_a = Chatmode(
            name="shared",
            file_path=Path("a.chatmode.md"),
            description="from a",
            apply_to=None,
            content="content a",
            source="dependency:pkg-a",
        )
        dep_b = Chatmode(
            name="shared",
            file_path=Path("b.chatmode.md"),
            description="from b",
            apply_to=None,
            content="content b",
            source="dependency:pkg-b",
        )
        collection.add_primitive(dep_a)
        collection.add_primitive(dep_b)

        self.assertEqual(len(collection.chatmodes), 1)
        self.assertEqual(collection.chatmodes[0].source, "dependency:pkg-a")
        self.assertTrue(collection.has_conflicts())

    def test_primitive_collection_no_conflicts(self):
        """Test has_conflicts returns False when no conflicts exist."""
        collection = PrimitiveCollection()
        self.assertFalse(collection.has_conflicts())

    def test_primitive_collection_get_conflicts_by_type(self):
        """Test filtering conflicts by primitive type."""
        collection = PrimitiveCollection()
        # Trigger a conflict for chatmode
        dep = Chatmode(
            name="x",
            file_path=Path("dep.chatmode.md"),
            description="d",
            apply_to=None,
            content="c",
            source="dependency:a",
        )
        local = Chatmode(
            name="x",
            file_path=Path("local.chatmode.md"),
            description="d",
            apply_to=None,
            content="c",
            source="local",
        )
        collection.add_primitive(dep)
        collection.add_primitive(local)

        chatmode_conflicts = collection.get_conflicts_by_type("chatmode")
        instruction_conflicts = collection.get_conflicts_by_type("instruction")
        self.assertEqual(len(chatmode_conflicts), 1)
        self.assertEqual(len(instruction_conflicts), 0)

    def test_primitive_collection_get_primitives_by_source(self):
        """Test filtering primitives by source."""
        collection = PrimitiveCollection()
        local_c = Chatmode(
            name="a",
            file_path=Path("a.chatmode.md"),
            description="d",
            apply_to=None,
            content="c",
            source="local",
        )
        dep_c = Chatmode(
            name="b",
            file_path=Path("b.chatmode.md"),
            description="d",
            apply_to=None,
            content="c",
            source="dependency:pkg",
        )
        collection.add_primitive(local_c)
        collection.add_primitive(dep_c)

        local_prims = collection.get_primitives_by_source("local")
        dep_prims = collection.get_primitives_by_source("dependency:pkg")
        self.assertEqual(len(local_prims), 1)
        self.assertEqual(len(dep_prims), 1)

    def test_primitive_collection_add_unknown_type_raises(self):
        """Test that adding an unknown type raises ValueError."""
        collection = PrimitiveCollection()
        with self.assertRaises((ValueError, AttributeError)):
            collection.add_primitive("not-a-primitive")


class TestPrimitiveParser(unittest.TestCase):
    """Test cases for the primitive parser."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_path = self.temp_dir.name

    def tearDown(self):
        """Tear down test fixtures."""
        self.temp_dir.cleanup()

    def test_parse_chatmode_file(self):
        """Test parsing a chatmode file."""
        chatmode_content = """---
description: Test chatmode for code review
author: Test Author
applyTo: "**/*.{py,js}"
version: "1.0.0"
---

# Code Review Assistant

You are an expert code reviewer. Analyze the provided code for:

1. Code quality and best practices
2. Potential bugs or issues
3. Performance considerations
4. Security vulnerabilities

Provide constructive feedback and suggestions for improvement.
"""

        # Create test file
        file_path = os.path.join(self.temp_dir_path, "code-review.chatmode.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(chatmode_content)

        # Parse file
        primitive = parse_primitive_file(file_path)

        # Verify it's a Chatmode
        self.assertIsInstance(primitive, Chatmode)
        self.assertEqual(primitive.name, "code-review")
        self.assertEqual(primitive.description, "Test chatmode for code review")
        self.assertEqual(primitive.author, "Test Author")
        self.assertEqual(primitive.apply_to, "**/*.{py,js}")
        self.assertEqual(primitive.version, "1.0.0")
        self.assertIn("Code Review Assistant", primitive.content)

        # Test validation
        errors = primitive.validate()
        self.assertEqual(len(errors), 0)

    def test_parse_instruction_file(self):
        """Test parsing an instruction file."""
        instruction_content = """---
description: Python coding standards and conventions
applyTo: "**/*.py"
author: Development Team
---

# Python Coding Standards

Follow these coding standards when writing Python code:

## Style Guide
- Use PEP 8 for formatting
- Maximum line length of 88 characters
- Use type hints for function parameters and returns

## Best Practices
- Write docstrings for all public functions
- Use meaningful variable names
- Follow SOLID principles
"""

        # Create test file
        file_path = os.path.join(self.temp_dir_path, "python-standards.instructions.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(instruction_content)

        # Parse file
        primitive = parse_primitive_file(file_path)

        # Verify it's an Instruction
        self.assertIsInstance(primitive, Instruction)
        self.assertEqual(primitive.name, "python-standards")
        self.assertEqual(primitive.description, "Python coding standards and conventions")
        self.assertEqual(primitive.apply_to, "**/*.py")
        self.assertEqual(primitive.author, "Development Team")
        self.assertIn("Python Coding Standards", primitive.content)

        # Test validation
        errors = primitive.validate()
        self.assertEqual(len(errors), 0)

    def test_parse_context_file(self):
        """Test parsing a context file."""
        context_content = """---
description: Project context and background
---

# Project Context

This project is a command-line tool for managing AI workflows.

## Architecture
- Built with Python and Click
- Uses frontmatter for file parsing
- Supports multiple runtime environments

## Key Components
- CLI interface
- Workflow engine
- Runtime management
"""

        # Create test file
        file_path = os.path.join(self.temp_dir_path, "project-info.context.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(context_content)

        # Parse file
        primitive = parse_primitive_file(file_path)

        # Verify it's a Context
        self.assertIsInstance(primitive, Context)
        self.assertEqual(primitive.name, "project-info")
        self.assertEqual(primitive.description, "Project context and background")
        self.assertIn("Project Context", primitive.content)

        # Test validation
        errors = primitive.validate()
        self.assertEqual(len(errors), 0)

    def test_extract_primitive_name(self):
        """Test primitive name extraction from various path formats."""
        # Test structured .apm/ paths
        self.assertEqual(
            _extract_primitive_name(Path(".apm/chatmodes/code-review.chatmode.md")),
            "code-review",
        )
        self.assertEqual(
            _extract_primitive_name(Path(".apm/instructions/python-style.instructions.md")),
            "python-style",
        )
        self.assertEqual(
            _extract_primitive_name(Path(".apm/context/project-info.context.md")),
            "project-info",
        )

        # Test .github/ paths (VSCode compatibility)
        self.assertEqual(
            _extract_primitive_name(Path(".github/chatmodes/assistant.chatmode.md")),
            "assistant",
        )

        # Test memory files
        self.assertEqual(
            _extract_primitive_name(Path(".apm/memory/team-info.memory.md")),
            "team-info",
        )

        # Test generic files
        self.assertEqual(_extract_primitive_name(Path("my-chatmode.chatmode.md")), "my-chatmode")

    def test_malformed_files(self):
        """Test handling of malformed files."""
        # File with invalid YAML frontmatter
        malformed_content = """---
description: Test
invalid yaml: [
---

# Test content
"""

        file_path = os.path.join(self.temp_dir_path, "malformed.chatmode.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(malformed_content)

        # Should raise ValueError
        with self.assertRaises(ValueError):
            parse_primitive_file(file_path)


class TestPrimitiveDiscovery(unittest.TestCase):
    """Test cases for primitive discovery."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_path = self.temp_dir.name

        # Create directory structure
        os.makedirs(os.path.join(self.temp_dir_path, ".apm", "chatmodes"), exist_ok=True)
        os.makedirs(os.path.join(self.temp_dir_path, ".apm", "instructions"), exist_ok=True)
        os.makedirs(os.path.join(self.temp_dir_path, ".apm", "context"), exist_ok=True)
        os.makedirs(os.path.join(self.temp_dir_path, ".github", "chatmodes"), exist_ok=True)

    def tearDown(self):
        """Tear down test fixtures."""
        self.temp_dir.cleanup()

    def test_discover_primitives_structured(self):
        """Test discovering primitives in structured directories."""
        # Create test files
        chatmode_content = """---
description: Test chatmode
---

# Test Chatmode Content
"""

        instruction_content = """---
description: Test instruction
applyTo: "**/*.py"
---

# Test Instruction Content
"""

        context_content = """---
description: Test context
---

# Test Context Content
"""

        # Write files
        with open(
            os.path.join(self.temp_dir_path, ".apm", "chatmodes", "assistant.chatmode.md"),
            "w",
        ) as f:
            f.write(chatmode_content)

        with open(
            os.path.join(self.temp_dir_path, ".apm", "instructions", "style.instructions.md"),
            "w",
        ) as f:
            f.write(instruction_content)

        with open(
            os.path.join(self.temp_dir_path, ".apm", "context", "project.context.md"),
            "w",
        ) as f:
            f.write(context_content)

        with open(
            os.path.join(self.temp_dir_path, ".github", "chatmodes", "vscode.chatmode.md"),
            "w",
        ) as f:
            f.write(chatmode_content)

        # Discover primitives
        collection = discover_primitives(self.temp_dir_path)

        # Verify discovery
        self.assertEqual(collection.count(), 4)
        self.assertEqual(len(collection.chatmodes), 2)
        self.assertEqual(len(collection.instructions), 1)
        self.assertEqual(len(collection.contexts), 1)

        # Check names
        chatmode_names = [c.name for c in collection.chatmodes]
        self.assertIn("assistant", chatmode_names)
        self.assertIn("vscode", chatmode_names)

        instruction_names = [i.name for i in collection.instructions]
        self.assertIn("style", instruction_names)

        context_names = [c.name for c in collection.contexts]
        self.assertIn("project", context_names)

    def test_find_primitive_files(self):
        """Test finding primitive files with glob patterns."""
        # Create test files
        os.makedirs(os.path.join(self.temp_dir_path, "custom"), exist_ok=True)

        test_files = ["test1.chatmode.md", "custom/test2.chatmode.md"]

        for file_rel_path in test_files:
            file_path = os.path.join(self.temp_dir_path, file_rel_path)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w") as f:
                f.write("---\ndescription: Test\n---\n\n# Test")

        # Test pattern matching
        patterns = ["**/*.chatmode.md"]
        found_files = find_primitive_files(self.temp_dir_path, patterns)

        # Should find 2 files (glob doesn't match hidden directories by default)
        self.assertEqual(len(found_files), 2)

        # Verify all are Path objects
        for file_path in found_files:
            self.assertIsInstance(file_path, Path)
            self.assertTrue(file_path.name.endswith(".chatmode.md"))

    def test_find_primitive_files_specific_patterns(self):
        """Test finding primitive files with specific patterns."""
        # Test with .apm specific pattern
        apm_file = os.path.join(self.temp_dir_path, ".apm", "chatmodes", "test.chatmode.md")
        with open(apm_file, "w") as f:
            f.write("---\ndescription: Test\n---\n\n# Test")

        # Test .apm specific pattern
        patterns = ["**/.apm/chatmodes/*.chatmode.md"]
        found_files = find_primitive_files(self.temp_dir_path, patterns)

        # Should find the .apm file
        self.assertEqual(len(found_files), 1)
        self.assertTrue(found_files[0].name.endswith(".chatmode.md"))


if __name__ == "__main__":
    unittest.main()
