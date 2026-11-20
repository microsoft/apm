"""Tests for primitives discovery and parser covering uncovered code paths.

🤖 Test Improver: automated AI assistant focused on improving test coverage.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.compilation.inventory import CompileInventory
from apm_cli.constants import DEFAULT_SKIP_DIRS
from apm_cli.primitives.discovery import (
    _discover_local_skill,
    _discover_skill_in_directory,
    _is_readable,
    discover_primitives,
    find_primitive_files,
    get_dependency_declaration_order,
    scan_directory_with_source,
    scan_local_primitives,
)
from apm_cli.primitives.models import (
    Chatmode,
    Context,
    Instruction,
    PrimitiveCollection,
)
from apm_cli.primitives.parser import (
    _extract_primitive_name,
    parse_primitive_file,
    parse_skill_file,
)

pytestmark = pytest.mark.component


def _write(path: Path, content: str = "---\ndescription: stub\n---\n\n# Stub\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


CHATMODE_CONTENT = "---\ndescription: Test chatmode\n---\n\n# Chatmode body\n"
INSTRUCTION_CONTENT = (
    "---\ndescription: Test instruction\napplyTo: '**/*.py'\n---\n\n# Instruction body\n"
)
CONTEXT_CONTENT = "---\ndescription: Test context\n---\n\n# Context body\n"
SKILL_CONTENT = "---\nname: my-skill\ndescription: A skill\n---\n\n# Skill body\n"
SKILL_NO_NAME = "---\ndescription: A skill\n---\n\n# Skill body\n"


class TestParseSkillFile(unittest.TestCase):
    """Tests for parse_skill_file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parse_skill_with_name_in_frontmatter(self):
        path = Path(self.tmp) / "mypackage" / "SKILL.md"
        _write(path, SKILL_CONTENT)
        skill = parse_skill_file(path, source="local")
        self.assertEqual(skill.name, "my-skill")
        self.assertEqual(skill.description, "A skill")
        self.assertEqual(skill.source, "local")

    def test_parse_skill_derives_name_from_parent_dir(self):
        """When name not in frontmatter, name is derived from parent directory."""
        path = Path(self.tmp) / "awesome-package" / "SKILL.md"
        _write(path, SKILL_NO_NAME)
        skill = parse_skill_file(path, source="dependency:owner/repo")
        self.assertEqual(skill.name, "awesome-package")

    def test_parse_skill_invalid_file_raises(self):
        path = Path(self.tmp) / "nonexistent.md"
        with self.assertRaises(ValueError):
            parse_skill_file(path)


class TestParseUnknownPrimitiveType(unittest.TestCase):
    """Tests for parse_primitive_file with unknown file types."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unknown_extension_raises_value_error(self):
        path = Path(self.tmp) / "test.unknown.md"
        _write(path, "---\ntitle: test\n---\n\n# Content\n")
        with self.assertRaises(ValueError):
            parse_primitive_file(path)


class TestDiscoverLocalSkill(unittest.TestCase):
    """Tests for _discover_local_skill."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_discovers_skill_md_at_root(self):
        _write(Path(self.tmp) / "SKILL.md", SKILL_CONTENT)
        collection = PrimitiveCollection()
        _discover_local_skill(self.tmp, collection)
        self.assertEqual(len(collection.skills), 1)
        self.assertEqual(collection.skills[0].name, "my-skill")

    def test_no_skill_md_leaves_collection_empty(self):
        collection = PrimitiveCollection()
        _discover_local_skill(self.tmp, collection)
        self.assertEqual(len(collection.skills), 0)

    def test_parse_error_on_skill_md_warns_and_skips(self):
        """A parse error on SKILL.md is caught, printed as warning, and skipped."""
        skill_path = Path(self.tmp) / "SKILL.md"
        _write(skill_path, SKILL_CONTENT)
        collection = PrimitiveCollection()
        with patch(
            "apm_cli.primitives.discovery.parse_skill_file",
            side_effect=ValueError("bad"),
        ):
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                _discover_local_skill(self.tmp, collection)
            self.assertIn("Warning", buf.getvalue())
        self.assertEqual(len(collection.skills), 0)


