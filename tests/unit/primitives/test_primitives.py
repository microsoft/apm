"""Unit tests for primitive models (parser file tests live in test_discovery_parser.py)."""

import unittest
from pathlib import Path

import pytest

from apm_cli.primitives.models import (
    Chatmode,
    Context,
    Instruction,
    PrimitiveCollection,
    PrimitiveConflict,
    Skill,
    resolve_user_invocable,
)
from apm_cli.primitives.parser import (
    validate_primitive,  # noqa: F401
)

pytestmark = pytest.mark.unit


class TestPrimitiveModels(unittest.TestCase):
    """Test cases for primitive data models."""

    def test_chatmode_validation(self):
        """Test chatmode validation."""
        # Valid chatmode
        chatmode = Chatmode(
            name="test-chatmode",
            file_path=Path("test.agent.md"),
            description="Test chatmode",
            apply_to="**/*.py",
            content="# Test content",
            author="Test Author",
        )
        self.assertEqual(chatmode.validate(), [])

        # Missing description
        chatmode_no_desc = Chatmode(
            name="test",
            file_path=Path("test.agent.md"),
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
            file_path=Path("test.agent.md"),
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
        chatmode = Chatmode("test", Path("test.agent.md"), "desc", None, "content")
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
            file_path=Path(".github/agents/my-chatmode.agent.md"),
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
            file_path=Path("dep.agent.md"),
            description="dep version",
            apply_to=None,
            content="dep content",
            source="dependency:pkg-a",
        )
        local_chatmode = Chatmode(
            name="assistant",
            file_path=Path("local.agent.md"),
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
            file_path=Path("local.agent.md"),
            description="local version",
            apply_to=None,
            content="local content",
            source="local",
        )
        dep_chatmode = Chatmode(
            name="assistant",
            file_path=Path("dep.agent.md"),
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
            file_path=Path("a.agent.md"),
            description="from a",
            apply_to=None,
            content="content a",
            source="dependency:pkg-a",
        )
        dep_b = Chatmode(
            name="shared",
            file_path=Path("b.agent.md"),
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
            file_path=Path("dep.agent.md"),
            description="d",
            apply_to=None,
            content="c",
            source="dependency:a",
        )
        local = Chatmode(
            name="x",
            file_path=Path("local.agent.md"),
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
            file_path=Path("a.agent.md"),
            description="d",
            apply_to=None,
            content="c",
            source="local",
        )
        dep_c = Chatmode(
            name="b",
            file_path=Path("b.agent.md"),
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


if __name__ == "__main__":
    unittest.main()


def test_absent_defaults_to_user_invocable() -> None:
    """A missing field, including an explicit null, stays user-invocable."""
    assert resolve_user_invocable({}) is True
    assert resolve_user_invocable({"description": "Agent"}) is True
    assert resolve_user_invocable({"user-invocable": None}) is True


def test_boolean_user_invocable_passes_through() -> None:
    """Boolean user-invocable values are returned unchanged."""
    assert resolve_user_invocable({"user-invocable": False}) is False
    assert resolve_user_invocable({"user-invocable": True}) is True


def test_visibility_internal_is_programmatic_only_alias() -> None:
    """Only the internal visibility token aliases to not user-invocable."""
    assert resolve_user_invocable({"visibility": "internal"}) is False
    assert resolve_user_invocable({"visibility": " Internal "}) is False
    assert resolve_user_invocable({"visibility": "INTERNAL"}) is False
    assert resolve_user_invocable({"visibility": "public"}) is True
    # Non-string visibility is not the alias and must not be stringified.
    assert resolve_user_invocable({"visibility": True}) is True
    assert resolve_user_invocable({"visibility": ["internal"]}) is True
    assert resolve_user_invocable({"visibility": 0}) is True


def test_canonical_field_wins_when_both_are_set() -> None:
    """user-invocable wins over visibility, including quoted tokens."""
    assert resolve_user_invocable({"user-invocable": True, "visibility": "internal"}) is True
    assert resolve_user_invocable({"user-invocable": False, "visibility": "public"}) is False
    assert resolve_user_invocable({"user-invocable": "false", "visibility": "public"}) is False
    assert resolve_user_invocable({"user-invocable": "true", "visibility": "internal"}) is True


def test_string_and_non_bool_values_follow_coercion() -> None:
    """Quoted falsy tokens and non-bool values use the documented coercion."""
    falsy_tokens = ("false", "no", "0", "off", "False", " OFF ")
    for token in falsy_tokens:
        assert resolve_user_invocable({"user-invocable": token}) is False, token
    truthy_tokens = ("true", "yes", "1", "on", "TRUE", "", "internal")
    for token in truthy_tokens:
        assert resolve_user_invocable({"user-invocable": token}) is True, token
    assert resolve_user_invocable({"user-invocable": 0}) is False
    assert resolve_user_invocable({"user-invocable": 1}) is True
    assert resolve_user_invocable({"user-invocable": 0.0}) is False
    assert resolve_user_invocable({"user-invocable": []}) is False
    assert resolve_user_invocable({"user-invocable": ["x"]}) is True
    # Explicit null is absent, so the visibility alias still applies.
    assert resolve_user_invocable({"user-invocable": None, "visibility": "internal"}) is False
