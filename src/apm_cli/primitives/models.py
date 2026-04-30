"""Data models for APM context."""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union  # noqa: F401, UP035


@dataclass
class Chatmode:
    """Represents a chatmode primitive."""

    name: str
    file_path: Path
    description: str
    apply_to: str | None  # Glob pattern for file targeting (optional for chatmodes)
    content: str
    author: str | None = None
    version: str | None = None
    source: str | None = None  # Source of primitive: "local" or "dependency:{package_name}"

    def validate(self) -> list[str]:
        """Validate chatmode structure.

        Returns:
            List[str]: List of validation errors.
        """
        errors = []
        if not self.description:
            errors.append("Missing 'description' in frontmatter")
        if not self.content.strip():
            errors.append("Empty content")
        return errors


@dataclass
class Instruction:
    """Represents an instruction primitive."""

    name: str
    file_path: Path
    description: str
    apply_to: str  # Glob pattern for file targeting (required for instructions)
    content: str
    author: str | None = None
    version: str | None = None
    source: str | None = None  # Source of primitive: "local" or "dependency:{package_name}"

    def validate(self) -> list[str]:
        """Validate instruction structure.

        Returns:
            List[str]: List of validation errors.
        """
        errors = []
        if not self.description:
            errors.append("Missing 'description' in frontmatter")
        if not self.apply_to:
            errors.append("No 'applyTo' pattern specified -- instruction will apply globally")
        if not self.content.strip():
            errors.append("Empty content")
        return errors


@dataclass
class Context:
    """Represents a context primitive."""

    name: str
    file_path: Path
    content: str
    description: str | None = None
    author: str | None = None
    version: str | None = None
    source: str | None = None  # Source of primitive: "local" or "dependency:{package_name}"

    def validate(self) -> list[str]:
        """Validate context structure.

        Returns:
            List[str]: List of validation errors.
        """
        errors = []
        if not self.content.strip():
            errors.append("Empty content")
        return errors


@dataclass
class Skill:
    """Represents a SKILL.md primitive (package meta-guide).

    SKILL.md is an optional file at the package root that describes
    how to use the package. It's the fourth APM primitive type.

    For Claude: SKILL.md is used natively for contextual activation.
    For VSCode: SKILL.md is transformed to .agent.md for dropdown selection.
    """

    name: str
    file_path: Path
    description: str
    content: str
    source: str | None = None  # Source of primitive: "local" or "dependency:{package_name}"

    def validate(self) -> list[str]:
        """Validate skill structure.

        Returns:
            List[str]: List of validation errors.
        """
        errors = []
        if not self.name:
            errors.append("Missing 'name' in frontmatter")
        if not self.description:
            errors.append("Missing 'description' in frontmatter")
        if not self.content.strip():
            errors.append("Empty content")
        return errors


# Union type for all primitive types
Primitive = Union[Chatmode, Instruction, Context, Skill]  # noqa: UP007


@dataclass
class PrimitiveConflict:
    """Represents a conflict between primitives from different sources."""

    primitive_name: str
    primitive_type: str  # 'chatmode', 'instruction', 'context'
    winning_source: str  # Source that won the conflict
    losing_sources: list[str]  # Sources that lost the conflict
    file_path: Path  # Path of the winning primitive

    def __str__(self) -> str:
        """String representation of the conflict."""
        losing_list = ", ".join(self.losing_sources)
        return f"{self.primitive_type} '{self.primitive_name}': {self.winning_source} overrides {losing_list}"