class TestDiscoverSkillInDirectory(unittest.TestCase):
    """Tests for _discover_skill_in_directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_discovers_skill_in_dep_dir(self):
        dep_dir = Path(self.tmp) / "owner" / "repo"
        _write(dep_dir / "SKILL.md", SKILL_CONTENT)
        collection = PrimitiveCollection()
        _discover_skill_in_directory(dep_dir, collection, source="dependency:owner/repo")
        self.assertEqual(len(collection.skills), 1)
        self.assertEqual(collection.skills[0].source, "dependency:owner/repo")

    def test_no_skill_md_in_dep_dir(self):
        dep_dir = Path(self.tmp) / "owner" / "repo"
        dep_dir.mkdir(parents=True)
        collection = PrimitiveCollection()
        _discover_skill_in_directory(dep_dir, collection, source="dependency:owner/repo")
        self.assertEqual(len(collection.skills), 0)

    def test_parse_error_in_dep_skill_warns_and_skips(self):
        dep_dir = Path(self.tmp) / "owner" / "repo"
        _write(dep_dir / "SKILL.md", SKILL_CONTENT)
        collection = PrimitiveCollection()
        with patch(
            "apm_cli.primitives.discovery.parse_skill_file",
            side_effect=ValueError("bad"),
        ):
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                _discover_skill_in_directory(dep_dir, collection, source="dep:x")
            self.assertIn("Warning", buf.getvalue())
        self.assertEqual(len(collection.skills), 0)


class TestScanDirectoryWithSource(unittest.TestCase):
    """Tests for scan_directory_with_source."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_apm_dir_checks_for_skill_md(self):
        """Without .apm dir, falls back to checking for SKILL.md."""
        dep_dir = Path(self.tmp) / "owner" / "repo"
        dep_dir.mkdir(parents=True)
        _write(dep_dir / "SKILL.md", SKILL_CONTENT)
        collection = PrimitiveCollection()
        scan_directory_with_source(dep_dir, collection, source="dependency:owner/repo")
        self.assertEqual(len(collection.skills), 1)

    def test_no_apm_dir_no_skill_md_leaves_empty(self):
        dep_dir = Path(self.tmp) / "owner" / "repo"
        dep_dir.mkdir(parents=True)
        collection = PrimitiveCollection()
        scan_directory_with_source(dep_dir, collection, source="dependency:owner/repo")
        self.assertEqual(collection.count(), 0)

    def test_with_apm_dir_discovers_primitives(self):
        dep_dir = Path(self.tmp) / "owner" / "repo"
        _write(
            dep_dir / ".apm" / "instructions" / "guide.instructions.md",
            INSTRUCTION_CONTENT,
        )
        collection = PrimitiveCollection()
        scan_directory_with_source(dep_dir, collection, source="dependency:owner/repo")
        self.assertEqual(len(collection.instructions), 1)
        self.assertEqual(collection.instructions[0].source, "dependency:owner/repo")

    def test_inventory_scan_skips_symlinked_dependency_primitive(self):
        """Inventory-backed dependency discovery does not dereference file links."""
        dep_dir = Path(self.tmp) / "owner" / "repo"
        target = Path(self.tmp) / "outside.instructions.md"
        _write(target, INSTRUCTION_CONTENT)
        link = dep_dir / ".apm" / "instructions" / "linked.instructions.md"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("file symlinks are unavailable on this platform")

        collection = PrimitiveCollection()
        scan_directory_with_source(
            dep_dir,
            collection,
            source="dependency:owner/repo",
            inventory=CompileInventory.collect(Path(self.tmp)),
        )

        self.assertEqual(len(collection.instructions), 0)

    def test_dependency_skill_symlink_is_not_discovered(self):
        """Dependency skills do not load content from outside their package."""
        dep_dir = Path(self.tmp) / "owner" / "repo"
        target = Path(self.tmp) / "outside-skill.md"
        _write(target, SKILL_CONTENT)
        link = dep_dir / "SKILL.md"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("file symlinks are unavailable on this platform")

        collection = PrimitiveCollection()
        scan_directory_with_source(dep_dir, collection, source="dependency:owner/repo")

        self.assertEqual(len(collection.skills), 0)

    def test_parse_error_in_dep_primitive_warns_and_continues(self):
        dep_dir = Path(self.tmp) / "owner" / "repo"
        _write(
            dep_dir / ".apm" / "instructions" / "guide.instructions.md",
            INSTRUCTION_CONTENT,
        )
        collection = PrimitiveCollection()
        with patch(
            "apm_cli.primitives.discovery.parse_primitive_file",
            side_effect=ValueError("bad"),
        ):
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                scan_directory_with_source(dep_dir, collection, source="dep:x")
            self.assertIn("Warning", buf.getvalue())
        self.assertEqual(collection.count(), 0)

    def test_github_instructions_discovered_when_no_apm_dir(self):
        """Regression test for issue #631.

        Dependency instructions stored in .github/instructions/ must be
        included in compile --target claude without --local-only.
        """
        dep_dir = Path(self.tmp) / "chkp-roniz" / "cc-rubber-duck"
        _write(
            dep_dir / ".github" / "instructions" / "rubber-duck.instructions.md",
            INSTRUCTION_CONTENT,
        )
        collection = PrimitiveCollection()
        scan_directory_with_source(
            dep_dir, collection, source="dependency:chkp-roniz/cc-rubber-duck"
        )
        self.assertEqual(len(collection.instructions), 1)
        self.assertEqual(
            collection.instructions[0].source,
            "dependency:chkp-roniz/cc-rubber-duck",
        )

    def test_github_instructions_discovered_alongside_apm_dir(self):
        """Regression test for issue #631.

        When a dependency has both .apm/instructions/ and .github/instructions/,
        primitives from both directories must be discovered.
        """
        dep_dir = Path(self.tmp) / "owner" / "mixed-pkg"
        _write(
            dep_dir / ".apm" / "instructions" / "from-apm.instructions.md",
            INSTRUCTION_CONTENT,
        )
        _write(
            dep_dir / ".github" / "instructions" / "from-github.instructions.md",
            INSTRUCTION_CONTENT,
        )
        collection = PrimitiveCollection()
        scan_directory_with_source(dep_dir, collection, source="dependency:owner/mixed-pkg")
        self.assertEqual(len(collection.instructions), 2)


