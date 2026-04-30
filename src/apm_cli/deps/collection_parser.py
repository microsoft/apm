"""Parser for APM collection manifest files (.collection.yml)."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional  # noqa: F401, UP035

import yaml


@dataclass
class CollectionItem:
    """Represents a single item in a collection manifest."""

    path: str  # Relative path to the file (e.g., "prompts/code-review.prompt.md")
    kind: str  # Type of primitive (e.g., "prompt", "instruction", "chat-mode")

    @property
    def subdirectory(self) -> str:
        """Get the .apm subdirectory for this item based on its kind.

        Returns:
            str: Subdirectory name (e.g., "prompts", "instructions", "chatmodes")
        """
        kind_to_subdir = {
            "prompt": "prompts",
            "instruction": "instructions",
            "chat-mode": "chatmodes",
            "chatmode": "chatmodes",
            "agent": "agents",
            "context": "contexts",
        }
        return kind_to_subdir.get(self.kind.lower(), "prompts")  # Default to prompts


@dataclass
class CollectionManifest:
    """Represents a parsed collection manifest (.collection.yml)."""

    id: str
    name: str
    description: str
    items: list[CollectionItem]
    tags: list[str] | None = None
    display: dict[str, Any] | None = None

    @property
    def item_count(self) -> int:
        """Get the number of items in this collection."""
        return len(self.items)

    def get_items_by_kind(self, kind: str) -> list[CollectionItem]:
        """Get all items of a specific kind.

        Args:
            kind: The kind to filter by (e.g., "prompt", "instruction")

        Returns:
            List of items matching the kind
        """
        return [item for item in self.items if item.kind.lower() == kind.lower()]


def parse_collection_yml(content: bytes) -> CollectionManifest:
    """Parse a collection YAML manifest.

    Args:
        content: Raw YAML content as bytes

    Returns:
        CollectionManifest: Parsed and validated collection manifest

    Raises:
        ValueError: If the YAML is invalid or missing required fields
        yaml.YAMLError: If YAML parsing fails
    """
    try:
        # Parse YAML
        data = yaml.safe_load(content)

        if not isinstance(data, dict):
            raise ValueError("Collection YAML must be a dictionary")

        # Validate required fields
        required_fields = ["id", "name", "description", "items"]
        missing_fields = [field for field in required_fields if field not in data]

        if missing_fields:
            raise ValueError(
                f"Collection manifest missing required fields: {', '.join(missing_fields)}"
            )

        # Validate items array
        items_data = data.get("items", [])
        if not isinstance(items_data, list):
            raise ValueError("Collection 'items' must be a list")

        if not items_data:
            raise ValueError("Collection must contain at least one item")

        # Parse items
        items = []
        for idx, item_data in enumerate(items_data):
            if not isinstance(item_data, dict):
                raise ValueError(f"Collection item {idx} must be a dictionary")

            # Validate item fields
            if "path" not in item_data:
                raise ValueError(f"Collection item {idx} missing required field 'path'")

            if "kind" not in item_data:
                raise ValueError(f"Collection item {idx} missing required field 'kind'")

            items.append(CollectionItem(path=item_data["path"], kind=item_data["kind"]))

        # Create manifest
        return CollectionManifest(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            items=items,
            tags=data.get("tags"),
            display=data.get("display"),
        )

    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML format: {e}")  # noqa: B904
