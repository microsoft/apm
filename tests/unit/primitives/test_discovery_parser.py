"""Tests for primitives discovery and parser covering uncovered code paths.

🤖 Test Improver: automated AI assistant focused on improving test coverage.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.primitives.discovery import (
    _discover_local_skill,
    _discover_skill_in_directory,
    _is_readable,
    _is_under_directory,
    _should_skip_directory,
    find_primitive_files,
    get_dependency_declaration_order,
    scan_directory_with_source,
    scan_local_primitives,
)
from apm_cli.primitives.models import PrimitiveCollection
from apm_cli.primitives.parser import (
    _extract_primitive_name,
    _is_context_file,
    parse_primitive_file,
    parse_skill_file,
    validate_primitive,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


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


class TestExtractPrimitiveName(unittest.TestCase):
    """Tests for _extract_primitive_name with various path structures."""

    def test_agent_md_in_apm_agents_dir(self):
        """Files in .apm/agents/ are treated as structured agent primitives.
        The '.agent.md' suffix is stripped, yielding just the agent name."""
        path = Path("/project/.apm/agents/myagent.agent.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "myagent")

    def test_agent_md_in_github_agents_dir(self):
        """.github/agents/ is also treated as a structured agent primitive directory."""
        path = Path("/project/.github/agents/reviewer.agent.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "reviewer")

    def test_instruction_in_structured_dir(self):
        path = Path("/project/.apm/instructions/coding-style.instructions.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "coding-style")

    def test_context_in_structured_dir(self):
        path = Path("/project/.apm/context/project-info.context.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "project-info")

    def test_memory_in_structured_dir(self):
        path = Path("/project/.github/memory/state.memory.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "state")

    def test_plain_md_fallback(self):
        path = Path("/project/notes.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "notes")

    def test_stem_fallback_no_known_extension(self):
        path = Path("/project/something.xyz.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "something.xyz")


class TestIsContextFile(unittest.TestCase):
    """Tests for _is_context_file."""

    def test_apm_memory_dir_is_context(self):
        path = Path("/project/.apm/memory/notes.md")
        self.assertTrue(_is_context_file(path))

    def test_github_memory_dir_is_context(self):
        path = Path("/project/.github/memory/state.md")
        self.assertTrue(_is_context_file(path))

    def test_random_dir_is_not_context(self):
        path = Path("/project/docs/notes.md")
        self.assertFalse(_is_context_file(path))

    def test_apm_context_dir_is_not_matched_here(self):
        """_is_context_file only matches memory/ dirs, not context/."""
        path = Path("/project/.apm/context/info.context.md")
        self.assertFalse(_is_context_file(path))


class TestValidatePrimitive(unittest.TestCase):
    """Tests for the validate_primitive wrapper."""

    def test_valid_chatmode_returns_no_errors(self):
        from apm_cli.primitives.models import Chatmode

        cm = Chatmode(
            name="test",
            file_path=Path("test.chatmode.md"),
            description="desc",
            apply_to=None,
            content="# body",
        )
        self.assertEqual(validate_primitive(cm), [])

    def test_invalid_chatmode_returns_errors(self):
        from apm_cli.primitives.models import Chatmode

        cm = Chatmode(
            name="test",
            file_path=Path("test.chatmode.md"),
            description="",
            apply_to=None,
            content="",
        )
        errors = validate_primitive(cm)
        self.assertTrue(len(errors) > 0)


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

    def test_exception_returns_empty_with_warning(self):
        """When APMPackage.from_apm_yml raises, returns [] with a warning."""
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\n")
        with patch(
            "apm_cli.primitives.discovery.APMPackage.from_apm_yml",
            side_effect=RuntimeError("bad"),
        ):
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                result = get_dependency_declaration_order(self.tmp)
            self.assertEqual(result, [])
            self.assertIn("Warning", buf.getvalue())

    def test_dependency_with_alias_uses_alias(self):
        """Dependency with alias uses the alias as the installed path."""
        apm_yml = Path(self.tmp) / "apm.yml"
        apm_yml.write_text("name: test\n")
        mock_dep = MagicMock()
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
        mock_dep = MagicMock()
        mock_dep.alias = None
        mock_dep.is_virtual = False
        mock_dep.repo_url = "owner/direct-dep"
        mock_package = MagicMock()
        mock_package.get_apm_dependencies.return_value = [mock_dep]
        with (
            patch(
                "apm_cli.primitives.discovery.APMPackage.from_apm_yml",
                return_value=mock_package,
            ),
            patch(
                "apm_cli.primitives.discovery.LockFile.installed_paths_for_project",
                return_value=["owner/direct-dep", "owner/transitive-dep"],
            ),
        ):
            result = get_dependency_declaration_order(self.tmp)
        self.assertEqual(result, ["owner/direct-dep", "owner/transitive-dep"])


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


class TestShouldSkipDirectory(unittest.TestCase):
    """Tests for _should_skip_directory."""

    def test_git_dir_skipped(self):
        self.assertTrue(_should_skip_directory("/project/.git"))

    def test_node_modules_skipped(self):
        self.assertTrue(_should_skip_directory("/project/node_modules"))

    def test_pycache_skipped(self):
        self.assertTrue(_should_skip_directory("/project/__pycache__"))

    def test_pytest_cache_skipped(self):
        self.assertTrue(_should_skip_directory("/project/.pytest_cache"))

    def test_venv_skipped(self):
        self.assertTrue(_should_skip_directory("/project/.venv"))
        self.assertTrue(_should_skip_directory("/project/venv"))

    def test_build_skipped(self):
        self.assertTrue(_should_skip_directory("/project/build"))

    def test_normal_dir_not_skipped(self):
        self.assertFalse(_should_skip_directory("/project/src"))
        self.assertFalse(_should_skip_directory("/project/.apm"))
        self.assertFalse(_should_skip_directory("/project/tests"))


class TestIsUnderDirectory(unittest.TestCase):
    """Tests for _is_under_directory."""

    def test_file_under_directory_returns_true(self):
        self.assertTrue(
            _is_under_directory(
                Path("/project/apm_modules/owner/repo/file.md"),
                Path("/project/apm_modules"),
            )
        )

    def test_file_not_under_directory_returns_false(self):
        self.assertFalse(
            _is_under_directory(Path("/project/.apm/file.md"), Path("/project/apm_modules"))
        )


class TestFindPrimitiveFilesEdgeCases(unittest.TestCase):
    """Tests for find_primitive_files edge cases."""

    def test_nonexistent_directory_returns_empty(self):
        result = find_primitive_files("/nonexistent/path", ["**/*.chatmode.md"])
        self.assertEqual(result, [])

    def test_deduplicates_matched_files(self):
        """Multiple patterns matching same file should yield one result."""
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "test.chatmode.md", CHATMODE_CONTENT)
            # Both patterns should match the same file
            result = find_primitive_files(tmp, ["**/*.chatmode.md", "*.chatmode.md"])
            self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
