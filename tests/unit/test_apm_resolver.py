"""Unit tests for APM dependency resolver and dependency graph data structures."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.dependency_graph import (
    CircularRef,
    ConflictInfo,
    DependencyGraph,
    DependencyNode,
    DependencyTree,
    FlatDependencyMap,
)
from apm_cli.models.apm_package import APMPackage, DependencyReference

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node(repo: str, depth: int = 1, parent=None) -> DependencyNode:
    dep_ref = DependencyReference.parse(repo)
    return DependencyNode(
        package=APMPackage(name=repo.split("/")[-1], version="1.0.0"),
        dependency_ref=dep_ref,
        depth=depth,
        parent=parent,
    )


def _make_tree(*repos_at_depth1: str) -> DependencyTree:
    root = APMPackage(name="root", version="1.0.0")
    tree = DependencyTree(root_package=root)
    for repo in repos_at_depth1:
        tree.add_node(_make_node(repo, depth=1))
    return tree


# ---------------------------------------------------------------------------
# APMDependencyResolver – init
# ---------------------------------------------------------------------------


class TestAPMDependencyResolverInit(unittest.TestCase):
    def test_default_initialization(self):
        resolver = APMDependencyResolver()
        self.assertEqual(resolver.max_depth, 50)
        self.assertEqual(resolver._resolution_path, [])
        self.assertIsNone(resolver._apm_modules_dir)
        self.assertIsNone(resolver._download_callback)

    def test_custom_max_depth(self):
        resolver = APMDependencyResolver(max_depth=10)
        self.assertEqual(resolver.max_depth, 10)

    def test_custom_apm_modules_dir(self):
        p = Path("/some/dir")
        resolver = APMDependencyResolver(apm_modules_dir=p)
        self.assertEqual(resolver._apm_modules_dir, p)

    def test_custom_download_callback(self):
        cb = Mock()
        resolver = APMDependencyResolver(download_callback=cb)
        self.assertEqual(resolver._download_callback, cb)


# ---------------------------------------------------------------------------
# APMDependencyResolver – resolve_dependencies
# ---------------------------------------------------------------------------


class TestResolveDependencies(unittest.TestCase):
    def setUp(self):
        self.resolver = APMDependencyResolver()

    def test_no_apm_yml_returns_empty_graph(self):
        with TemporaryDirectory() as tmp:
            result = self.resolver.resolve_dependencies(Path(tmp))
        self.assertIsInstance(result, DependencyGraph)
        self.assertEqual(result.root_package.name, "unknown")
        self.assertEqual(result.root_package.version, "0.0.0")
        self.assertEqual(result.flattened_dependencies.total_dependencies(), 0)
        self.assertFalse(result.has_circular_dependencies())
        self.assertFalse(result.has_conflicts())

    def test_invalid_apm_yml_returns_error_graph(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "apm.yml").write_text("invalid: yaml: content: [")
            result = self.resolver.resolve_dependencies(Path(tmp))
        self.assertEqual(result.root_package.name, "error")
        self.assertTrue(result.has_errors())
        self.assertIn("Failed to load root apm.yml", result.resolution_errors[0])

    def test_valid_apm_yml_no_deps(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "apm.yml").write_text(
                "name: my-pkg\nversion: 2.0.0\ndescription: test\n"
            )
            result = self.resolver.resolve_dependencies(Path(tmp))
        self.assertEqual(result.root_package.name, "my-pkg")
        self.assertEqual(result.root_package.version, "2.0.0")
        self.assertEqual(result.flattened_dependencies.total_dependencies(), 0)
        self.assertTrue(result.is_valid())

    def test_valid_apm_yml_with_two_apm_deps(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "apm.yml").write_text(
                "name: my-pkg\nversion: 1.0.0\ndependencies:\n  apm:\n    - user/repo1\n    - user/repo2#v1\n"
            )
            result = self.resolver.resolve_dependencies(Path(tmp))
        self.assertEqual(result.flattened_dependencies.total_dependencies(), 2)
        self.assertIn("user/repo1", result.flattened_dependencies.dependencies)
        self.assertIn("user/repo2", result.flattened_dependencies.dependencies)

    def test_apm_modules_dir_defaults_to_project_root_subdir(self):
        resolver = APMDependencyResolver()
        with TemporaryDirectory() as tmp:
            resolver.resolve_dependencies(Path(tmp))
        self.assertEqual(resolver._apm_modules_dir, Path(tmp) / "apm_modules")

    def test_explicit_apm_modules_dir_preserved(self):
        custom_dir = Path("/custom/apm_modules")
        resolver = APMDependencyResolver(apm_modules_dir=custom_dir)
        with TemporaryDirectory() as tmp:
            resolver.resolve_dependencies(Path(tmp))
        self.assertEqual(resolver._apm_modules_dir, custom_dir)


# ---------------------------------------------------------------------------
# APMDependencyResolver – build_dependency_tree
# ---------------------------------------------------------------------------


class TestBuildDependencyTree(unittest.TestCase):
    def setUp(self):
        self.resolver = APMDependencyResolver()

    def test_valid_apm_yml_no_deps(self):
        with TemporaryDirectory() as tmp:
            yml = Path(tmp) / "apm.yml"
            yml.write_text("name: empty\nversion: 1.0.0\n")
            tree = self.resolver.build_dependency_tree(yml)
        self.assertIsInstance(tree, DependencyTree)
        self.assertEqual(tree.root_package.name, "empty")
        self.assertEqual(len(tree.nodes), 0)
        self.assertEqual(tree.max_depth, 0)

    def test_invalid_apm_yml(self):
        with TemporaryDirectory() as tmp:
            yml = Path(tmp) / "apm.yml"
            yml.write_text("invalid yaml [")
            tree = self.resolver.build_dependency_tree(yml)
        self.assertEqual(tree.root_package.name, "error")
        self.assertEqual(len(tree.nodes), 0)

    def test_two_direct_dependencies(self):
        with TemporaryDirectory() as tmp:
            yml = Path(tmp) / "apm.yml"
            yml.write_text(
                "name: parent\nversion: 1.0.0\ndependencies:\n  apm:\n    - user/dep1\n    - user/dep2#v1\n"
            )
            tree = self.resolver.build_dependency_tree(yml)
        self.assertEqual(len(tree.nodes), 2)
        self.assertEqual(tree.max_depth, 1)
        self.assertTrue(tree.has_dependency("user/dep1"))
        self.assertTrue(tree.has_dependency("user/dep2"))
        self.assertEqual(len(tree.get_nodes_at_depth(1)), 2)

    def test_max_depth_respected(self):
        resolver = APMDependencyResolver(max_depth=1)
        with TemporaryDirectory() as tmp:
            yml = Path(tmp) / "apm.yml"
            yml.write_text(
                "name: pkg\nversion: 1.0.0\ndependencies:\n  apm:\n    - user/dep\n"
            )
            tree = resolver.build_dependency_tree(yml)
        self.assertLessEqual(tree.max_depth, 1)

    def test_duplicate_dep_not_added_twice(self):
        """Same repo listed twice should produce only one node."""
        with TemporaryDirectory() as tmp:
            yml = Path(tmp) / "apm.yml"
            yml.write_text(
                "name: pkg\nversion: 1.0.0\ndependencies:\n  apm:\n    - user/dep\n    - user/dep\n"
            )
            tree = self.resolver.build_dependency_tree(yml)
        self.assertEqual(len(tree.nodes), 1)

    def test_load_transitive_deps_via_installed_package(self):
        """If apm_modules has an installed dep with its own apm.yml, transitive deps load."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Root package depends on user/dep1
            (root / "apm.yml").write_text(
                "name: root\nversion: 1.0.0\ndependencies:\n  apm:\n    - user/dep1\n"
            )
            # dep1 is 'installed' in apm_modules and has its own dep on user/dep2
            dep1_dir = root / "apm_modules" / "user" / "dep1"
            dep1_dir.mkdir(parents=True)
            (dep1_dir / "apm.yml").write_text(
                "name: dep1\nversion: 1.0.0\ndependencies:\n  apm:\n    - user/dep2\n"
            )
            resolver = APMDependencyResolver(apm_modules_dir=root / "apm_modules")
            tree = resolver.build_dependency_tree(root / "apm.yml")
        self.assertTrue(tree.has_dependency("user/dep1"))
        self.assertTrue(tree.has_dependency("user/dep2"))
        self.assertEqual(tree.max_depth, 2)