class TestGetDependencyDeclarationOrder(unittest.TestCase):
    """Tests for get_dependency_declaration_order."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_apm_yml_returns_empty(self):
        result = get_dependency_declaration_order(self.tmp)
        self.assertEqual(result, [])

    def test_apm_yml_no_dependencies_returns_empty(self):
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test-package\nversion: 1.0.0\n")
        result = get_dependency_declaration_order(self.tmp)
        self.assertEqual(result, [])

    def test_invalid_metadata_does_not_become_empty_discovery(self):
        """A parser failure must reach the consumer's error handling."""
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\n")
        with patch(
            "apm_cli.primitives.discovery.APMPackage.from_apm_yml",
            side_effect=RuntimeError("bad"),
        ):
            with self.assertRaisesRegex(RuntimeError, "bad"):
                get_dependency_declaration_order(self.tmp)

    def test_dependency_with_alias_uses_alias(self):
        """Dependency with alias uses the alias as the installed path."""
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\n")
        mock_dep = MagicMock()
        mock_dep.is_marketplace = False
        mock_dep.get_install_path.return_value = Path(self.tmp) / "apm_modules/my-alias"
        mock_dep.alias = "my-alias"
        mock_dep.is_virtual = False
        mock_package = MagicMock()
        mock_package.get_apm_dependencies.return_value = [mock_dep]
        with (
            patch(
                "apm_cli.primitives.discovery.APMPackage.from_apm_yml",
                return_value=mock_package,
            ),
            patch(
                "apm_cli.primitives.discovery.LockFile.installed_paths_for_project",
                return_value=[],
            ),
        ):
            result = get_dependency_declaration_order(self.tmp)
        self.assertEqual(result, ["my-alias"])

    def test_virtual_github_subdir_dependency(self):
        """Virtual subdirectory GitHub dep uses owner/repo/subdir format."""
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\n")
        mock_dep = MagicMock()
        mock_dep.is_marketplace = False
        mock_dep.get_install_path.return_value = Path(self.tmp) / "apm_modules/owner/repo/subdir"
        mock_dep.alias = None
        mock_dep.is_virtual = True
        mock_dep.repo_url = "owner/repo"
        mock_dep.virtual_path = "subdir"
        mock_dep.is_virtual_subdirectory.return_value = True
        mock_dep.is_azure_devops.return_value = False
        mock_package = MagicMock()
        mock_package.get_apm_dependencies.return_value = [mock_dep]
        with (
            patch(
                "apm_cli.primitives.discovery.APMPackage.from_apm_yml",
                return_value=mock_package,
            ),
            patch(
                "apm_cli.primitives.discovery.LockFile.installed_paths_for_project",
                return_value=[],
            ),
        ):
            result = get_dependency_declaration_order(self.tmp)
        self.assertEqual(result, ["owner/repo/subdir"])

    def test_virtual_github_collection_dependency(self):
        """Virtual collection GitHub dep uses owner/virtual-name format."""
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\n")
        mock_dep = MagicMock()
        mock_dep.is_marketplace = False
        mock_dep.get_install_path.return_value = Path(self.tmp) / "apm_modules/owner/my-coll"
        mock_dep.alias = None
        mock_dep.is_virtual = True
        mock_dep.repo_url = "owner/repo"
        mock_dep.virtual_path = "collections/my-coll"
        mock_dep.is_virtual_subdirectory.return_value = False
        mock_dep.is_azure_devops.return_value = False
        mock_dep.get_virtual_package_name.return_value = "my-coll"
        mock_package = MagicMock()
        mock_package.get_apm_dependencies.return_value = [mock_dep]
        with (
            patch(
                "apm_cli.primitives.discovery.APMPackage.from_apm_yml",
                return_value=mock_package,
            ),
            patch(
                "apm_cli.primitives.discovery.LockFile.installed_paths_for_project",
                return_value=[],
            ),
        ):
            result = get_dependency_declaration_order(self.tmp)
        self.assertEqual(result, ["owner/my-coll"])

    def test_virtual_ado_subdir_dependency(self):
        """Virtual subdirectory ADO dep uses org/project/repo/subdir format."""
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\n")
        mock_dep = MagicMock()
        mock_dep.is_marketplace = False
        mock_dep.get_install_path.return_value = (
            Path(self.tmp) / "apm_modules/org/project/repo/subdir"
        )
        mock_dep.alias = None
        mock_dep.is_virtual = True
        mock_dep.repo_url = "org/project/repo"
        mock_dep.virtual_path = "subdir"
        mock_dep.is_virtual_subdirectory.return_value = True
        mock_dep.is_azure_devops.return_value = True
        mock_package = MagicMock()
        mock_package.get_apm_dependencies.return_value = [mock_dep]
        with (
            patch(
                "apm_cli.primitives.discovery.APMPackage.from_apm_yml",
                return_value=mock_package,
            ),
            patch(
                "apm_cli.primitives.discovery.LockFile.installed_paths_for_project",
                return_value=[],
            ),
        ):
            result = get_dependency_declaration_order(self.tmp)
        self.assertEqual(result, ["org/project/repo/subdir"])

    def test_virtual_ado_collection_dependency(self):
        """Virtual collection ADO dep uses org/project/virtual-name format."""
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\n")
        mock_dep = MagicMock()
        mock_dep.is_marketplace = False
        mock_dep.get_install_path.return_value = Path(self.tmp) / "apm_modules/org/project/my-coll"
        mock_dep.alias = None
        mock_dep.is_virtual = True
        mock_dep.repo_url = "org/project/repo"
        mock_dep.virtual_path = "collections/my-coll"
        mock_dep.is_virtual_subdirectory.return_value = False
        mock_dep.is_azure_devops.return_value = True
        mock_dep.get_virtual_package_name.return_value = "my-coll"
        mock_package = MagicMock()
        mock_package.get_apm_dependencies.return_value = [mock_dep]
        with (
            patch(
                "apm_cli.primitives.discovery.APMPackage.from_apm_yml",
                return_value=mock_package,
            ),
            patch(
                "apm_cli.primitives.discovery.LockFile.installed_paths_for_project",
                return_value=[],
            ),
        ):
            result = get_dependency_declaration_order(self.tmp)
        self.assertEqual(result, ["org/project/my-coll"])

    def test_virtual_single_part_repo_url_subdir(self):
        """Virtual subdir dep with single-part repo_url falls back to virtual_path."""
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\n")
        mock_dep = MagicMock()
        mock_dep.is_marketplace = False
        mock_dep.get_install_path.return_value = Path(self.tmp) / "apm_modules/subdir"
        mock_dep.alias = None
        mock_dep.is_virtual = True
        mock_dep.repo_url = "singlepart"
        mock_dep.virtual_path = "subdir"
        mock_dep.is_virtual_subdirectory.return_value = True
        mock_dep.is_azure_devops.return_value = False
        mock_package = MagicMock()
        mock_package.get_apm_dependencies.return_value = [mock_dep]
        with (
            patch(
                "apm_cli.primitives.discovery.APMPackage.from_apm_yml",
                return_value=mock_package,
            ),
            patch(
                "apm_cli.primitives.discovery.LockFile.installed_paths_for_project",
                return_value=[],
            ),
        ):
            result = get_dependency_declaration_order(self.tmp)
        self.assertEqual(result, ["subdir"])

    def test_virtual_single_part_repo_url_collection(self):
        """Virtual collection with single-part repo_url uses just the virtual name."""
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\n")
        mock_dep = MagicMock()
        mock_dep.is_marketplace = False
        mock_dep.get_install_path.return_value = Path(self.tmp) / "apm_modules/my-coll"
        mock_dep.alias = None
        mock_dep.is_virtual = True
        mock_dep.repo_url = "singlepart"
        mock_dep.virtual_path = "collections/my-coll"
        mock_dep.is_virtual_subdirectory.return_value = False
        mock_dep.is_azure_devops.return_value = False
        mock_dep.get_virtual_package_name.return_value = "my-coll"
        mock_package = MagicMock()
        mock_package.get_apm_dependencies.return_value = [mock_dep]
        with (
            patch(
                "apm_cli.primitives.discovery.APMPackage.from_apm_yml",
                return_value=mock_package,
            ),
            patch(
                "apm_cli.primitives.discovery.LockFile.installed_paths_for_project",
                return_value=[],
            ),
        ):
            result = get_dependency_declaration_order(self.tmp)
        self.assertEqual(result, ["my-coll"])

    def test_transitive_deps_appended_deduped(self):
        """Transitive deps from lockfile are appended but not duplicated."""
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\n")
        # Write a real lockfile so discovery's single LockFile.read() call
        # picks up the transitive entry. apm_modules dir doesn't need to
        # actually exist for get_installed_paths to return the entries.
        (Path(self.tmp) / "apm.lock.yaml").write_text(
            "lockfile_version: '1'\n"
            "dependencies:\n"
            "  - repo_url: owner/direct-dep\n"
            "    resolved_ref: HEAD\n"
            "    resolved_commit: 0\n"
            "    version: 0.0.0\n"
            "    depth: 0\n"
            "    source: registry\n"
            "  - repo_url: owner/transitive-dep\n"
            "    resolved_ref: HEAD\n"
            "    resolved_commit: 0\n"
            "    version: 0.0.0\n"
            "    depth: 1\n"
            "    source: registry\n"
        )
        mock_dep = MagicMock()
        mock_dep.is_marketplace = False
        mock_dep.get_install_path.return_value = Path(self.tmp) / "apm_modules/owner/direct-dep"
        mock_dep.alias = None
        mock_dep.is_virtual = False
        mock_dep.repo_url = "owner/direct-dep"
        mock_package = MagicMock()
        mock_package.get_apm_dependencies.return_value = [mock_dep]
        with patch(
            "apm_cli.primitives.discovery.APMPackage.from_apm_yml",
            return_value=mock_package,
        ):
            result = get_dependency_declaration_order(self.tmp)
        self.assertEqual(result, ["owner/direct-dep", "owner/transitive-dep"])


