"""Comprehensive tests for enhanced primitive discovery with dependencies."""

import os

# Test imports - using absolute imports since we may not have proper package setup
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

# Add src to path for testing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from apm_cli.models.apm_package import APMPackage, DependencyReference  # noqa: F401
    from apm_cli.primitives.discovery import (
        discover_primitives_with_dependencies,
        get_dependency_declaration_order,
        scan_dependency_primitives,
        scan_directory_with_source,
        scan_local_primitives,
    )
    from apm_cli.primitives.models import (
        Chatmode,
        Context,
        Instruction,
        PrimitiveCollection,
        PrimitiveConflict,  # noqa: F401
    )
except ImportError as e:
    print(f"Import error: {e}")
    print("Skipping enhanced discovery tests due to missing dependencies")
    sys.exit(0)


class TestEnhancedPrimitiveDiscovery(unittest.TestCase):
    """Test cases for enhanced primitive discovery with dependency support."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_path = Path(self.temp_dir.name)

        # Create basic directory structure
        self._create_directory_structure()

    def tearDown(self):
        """Tear down test fixtures."""
        self.temp_dir.cleanup()

    def _create_directory_structure(self):
        """Create basic test directory structure."""
        # Local .apm directory
        (self.temp_dir_path / ".apm" / "chatmodes").mkdir(parents=True, exist_ok=True)
        (self.temp_dir_path / ".apm" / "instructions").mkdir(parents=True, exist_ok=True)
        (self.temp_dir_path / ".apm" / "context").mkdir(parents=True, exist_ok=True)

        # Dependency directories
        (self.temp_dir_path / "apm_modules" / "dep1" / ".apm" / "chatmodes").mkdir(
            parents=True, exist_ok=True
        )
        (self.temp_dir_path / "apm_modules" / "dep1" / ".apm" / "instructions").mkdir(
            parents=True, exist_ok=True
        )
        (self.temp_dir_path / "apm_modules" / "dep2" / ".apm" / "chatmodes").mkdir(
            parents=True, exist_ok=True
        )
        (self.temp_dir_path / "apm_modules" / "dep2" / ".apm" / "context").mkdir(
            parents=True, exist_ok=True
        )

    def _create_apm_yml(self, dependencies=None):
        """Create an apm.yml file with specified dependencies."""
        apm_content = {"name": "test-project", "version": "1.0.0", "description": "Test project"}

        if dependencies:
            apm_content["dependencies"] = dependencies

        with open(self.temp_dir_path / "apm.yml", "w") as f:
            yaml.dump(apm_content, f)

    def _create_primitive_file(
        self,
        file_path: Path,
        primitive_type: str,
        name: str,
        content: str = None,  # noqa: RUF013
    ):
        """Create a primitive file with frontmatter."""
        if content is None:
            content = f"# {name.title()} Content\n\nThis is test content for {name}."

        frontmatter = f"""---
