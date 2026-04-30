"""Tests for installing multiple virtual packages from the same repository."""

from pathlib import Path  # noqa: F401

import pytest

from src.apm_cli.deps.apm_resolver import APMDependencyResolver
from src.apm_cli.models.apm_package import DependencyReference


class TestVirtualPackageMultiInstall:
    """Test that multiple virtual packages from same repo can be installed."""

    def test_unique_key_for_regular_package(self):
        """Regular packages use repo_url as unique key."""
        dep_ref = DependencyReference.parse("github/design-guidelines")
        unique_key = dep_ref.get_unique_key()
        assert unique_key == "github/design-guidelines"
        assert not dep_ref.is_virtual

    def test_unique_key_for_virtual_file_package(self):
        """Virtual file packages use repo_url + virtual_path as unique key."""
        dep_ref = DependencyReference.parse("owner/test-repo/prompts/code-review.prompt.md")
        unique_key = dep_ref.get_unique_key()
        assert unique_key == "owner/test-repo/prompts/code-review.prompt.md"
        assert dep_ref.is_virtual
        assert dep_ref.repo_url == "owner/test-repo"
        assert dep_ref.virtual_path == "prompts/code-review.prompt.md"

    def test_unique_key_for_different_files_from_same_repo(self):
        """Two virtual files from same repo have different unique keys."""
        dep_ref1 = DependencyReference.parse("owner/test-repo/prompts/file1.prompt.md")
        dep_ref2 = DependencyReference.parse("owner/test-repo/prompts/file2.prompt.md")

        key1 = dep_ref1.get_unique_key()
        key2 = dep_ref2.get_unique_key()

        # Keys should be different
        assert key1 != key2

        # But both share the same repo_url
        assert dep_ref1.repo_url == dep_ref2.repo_url == "owner/test-repo"

        # And both are virtual
        assert dep_ref1.is_virtual and dep_ref2.is_virtual

    def test_dependency_graph_resolution_with_multiple_virtual_packages(self, tmp_path):
        """Test dependency resolution includes all virtual packages from same repo."""
        # Create apm.yml with multiple virtual packages from same repo
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("""
name: test-multi-virtual
version: 1.0.0
dependencies:
  apm:
    - owner/test-repo/prompts/file1.prompt.md
    - owner/test-repo/prompts/file2.prompt.md
    - owner/test-repo/prompts/file3.prompt.md
""")

        # Resolve dependencies
        resolver = APMDependencyResolver()
        graph = resolver.resolve_dependencies(tmp_path)

        # Get flattened dependencies
        flat_deps = graph.flattened_dependencies
        deps_list = flat_deps.get_installation_list()

        # Should have 3 dependencies (all 3 virtual packages)
        assert len(deps_list) == 3

        # All should be virtual packages from the same repo
        for dep in deps_list:
            assert dep.is_virtual
            assert dep.repo_url == "owner/test-repo"

        # Each should have a different virtual_path
        virtual_paths = {dep.virtual_path for dep in deps_list}
        assert len(virtual_paths) == 3
        assert "prompts/file1.prompt.md" in virtual_paths
        assert "prompts/file2.prompt.md" in virtual_paths
        assert "prompts/file3.prompt.md" in virtual_paths

    def test_dependency_graph_with_mix_of_regular_and_virtual_packages(self, tmp_path):
        """Test resolution with both regular packages and virtual packages."""
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("""
name: test-mixed
version: 1.0.0
dependencies:
  apm:
    - github/design-guidelines
    - owner/test-repo/prompts/file1.prompt.md
    - owner/test-repo/prompts/file2.prompt.md
    - github/compliance-rules
""")

        resolver = APMDependencyResolver()
        graph = resolver.resolve_dependencies(tmp_path)
        flat_deps = graph.flattened_dependencies
        deps_list = flat_deps.get_installation_list()

        # Should have 4 dependencies
        assert len(deps_list) == 4

        # Count virtual vs regular
        virtual_count = sum(1 for dep in deps_list if dep.is_virtual)
        regular_count = sum(1 for dep in deps_list if not dep.is_virtual)

        assert virtual_count == 2
        assert regular_count == 2

        # Verify unique keys are all different
        unique_keys = {dep.get_unique_key() for dep in deps_list}
        assert len(unique_keys) == 4

    def test_dependency_tree_node_unique_ids(self):
        """Test that dependency tree nodes use unique keys as IDs."""
        from src.apm_cli.deps.dependency_graph import DependencyNode
        from src.apm_cli.models.apm_package import APMPackage

        # Create two virtual packages from same repo
        dep_ref1 = DependencyReference.parse("owner/test-repo/prompts/file1.prompt.md")
        dep_ref2 = DependencyReference.parse("owner/test-repo/prompts/file2.prompt.md")

        # Create placeholder packages
        pkg1 = APMPackage(name="file1", version="1.0.0")
        pkg2 = APMPackage(name="file2", version="1.0.0")

        # Create nodes
        node1 = DependencyNode(package=pkg1, dependency_ref=dep_ref1, depth=1)
        node2 = DependencyNode(package=pkg2, dependency_ref=dep_ref2, depth=1)

        # Node IDs should be different even though repo_url is same
        assert node1.get_id() != node2.get_id()
        assert node1.get_id() == "owner/test-repo/prompts/file1.prompt.md"
        assert node2.get_id() == "owner/test-repo/prompts/file2.prompt.md"

    def test_flat_dependency_map_uses_unique_keys(self):
        """Test that FlatDependencyMap properly uses unique keys for storage."""
        from src.apm_cli.deps.dependency_graph import FlatDependencyMap

        # Create multiple virtual packages from same repo
        dep_ref1 = DependencyReference.parse("owner/test-repo/prompts/file1.prompt.md")
        dep_ref2 = DependencyReference.parse("owner/test-repo/prompts/file2.prompt.md")

        # Add to flat map
        flat_map = FlatDependencyMap()
        flat_map.add_dependency(dep_ref1, is_conflict=False)
        flat_map.add_dependency(dep_ref2, is_conflict=False)

        # Both should be in the map
        assert flat_map.total_dependencies() == 2

        # Should be able to retrieve both
        key1 = dep_ref1.get_unique_key()
        key2 = dep_ref2.get_unique_key()

        assert flat_map.get_dependency(key1) == dep_ref1
        assert flat_map.get_dependency(key2) == dep_ref2

        # Installation list should contain both
        install_list = flat_map.get_installation_list()
        assert len(install_list) == 2

    def test_no_false_conflicts_for_virtual_packages(self):
        """Virtual packages from same repo should not be flagged as conflicts."""
        from src.apm_cli.deps.dependency_graph import FlatDependencyMap

        dep_ref1 = DependencyReference.parse("owner/test-repo/prompts/file1.prompt.md")
        dep_ref2 = DependencyReference.parse("owner/test-repo/prompts/file2.prompt.md")

        flat_map = FlatDependencyMap()
        flat_map.add_dependency(dep_ref1, is_conflict=False)
        flat_map.add_dependency(dep_ref2, is_conflict=False)

        # Should have no conflicts
        assert not flat_map.has_conflicts()
        assert len(flat_map.conflicts) == 0

    def test_actual_conflict_detection_still_works(self):
        """Ensure real conflicts (same unique key) are still detected."""
        from src.apm_cli.deps.dependency_graph import FlatDependencyMap

        # Same package, different references (this is a real conflict)
        dep_ref1 = DependencyReference.parse("github/design-guidelines#main")
        dep_ref2 = DependencyReference.parse("github/design-guidelines#v1.0.0")

        flat_map = FlatDependencyMap()
        flat_map.add_dependency(dep_ref1, is_conflict=False)
        flat_map.add_dependency(dep_ref2, is_conflict=True)

        # Should detect conflict
        assert flat_map.has_conflicts()
        assert len(flat_map.conflicts) == 1

        # First one should win
        winner = flat_map.get_dependency(dep_ref1.get_unique_key())
        assert winner.reference == "main"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
