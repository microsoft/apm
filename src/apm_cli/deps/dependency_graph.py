"""Data structures for dependency graph representation and resolution."""

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path  # noqa: F401
from typing import Any, Dict, List, Optional, Set, Tuple  # noqa: F401, UP035

from ..models.apm_package import APMPackage, DependencyReference


@dataclass
class DependencyNode:
    """Represents a single dependency node in the dependency graph."""

    package: APMPackage
    dependency_ref: DependencyReference
    depth: int = 0
    children: list["DependencyNode"] = field(default_factory=list)
    parent: Optional["DependencyNode"] = None
    is_dev: bool = False  # True when reached exclusively through devDependencies

    def get_id(self) -> str:
        """Get unique identifier for this node."""
        unique_key = self.dependency_ref.get_unique_key()
        # Include reference to distinguish between different versions/branches of same repo
        if self.dependency_ref.reference:
            return f"{unique_key}#{self.dependency_ref.reference}"
        return unique_key

    def get_display_name(self) -> str:
        """Get display name for this dependency."""
        return self.dependency_ref.get_display_name()

    def get_ancestor_chain(self) -> str:
        """Build a human-readable breadcrumb from this node's ancestry.

        Walks up ``parent`` links to produce e.g. ``"root-pkg > mid-pkg > this-pkg"``
        so error messages can show which dependency path led here.
        Returns just the node's display name for root-level (depth-0/1) deps.
        """
        parts: list[str] = []
        current: DependencyNode | None = self
        while current is not None:
            parts.append(current.get_display_name())
            current = current.parent
        parts.reverse()
        return " > ".join(parts)


@dataclass
class CircularRef:
    """Represents a circular dependency reference."""

    cycle_path: list[str]  # List of repo URLs forming the cycle
    detected_at_depth: int

    def _format_complete_cycle(self) -> str:
        """
        Return a string representation of the cycle, ensuring it is visually complete.
        If the cycle path does not end at the starting node, append the start to the end.
        """
        if not self.cycle_path:
            return "(empty path)"
        cycle_display = " -> ".join(self.cycle_path)
        # Ensure the cycle visually returns to the start node
        if len(self.cycle_path) > 1 and self.cycle_path[0] != self.cycle_path[-1]:
            cycle_display += f" -> {self.cycle_path[0]}"
        return cycle_display

    def __str__(self) -> str:
        """String representation of the circular dependency."""
        return f"Circular dependency detected: {self._format_complete_cycle()}"


@dataclass
class DependencyTree:
    """Hierarchical representation of dependencies before flattening."""

    root_package: APMPackage
    nodes: dict[str, DependencyNode] = field(default_factory=dict)
    _nodes_by_depth: dict[int, list[DependencyNode]] = field(
        default_factory=lambda: defaultdict(list)
    )
    max_depth: int = 0

    def add_node(self, node: DependencyNode) -> None:
        """Add a node to the tree."""
        node_id = node.get_id()
        is_new = node_id not in self.nodes
        self.nodes[node_id] = node
        if is_new:
            self._nodes_by_depth[node.depth].append(node)
        self.max_depth = max(self.max_depth, node.depth)

    def get_node(self, unique_key: str) -> DependencyNode | None:
        """Get a node by its unique key."""
        return self.nodes.get(unique_key)

    def get_nodes_at_depth(self, depth: int) -> list[DependencyNode]:
        """Get all nodes at a specific depth level."""
        return list(self._nodes_by_depth.get(depth, []))

    def has_dependency(self, repo_url: str) -> bool:
        """Check if a dependency exists in the tree."""
        # Check by repo URL, not by full node ID (which may include reference)
        return any(node.dependency_ref.repo_url == repo_url for node in self.nodes.values())


