"""Comprehensive tests for APM dependency resolver."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch  # noqa: F401

from src.apm_cli.deps.apm_resolver import APMDependencyResolver
from src.apm_cli.deps.dependency_graph import (
    CircularRef,
    ConflictInfo,  # noqa: F401
    DependencyGraph,
    DependencyNode,
    DependencyTree,
    FlatDependencyMap,
)
from src.apm_cli.models.apm_package import APMPackage, DependencyReference


class TestAPMDependencyResolver(unittest.TestCase):
    """Test suite for APMDependencyResolver."""

    def setUp(self):
        """Set up test fixtures."""
        self.resolver = APMDependencyResolver()

    def test_resolver_initialization(self):
        """Test resolver initialization with default and custom parameters."""
        # Default initialization
        resolver = APMDependencyResolver()
        assert resolver.max_depth == 50
        # Custom initialization
        custom_resolver = APMDependencyResolver(max_depth=10)
        assert custom_resolver.max_depth == 10

    def test_resolve_dependencies_no_apm_yml(self):
        """Test resolving dependencies when no apm.yml exists."""
        with TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)

            result = self.resolver.resolve_dependencies(project_root)

            assert isinstance(result, DependencyGraph)
            assert result.root_package.name == "unknown"
            assert result.root_package.version == "0.0.0"
            assert result.flattened_dependencies.total_dependencies() == 0
            assert not result.has_circular_dependencies()
            assert not result.has_conflicts()

    def test_resolve_dependencies_invalid_apm_yml(self):
        """Test resolving dependencies with invalid apm.yml."""
        with TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            apm_yml = project_root / "apm.yml"

            # Create invalid YAML
            apm_yml.write_text("invalid: yaml: content: [")

            result = self.resolver.resolve_dependencies(project_root)

            assert isinstance(result, DependencyGraph)
            assert result.root_package.name == "error"
            assert result.has_errors()
            assert "Failed to load root apm.yml" in result.resolution_errors[0]

    def test_resolve_dependencies_valid_apm_yml_no_deps(self):
        """Test resolving dependencies with valid apm.yml but no dependencies."""
        with TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            apm_yml = project_root / "apm.yml"

            apm_yml.write_text("""
name: test-package
version: 1.0.0
description: A test package
""")

            result = self.resolver.resolve_dependencies(project_root)

            assert isinstance(result, DependencyGraph)
            assert result.root_package.name == "test-package"
            assert result.root_package.version == "1.0.0"
            assert result.flattened_dependencies.total_dependencies() == 0
            assert not result.has_circular_dependencies()
            assert not result.has_conflicts()
            assert result.is_valid()

    def test_resolve_dependencies_with_apm_deps(self):
        """Test resolving dependencies with APM dependencies."""
        with TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            apm_yml = project_root / "apm.yml"

            apm_yml.write_text("""
name: test-package
version: 1.0.0
dependencies:
  apm:
    - user/repo1
    - user/repo2#v1.0.0
""")

            result = self.resolver.resolve_dependencies(project_root)

            assert isinstance(result, DependencyGraph)
            assert result.root_package.name == "test-package"
            assert result.flattened_dependencies.total_dependencies() == 2
            assert "user/repo1" in result.flattened_dependencies.dependencies
            assert "user/repo2" in result.flattened_dependencies.dependencies

    def test_build_dependency_tree_empty_root(self):
        """Test building dependency tree with empty root package."""
        with TemporaryDirectory() as temp_dir:
            apm_yml = Path(temp_dir) / "apm.yml"
            apm_yml.write_text("""
name: empty-package
version: 1.0.0
""")

            tree = self.resolver.build_dependency_tree(apm_yml)

            assert isinstance(tree, DependencyTree)
            assert tree.root_package.name == "empty-package"
            assert len(tree.nodes) == 0
            assert tree.max_depth == 0

    def test_build_dependency_tree_with_dependencies(self):
        """Test building dependency tree with dependencies."""
        with TemporaryDirectory() as temp_dir:
            apm_yml = Path(temp_dir) / "apm.yml"
            apm_yml.write_text("""
