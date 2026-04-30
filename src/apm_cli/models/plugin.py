"""Plugin management data models."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional  # noqa: F401, UP035


@dataclass
class PluginMetadata:
    """Metadata for a plugin.

    Attributes:
        id: Unique plugin identifier (e.g., "awesome-copilot")
        name: Human-readable plugin name
        version: Semantic version string
        description: Short description of the plugin
        author: Plugin author name or organization
        repository: Repository reference (e.g., "owner/repo" or "dev.azure.com/org/project/repo")
        homepage: Optional homepage URL
        license: Optional license identifier (e.g., "MIT", "Apache-2.0")
        tags: List of tags for categorization
        dependencies: List of plugin dependencies (plugin IDs)
    """

    id: str
    name: str
    version: str
    description: str
    author: str
    repository: str | None = None
    homepage: str | None = None
    license: str | None = None
    tags: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert metadata to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "repository": self.repository,
            "homepage": self.homepage,
            "license": self.license,
            "tags": self.tags,
            "dependencies": self.dependencies,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginMetadata":
        """Create metadata from dictionary."""
        return cls(
            id=data["id"],
            name=data["name"],
            version=data["version"],
            description=data["description"],
            author=data["author"],
            repository=data.get("repository"),
            homepage=data.get("homepage"),
            license=data.get("license"),
            tags=data.get("tags", []),
            dependencies=data.get("dependencies", []),
        )


@dataclass
class Plugin:
    """Represents an installed plugin.

    Attributes:
        metadata: Plugin metadata
        path: Path to the plugin directory
        commands: List of command file paths
        agents: List of agent file paths (*.agent.md)
        hooks: List of hook script paths
        skills: List of skill file paths (*.skill.md)
    """

    metadata: PluginMetadata
    path: Path
    commands: list[Path] = field(default_factory=list)
    agents: list[Path] = field(default_factory=list)
    hooks: list[Path] = field(default_factory=list)
    skills: list[Path] = field(default_factory=list)

    @classmethod
    def from_path(cls, plugin_path: Path) -> "Plugin":
        """Load a plugin from its installation directory.

        Plugin structure: plugin.json can be in root, .github/plugin/, or .claude-plugin/.
        Primitives (agents, skills, etc.) are always at the repository root.

        Args:
            plugin_path: Path to the plugin directory

        Returns:
            Plugin: The loaded plugin instance

        Raises:
            FileNotFoundError: If plugin.json is not found
            ValueError: If plugin.json is invalid
        """
        # Find plugin.json using centralized helper
        from ..utils.helpers import find_plugin_json

        metadata_file = find_plugin_json(plugin_path)

        if metadata_file is None:
            raise FileNotFoundError(
                f"Plugin metadata not found in any expected location: {plugin_path}"
            )

        with open(metadata_file) as f:
            metadata_dict = json.load(f)

        metadata = PluginMetadata.from_dict(metadata_dict)

        # Primitives are always at the repository root
        base_dir = plugin_path

        # Discover plugin components in plugins/ subdirectory (including subdirectories)
        commands = (
            list((base_dir / "commands").rglob("*.py")) if (base_dir / "commands").exists() else []
        )
        # Agents: include both .agent.md and plain .md (plugins may omit the
        # .agent.md convention).
        agents = []
        if (base_dir / "agents").exists():
            agents = [f for f in (base_dir / "agents").rglob("*.md")]
        hooks = list((base_dir / "hooks").rglob("*.py")) if (base_dir / "hooks").exists() else []

        # Skills: each subdirectory in skills/ must contain a SKILL.md
        skills = []
        skills_dir = base_dir / "skills"
        if skills_dir.exists():
            for skill_subdir in skills_dir.iterdir():
                if skill_subdir.is_dir():
                    skill_file = skill_subdir / "SKILL.md"
                    if skill_file.exists():
                        skills.append(skill_file)

        return cls(
            metadata=metadata,
            path=plugin_path,
            commands=commands,
            agents=agents,
            hooks=hooks,
            skills=skills,
        )