class TestLocalBundleStagedSlugs(unittest.TestCase):
    """Regression tests for issue #1363.

    Local-bundle install stages instructions under
    ``apm_modules/<slug>/.apm/instructions/`` and records the deployed paths
    in the lockfile's top-level ``local_deployed_files`` field. The install
    intentionally does NOT mutate ``apm.yml`` (services.py:489-490), so
    ``scan_dependency_primitives`` previously could not see the staged
    primitives, and ``apm compile`` produced no output for compile-only
    targets (opencode, codex, gemini).

    The fix derives bundle slugs from ``local_deployed_files`` entries that
    match ``apm_modules/<slug>/.apm/...`` and feeds them into the discovery
    scan list, so compile picks them up while the apm.yml contract stays
    untouched. Provenance is anchored to the lockfile record, not to a blind
    walk of ``apm_modules/`` -- a stray directory under ``apm_modules/``
    without a lockfile record must NOT be discovered.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_local_bundle_lockfile(self, deployed_files: list[str]) -> None:
        """Write an apm.lock.yaml with ``local_deployed_files`` populated.

        Mirrors what ``install_local_bundle`` writes in
        ``local_bundle_handler.py:194-199``.
        """
        import yaml

        data: dict = {
            "lockfile_version": "1",
            "generated_at": "2025-01-01T00:00:00+00:00",
        }
        if deployed_files:
            data["local_deployed_files"] = sorted(deployed_files)
        (Path(self.tmp) / "apm.lock.yaml").write_text(
            yaml.dump(data, default_flow_style=False), encoding="utf-8"
        )

    def test_staged_bundle_slug_appended_when_apm_yml_empty(self):
        """A local bundle staged under apm_modules/<slug>/.apm/ appears in
        the declaration order even with no apm.yml dependency entry.
        """
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\nversion: 1.0.0\n")
        self._write_local_bundle_lockfile(
            [
                "apm_modules/my-bundle/.apm/instructions/style.md",
                "apm_modules/my-bundle/.apm/instructions/style.instructions.md",
            ]
        )
        result = get_dependency_declaration_order(self.tmp)
        self.assertIn("my-bundle", result)

    def test_no_lockfile_record_no_slug(self):
        """A stray apm_modules/<slug>/.apm/ directory with NO lockfile
        record must NOT be surfaced. This defends the security invariant
        that discovery is gated by lockfile provenance, not by raw
        on-disk presence (otherwise a hostile actor or stale debris
        could inject content into compile output).
        """
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\nversion: 1.0.0\n")
        # No lockfile written at all.
        # Even if we physically stage files on disk, discovery must not
        # surface them without a lockfile record.
        staged = Path(self.tmp) / "apm_modules" / "phantom" / ".apm" / "instructions"
        staged.mkdir(parents=True)
        (staged / "evil.instructions.md").write_text(INSTRUCTION_CONTENT)
        result = get_dependency_declaration_order(self.tmp)
        self.assertNotIn("phantom", result)

    def test_declared_dep_wins_over_local_bundle_with_same_slug(self):
        """When both a declared dep and a local-bundle slug share a name,
        the declared dep appears first (priority). The local-bundle slug
        is deduplicated against the existing entry.
        """
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\n")
        mock_dep = MagicMock()
        mock_dep.is_marketplace = False
        mock_dep.get_install_path.return_value = Path(self.tmp) / "apm_modules/shared-name"
        mock_dep.alias = "shared-name"
        mock_dep.is_virtual = False
        mock_package = MagicMock()
        mock_package.get_apm_dependencies.return_value = [mock_dep]
        self._write_local_bundle_lockfile(["apm_modules/shared-name/.apm/instructions/x.md"])
        with patch(
            "apm_cli.primitives.discovery.APMPackage.from_apm_yml",
            return_value=mock_package,
        ):
            result = get_dependency_declaration_order(self.tmp)
        # "shared-name" appears exactly once, and the alias (declared)
        # comes first.
        self.assertEqual(result.count("shared-name"), 1)
        self.assertEqual(result[0], "shared-name")

    def test_multiple_local_bundles_sorted_deterministically(self):
        """Two local bundles surface in deterministic order (alphabetical
        by slug) so compile output is reproducible across runs.
        """
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\nversion: 1.0.0\n")
        self._write_local_bundle_lockfile(
            [
                "apm_modules/bundle-b/.apm/instructions/b.md",
                "apm_modules/bundle-a/.apm/instructions/a.md",
            ]
        )
        result = get_dependency_declaration_order(self.tmp)
        # bundle-a appears before bundle-b regardless of file insertion
        # order in the lockfile list.
        idx_a = result.index("bundle-a")
        idx_b = result.index("bundle-b")
        self.assertLess(idx_a, idx_b)

    def test_non_apm_modules_paths_ignored(self):
        """``local_deployed_files`` also records files outside
        apm_modules/ (e.g. .github/instructions/x.md from the same bundle
        install). Those must NOT produce phantom slugs.
        """
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\nversion: 1.0.0\n")
        self._write_local_bundle_lockfile(
            [
                ".github/instructions/style.md",
                ".agents/skills/coding/SKILL.md",
                "apm_modules/real-bundle/.apm/instructions/x.md",
            ]
        )
        result = get_dependency_declaration_order(self.tmp)
        # The legitimate apm_modules entry must produce its slug.
        self.assertIn("real-bundle", result)
        # The non-apm_modules entries must NOT produce phantom slugs --
        # check absence of plausible spoofs derived from their paths
        # (filenames or first-segment dir names) without forbidding
        # unrelated dependency entries from coexisting.
        for phantom in ("style", ".github", "coding", ".agents"):
            self.assertNotIn(phantom, result)

    def test_non_instructions_apm_subdirs_also_recognized(self):
        """Future-proof: bundles may stage other primitive types under
        apm_modules/<slug>/.apm/ (e.g. context/, agents/). The slug
        derivation must trigger on any apm_modules/<slug>/.apm/* path,
        not only instructions/.
        """
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\nversion: 1.0.0\n")
        self._write_local_bundle_lockfile(["apm_modules/ctx-bundle/.apm/context/notes.context.md"])
        result = get_dependency_declaration_order(self.tmp)
        self.assertIn("ctx-bundle", result)


class TestScanLocalPrimitives(unittest.TestCase):
    """Tests for scan_local_primitives."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scans_local_primitives_excluding_apm_modules(self):
        base = Path(self.tmp)
        _write(
            base / ".apm" / "instructions" / "guide.instructions.md",
            INSTRUCTION_CONTENT,
        )
        # Also write a file inside apm_modules (should be excluded)
        _write(
            base
            / "apm_modules"
            / "owner"
            / "repo"
            / ".apm"
            / "instructions"
            / "dep.instructions.md",
            INSTRUCTION_CONTENT,
        )
        collection = PrimitiveCollection()
        scan_local_primitives(self.tmp, collection)
        # Only the local one should be discovered
        self.assertEqual(len(collection.instructions), 1)

    def test_parse_error_warns_and_continues(self):
        base = Path(self.tmp)
        _write(
            base / ".apm" / "instructions" / "guide.instructions.md",
            INSTRUCTION_CONTENT,
        )
        collection = PrimitiveCollection()
        with patch(
            "apm_cli.primitives.discovery.parse_primitive_file",
            side_effect=ValueError("bad"),
        ):
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                scan_local_primitives(self.tmp, collection)
            self.assertIn("Warning", buf.getvalue())
        self.assertEqual(collection.count(), 0)


class TestExcludePatternsInDiscovery(unittest.TestCase):
    """Tests for compilation.exclude filtering during primitive discovery."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_local_primitives_excludes_matching_directory(self):
        """Primitives under excluded directories are filtered out."""
        base = Path(self.tmp)
        # Local instruction (should be kept)
        _write(
            base / ".apm" / "instructions" / "general.instructions.md",
            INSTRUCTION_CONTENT,
        )
        # Instruction inside docs/ (should be excluded)
        _write(
            base / "docs" / "labs" / ".github" / "instructions" / "react.instructions.md",
            INSTRUCTION_CONTENT,
        )
        collection = PrimitiveCollection()
        scan_local_primitives(self.tmp, collection, exclude_patterns=["docs/**"])
        self.assertEqual(len(collection.instructions), 1)

    def test_scan_local_primitives_no_exclude_discovers_all(self):
        """Without exclude patterns, all primitives are discovered."""
        base = Path(self.tmp)
        _write(
            base / ".apm" / "instructions" / "general.instructions.md",
            INSTRUCTION_CONTENT,
        )
        _write(
            base / "docs" / "labs" / ".github" / "instructions" / "react.instructions.md",
            INSTRUCTION_CONTENT,
        )
        collection = PrimitiveCollection()
        scan_local_primitives(self.tmp, collection, exclude_patterns=None)
        self.assertEqual(len(collection.instructions), 2)

    def test_scan_local_primitives_multiple_exclude_patterns(self):
        """Multiple exclude patterns each filter their respective files."""
        base = Path(self.tmp)
        _write(
            base / ".apm" / "instructions" / "kept.instructions.md",
            INSTRUCTION_CONTENT,
        )
        _write(
            base / "docs" / ".github" / "instructions" / "a.instructions.md",
            INSTRUCTION_CONTENT,
        )
        _write(
            base / "tmp" / ".github" / "instructions" / "b.instructions.md",
            INSTRUCTION_CONTENT,
        )
        collection = PrimitiveCollection()
        scan_local_primitives(self.tmp, collection, exclude_patterns=["docs/**", "tmp/**"])
        self.assertEqual(len(collection.instructions), 1)

    def test_discover_primitives_respects_exclude(self):
        """discover_primitives() filters with exclude_patterns."""
        base = Path(self.tmp)
        _write(
            base / ".apm" / "instructions" / "general.instructions.md",
            INSTRUCTION_CONTENT,
        )
        _write(
            base / "docs" / ".github" / "instructions" / "leak.instructions.md",
            INSTRUCTION_CONTENT,
        )
        from apm_cli.primitives.discovery import discover_primitives

        collection = discover_primitives(self.tmp, exclude_patterns=["docs/**"])
        self.assertEqual(len(collection.instructions), 1)

    def test_discover_primitives_with_dependencies_respects_exclude(self):
        """discover_primitives_with_dependencies() filters with exclude_patterns."""
        base = Path(self.tmp)
        _write(
            base / ".apm" / "instructions" / "general.instructions.md",
            INSTRUCTION_CONTENT,
        )
        _write(
            base / "docs" / ".github" / "instructions" / "leak.instructions.md",
            INSTRUCTION_CONTENT,
        )
        # Create minimal apm.yml for the function to work
        (base / "apm.yml").write_text("name: test\nversion: 1.0.0\n", encoding="utf-8")
        from apm_cli.primitives.discovery import (
            discover_primitives_with_dependencies,
        )

        collection = discover_primitives_with_dependencies(self.tmp, exclude_patterns=["docs/**"])
        self.assertEqual(len(collection.instructions), 1)

    def test_discover_primitives_excludes_skill_md(self):
        """SKILL.md at project root is excluded when matching pattern."""
        base = Path(self.tmp)
        skill_content = "# My Skill\n\nSome skill content."
        (base / "SKILL.md").write_text(skill_content, encoding="utf-8")
        from apm_cli.primitives.discovery import discover_primitives

        # Without exclusion -- SKILL.md found
        collection = discover_primitives(self.tmp, exclude_patterns=None)
        self.assertEqual(len(collection.skills), 1)

        # With exclusion matching SKILL.md
        collection = discover_primitives(self.tmp, exclude_patterns=["SKILL.md"])
        self.assertEqual(len(collection.skills), 0)

    def test_validate_rejects_dos_pattern(self):
        """Patterns with excessive non-consecutive ** segments are rejected."""
        from apm_cli.utils.exclude import validate_exclude_patterns

        # 7 non-consecutive ** segments (consecutive ones collapse)
        with self.assertRaises(ValueError):
            validate_exclude_patterns(["a/**/b/**/c/**/d/**/e/**/f/**/g/**"])


class TestIsReadable(unittest.TestCase):
    """Tests for _is_readable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_readable_file_returns_true(self):
        path = Path(self.tmp) / "test.md"
        path.write_text("content")
        self.assertTrue(_is_readable(path))

    def test_unreadable_file_returns_false(self):
        path = Path(self.tmp) / "test.md"
        path.write_text("content")
        # Simulate unreadable file by forcing PermissionError when opening.
        with patch("apm_cli.primitives.discovery.open", side_effect=PermissionError):
            result = _is_readable(path)
            self.assertFalse(result)

    def test_binary_file_returns_false(self):
        path = Path(self.tmp) / "test.md"
        path.write_bytes(b"\xff\xfe\x00invalid-utf8\x80\x90")
        self.assertFalse(_is_readable(path))


class TestFindPrimitiveFilesEdgeCases(unittest.TestCase):
    """Tests for find_primitive_files edge cases."""

    def test_nonexistent_directory_returns_empty(self):
        result = find_primitive_files("/nonexistent/path", ["**/*.agent.md"])
        self.assertEqual(result, [])

    def test_deduplicates_matched_files(self):
        """Multiple patterns matching same file should yield one result."""
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "test.agent.md", CHATMODE_CONTENT)
            # Both patterns should match the same file
            result = find_primitive_files(tmp, ["**/*.agent.md", "*.agent.md"])
            self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()