# ---------------------------------------------------------------------------
# APMDependencyResolver – detect_circular_dependencies
# ---------------------------------------------------------------------------


class TestDetectCircularDependencies(unittest.TestCase):
    def setUp(self):
        self.resolver = APMDependencyResolver()

    def test_no_cycles(self):
        tree = _make_tree("user/a", "user/b")
        self.assertEqual(self.resolver.detect_circular_dependencies(tree), [])

    def test_simple_cycle(self):
        """A -> B -> A should produce one CircularRef."""
        root = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root)

        node_a = _make_node("user/pkg-a", depth=1)
        node_b = _make_node("user/pkg-b", depth=2, parent=node_a)
        node_a.children = [node_b]
        node_b.children = [node_a]  # back-edge -> cycle

        tree.add_node(node_a)
        tree.add_node(node_b)

        result = self.resolver.detect_circular_dependencies(tree)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], CircularRef)

    def test_empty_tree(self):
        root = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root)
        self.assertEqual(self.resolver.detect_circular_dependencies(tree), [])


# ---------------------------------------------------------------------------
# APMDependencyResolver – flatten_dependencies
# ---------------------------------------------------------------------------


class TestFlattenDependencies(unittest.TestCase):
    def setUp(self):
        self.resolver = APMDependencyResolver()

    def test_no_conflicts(self):
        tree = _make_tree("user/a", "user/b")
        node_c = _make_node("user/c", depth=2)
        tree.add_node(node_c)

        flat = self.resolver.flatten_dependencies(tree)
        self.assertEqual(flat.total_dependencies(), 3)
        self.assertFalse(flat.has_conflicts())

    def test_conflicting_versions_first_wins(self):
        root = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root)

        node1 = DependencyNode(
            package=APMPackage(name="lib", version="1.0.0"),
            dependency_ref=DependencyReference.parse("user/lib#v1"),
            depth=1,
        )
        node2 = DependencyNode(
            package=APMPackage(name="lib", version="2.0.0"),
            dependency_ref=DependencyReference.parse("user/lib#v2"),
            depth=2,
        )
        tree.add_node(node1)
        tree.add_node(node2)

        flat = self.resolver.flatten_dependencies(tree)
        self.assertEqual(flat.total_dependencies(), 1)
        self.assertTrue(flat.has_conflicts())
        self.assertEqual(flat.conflicts[0].winner.reference, "v1")

    def test_empty_tree(self):
        root = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root)
        flat = self.resolver.flatten_dependencies(tree)
        self.assertEqual(flat.total_dependencies(), 0)
        self.assertFalse(flat.has_conflicts())