@dataclass
class PrimitiveCollection:
    """Collection of discovered primitives."""

    chatmodes: list[Chatmode]
    instructions: list[Instruction]
    contexts: list[Context]
    skills: list[Skill]  # SKILL.md primitives (package meta-guides)
    conflicts: list[PrimitiveConflict]  # Track conflicts during discovery

    def __init__(self):
        self.chatmodes = []
        self.instructions = []
        self.contexts = []
        self.skills = []
        self.conflicts = []
        # Name->index maps for O(1) conflict lookups (see #171)
        self._chatmode_index: dict[str, int] = {}
        self._instruction_index: dict[str, int] = {}
        self._context_index: dict[str, int] = {}
        self._skill_index: dict[str, int] = {}

    def _index_for(self, primitive_type: str) -> dict[str, int]:
        """Return the name->index map for the given primitive type."""
        if primitive_type == "chatmode":  # noqa: SIM116
            return self._chatmode_index
        elif primitive_type == "instruction":
            return self._instruction_index
        elif primitive_type == "context":
            return self._context_index
        else:
            return self._skill_index

    def add_primitive(self, primitive: Primitive) -> None:
        """Add a primitive to the appropriate collection.

        If a primitive with the same name already exists, the new primitive
        will only be added if it has higher priority (lower priority primitives
        are tracked as conflicts).
        """
        if isinstance(primitive, Chatmode):
            self._add_with_conflict_detection(primitive, self.chatmodes, "chatmode")
        elif isinstance(primitive, Instruction):
            self._add_with_conflict_detection(primitive, self.instructions, "instruction")
        elif isinstance(primitive, Context):
            self._add_with_conflict_detection(primitive, self.contexts, "context")
        elif isinstance(primitive, Skill):
            self._add_with_conflict_detection(primitive, self.skills, "skill")
        else:
            raise ValueError(f"Unknown primitive type: {type(primitive)}")

    def _add_with_conflict_detection(
        self, new_primitive: Primitive, collection: list[Primitive], primitive_type: str
    ) -> None:
        """Add primitive with conflict detection."""
        name_index = self._index_for(primitive_type)
        existing_index = name_index.get(new_primitive.name)

        if existing_index is None:
            # No conflict, just add the primitive
            name_index[new_primitive.name] = len(collection)
            collection.append(new_primitive)
        else:
            # Conflict detected - apply priority rules
            existing = collection[existing_index]

            # Priority rules:
            # 1. Local always wins over dependency
            # 2. Earlier dependency wins over later dependency
            should_replace = self._should_replace_primitive(existing, new_primitive)

            if should_replace:
                # Replace existing with new primitive and record conflict
                conflict = PrimitiveConflict(
                    primitive_name=new_primitive.name,
                    primitive_type=primitive_type,
                    winning_source=new_primitive.source or "unknown",
                    losing_sources=[existing.source or "unknown"],
                    file_path=new_primitive.file_path,
                )
                self.conflicts.append(conflict)
                collection[existing_index] = new_primitive
            else:
                # Keep existing and record that new primitive was ignored
                conflict = PrimitiveConflict(
                    primitive_name=existing.name,
                    primitive_type=primitive_type,
                    winning_source=existing.source or "unknown",
                    losing_sources=[new_primitive.source or "unknown"],
                    file_path=existing.file_path,
                )
                self.conflicts.append(conflict)
                # Don't add new_primitive to collection

    def _should_replace_primitive(self, existing: Primitive, new: Primitive) -> bool:
        """Determine if new primitive should replace existing based on priority."""
        existing_source = existing.source or "unknown"
        new_source = new.source or "unknown"

        # Local always wins
        if existing_source == "local":
            return False  # Never replace local
        if new_source == "local":  # noqa: SIM103
            return True  # Always replace with local

        # Both are dependencies - this shouldn't happen in correct usage
        # since dependencies should be processed in order, but handle gracefully
        return False  # Keep first dependency (existing)

    def all_primitives(self) -> list[Primitive]:
        """Get all primitives as a single list."""
        return self.chatmodes + self.instructions + self.contexts + self.skills

    def count(self) -> int:
        """Get total count of all primitives."""
        return len(self.chatmodes) + len(self.instructions) + len(self.contexts) + len(self.skills)

    def has_conflicts(self) -> bool:
        """Check if any conflicts were detected during discovery."""
        return len(self.conflicts) > 0

    def get_conflicts_by_type(self, primitive_type: str) -> list[PrimitiveConflict]:
        """Get conflicts for a specific primitive type."""
        return [c for c in self.conflicts if c.primitive_type == primitive_type]

    def get_primitives_by_source(self, source: str) -> list[Primitive]:
        """Get all primitives from a specific source."""
        all_primitives = self.all_primitives()
        return [p for p in all_primitives if p.source == source]