description: Test {primitive_type} for {name}
"""

        if primitive_type == "instruction":
            frontmatter += 'applyTo: "**/*.py"\n'

        frontmatter += "---\n\n"

        # Create parent directories if they don't exist
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(frontmatter + content)

    def test_source_tracking_models(self):
        """Test that primitive models correctly track source information."""
        # Test chatmode with source
        chatmode = Chatmode(
            name="test-chatmode",
            file_path=Path("test.chatmode.md"),
            description="Test chatmode",
            apply_to="**/*.py",
            content="# Test content",
            source="local",
        )
        self.assertEqual(chatmode.source, "local")

        # Test instruction with dependency source
        instruction = Instruction(
            name="test-instruction",
            file_path=Path("test.instructions.md"),
            description="Test instruction",
            apply_to="**/*.py",
            content="# Test content",
            source="dependency:package1",
        )
        self.assertEqual(instruction.source, "dependency:package1")

        # Test context with no source (should be None)
        context = Context(
            name="test-context", file_path=Path("test.context.md"), content="# Test content"
        )
        self.assertIsNone(context.source)

    def test_primitive_collection_conflict_detection(self):
        """Test conflict detection in primitive collection."""
        collection = PrimitiveCollection()

        # Add first primitive (local)
        local_chatmode = Chatmode(
            name="assistant",
            file_path=Path("local.chatmode.md"),
            description="Local assistant",
            apply_to="**/*.py",
            content="Local content",
            source="local",
        )
        collection.add_primitive(local_chatmode)

        # Add conflicting primitive (dependency)
        dep_chatmode = Chatmode(
            name="assistant",
            file_path=Path("dep.chatmode.md"),
            description="Dependency assistant",
            apply_to="**/*.py",
            content="Dependency content",
            source="dependency:dep1",
        )
        collection.add_primitive(dep_chatmode)

        # Should keep local primitive and track conflict
        self.assertEqual(len(collection.chatmodes), 1)
        self.assertEqual(collection.chatmodes[0].source, "local")
        self.assertTrue(collection.has_conflicts())
        self.assertEqual(len(collection.conflicts), 1)

        conflict = collection.conflicts[0]
        self.assertEqual(conflict.primitive_name, "assistant")
        self.assertEqual(conflict.primitive_type, "chatmode")
        self.assertEqual(conflict.winning_source, "local")
        self.assertIn("dependency:dep1", conflict.losing_sources)

    def test_dependency_order_from_apm_yml(self):
        """Test parsing dependency declaration order from apm.yml."""
        # Create apm.yml with APM dependencies
        dependencies = {"apm": ["company/standards", "team/workflows#v1.0.0", "user/utilities"]}
        self._create_apm_yml(dependencies)

        # Test dependency order extraction
        order = get_dependency_declaration_order(str(self.temp_dir_path))
        expected = ["company/standards", "team/workflows", "user/utilities"]
        self.assertEqual(order, expected)

    def test_dependency_order_no_apm_yml(self):
        """Test dependency order when no apm.yml exists."""
        order = get_dependency_declaration_order(str(self.temp_dir_path))
        self.assertEqual(order, [])

    def test_scan_local_primitives_only(self):
        """Test scanning only local primitives."""
        # Create local primitives
        self._create_primitive_file(
            self.temp_dir_path / ".apm" / "chatmodes" / "local-assistant.chatmode.md",
            "chatmode",
            "local-assistant",
        )
        self._create_primitive_file(
            self.temp_dir_path / ".apm" / "instructions" / "local-style.instructions.md",
            "instruction",
            "local-style",
        )

        collection = PrimitiveCollection()
        scan_local_primitives(str(self.temp_dir_path), collection)

        # Should find 2 local primitives
        self.assertEqual(collection.count(), 2)
        self.assertEqual(len(collection.chatmodes), 1)
        self.assertEqual(len(collection.instructions), 1)

        # All should have local source
        for primitive in collection.all_primitives():
            self.assertEqual(primitive.source, "local")

    def test_scan_dependency_primitives(self):
        """Test scanning dependency primitives."""
        # Create apm.yml with dependencies
        dependencies = {"apm": ["company/dep1", "team/dep2"]}
        self._create_apm_yml(dependencies)

        # Create dependency primitives with proper org-namespaced structure
        (self.temp_dir_path / "apm_modules" / "company" / "dep1" / ".apm" / "chatmodes").mkdir(
            parents=True, exist_ok=True
        )
        self._create_primitive_file(
            self.temp_dir_path
            / "apm_modules"
            / "company"
            / "dep1"
            / ".apm"
            / "chatmodes"
            / "dep-assistant.chatmode.md",
            "chatmode",
            "dep-assistant",
        )

        (self.temp_dir_path / "apm_modules" / "team" / "dep2" / ".apm" / "context").mkdir(
            parents=True, exist_ok=True
        )
        self._create_primitive_file(
            self.temp_dir_path
            / "apm_modules"
            / "team"
            / "dep2"
            / ".apm"
            / "context"
            / "dep-context.context.md",
            "context",
            "dep-context",
        )

        collection = PrimitiveCollection()
        scan_dependency_primitives(str(self.temp_dir_path), collection)

        # Should find 2 dependency primitives
        self.assertEqual(collection.count(), 2)
        self.assertEqual(len(collection.chatmodes), 1)
        self.assertEqual(len(collection.contexts), 1)

        # Check source tracking
        chatmode = collection.chatmodes[0]
        self.assertTrue("company" in chatmode.source or "dep1" in chatmode.source)

        context = collection.contexts[0]
        self.assertTrue("team" in context.source or "dep2" in context.source)

    def test_full_discovery_with_local_override(self):
        """Test full discovery where local primitives override dependencies."""
        # Create apm.yml with dependencies
        dependencies = {"apm": ["company/dep1", "team/dep2"]}
        self._create_apm_yml(dependencies)

        # Create conflicting primitives - same name in local and dependency
        self._create_primitive_file(
            self.temp_dir_path / ".apm" / "chatmodes" / "assistant.chatmode.md",
            "chatmode",
            "assistant",
            "Local assistant content",
        )

        # Create dependency primitive with proper org-namespaced structure
        (self.temp_dir_path / "apm_modules" / "company" / "dep1" / ".apm" / "chatmodes").mkdir(
            parents=True, exist_ok=True
        )
        self._create_primitive_file(
            self.temp_dir_path
            / "apm_modules"
            / "company"
            / "dep1"
            / ".apm"
            / "chatmodes"
            / "assistant.chatmode.md",
            "chatmode",
            "assistant",
            "Dependency assistant content",
        )

        # Create non-conflicting dependency primitive
        (self.temp_dir_path / "apm_modules" / "team" / "dep2" / ".apm" / "context").mkdir(
            parents=True, exist_ok=True
        )
        self._create_primitive_file(
            self.temp_dir_path
            / "apm_modules"
            / "team"
            / "dep2"
            / ".apm"
            / "context"
            / "project-info.context.md",
            "context",
            "project-info",
        )

        # Run full discovery
        collection = discover_primitives_with_dependencies(str(self.temp_dir_path))

        # Should have 2 primitives total: 1 chatmode (local wins) + 1 context (from dep)
        self.assertEqual(collection.count(), 2)
        self.assertEqual(len(collection.chatmodes), 1)
        self.assertEqual(len(collection.contexts), 1)

        # Chatmode should be from local source
        chatmode = collection.chatmodes[0]
        self.assertEqual(chatmode.name, "assistant")
        self.assertEqual(chatmode.source, "local")
        self.assertIn("Local assistant content", chatmode.content)

        # Context should be from dependency
        context = collection.contexts[0]
        self.assertEqual(context.name, "project-info")
        self.assertEqual(context.source, "dependency:team/dep2")

        # Should have conflict recorded
        self.assertTrue(collection.has_conflicts())
        self.assertEqual(len(collection.conflicts), 1)

        conflict = collection.conflicts[0]
        self.assertEqual(conflict.primitive_name, "assistant")
        self.assertEqual(conflict.winning_source, "local")
        self.assertIn("dependency:company/dep1", conflict.losing_sources)

    def test_dependency_priority_order(self):
        """Test that dependencies are processed in declaration order with first-wins."""
        # Create apm.yml with specific order
        dependencies = {"apm": ["first/dep", "second/dep"]}
        self._create_apm_yml(dependencies)

        # Create conflicting primitives in both dependencies with proper directory names
        # Create first dependency directory structure
        (self.temp_dir_path / "apm_modules" / "first" / "dep" / ".apm" / "instructions").mkdir(
            parents=True, exist_ok=True
        )
        self._create_primitive_file(
            self.temp_dir_path
            / "apm_modules"
            / "first"
            / "dep"
            / ".apm"
            / "instructions"
            / "style.instructions.md",
            "instruction",
            "style",
            "First dependency content",
        )

        # Create second dependency directory structure
        (self.temp_dir_path / "apm_modules" / "second" / "dep" / ".apm" / "instructions").mkdir(
            parents=True, exist_ok=True
        )
        self._create_primitive_file(
            self.temp_dir_path
            / "apm_modules"
            / "second"
            / "dep"
            / ".apm"
            / "instructions"
            / "style.instructions.md",
            "instruction",
            "style",
            "Second dependency content",
        )

        collection = PrimitiveCollection()
        scan_dependency_primitives(str(self.temp_dir_path), collection)

        # Should have 1 instruction (first one encountered)
        self.assertEqual(len(collection.instructions), 1)
        instruction = collection.instructions[0]
        # Source should be the first dependency in declaration order
        self.assertTrue("first" in instruction.source)

    def test_scan_directory_with_source(self):
        """Test scanning a specific directory with source tracking."""
        # Create dependency directory with primitives
        dep_dir = self.temp_dir_path / "test_dep"
        (dep_dir / ".apm" / "chatmodes").mkdir(parents=True, exist_ok=True)
        (dep_dir / ".apm" / "instructions").mkdir(parents=True, exist_ok=True)

        self._create_primitive_file(
            dep_dir / ".apm" / "chatmodes" / "test-chatmode.chatmode.md",
            "chatmode",
            "test-chatmode",
        )
        self._create_primitive_file(
            dep_dir / ".apm" / "instructions" / "test-instruction.instructions.md",
            "instruction",
            "test-instruction",
        )

        collection = PrimitiveCollection()
        scan_directory_with_source(dep_dir, collection, "dependency:test")

        # Should find 2 primitives
        self.assertEqual(collection.count(), 2)

        # Both should have the specified source
        for primitive in collection.all_primitives():
            self.assertEqual(primitive.source, "dependency:test")

    def test_no_apm_modules_directory(self):
        """Test discovery when apm_modules directory doesn't exist."""
        # Create local primitive only
        self._create_primitive_file(
            self.temp_dir_path / ".apm" / "chatmodes" / "local.chatmode.md", "chatmode", "local"
        )

        # Don't create apm_modules directory
        collection = discover_primitives_with_dependencies(str(self.temp_dir_path))

        # Should find only local primitive
        self.assertEqual(collection.count(), 1)
        self.assertEqual(len(collection.chatmodes), 1)
        self.assertEqual(collection.chatmodes[0].source, "local")
        self.assertFalse(collection.has_conflicts())

    def test_empty_dependency_directory(self):
        """Test handling of dependency directory without .apm subdirectory."""
        # Create apm.yml with dependencies
        dependencies = {"apm": ["company/empty-dep"]}
        self._create_apm_yml(dependencies)

        # Create dependency directory but no .apm subdirectory
        (self.temp_dir_path / "apm_modules" / "empty-dep").mkdir(parents=True, exist_ok=True)

        collection = discover_primitives_with_dependencies(str(self.temp_dir_path))

        # Should find no primitives
        self.assertEqual(collection.count(), 0)
        self.assertFalse(collection.has_conflicts())

    def test_collection_methods(self):
        """Test additional PrimitiveCollection methods."""
        collection = PrimitiveCollection()

        # Add primitives from different sources
        local_chatmode = Chatmode(
            name="local",
            file_path=Path("local.md"),
            description="Local",
            apply_to=None,
            content="Local content",
            source="local",
        )
        dep_instruction = Instruction(
            name="dep",
            file_path=Path("dep.md"),
            description="Dep",
            apply_to="**/*.py",
            content="Dep content",
            source="dependency:dep1",
        )

        collection.add_primitive(local_chatmode)
        collection.add_primitive(dep_instruction)

        # Test get_primitives_by_source
        local_prims = collection.get_primitives_by_source("local")
        self.assertEqual(len(local_prims), 1)
        self.assertEqual(local_prims[0].name, "local")

        dep_prims = collection.get_primitives_by_source("dependency:dep1")
        self.assertEqual(len(dep_prims), 1)
        self.assertEqual(dep_prims[0].name, "dep")

        # Test get_conflicts_by_type
        chatmode_conflicts = collection.get_conflicts_by_type("chatmode")
        self.assertEqual(len(chatmode_conflicts), 0)  # No conflicts yet

    def test_dependency_order_includes_transitive_from_lockfile(self):
        """Test that transitive dependencies from apm.lock are included in declaration order."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile

        # Create apm.yml with only one direct dependency
        dependencies = {"apm": ["rieraj/team-cot-agent-instructions"]}
        self._create_apm_yml(dependencies)

        # Create apm.lock with transitive dependencies
        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="rieraj/team-cot-agent-instructions",
                depth=1,
            )
        )
        lockfile.add_dependency(
            LockedDependency(
                repo_url="rieraj/division-ime-agent-instructions",
                depth=2,
                resolved_by="rieraj/team-cot-agent-instructions",
            )
        )
        lockfile.add_dependency(
            LockedDependency(
                repo_url="rieraj/autodesk-agent-instructions",
                depth=3,
                resolved_by="rieraj/division-ime-agent-instructions",
            )
        )
        lockfile.write(self.temp_dir_path / "apm.lock.yaml")

        order = get_dependency_declaration_order(str(self.temp_dir_path))

        # Direct dep should come first, then transitive deps from lockfile
        self.assertIn("rieraj/team-cot-agent-instructions", order)
        self.assertIn("rieraj/division-ime-agent-instructions", order)
        self.assertIn("rieraj/autodesk-agent-instructions", order)
        # Direct dep first
        self.assertEqual(order[0], "rieraj/team-cot-agent-instructions")
        self.assertEqual(len(order), 3)

    def test_dependency_order_no_lockfile(self):
        """Test that dependency order works without a lockfile (backward compat)."""
        # Create apm.yml with dependencies but no lockfile
        dependencies = {"apm": ["company/standards", "team/workflows"]}
        self._create_apm_yml(dependencies)

        order = get_dependency_declaration_order(str(self.temp_dir_path))
        self.assertEqual(order, ["company/standards", "team/workflows"])

    def test_dependency_order_lockfile_no_duplicates(self):
        """Test that direct deps already in apm.yml are not duplicated from lockfile."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile

        # Create apm.yml with all deps listed directly
        dependencies = {
            "apm": [
                "rieraj/team-cot",
                "rieraj/division-ime",
                "rieraj/autodesk",
            ]
        }
        self._create_apm_yml(dependencies)

        # Create apm.lock that also contains all deps
        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="rieraj/team-cot", depth=1))
        lockfile.add_dependency(LockedDependency(repo_url="rieraj/division-ime", depth=1))
        lockfile.add_dependency(LockedDependency(repo_url="rieraj/autodesk", depth=1))
        lockfile.write(self.temp_dir_path / "apm.lock.yaml")

        order = get_dependency_declaration_order(str(self.temp_dir_path))
        # No duplicates
        self.assertEqual(len(order), 3)
        self.assertEqual(len(set(order)), 3)

    def test_scan_dependency_primitives_with_transitive(self):
        """Test that scan_dependency_primitives finds transitive dep primitives."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile

        # Create apm.yml with only one direct dependency
        dependencies = {"apm": ["owner/direct-dep"]}
        self._create_apm_yml(dependencies)

        # Create apm.lock with a transitive dependency
        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="owner/direct-dep", depth=1))
        lockfile.add_dependency(
            LockedDependency(
                repo_url="owner/transitive-dep",
                depth=2,
                resolved_by="owner/direct-dep",
            )
        )
        lockfile.write(self.temp_dir_path / "apm.lock.yaml")

        # Create dependency directories with primitives
        direct_dep_dir = (
            self.temp_dir_path / "apm_modules" / "owner" / "direct-dep" / ".apm" / "instructions"
        )
        direct_dep_dir.mkdir(parents=True, exist_ok=True)
        self._create_primitive_file(
            direct_dep_dir / "direct.instructions.md", "instruction", "direct"
        )

        transitive_dep_dir = (
            self.temp_dir_path
            / "apm_modules"
            / "owner"
            / "transitive-dep"
            / ".apm"
            / "instructions"
        )
        transitive_dep_dir.mkdir(parents=True, exist_ok=True)
        self._create_primitive_file(
            transitive_dep_dir / "transitive.instructions.md", "instruction", "transitive"
        )

        collection = PrimitiveCollection()
        scan_dependency_primitives(str(self.temp_dir_path), collection)

        # Both direct and transitive deps should be found
        self.assertEqual(len(collection.instructions), 2)
        instruction_names = {i.name for i in collection.instructions}
        self.assertIn("direct", instruction_names)
        self.assertIn("transitive", instruction_names)


if __name__ == "__main__":
    unittest.main()