# ---------------------------------------------------------------------------
# APMDependencyResolver – _validate_dependency_reference
# ---------------------------------------------------------------------------


class TestValidateDependencyReference(unittest.TestCase):
    def setUp(self):
        self.resolver = APMDependencyResolver()

    def test_valid_refs(self):
        for spec in ("user/repo", "user/repo#main", "user/repo#v1.0.0"):
            self.assertTrue(
                self.resolver._validate_dependency_reference(
                    DependencyReference.parse(spec)
                )
            )

    def test_empty_repo_url_is_invalid(self):
        ref = DependencyReference(repo_url="", reference="main")
        self.assertFalse(self.resolver._validate_dependency_reference(ref))

    def test_url_without_slash_is_invalid(self):
        ref = DependencyReference(repo_url="noslash", reference="main")
        self.assertFalse(self.resolver._validate_dependency_reference(ref))


# ---------------------------------------------------------------------------
# APMDependencyResolver – _try_load_dependency_package
# ---------------------------------------------------------------------------


class TestTryLoadDependencyPackage(unittest.TestCase):
    def setUp(self):
        self.resolver = APMDependencyResolver()

    def test_returns_none_when_no_apm_modules_dir(self):
        dep_ref = DependencyReference.parse("user/repo")
        result = self.resolver._try_load_dependency_package(dep_ref)
        self.assertIsNone(result)

    def test_returns_none_when_install_path_missing_no_callback(self):
        with TemporaryDirectory() as tmp:
            self.resolver._apm_modules_dir = Path(tmp)
            dep_ref = DependencyReference.parse("user/missing")
            result = self.resolver._try_load_dependency_package(dep_ref)
        self.assertIsNone(result)

    def test_download_callback_invoked_for_missing_package(self):
        with TemporaryDirectory() as tmp:
            modules_dir = Path(tmp)
            self.resolver._apm_modules_dir = modules_dir
            dep_ref = DependencyReference.parse("user/pkg")

            # Callback that creates the directory (simulates successful download)
            install_path = dep_ref.get_install_path(modules_dir)

            def fake_download(ref, mdir):
                install_path.mkdir(parents=True, exist_ok=True)
                (install_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
                return install_path

            self.resolver._download_callback = fake_download
            result = self.resolver._try_load_dependency_package(dep_ref)

        self.assertIsNotNone(result)
        self.assertEqual(result.name, "pkg")

    def test_download_callback_failure_returns_none(self):
        with TemporaryDirectory() as tmp:
            self.resolver._apm_modules_dir = Path(tmp)
            dep_ref = DependencyReference.parse("user/pkg")

            def failing_download(ref, mdir):
                raise RuntimeError("network error")

            self.resolver._download_callback = failing_download
            result = self.resolver._try_load_dependency_package(dep_ref)
        self.assertIsNone(result)

    def test_download_callback_returns_none_path(self):
        with TemporaryDirectory() as tmp:
            self.resolver._apm_modules_dir = Path(tmp)
            dep_ref = DependencyReference.parse("user/pkg")
            self.resolver._download_callback = Mock(return_value=None)
            result = self.resolver._try_load_dependency_package(dep_ref)
        self.assertIsNone(result)

    def test_installed_package_with_valid_apm_yml(self):
        with TemporaryDirectory() as tmp:
            modules_dir = Path(tmp)
            self.resolver._apm_modules_dir = modules_dir
            dep_ref = DependencyReference.parse("user/repo")
            install_path = dep_ref.get_install_path(modules_dir)
            install_path.mkdir(parents=True)
            (install_path / "apm.yml").write_text("name: repo\nversion: 3.0.0\n")
            result = self.resolver._try_load_dependency_package(dep_ref)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "repo")

    def test_installed_package_with_skill_md_no_apm_yml(self):
        with TemporaryDirectory() as tmp:
            modules_dir = Path(tmp)
            self.resolver._apm_modules_dir = modules_dir
            dep_ref = DependencyReference.parse("user/skill-pkg")
            install_path = dep_ref.get_install_path(modules_dir)
            install_path.mkdir(parents=True)
            (install_path / "SKILL.md").write_text("# My Skill\n")
            result = self.resolver._try_load_dependency_package(dep_ref)
        self.assertIsNotNone(result)
        self.assertEqual(result.version, "1.0.0")
        self.assertEqual(result.package_path, install_path)

    def test_installed_package_no_manifest_returns_none(self):
        with TemporaryDirectory() as tmp:
            modules_dir = Path(tmp)
            self.resolver._apm_modules_dir = modules_dir
            dep_ref = DependencyReference.parse("user/bare-pkg")
            install_path = dep_ref.get_install_path(modules_dir)
            install_path.mkdir(parents=True)
            # no apm.yml and no SKILL.md
            result = self.resolver._try_load_dependency_package(dep_ref)
        self.assertIsNone(result)

    def test_installed_package_with_invalid_apm_yml_returns_none(self):
        with TemporaryDirectory() as tmp:
            modules_dir = Path(tmp)
            self.resolver._apm_modules_dir = modules_dir
            dep_ref = DependencyReference.parse("user/bad-pkg")
            install_path = dep_ref.get_install_path(modules_dir)
            install_path.mkdir(parents=True)
            (install_path / "apm.yml").write_text("invalid yaml [[[")
            result = self.resolver._try_load_dependency_package(dep_ref)
        self.assertIsNone(result)

    def test_package_not_re_downloaded_in_same_resolution(self):
        """A package already in _downloaded_packages is not downloaded again."""
        with TemporaryDirectory() as tmp:
            modules_dir = Path(tmp)
            self.resolver._apm_modules_dir = modules_dir
            dep_ref = DependencyReference.parse("user/pkg")

            cb = Mock(return_value=None)
            self.resolver._download_callback = cb
            unique_key = dep_ref.get_unique_key()
            self.resolver._downloaded_packages.add(
                unique_key
            )  # simulate already downloaded

            result = self.resolver._try_load_dependency_package(dep_ref)

        cb.assert_not_called()
        self.assertIsNone(result)

    def test_source_set_from_dep_ref_when_missing(self):
        """Package.source is populated from dep_ref.repo_url if empty."""
        with TemporaryDirectory() as tmp:
            modules_dir = Path(tmp)
            self.resolver._apm_modules_dir = modules_dir
            dep_ref = DependencyReference.parse("user/sourced-pkg")
            install_path = dep_ref.get_install_path(modules_dir)
            install_path.mkdir(parents=True)
            (install_path / "apm.yml").write_text("name: sourced-pkg\nversion: 1.0.0\n")
            result = self.resolver._try_load_dependency_package(dep_ref)
        self.assertIsNotNone(result)
        self.assertEqual(result.source, dep_ref.repo_url)