def test_dependency_skill_respects_parent_nested_repository_boundary(tmp_path: Path) -> None:
    """Direct SKILL.md discovery consumes the parent inventory boundary."""
    from apm_cli.compilation.inventory import CompileInventory

    dep_dir = tmp_path / "nested-dependency"
    dep_dir.mkdir()
    (dep_dir / ".git").write_text(
        "gitdir: ../.git/worktrees/nested-dependency\n",
        encoding="utf-8",
    )
    (dep_dir / "SKILL.md").write_text(
        "---\nname: foreign\ndescription: Foreign skill.\n---\n# Foreign\n",
        encoding="utf-8",
    )
    inventory = CompileInventory.collect(tmp_path)
    collection = PrimitiveCollection()

    scan_directory_with_source(
        dep_dir,
        collection,
        "dependency:nested",
        inventory=inventory,
    )

    assert collection.count() == 0


# -------------------------------------------------------------------
# find_primitive_files -- early directory pruning
# -------------------------------------------------------------------
class TestFindPrimitiveFilesExclude(unittest.TestCase):
    """Tests that find_primitive_files prunes directories via exclude_patterns."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = Path(self.tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_instruction_in_apm_dir(self):
        _write(self.base / ".apm" / "instructions" / "style.instructions.md")
        result = find_primitive_files(str(self.base), ["**/.apm/instructions/*.instructions.md"])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].name == "style.instructions.md")

    def test_finds_file_at_root(self):
        _write(self.base / "root.instructions.md")
        result = find_primitive_files(str(self.base), ["**/*.instructions.md"])
        self.assertEqual(len(result), 1)

    def test_skips_default_dirs(self):
        """Files inside DEFAULT_SKIP_DIRS should never be returned."""
        _write(self.base / "node_modules" / "pkg" / "bad.instructions.md")
        _write(self.base / "__pycache__" / "bad.instructions.md")
        _write(self.base / ".git" / "hooks" / "bad.instructions.md")
        _write(self.base / "src" / "good.instructions.md")

        result = find_primitive_files(str(self.base), ["**/*.instructions.md"])
        names = [f.name for f in result]
        self.assertIn("good.instructions.md", names)
        self.assertNotIn("bad.instructions.md", names)

    def test_exclude_patterns_prune_custom_dirs(self):
        """User-supplied exclude_patterns prevent traversal into named dirs."""
        _write(self.base / "Binaries" / "Win64" / "deep.instructions.md")
        _write(self.base / "Content" / "Textures" / "deep.instructions.md")
        _write(self.base / "Source" / "style.instructions.md")

        result = find_primitive_files(
            str(self.base),
            ["**/*.instructions.md"],
            exclude_patterns=["Binaries", "Content"],
        )
        names = [f.name for f in result]
        self.assertIn("style.instructions.md", names)
        self.assertNotIn("deep.instructions.md", names)

    def test_exclude_patterns_glob_style(self):
        """Glob-style exclude patterns work for nested matches."""
        _write(self.base / "a" / "test-fixtures" / "f.instructions.md")
        _write(self.base / "b" / "real.instructions.md")

        result = find_primitive_files(
            str(self.base),
            ["**/*.instructions.md"],
            exclude_patterns=["**/test-fixtures"],
        )
        names = [f.name for f in result]
        self.assertIn("real.instructions.md", names)
        self.assertNotIn("f.instructions.md", names)

    def test_exclude_patterns_none_finds_everything(self):
        """When exclude_patterns is None, only default skips apply."""
        _write(self.base / "a" / "one.instructions.md")
        _write(self.base / "b" / "two.instructions.md")

        result = find_primitive_files(
            str(self.base), ["**/*.instructions.md"], exclude_patterns=None
        )
        self.assertEqual(len(result), 2)

    def test_deduplicates_across_patterns(self):
        """Overlapping patterns should not produce duplicate results."""
        _write(self.base / ".apm" / "instructions" / "style.instructions.md")
        result = find_primitive_files(
            str(self.base),
            [
                "**/.apm/instructions/*.instructions.md",
                "**/*.instructions.md",
            ],
        )
        self.assertEqual(len(result), 1)

    def test_symlink_rejected(self):
        """Symlinked files should be filtered out."""
        real = self.base / "real.instructions.md"
        _write(real)
        link = self.base / "link.instructions.md"
        try:
            link.symlink_to(real)
        except OSError:
            self.skipTest("Cannot create symlinks on this platform")
        result = find_primitive_files(str(self.base), ["**/*.instructions.md"])
        names = [f.name for f in result]
        self.assertIn("real.instructions.md", names)
        self.assertNotIn("link.instructions.md", names)

    def test_nonexistent_dir_returns_empty(self):
        result = find_primitive_files("/nonexistent/path/1234", ["**/*.md"])
        self.assertEqual(result, [])

    def test_apm_dir_not_skipped(self):
        """.apm must NOT be in the default skip set -- primitives live there."""
        self.assertNotIn(".apm", DEFAULT_SKIP_DIRS)


# -------------------------------------------------------------------
# File-level exclude patterns (PR #870 review C1)
# -------------------------------------------------------------------
class TestFindPrimitiveFilesFileExclude(unittest.TestCase):
    """File-level exclude patterns must filter individual files,
    not just whole directories."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = Path(self.tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_pattern_excludes_individual_files(self):
        from apm_cli.primitives.discovery import find_primitive_files

        skills_dir = self.base / ".apm" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "good.md").touch()
        (skills_dir / "bad.draft.md").touch()

        results = find_primitive_files(
            str(self.base),
            ["**/.apm/skills/*.md"],
            exclude_patterns=["**/*.draft.md"],
        )

        names = sorted(p.name for p in results)
        self.assertIn("good.md", names)
        self.assertNotIn("bad.draft.md", names)

    def test_files_sorted_deterministically(self):
        """os.walk does not sort files -- find_primitive_files must."""
        from apm_cli.primitives.discovery import find_primitive_files

        skills_dir = self.base / ".apm" / "skills"
        skills_dir.mkdir(parents=True)
        # Create files in non-alphabetical order
        for name in ["zeta.md", "alpha.md", "mike.md"]:
            (skills_dir / name).touch()

        results = find_primitive_files(
            str(self.base),
            ["**/.apm/skills/*.md"],
        )
        names = [p.name for p in results]
        self.assertEqual(names, sorted(names))