name: parent-package
version: 1.0.0
dependencies:
  apm:
    - user/dependency1
    - user/dependency2#v1.2.0
""")

            tree = self.resolver.build_dependency_tree(apm_yml)

            assert isinstance(tree, DependencyTree)
            assert tree.root_package.name == "parent-package"
            assert len(tree.nodes) == 2
            assert tree.max_depth == 1

            # Check that dependencies were added
            assert tree.has_dependency("user/dependency1")
            assert tree.has_dependency("user/dependency2")

            # Check depth of dependencies
            nodes_at_depth_1 = tree.get_nodes_at_depth(1)
            assert len(nodes_at_depth_1) == 2

    def test_build_dependency_tree_invalid_apm_yml(self):
        """Test building dependency tree with invalid apm.yml."""
        with TemporaryDirectory() as temp_dir:
            apm_yml = Path(temp_dir) / "apm.yml"
            apm_yml.write_text("invalid yaml content [")

            tree = self.resolver.build_dependency_tree(apm_yml)

            assert isinstance(tree, DependencyTree)
            assert tree.root_package.name == "error"
            assert len(tree.nodes) == 0

    def test_detect_circular_dependencies_no_cycles(self):
        """Test circular dependency detection with no cycles."""
        # Create a simple tree without cycles
        root_package = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root_package)

        # Add some nodes without cycles
        dep1 = DependencyReference.parse("user/dep1")
        dep2 = DependencyReference.parse("user/dep2")

        node1 = DependencyNode(
            package=APMPackage(name="dep1", version="1.0.0"), dependency_ref=dep1, depth=1
        )
        node2 = DependencyNode(
            package=APMPackage(name="dep2", version="1.0.0"), dependency_ref=dep2, depth=1
        )

        tree.add_node(node1)
        tree.add_node(node2)

        circular_deps = self.resolver.detect_circular_dependencies(tree)
        assert len(circular_deps) == 0

    def test_detect_circular_dependencies_with_cycle(self):
        """Test circular dependency detection with actual cycle."""
        root_package = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root_package)

        # Create a circular dependency: A -> B -> A
        dep_a = DependencyReference.parse("user/package-a")
        dep_b = DependencyReference.parse("user/package-b")

        node_a = DependencyNode(
            package=APMPackage(name="package-a", version="1.0.0"), dependency_ref=dep_a, depth=1
        )
        node_b = DependencyNode(
            package=APMPackage(name="package-b", version="1.0.0"),
            dependency_ref=dep_b,
            depth=2,
            parent=node_a,
        )

        # Create the cycle by making B depend back on A (existing node)
        # This creates: A -> B -> A (back to the original A)
        node_a.children = [node_b]
        node_b.children = [node_a]  # This creates the cycle

        tree.add_node(node_a)
        tree.add_node(node_b)

        circular_deps = self.resolver.detect_circular_dependencies(tree)
        assert len(circular_deps) == 1
        assert isinstance(circular_deps[0], CircularRef)

    def test_flatten_dependencies_no_conflicts(self):
        """Test flattening dependencies without conflicts."""
        root_package = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root_package)

        # Add unique dependencies at different levels
        deps = [("user/dep1", 1), ("user/dep2", 1), ("user/dep3", 2)]

        for repo, depth in deps:
            dep_ref = DependencyReference.parse(repo)
            node = DependencyNode(
                package=APMPackage(name=repo.split("/")[-1], version="1.0.0"),
                dependency_ref=dep_ref,
                depth=depth,
            )
            tree.add_node(node)

        flattened = self.resolver.flatten_dependencies(tree)

        assert isinstance(flattened, FlatDependencyMap)
        assert flattened.total_dependencies() == 3
        assert not flattened.has_conflicts()
        assert len(flattened.install_order) == 3

    def test_flatten_dependencies_with_conflicts(self):
        """Test flattening dependencies with conflicts."""
        root_package = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root_package)

        # Add conflicting dependencies (same repo, different refs)
        dep1_ref = DependencyReference.parse("user/shared-lib#v1.0.0")
        dep2_ref = DependencyReference.parse("user/shared-lib#v2.0.0")

        node1 = DependencyNode(
            package=APMPackage(name="shared-lib", version="1.0.0"), dependency_ref=dep1_ref, depth=1
        )
        node2 = DependencyNode(
            package=APMPackage(name="shared-lib", version="2.0.0"), dependency_ref=dep2_ref, depth=2
        )

        tree.add_node(node1)
        tree.add_node(node2)

        flattened = self.resolver.flatten_dependencies(tree)

        assert flattened.total_dependencies() == 1  # Only one version should win
        assert flattened.has_conflicts()
        assert len(flattened.conflicts) == 1

        conflict = flattened.conflicts[0]
        assert conflict.repo_url == "user/shared-lib"
        assert conflict.winner.reference == "v1.0.0"  # First wins
        assert len(conflict.conflicts) == 1
        assert conflict.conflicts[0].reference == "v2.0.0"

    def test_validate_dependency_reference_valid(self):
        """Test dependency reference validation with valid references."""
        valid_refs = [
            DependencyReference.parse("user/repo"),
            DependencyReference.parse("user/repo#main"),
            DependencyReference.parse("user/repo#v1.0.0"),
        ]

        for ref in valid_refs:
            assert self.resolver._validate_dependency_reference(ref)

    def test_validate_dependency_reference_invalid(self):
        """Test dependency reference validation with invalid references."""
        # Test empty repo URL
        invalid_ref = DependencyReference(repo_url="", reference="main")
        assert not self.resolver._validate_dependency_reference(invalid_ref)

        # Test repo URL without slash
        invalid_ref2 = DependencyReference(repo_url="invalidrepo", reference="main")
        assert not self.resolver._validate_dependency_reference(invalid_ref2)

    def test_create_resolution_summary(self):
        """Test creation of resolution summary."""
        # Create a mock dependency graph
        root_package = APMPackage(name="test-package", version="1.0.0")
        tree = DependencyTree(root_package=root_package)
        flat_map = FlatDependencyMap()

        # Add some dependencies to flat map
        dep1 = DependencyReference.parse("user/dep1")
        flat_map.add_dependency(dep1)

        graph = DependencyGraph(
            root_package=root_package, dependency_tree=tree, flattened_dependencies=flat_map
        )

        summary = self.resolver._create_resolution_summary(graph)

        assert "test-package" in summary
        assert "Total dependencies: 1" in summary
        assert "[+] Valid" in summary

    def test_max_depth_limit(self):
        """Test that maximum depth limit is respected."""
        resolver = APMDependencyResolver(max_depth=2)

        with TemporaryDirectory() as temp_dir:
            apm_yml = Path(temp_dir) / "apm.yml"
            apm_yml.write_text("""