# ---------------------------------------------------------------------------
# APMDependencyResolver – _create_resolution_summary
# ---------------------------------------------------------------------------


class TestCreateResolutionSummary(unittest.TestCase):
    def setUp(self):
        self.resolver = APMDependencyResolver()

    def _make_graph(self, errors=None, circular=None):
        root = APMPackage(name="my-pkg", version="1.0.0")
        tree = DependencyTree(root_package=root)
        flat = FlatDependencyMap()
        flat.add_dependency(DependencyReference.parse("user/dep1"))
        graph = DependencyGraph(
            root_package=root,
            dependency_tree=tree,
            flattened_dependencies=flat,
        )
        if errors:
            for e in errors:
                graph.add_error(e)
        if circular:
            for c in circular:
                graph.add_circular_dependency(c)
        return graph

    def test_basic_summary(self):
        graph = self._make_graph()
        s = self.resolver._create_resolution_summary(graph)
        self.assertIn("my-pkg", s)
        self.assertIn("Total dependencies: 1", s)
        self.assertIn("✅ Valid", s)

    def test_summary_with_errors(self):
        graph = self._make_graph(errors=["something broke"])
        s = self.resolver._create_resolution_summary(graph)
        self.assertIn("Resolution errors: 1", s)
        self.assertIn("❌ Invalid", s)

    def test_summary_with_circular_deps(self):
        cr = CircularRef(cycle_path=["a", "b", "a"], detected_at_depth=2)
        graph = self._make_graph(circular=[cr])
        s = self.resolver._create_resolution_summary(graph)
        self.assertIn("Circular dependencies: 1", s)

    def test_summary_with_conflicts(self):
        root = APMPackage(name="my-pkg", version="1.0.0")
        tree = DependencyTree(root_package=root)
        flat = FlatDependencyMap()
        flat.add_dependency(DependencyReference.parse("user/lib#v1"))
        flat.add_dependency(DependencyReference.parse("user/lib#v2"), is_conflict=True)
        graph = DependencyGraph(
            root_package=root, dependency_tree=tree, flattened_dependencies=flat
        )
        s = self.resolver._create_resolution_summary(graph)
        self.assertIn("Conflicts detected: 1", s)