class TestPrimitiveParser(unittest.TestCase):
    """Test cases for the primitive parser."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_path = self.temp_dir.name

    def tearDown(self):
        """Tear down test fixtures."""
        self.temp_dir.cleanup()

    def test_parse_chatmode_file(self):
        """Test parsing a chatmode file."""
        chatmode_content = """---
description: Test chatmode for code review
author: Test Author
applyTo: "**/*.{py,js}"
version: "1.0.0"
---

# Code Review Assistant

You are an expert code reviewer. Analyze the provided code for:

1. Code quality and best practices
2. Potential bugs or issues
3. Performance considerations
4. Security vulnerabilities

Provide constructive feedback and suggestions for improvement.
"""

        # Create test file
        file_path = os.path.join(self.temp_dir_path, "code-review.agent.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(chatmode_content)

        # Parse file
        primitive = parse_primitive_file(file_path)

        # Verify it's a Chatmode
        self.assertIsInstance(primitive, Chatmode)
        self.assertEqual(primitive.name, "code-review")
        self.assertEqual(primitive.description, "Test chatmode for code review")
        self.assertEqual(primitive.author, "Test Author")
        self.assertEqual(primitive.apply_to, "**/*.{py,js}")
        self.assertEqual(primitive.version, "1.0.0")
        self.assertIn("Code Review Assistant", primitive.content)

        # Test validation
        errors = primitive.validate()
        self.assertEqual(len(errors), 0)

    def _parse_agent_with_frontmatter(self, frontmatter):
        """Write a minimal .agent.md with the given frontmatter and parse it."""
        content = f"---\n{frontmatter}\n---\n\n# Agent\n\nBody.\n"
        file_path = os.path.join(self.temp_dir_path, "sample.agent.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return parse_primitive_file(file_path)

    def test_user_invocable_defaults_true_when_absent(self):
        """An agent with no user-invocable field defaults to user-invocable."""
        primitive = self._parse_agent_with_frontmatter("description: Default agent")
        self.assertIsInstance(primitive, Chatmode)
        self.assertTrue(primitive.user_invocable)

    def test_user_invocable_false_marks_programmatic_only(self):
        """user-invocable: false marks the agent programmatic-only."""
        primitive = self._parse_agent_with_frontmatter(
            "description: Internal agent\nuser-invocable: false"
        )
        self.assertFalse(primitive.user_invocable)

    def test_visibility_internal_alias_marks_programmatic_only(self):
        """visibility: internal is accepted as an alias for user-invocable: false."""
        primitive = self._parse_agent_with_frontmatter(
            "description: Internal agent\nvisibility: internal"
        )
        self.assertFalse(primitive.user_invocable)

    def test_canonical_user_invocable_wins_over_visibility_alias(self):
        """Canonical user-invocable overrides the visibility alias."""
        primitive = self._parse_agent_with_frontmatter(
            "description: Mixed agent\nuser-invocable: true\nvisibility: internal"
        )
        self.assertTrue(primitive.user_invocable)

    def test_user_invocable_string_tokens_coerce_to_false(self):
        """Defensive string tokens for falsiness coerce to not-user-invocable."""
        for token in ("false", "no", "0", "off", "False", "OFF"):
            primitive = self._parse_agent_with_frontmatter(
                f'description: Agent\nuser-invocable: "{token}"'
            )
            self.assertFalse(
                primitive.user_invocable,
                f"token {token!r} should mark the agent programmatic-only",
            )

    def test_parse_instruction_file(self):
        """Test parsing an instruction file."""
        instruction_content = """---