@dataclass
class ConflictInfo:
    """Information about a dependency conflict."""

    repo_url: str
    winner: DependencyReference  # The dependency that "wins"
    conflicts: list[DependencyReference]  # All conflicting dependencies
    reason: str  # Explanation of why winner was chosen

    def __str__(self) -> str:
        """String representation of the conflict."""
        conflict_refs = [str(ref) for ref in self.conflicts]
        return f"Conflict for {self.repo_url}: {self.winner!s} wins over {', '.join(conflict_refs)} ({self.reason})"


@dataclass
class FlatDependencyMap:
    """Final flattened dependency mapping ready for installation."""

    dependencies: dict[str, DependencyReference] = field(default_factory=dict)
    conflicts: list[ConflictInfo] = field(default_factory=list)
    install_order: list[str] = field(default_factory=list)  # Order for installation

    def add_dependency(self, dep_ref: DependencyReference, is_conflict: bool = False) -> None:
        """Add a dependency to the flat map."""
        unique_key = dep_ref.get_unique_key()

        # If this is the first occurrence, just add it
        if unique_key not in self.dependencies:
            self.dependencies[unique_key] = dep_ref
            self.install_order.append(unique_key)
        elif is_conflict:
            # Record the conflict but keep the first one (first wins strategy)
            existing_ref = self.dependencies[unique_key]
            conflict = ConflictInfo(
                repo_url=dep_ref.repo_url,
                winner=existing_ref,
                conflicts=[dep_ref],
                reason="first declared dependency wins",
            )

            # Check if we already have a conflict for this repo
            existing_conflict = next(
                (c for c in self.conflicts if c.repo_url == dep_ref.repo_url), None
            )
            if existing_conflict:
                existing_conflict.conflicts.append(dep_ref)
            else:
                self.conflicts.append(conflict)

    def get_dependency(self, unique_key: str) -> DependencyReference | None:
        """Get a dependency by unique key (repo_url or repo_url/virtual_path)."""
        return self.dependencies.get(unique_key)

    def has_conflicts(self) -> bool:
        """Check if there are any conflicts in the flattened map."""
        return bool(self.conflicts)

    def total_dependencies(self) -> int:
        """Get total number of unique dependencies."""
        return len(self.dependencies)

    def get_installation_list(self) -> list[DependencyReference]:
        """Get dependencies in installation order."""
        return [
            self.dependencies[unique_key]
            for unique_key in self.install_order
            if unique_key in self.dependencies
        ]


@dataclass
class DependencyGraph:
    """Complete resolved dependency information."""

    root_package: APMPackage
    dependency_tree: DependencyTree
    flattened_dependencies: FlatDependencyMap
    circular_dependencies: list[CircularRef] = field(default_factory=list)
    resolution_errors: list[str] = field(default_factory=list)

    def has_circular_dependencies(self) -> bool:
        """Check if there are any circular dependencies."""
        return bool(self.circular_dependencies)

    def has_conflicts(self) -> bool:
        """Check if there are any dependency conflicts."""
        return self.flattened_dependencies.has_conflicts()

    def has_errors(self) -> bool:
        """Check if there are any resolution errors."""
        return bool(self.resolution_errors)

    def is_valid(self) -> bool:
        """Check if the dependency graph is valid (no circular deps or errors)."""
        return not self.has_circular_dependencies() and not self.has_errors()

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of the dependency resolution."""
        return {
            "root_package": self.root_package.name,
            "total_dependencies": self.flattened_dependencies.total_dependencies(),
            "max_depth": self.dependency_tree.max_depth,
            "has_circular_dependencies": self.has_circular_dependencies(),
            "circular_count": len(self.circular_dependencies),
            "has_conflicts": self.has_conflicts(),
            "conflict_count": len(self.flattened_dependencies.conflicts),
            "has_errors": self.has_errors(),
            "error_count": len(self.resolution_errors),
            "is_valid": self.is_valid(),
        }

    def add_error(self, error: str) -> None:
        """Add a resolution error."""
        self.resolution_errors.append(error)

    def add_circular_dependency(self, circular_ref: CircularRef) -> None:
        """Add a circular dependency detection."""
        self.circular_dependencies.append(circular_ref)