# ---------------------------------------------------------------------------
# DependencyNode
# ---------------------------------------------------------------------------


class TestDependencyNode(unittest.TestCase):
    def test_get_id_without_reference(self):
        node = _make_node("user/pkg")
        self.assertEqual(node.get_id(), "user/pkg")

    def test_get_id_with_reference(self):
        dep_ref = DependencyReference.parse("user/pkg#v2")
        node = DependencyNode(
            package=APMPackage(name="pkg", version="2.0.0"),
            dependency_ref=dep_ref,
            depth=1,
        )
        self.assertEqual(node.get_id(), "user/pkg#v2")

    def test_get_display_name(self):
        node = _make_node("owner/my-repo")
        self.assertIn("my-repo", node.get_display_name())

    def test_defaults(self):
        node = _make_node("user/pkg")
        self.assertEqual(node.children, [])
        self.assertIsNone(node.parent)


# ---------------------------------------------------------------------------
# CircularRef
# ---------------------------------------------------------------------------


class TestCircularRef(unittest.TestCase):
    def test_str_cycle(self):
        cr = CircularRef(cycle_path=["a/x", "b/y", "a/x"], detected_at_depth=3)
        s = str(cr)
        self.assertIn("Circular dependency detected", s)
        self.assertIn("a/x -> b/y -> a/x", s)

    def test_empty_cycle_path(self):
        cr = CircularRef(cycle_path=[], detected_at_depth=0)
        s = cr._format_complete_cycle()
        self.assertIn("empty", s)

    def test_cycle_path_first_equals_last_no_duplicate_appended(self):
        cr = CircularRef(cycle_path=["a/x", "b/y", "a/x"], detected_at_depth=2)
        formatted = cr._format_complete_cycle()
        # Should not duplicate: "a/x -> b/y -> a/x -> a/x"
        self.assertNotIn("a/x -> a/x", formatted)

    def test_cycle_path_first_differs_from_last_appends_start(self):
        cr = CircularRef(cycle_path=["a/x", "b/y"], detected_at_depth=2)
        formatted = cr._format_complete_cycle()
        self.assertTrue(formatted.endswith("a/x"))