description: Python coding standards and conventions
applyTo: "**/*.py"
author: Development Team
---

# Python Coding Standards

Follow these coding standards when writing Python code:

## Style Guide
- Use PEP 8 for formatting
- Maximum line length of 88 characters
- Use type hints for function parameters and returns

## Best Practices
- Write docstrings for all public functions
- Use meaningful variable names
- Follow SOLID principles
"""

        # Create test file
        file_path = os.path.join(self.temp_dir_path, "python-standards.instructions.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(instruction_content)

        # Parse file
        primitive = parse_primitive_file(file_path)

        # Verify it's an Instruction
        self.assertIsInstance(primitive, Instruction)
        self.assertEqual(primitive.name, "python-standards")
        self.assertEqual(primitive.description, "Python coding standards and conventions")
        self.assertEqual(primitive.apply_to, "**/*.py")
        self.assertEqual(primitive.author, "Development Team")
        self.assertIn("Python Coding Standards", primitive.content)

        # Test validation
        errors = primitive.validate()
        self.assertEqual(len(errors), 0)

    def test_parse_context_file(self):
        """Test parsing a context file."""
        context_content = """---
description: Project context and background
---

# Project Context

This project is a command-line tool for managing AI workflows.

## Architecture
- Built with Python and Click
- Uses frontmatter for file parsing
- Supports multiple runtime environments

## Key Components
- CLI interface
- Workflow engine
- Runtime management
"""

        # Create test file
        file_path = os.path.join(self.temp_dir_path, "project-info.context.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(context_content)

        # Parse file
        primitive = parse_primitive_file(file_path)

        # Verify it's a Context
        self.assertIsInstance(primitive, Context)
        self.assertEqual(primitive.name, "project-info")
        self.assertEqual(primitive.description, "Project context and background")
        self.assertIn("Project Context", primitive.content)

        # Test validation
        errors = primitive.validate()
        self.assertEqual(len(errors), 0)

    def test_extract_primitive_name(self):
        """Test primitive name extraction from various path formats."""
        # Test structured .apm/ paths
        self.assertEqual(
            _extract_primitive_name(Path(".apm/agents/code-review.agent.md")),
            "code-review",
        )
        self.assertEqual(
            _extract_primitive_name(Path(".apm/instructions/python-style.instructions.md")),
            "python-style",
        )
        self.assertEqual(
            _extract_primitive_name(Path(".apm/context/project-info.context.md")),
            "project-info",
        )

        # Test .github/ paths (VSCode compatibility)
        self.assertEqual(
            _extract_primitive_name(Path(".github/agents/assistant.agent.md")),
            "assistant",
        )

        # Test memory files
        self.assertEqual(
            _extract_primitive_name(Path(".apm/memory/team-info.memory.md")),
            "team-info",
        )

        # Test generic files
        self.assertEqual(_extract_primitive_name(Path("my-chatmode.agent.md")), "my-chatmode")

    def test_malformed_files(self):
        """Test handling of malformed files."""
        # File with invalid YAML frontmatter
        malformed_content = """---