name: deep-package
version: 1.0.0
dependencies:
  apm:
    - user/level1
""")

            tree = resolver.build_dependency_tree(apm_yml)

            # Even if there were deeper dependencies, max depth should limit tree
            assert tree.max_depth <= 2


class TestDependencyGraphDataStructures(unittest.TestCase):
    """Test suite for dependency graph data structures."""

    def test_dependency_node_creation(self):
        """Test creating a dependency node."""
        package = APMPackage(name="test", version="1.0.0")
        dep_ref = DependencyReference.parse("user/test")

        node = DependencyNode(package=package, dependency_ref=dep_ref, depth=1)

        assert node.package == package
        assert node.dependency_ref == dep_ref
        assert node.depth == 1
        assert node.get_id() == "user/test"
        assert node.get_display_name() == "user/test"
        assert len(node.children) == 0
        assert node.parent is None

    def test_circular_ref_string_representation(self):
        """Test string representation of circular reference."""
        circular_ref = CircularRef(cycle_path=["user/a", "user/b", "user/a"], detected_at_depth=3)

        str_repr = str(circular_ref)
        assert "Circular dependency detected" in str_repr
        assert "user/a -> user/b -> user/a" in str_repr

    def test_dependency_tree_operations(self):
        """Test dependency tree operations."""
        root_package = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root_package)

        # Add a node
        dep_ref = DependencyReference.parse("user/test")
        node = DependencyNode(
            package=APMPackage(name="test", version="1.0.0"), dependency_ref=dep_ref, depth=1
        )
        tree.add_node(node)

        assert tree.has_dependency("user/test")
        assert tree.get_node("user/test") == node
        assert tree.max_depth == 1

        nodes_at_depth_1 = tree.get_nodes_at_depth(1)
        assert len(nodes_at_depth_1) == 1
        assert nodes_at_depth_1[0] == node

    def test_flat_dependency_map_operations(self):
        """Test flat dependency map operations."""
        flat_map = FlatDependencyMap()

        # Add dependencies
        dep1 = DependencyReference.parse("user/dep1")
        dep2 = DependencyReference.parse("user/dep2")

        flat_map.add_dependency(dep1)
        flat_map.add_dependency(dep2)

        assert flat_map.total_dependencies() == 2
        assert flat_map.get_dependency("user/dep1") == dep1
        assert flat_map.get_dependency("user/dep2") == dep2
        assert not flat_map.has_conflicts()
        assert "user/dep1" in flat_map.install_order
        assert "user/dep2" in flat_map.install_order

    def test_flat_dependency_map_conflicts(self):
        """Test conflict detection in flat dependency map."""
        flat_map = FlatDependencyMap()

        # Add conflicting dependencies
        dep1 = DependencyReference.parse("user/shared#v1.0.0")
        dep2 = DependencyReference.parse("user/shared#v2.0.0")

        flat_map.add_dependency(dep1)
        flat_map.add_dependency(dep2, is_conflict=True)

        assert flat_map.total_dependencies() == 1
        assert flat_map.has_conflicts()
        assert len(flat_map.conflicts) == 1

        conflict = flat_map.conflicts[0]
        assert conflict.repo_url == "user/shared"
        assert conflict.winner == dep1
        assert dep2 in conflict.conflicts

    def test_dependency_graph_summary(self):
        """Test dependency graph summary generation."""
        root_package = APMPackage(name="test", version="1.0.0")
        tree = DependencyTree(root_package=root_package)
        tree.max_depth = 2

        flat_map = FlatDependencyMap()
        dep1 = DependencyReference.parse("user/dep1")
        flat_map.add_dependency(dep1)

        graph = DependencyGraph(
            root_package=root_package, dependency_tree=tree, flattened_dependencies=flat_map
        )

        summary = graph.get_summary()

        assert summary["root_package"] == "test"
        assert summary["total_dependencies"] == 1
        assert summary["max_depth"] == 2
        assert not summary["has_circular_dependencies"]
        assert not summary["has_conflicts"]
        assert not summary["has_errors"]
        assert summary["is_valid"]

    def test_dependency_graph_error_handling(self):
        """Test dependency graph error handling."""
        root_package = APMPackage(name="test", version="1.0.0")
        tree = DependencyTree(root_package=root_package)
        flat_map = FlatDependencyMap()

        graph = DependencyGraph(
            root_package=root_package, dependency_tree=tree, flattened_dependencies=flat_map
        )

        # Add errors and circular dependencies
        graph.add_error("Test error")
        circular_ref = CircularRef(cycle_path=["a", "b", "a"], detected_at_depth=2)
        graph.add_circular_dependency(circular_ref)

        assert graph.has_errors()
        assert graph.has_circular_dependencies()
        assert not graph.is_valid()

        summary = graph.get_summary()
        assert summary["error_count"] == 1
        assert summary["circular_count"] == 1
        assert not summary["is_valid"]


if __name__ == "__main__":
    unittest.main()