# ---------------------------------------------------------------------------
# ConflictInfo
# ---------------------------------------------------------------------------


class TestConflictInfo(unittest.TestCase):
    def test_str(self):
        winner = DependencyReference.parse("user/lib#v1")
        loser = DependencyReference.parse("user/lib#v2")
        conflict = ConflictInfo(
            repo_url="user/lib",
            winner=winner,
            conflicts=[loser],
            reason="first declared dependency wins",
        )
        s = str(conflict)
        self.assertIn("user/lib", s)
        self.assertIn("wins", s)


# ---------------------------------------------------------------------------
# DependencyTree
# ---------------------------------------------------------------------------


class TestDependencyTree(unittest.TestCase):
    def test_add_and_get_node(self):
        tree = _make_tree()
        node = _make_node("user/test")
        tree.add_node(node)
        self.assertEqual(tree.get_node("user/test"), node)
        self.assertEqual(tree.max_depth, 1)

    def test_get_node_missing_returns_none(self):
        tree = _make_tree()
        self.assertIsNone(tree.get_node("user/nonexistent"))

    def test_has_dependency(self):
        tree = _make_tree("user/a")
        self.assertTrue(tree.has_dependency("user/a"))
        self.assertFalse(tree.has_dependency("user/b"))

    def test_get_nodes_at_depth(self):
        tree = _make_tree("user/a", "user/b")
        deep = _make_node("user/deep", depth=2)
        tree.add_node(deep)
        self.assertEqual(len(tree.get_nodes_at_depth(1)), 2)
        self.assertEqual(len(tree.get_nodes_at_depth(2)), 1)
        self.assertEqual(len(tree.get_nodes_at_depth(3)), 0)

    def test_max_depth_updates(self):
        root = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root)
        self.assertEqual(tree.max_depth, 0)
        tree.add_node(_make_node("user/a", depth=3))
        self.assertEqual(tree.max_depth, 3)


# ---------------------------------------------------------------------------
# FlatDependencyMap
# ---------------------------------------------------------------------------