description: Test
invalid yaml: [
---

# Test content
"""

        file_path = os.path.join(self.temp_dir_path, "malformed.agent.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(malformed_content)

        # Should raise ValueError
        with self.assertRaises(ValueError):
            parse_primitive_file(file_path)


class TestPrimitiveDiscovery(unittest.TestCase):
    """Test cases for primitive discovery."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_path = self.temp_dir.name

        # Create directory structure
        os.makedirs(os.path.join(self.temp_dir_path, ".apm", "agents"), exist_ok=True)
        os.makedirs(os.path.join(self.temp_dir_path, ".apm", "instructions"), exist_ok=True)
        os.makedirs(os.path.join(self.temp_dir_path, ".apm", "context"), exist_ok=True)
        os.makedirs(os.path.join(self.temp_dir_path, ".github", "agents"), exist_ok=True)

    def tearDown(self):
        """Tear down test fixtures."""
        self.temp_dir.cleanup()

    def test_discover_primitives_structured(self):
        """Test discovering primitives in structured directories."""
        # Create test files
        chatmode_content = """---
description: Test chatmode
---

# Test Chatmode Content
"""

        instruction_content = """---
description: Test instruction
applyTo: "**/*.py"
---

# Test Instruction Content
"""

        context_content = """---
description: Test context
---

# Test Context Content
"""

        # Write files
        with open(
            os.path.join(self.temp_dir_path, ".apm", "agents", "assistant.agent.md"),
            "w",
        ) as f:
            f.write(chatmode_content)

        with open(
            os.path.join(self.temp_dir_path, ".apm", "instructions", "style.instructions.md"),
            "w",
        ) as f:
            f.write(instruction_content)

        with open(
            os.path.join(self.temp_dir_path, ".apm", "context", "project.context.md"),
            "w",
        ) as f:
            f.write(context_content)

        with open(
            os.path.join(self.temp_dir_path, ".github", "agents", "vscode.agent.md"),
            "w",
        ) as f:
            f.write(chatmode_content)

        # Discover primitives
        collection = discover_primitives(self.temp_dir_path)

        # Verify discovery
        self.assertEqual(collection.count(), 4)
        self.assertEqual(len(collection.chatmodes), 2)
        self.assertEqual(len(collection.instructions), 1)
        self.assertEqual(len(collection.contexts), 1)

        # Check names
        chatmode_names = [c.name for c in collection.chatmodes]
        self.assertIn("assistant", chatmode_names)
        self.assertIn("vscode", chatmode_names)

        instruction_names = [i.name for i in collection.instructions]
        self.assertIn("style", instruction_names)

        context_names = [c.name for c in collection.contexts]
        self.assertIn("project", context_names)

    def test_find_primitive_files(self):
        """Test finding primitive files with glob patterns."""
        # Create test files
        os.makedirs(os.path.join(self.temp_dir_path, "custom"), exist_ok=True)

        test_files = ["test1.agent.md", "custom/test2.agent.md"]

        for file_rel_path in test_files:
            file_path = os.path.join(self.temp_dir_path, file_rel_path)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w") as f:
                f.write("---\ndescription: Test\n---\n\n# Test")

        # Test pattern matching
        patterns = ["**/*.agent.md"]
        found_files = find_primitive_files(self.temp_dir_path, patterns)

        # Should find 2 files (glob doesn't match hidden directories by default)
        self.assertEqual(len(found_files), 2)

        # Verify all are Path objects
        for file_path in found_files:
            self.assertIsInstance(file_path, Path)
            self.assertTrue(file_path.name.endswith(".agent.md"))

    def test_find_primitive_files_specific_patterns(self):
        """Test finding primitive files with specific patterns."""
        # Test with .apm specific pattern
        apm_file = os.path.join(self.temp_dir_path, ".apm", "agents", "test.agent.md")
        with open(apm_file, "w") as f:
            f.write("---\ndescription: Test\n---\n\n# Test")

        # Test .apm specific pattern
        patterns = ["**/.apm/agents/*.agent.md"]
        found_files = find_primitive_files(self.temp_dir_path, patterns)

        # Should find the .apm file
        self.assertEqual(len(found_files), 1)
        self.assertTrue(found_files[0].name.endswith(".agent.md"))


class TestListValuedFrontmatterNormalization(unittest.TestCase):
    """Regression tests for list-valued applyTo / handoffs in frontmatter.

    GitHub issue #1300: 'list' object has no attribute 'startswith' crash
    during distributed compilation when applyTo is a YAML list.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_path = self.temp_dir.name

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write(self, filename, content):
        path = os.path.join(self.temp_dir_path, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    # -- applyTo normalization for .instructions.md ----------------------------

    def test_apply_to_scalar_list_normalizes_to_string(self):
        """applyTo: ['**/*.py'] (YAML inline list) must normalize to a string."""
        path = self._write(
            "test.instructions.md",
            "---\ndescription: Test\napplyTo: ['**/*.py']\n---\n\n# content\n",
        )
        primitive = parse_primitive_file(path)
        self.assertIsInstance(primitive, Instruction)
        self.assertIsInstance(primitive.apply_to, str)
        self.assertIn("**/*.py", primitive.apply_to)

    def test_apply_to_multi_element_list_preserves_all_patterns(self):
        """A YAML applyTo list becomes the canonical comma-separated OR expression."""
        path = self._write(
            "multi.instructions.md",
            "---\ndescription: Test\napplyTo:\n  - '**/*.py'\n  - '**/*.ts'\n---\n\n# c\n",
        )
        primitive = parse_primitive_file(path)
        self.assertIsInstance(primitive, Instruction)
        self.assertEqual(primitive.apply_to, "**/*.py,**/*.ts")

    def test_apply_to_string_unchanged(self):
        """Scalar applyTo strings pass through unchanged."""
        path = self._write(
            "scalar.instructions.md",
            "---\ndescription: Test\napplyTo: '**/*.py'\n---\n\n# content\n",
        )
        primitive = parse_primitive_file(path)
        self.assertEqual(primitive.apply_to, "**/*.py")

    def test_apply_to_missing_defaults_to_empty_string(self):
        """Missing applyTo falls back to empty string for instructions."""
        path = self._write(
            "global.instructions.md",
            "---\ndescription: Test\n---\n\n# content\n",
        )
        primitive = parse_primitive_file(path)
        self.assertIsInstance(primitive, Instruction)
        self.assertEqual(primitive.apply_to, "")

    # -- applyTo normalization for .agent.md -------------------

    def test_chatmode_apply_to_list_normalizes(self):
        """applyTo list in .agent.md normalizes to a string (not None)."""
        path = self._write(
            "bot.agent.md",
            "---\ndescription: Bot\napplyTo: ['**/*.py']\n---\n\n# content\n",
        )
        primitive = parse_primitive_file(path)
        self.assertIsInstance(primitive, Chatmode)
        self.assertIsInstance(primitive.apply_to, str)
        self.assertIn("**/*.py", primitive.apply_to)

    def test_chatmode_apply_to_none_stays_none(self):
        """Missing applyTo in .agent.md remains None (optional field)."""
        path = self._write(
            "noapply.agent.md",
            "---\ndescription: Bot\n---\n\n# content\n",
        )
        primitive = parse_primitive_file(path)
        self.assertIsInstance(primitive, Chatmode)
        self.assertIsNone(primitive.apply_to)

    # -- handoffs field in .agent.md ------------------------------------------

    def test_handoffs_list_parsed_into_chatmode(self):
        """handoffs: list in .agent.md is stored in Chatmode.handoffs."""
        path = self._write(
            "agent.agent.md",
            "---\ndescription: Agent\nhandoffs:\n  - other-agent\n  - fallback\n---\n\n# c\n",
        )
        primitive = parse_primitive_file(path)
        self.assertIsInstance(primitive, Chatmode)
        self.assertEqual(primitive.handoffs, ["other-agent", "fallback"])

    def test_handoffs_inline_list_parsed(self):
        """handoffs: ['x'] inline YAML list is also parsed correctly."""
        path = self._write(
            "inline.agent.md",
            "---\ndescription: Agent\nhandoffs: ['only-one']\n---\n\n# c\n",
        )
        primitive = parse_primitive_file(path)
        self.assertIsInstance(primitive, Chatmode)
        self.assertEqual(primitive.handoffs, ["only-one"])

    def test_handoffs_absent_means_none(self):
        """No handoffs key means Chatmode.handoffs is None."""
        path = self._write(
            "nohandoffs.agent.md",
            "---\ndescription: Agent\n---\n\n# content\n",
        )
        primitive = parse_primitive_file(path)
        self.assertIsInstance(primitive, Chatmode)
        self.assertIsNone(primitive.handoffs)

    def test_handoffs_dict_entries_preserved(self):
        """Dict-typed handoff entries (VS Code structured form) are stored without coercion."""
        path = self._write(
            "dict-handoffs.agent.md",
            "---\ndescription: Agent\nhandoffs:\n  - label: Security\n    agent: security-review\n---\n\n# c\n",
        )
        primitive = parse_primitive_file(path)
        self.assertIsInstance(primitive, Chatmode)
        self.assertEqual(len(primitive.handoffs), 1)
        self.assertIsInstance(primitive.handoffs[0], dict)
        self.assertEqual(primitive.handoffs[0]["label"], "Security")
        self.assertEqual(primitive.handoffs[0]["agent"], "security-review")
