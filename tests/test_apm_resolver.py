"""Comprehensive tests for APM dependency resolver."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from src.apm_cli.deps.apm_resolver import APMDependencyResolver
from src.apm_cli.deps.dependency_graph import (
    DependencyGraph, DependencyTree, DependencyNode, FlatDependencyMap,
    CircularRef, ConflictInfo
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
            package=APMPackage(name="dep1", version="1.0.0"),
            dependency_ref=dep1,
            depth=1
        )
        node2 = DependencyNode(
            package=APMPackage(name="dep2", version="1.0.0"), 
            dependency_ref=dep2,
            depth=1
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
            package=APMPackage(name="package-a", version="1.0.0"),
            dependency_ref=dep_a,
            depth=1
        )
        node_b = DependencyNode(
            package=APMPackage(name="package-b", version="1.0.0"),
            dependency_ref=dep_b,
            depth=2,
            parent=node_a
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
        deps = [
            ("user/dep1", 1),
            ("user/dep2", 1), 
            ("user/dep3", 2)
        ]
        
        for repo, depth in deps:
            dep_ref = DependencyReference.parse(repo)
            node = DependencyNode(
                package=APMPackage(name=repo.split('/')[-1], version="1.0.0"),
                dependency_ref=dep_ref,
                depth=depth
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
            package=APMPackage(name="shared-lib", version="1.0.0"),
            dependency_ref=dep1_ref,
            depth=1
        )
        node2 = DependencyNode(
            package=APMPackage(name="shared-lib", version="2.0.0"),
            dependency_ref=dep2_ref,
            depth=2
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
            root_package=root_package,
            dependency_tree=tree,
            flattened_dependencies=flat_map
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
        
        node = DependencyNode(
            package=package,
            dependency_ref=dep_ref,
            depth=1
        )
        
        assert node.package == package
        assert node.dependency_ref == dep_ref
        assert node.depth == 1
        assert node.get_id() == "user/test"
        assert node.get_display_name() == "user/test"
        assert len(node.children) == 0
        assert node.parent is None
    
    def test_circular_ref_string_representation(self):
        """Test string representation of circular reference."""
        circular_ref = CircularRef(
            cycle_path=["user/a", "user/b", "user/a"],
            detected_at_depth=3
        )
        
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
            package=APMPackage(name="test", version="1.0.0"),
            dependency_ref=dep_ref,
            depth=1
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
            root_package=root_package,
            dependency_tree=tree,
            flattened_dependencies=flat_map
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
            root_package=root_package,
            dependency_tree=tree,
            flattened_dependencies=flat_map
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


class TestSourcePathField(unittest.TestCase):
    """APMPackage.source_path field exists and is independent of package_path."""

    def test_source_path_default_is_none(self):
        pkg = APMPackage(name="x", version="1.0.0")
        assert pkg.source_path is None

    def test_source_path_can_be_set(self):
        pkg = APMPackage(name="x", version="1.0.0", source_path=Path("/some/dir"))
        assert pkg.source_path == Path("/some/dir")


class TestComputeDepSourcePath(unittest.TestCase):
    """``_compute_dep_source_path`` anchors local-relative deps correctly (#857)."""

    def setUp(self):
        self.resolver = APMDependencyResolver()
        self.resolver._project_root = Path("/proj")

    def _local_ref(self, local_path: str):
        return DependencyReference.parse(local_path)

    def test_remote_dep_returns_install_path(self):
        ref = DependencyReference(repo_url="user/repo", is_local=False)
        install = Path("/proj/apm_modules/user/repo")
        result = self.resolver._compute_dep_source_path(ref, install, parent_pkg=None)
        assert result == install.resolve()

    def test_local_absolute_path_returns_resolved(self):
        ref = self._local_ref("/abs/path/pkg")
        install = Path("/proj/apm_modules/_local/pkg")
        result = self.resolver._compute_dep_source_path(ref, install, parent_pkg=None)
        assert result == Path("/abs/path/pkg").resolve()

    def test_local_relative_path_uses_parent_source_path(self):
        # The bug: ``../editorial-pipeline`` declared inside a transitive
        # package must resolve against that package's directory, not the
        # root consumer.
        ref = self._local_ref("../editorial-pipeline")
        install = Path("/proj/apm_modules/_local/editorial-pipeline")
        parent = APMPackage(
            name="handbook-agents",
            version="1.0.0",
            source_path=Path("/proj/packages/handbook-agents"),
        )
        result = self.resolver._compute_dep_source_path(ref, install, parent_pkg=parent)
        assert result == Path("/proj/packages/editorial-pipeline").resolve()

    def test_local_relative_path_falls_back_to_project_root_when_no_parent(self):
        # Direct deps from root: parent_pkg is None, anchor is project_root.
        ref = self._local_ref("./packages/foo")
        install = Path("/proj/apm_modules/_local/foo")
        result = self.resolver._compute_dep_source_path(ref, install, parent_pkg=None)
        assert result == Path("/proj/packages/foo").resolve()

    def test_local_relative_path_falls_back_to_project_root_when_parent_has_no_source_path(self):
        # Backwards compat: a parent package created before source_path
        # was added (still None) shouldn't break resolution -- fall back
        # to the project root rather than crashing.
        ref = self._local_ref("./packages/foo")
        install = Path("/proj/apm_modules/_local/foo")
        legacy_parent = APMPackage(name="legacy", version="1.0.0")
        result = self.resolver._compute_dep_source_path(
            ref, install, parent_pkg=legacy_parent
        )
        assert result == Path("/proj/packages/foo").resolve()


class TestResolverSetsRootSourcePath(unittest.TestCase):
    """``resolve_dependencies`` populates ``source_path`` on the root package."""

    def test_root_package_has_source_path_after_resolve(self):
        with TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            (project_root / "apm.yml").write_text(
                "name: root-pkg\nversion: 1.0.0\n"
            )
            graph = APMDependencyResolver().resolve_dependencies(project_root)
            assert graph.root_package.source_path == project_root.resolve()


class TestDownloadCallbackReceivesParent(unittest.TestCase):
    """The download callback is invoked with ``parent_pkg`` for transitive deps."""

    def test_callback_called_with_parent_package(self):
        with TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            apm_modules = project_root / "apm_modules"
            apm_modules.mkdir()
            (project_root / "apm.yml").write_text(
                "name: root-pkg\nversion: 1.0.0\n"
                "dependencies:\n  apm:\n    - user/dep1\n"
            )

            received_calls = []

            def fake_download(dep_ref, modules_dir, parent_chain="", parent_pkg=None):
                received_calls.append((dep_ref.get_unique_key(), parent_pkg))
                return None  # Simulate download miss; we only care about args

            resolver = APMDependencyResolver(
                apm_modules_dir=apm_modules,
                download_callback=fake_download,
            )
            resolver.resolve_dependencies(project_root)

            # The first (and only) download attempt is for the direct dep.
            # parent_pkg is None for direct deps -- the assert below pins
            # the contract that the callback receives the parameter (and
            # would receive a real parent for any transitive deps).
            assert received_calls, "download_callback was never invoked"
            _, parent_pkg = received_calls[0]
            assert parent_pkg is None  # direct dep from root

    def test_callback_receives_parent_for_transitive_dep(self):
        """Transitive deps invoke the callback with the *declaring* package
        as ``parent_pkg`` so its ``source_path`` can anchor relative local
        paths (#857)."""
        with TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            apm_modules = project_root / "apm_modules"
            apm_modules.mkdir()

            # Root depends on a local sibling that itself declares a
            # transitive remote dep. We satisfy the local dep by hand so
            # its apm.yml is loaded and its sub-deps get resolved (which
            # is when the callback should fire with parent_pkg=mid-pkg).
            mid_install = apm_modules / "_local" / "mid"
            mid_install.mkdir(parents=True)
            (mid_install / "apm.yml").write_text(
                "name: mid-pkg\nversion: 1.0.0\n"
                "dependencies:\n  apm:\n    - user/leaf-dep\n"
            )

            mid_src = project_root / "packages" / "mid"
            mid_src.mkdir(parents=True)
            (mid_src / "apm.yml").write_text(
                "name: mid-pkg\nversion: 1.0.0\n"
                "dependencies:\n  apm:\n    - user/leaf-dep\n"
            )

            (project_root / "apm.yml").write_text(
                "name: root-pkg\nversion: 1.0.0\n"
                "dependencies:\n  apm:\n    - ./packages/mid\n"
            )

            received_calls = []

            def fake_download(dep_ref, apm_modules_dir, parent_chain="", parent_pkg=None):
                received_calls.append(
                    (dep_ref.get_unique_key(), parent_pkg, parent_chain)
                )
                return None

            resolver = APMDependencyResolver(
                apm_modules_dir=apm_modules,
                download_callback=fake_download,
            )
            resolver.resolve_dependencies(project_root)

            # The transitive ``user/leaf-dep`` invocation must carry the
            # mid-pkg APMPackage as parent_pkg.
            leaf_calls = [c for c in received_calls if "leaf-dep" in c[0]]
            assert leaf_calls, (
                f"expected a callback call for the transitive leaf-dep, "
                f"got: {received_calls}"
            )
            _, parent_pkg, _ = leaf_calls[0]
            assert parent_pkg is not None, "transitive dep should have parent_pkg"
            assert parent_pkg.name == "mid-pkg"
            # source_path must be set so future relative resolution would work.
            assert parent_pkg.source_path is not None


class TestLegacyDownloadCallbackCompatibility(unittest.TestCase):
    """Callbacks that predate #857 (no ``parent_pkg`` parameter) still work."""

    def test_legacy_callback_signature_is_called(self):
        with TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            apm_modules = project_root / "apm_modules"
            apm_modules.mkdir()
            (project_root / "apm.yml").write_text(
                "name: root-pkg\nversion: 1.0.0\n"
                "dependencies:\n  apm:\n    - user/dep1\n"
            )

            invocations = []

            # Legacy 3-arg callback -- no ``parent_pkg``. Pre-#857 this is
            # the only signature that exists; if the resolver tried to call
            # with a 4th positional arg it would raise TypeError and the
            # download would be silently skipped.
            def legacy_download(dep_ref, apm_modules_dir, parent_chain=""):
                invocations.append(dep_ref.get_unique_key())
                return None

            resolver = APMDependencyResolver(
                apm_modules_dir=apm_modules,
                download_callback=legacy_download,  # type: ignore[arg-type]
            )
            resolver.resolve_dependencies(project_root)

            assert invocations, (
                "legacy callback should still be invoked; resolver must "
                "detect missing parent_pkg parameter via signature inspection"
            )

    def test_modern_callback_detected_as_parent_pkg_aware(self):
        def modern(dep_ref, apm_modules_dir, parent_chain="", parent_pkg=None):
            return None
        assert APMDependencyResolver._signature_accepts_parent_pkg(modern) is True

    def test_legacy_callback_detected_as_not_parent_pkg_aware(self):
        def legacy(dep_ref, apm_modules_dir, parent_chain=""):
            return None
        assert APMDependencyResolver._signature_accepts_parent_pkg(legacy) is False

    def test_kwargs_callback_detected_as_parent_pkg_aware(self):
        def varargs(dep_ref, apm_modules_dir, parent_chain="", **kwargs):
            return None
        assert APMDependencyResolver._signature_accepts_parent_pkg(varargs) is True


class TestDownloadDedupKey(unittest.TestCase):
    """Per-resolution download dedup key disambiguates identical local_path
    literals declared by different parents (#857)."""

    def test_remote_dep_uses_get_unique_key(self):
        resolver = APMDependencyResolver()
        dep = DependencyReference(repo_url="user/repo")
        assert resolver._download_dedup_key(dep, parent_pkg=None) == dep.get_unique_key()

    def test_local_dep_keys_include_resolved_path(self):
        resolver = APMDependencyResolver()
        dep = DependencyReference(
            repo_url="../common", is_local=True, local_path="../common"
        )
        parent_a = APMPackage(
            name="a", version="1.0.0", source="local",
            source_path=Path("/proj/packages/team-x/handbook").resolve(),
        )
        parent_b = APMPackage(
            name="b", version="1.0.0", source="local",
            source_path=Path("/proj/packages/team-y/handbook").resolve(),
        )
        key_a = resolver._download_dedup_key(dep, parent_pkg=parent_a)
        key_b = resolver._download_dedup_key(dep, parent_pkg=parent_b)
        assert key_a != key_b, (
            "same local_path literal under different parents must dedup separately"
        )
        assert "team-x" in key_a
        assert "team-y" in key_b


class TestEffectiveBaseDir(unittest.TestCase):
    """``_effective_base_dir`` centralizes the parent-or-project-root choice
    used by ``_download_dedup_key`` and ``_compute_dep_source_path`` (#940)."""

    def test_uses_parent_source_path_when_set(self):
        resolver = APMDependencyResolver()
        resolver._project_root = Path("/proj")
        parent = APMPackage(
            name="p", version="1.0.0",
            source_path=Path("/proj/packages/handbook"),
        )
        assert resolver._effective_base_dir(parent) == Path("/proj/packages/handbook")

    def test_falls_back_to_project_root_when_parent_is_none(self):
        resolver = APMDependencyResolver()
        resolver._project_root = Path("/proj")
        assert resolver._effective_base_dir(None) == Path("/proj")

    def test_falls_back_to_project_root_when_parent_has_no_source_path(self):
        resolver = APMDependencyResolver()
        resolver._project_root = Path("/proj")
        legacy_parent = APMPackage(name="legacy", version="1.0.0")
        assert resolver._effective_base_dir(legacy_parent) == Path("/proj")

    def test_returns_none_when_neither_parent_nor_project_root(self):
        resolver = APMDependencyResolver()
        # _project_root left as None
        assert resolver._effective_base_dir(None) is None


class TestRejectsLocalPathInRemoteParent(unittest.TestCase):
    """Relative ``local_path`` declared inside a remotely-fetched package is
    rejected as a path-traversal vector (#940)."""

    def test_remote_parent_with_relative_local_dep_returns_none(self):
        with TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            apm_modules = project_root / "apm_modules"
            apm_modules.mkdir()
            remote_pkg_dir = apm_modules / "evil-pkg"
            remote_pkg_dir.mkdir()

            resolver = APMDependencyResolver()
            resolver._project_root = project_root
            resolver._apm_modules_dir = apm_modules

            remote_parent = APMPackage(
                name="evil-pkg", version="1.0.0",
                source_path=remote_pkg_dir,
            )
            dep = DependencyReference(
                repo_url="../../etc/passwd",
                is_local=True,
                local_path="../../etc/passwd",
            )
            result = resolver._try_load_dependency_package(
                dep, parent_chain="root > evil-pkg", parent_pkg=remote_parent
            )
            assert result is None

    def test_root_project_relative_local_dep_is_allowed(self):
        # Sanity check: rejection only fires for *remote* parents. A direct
        # local dep declared by the root project (parent_pkg is None) must
        # still be processed normally -- here it just won't find anything
        # on disk and returns None for that reason, not for rejection.
        with TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            apm_modules = project_root / "apm_modules"
            apm_modules.mkdir()
            resolver = APMDependencyResolver()
            resolver._project_root = project_root
            resolver._apm_modules_dir = apm_modules
            dep = DependencyReference(
                repo_url="./packages/foo", is_local=True,
                local_path="./packages/foo",
            )
            # No download_callback set, so it returns None without raising.
            result = resolver._try_load_dependency_package(
                dep, parent_chain="", parent_pkg=None
            )
            assert result is None  # not found, but reached the install_path branch

    def test_remote_parent_with_absolute_local_dep_not_rejected_by_this_guard(self):
        # The reject guard only targets *relative* local paths; absolute
        # paths bypass the relative-anchor ambiguity (the path-traversal
        # vector this guard addresses) and are handled by the existing
        # install-path lookup (will return None if not on disk).
        with TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            apm_modules = project_root / "apm_modules"
            apm_modules.mkdir()
            remote_pkg_dir = apm_modules / "remote-pkg"
            remote_pkg_dir.mkdir()
            resolver = APMDependencyResolver()
            resolver._project_root = project_root
            resolver._apm_modules_dir = apm_modules
            remote_parent = APMPackage(
                name="remote-pkg", version="1.0.0", source_path=remote_pkg_dir,
            )
            dep = DependencyReference(
                repo_url="/abs/missing", is_local=True, local_path="/abs/missing",
            )
            # Should reach the install_path lookup and return None for
            # "not found" rather than for rejection.
            result = resolver._try_load_dependency_package(
                dep, parent_chain="root > remote-pkg", parent_pkg=remote_parent,
            )
            assert result is None


class TestSilentDownloadFailureLogsWarning(unittest.TestCase):
    """Failed transitive downloads no longer fail silently -- the underlying
    error is surfaced via the stdlib logger so ``--verbose`` makes the skip
    diagnosable (#940)."""

    def test_download_callback_exception_logs_warning(self):
        with TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            apm_modules = project_root / "apm_modules"
            apm_modules.mkdir()

            def boom(*args, **kwargs):
                raise RuntimeError("simulated network failure")

            resolver = APMDependencyResolver(download_callback=boom)
            resolver._project_root = project_root
            resolver._apm_modules_dir = apm_modules

            dep = DependencyReference(repo_url="user/repo")

            from src.apm_cli.deps import apm_resolver as _resolver_mod
            with self.assertLogs(
                _resolver_mod._logger.name, level="WARNING"
            ) as captured:
                result = resolver._try_load_dependency_package(
                    dep, parent_chain="root", parent_pkg=None
                )
            assert result is None
            joined = "\n".join(captured.output)
            assert "simulated network failure" in joined
            assert "user/repo" in joined or "user_repo" in joined or "repo" in joined


if __name__ == '__main__':
    unittest.main()