class TestFlatDependencyMap(unittest.TestCase):
    def test_add_and_get(self):
        fm = FlatDependencyMap()
        dep = DependencyReference.parse("user/dep")
        fm.add_dependency(dep)
        self.assertEqual(fm.get_dependency("user/dep"), dep)
        self.assertEqual(fm.total_dependencies(), 1)
        self.assertFalse(fm.has_conflicts())

    def test_adding_same_dep_twice_no_duplicate(self):
        fm = FlatDependencyMap()
        dep = DependencyReference.parse("user/dep")
        fm.add_dependency(dep)
        fm.add_dependency(dep)  # second add is a no-op (not a conflict)
        self.assertEqual(fm.total_dependencies(), 1)

    def test_conflict_first_wins(self):
        fm = FlatDependencyMap()
        d1 = DependencyReference.parse("user/lib#v1")
        d2 = DependencyReference.parse("user/lib#v2")
        fm.add_dependency(d1)
        fm.add_dependency(d2, is_conflict=True)
        self.assertEqual(fm.total_dependencies(), 1)
        self.assertTrue(fm.has_conflicts())
        self.assertEqual(fm.conflicts[0].winner, d1)
        self.assertIn(d2, fm.conflicts[0].conflicts)

    def test_multiple_conflicts_same_repo(self):
        fm = FlatDependencyMap()
        d1 = DependencyReference.parse("user/lib#v1")
        d2 = DependencyReference.parse("user/lib#v2")
        d3 = DependencyReference.parse("user/lib#v3")
        fm.add_dependency(d1)
        fm.add_dependency(d2, is_conflict=True)
        fm.add_dependency(d3, is_conflict=True)
        self.assertEqual(len(fm.conflicts), 1)
        self.assertEqual(len(fm.conflicts[0].conflicts), 2)

    def test_get_installation_list_order(self):
        fm = FlatDependencyMap()
        d1 = DependencyReference.parse("user/a")
        d2 = DependencyReference.parse("user/b")
        fm.add_dependency(d1)
        fm.add_dependency(d2)
        lst = fm.get_installation_list()
        self.assertEqual(lst[0], d1)
        self.assertEqual(lst[1], d2)

    def test_get_missing_dependency(self):
        fm = FlatDependencyMap()
        self.assertIsNone(fm.get_dependency("user/nonexistent"))


# ---------------------------------------------------------------------------
# DependencyGraph
# ---------------------------------------------------------------------------


class TestDependencyGraph(unittest.TestCase):
    def _make_graph(self) -> DependencyGraph:
        root = APMPackage(name="root", version="1.0.0")
        return DependencyGraph(
            root_package=root,
            dependency_tree=DependencyTree(root_package=root),
            flattened_dependencies=FlatDependencyMap(),
        )

    def test_is_valid_when_clean(self):
        g = self._make_graph()
        self.assertTrue(g.is_valid())
        self.assertFalse(g.has_errors())
        self.assertFalse(g.has_circular_dependencies())
        self.assertFalse(g.has_conflicts())

    def test_add_error(self):
        g = self._make_graph()
        g.add_error("broken")
        self.assertTrue(g.has_errors())
        self.assertFalse(g.is_valid())

    def test_add_circular_dependency(self):
        g = self._make_graph()
        cr = CircularRef(cycle_path=["a", "b", "a"], detected_at_depth=2)
        g.add_circular_dependency(cr)
        self.assertTrue(g.has_circular_dependencies())
        self.assertFalse(g.is_valid())

    def test_get_summary_keys(self):
        g = self._make_graph()
        s = g.get_summary()
        expected = {
            "root_package",
            "total_dependencies",
            "max_depth",
            "has_circular_dependencies",
            "circular_count",
            "has_conflicts",
            "conflict_count",
            "has_errors",
            "error_count",
            "is_valid",
        }
        self.assertEqual(set(s.keys()), expected)

    def test_get_summary_values(self):
        g = self._make_graph()
        s = g.get_summary()
        self.assertEqual(s["root_package"], "root")
        self.assertEqual(s["total_dependencies"], 0)
        self.assertTrue(s["is_valid"])


if __name__ == "__main__":
    unittest.main()
