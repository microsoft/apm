"""Unit tests for ClaudeFormatter - CLAUDE.md generation."""

import pathlib
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.compilation.claude_formatter import (
    CLAUDE_HEADER,
    ClaudeCompilationResult,
    ClaudeFormatter,
    ClaudePlacement,
)
from apm_cli.compilation.constants import BUILD_ID_PLACEHOLDER
from apm_cli.primitives.models import Chatmode, Instruction, PrimitiveCollection
from apm_cli.version import get_version

pytestmark = pytest.mark.component


class TestClaudeFormatterInit:
    """Tests for ClaudeFormatter initialization."""

    def test_init_with_valid_directory(self, tmp_path):
        """Test initialization with a valid directory path."""
        formatter = ClaudeFormatter(str(tmp_path))
        assert formatter.base_dir == tmp_path
        assert formatter.warnings == []
        assert formatter.errors == []

    def test_init_with_current_directory(self):
        """Test initialization with default current directory."""
        formatter = ClaudeFormatter()
        assert formatter.base_dir.exists()

    def test_init_with_relative_path(self, tmp_path):
        """Test initialization with relative path."""
        formatter = ClaudeFormatter(".")
        assert formatter.base_dir.is_absolute()

    def test_init_falls_back_to_absolute_on_oserror(self):
        """When Path.resolve() raises OSError, __init__ uses absolute() as fallback."""
        with patch.object(pathlib.Path, "resolve", side_effect=OSError("resolve failed")):
            formatter = ClaudeFormatter(".")
        # base_dir must be absolute regardless of which code path was taken
        assert formatter.base_dir.is_absolute()
        assert formatter.warnings == []
        assert formatter.errors == []

    def test_init_falls_back_to_absolute_on_filenotfounderror(self):
        """When Path.resolve() raises FileNotFoundError, __init__ uses absolute() as fallback."""
        with patch.object(pathlib.Path, "resolve", side_effect=FileNotFoundError("not found")):
            formatter = ClaudeFormatter(".")
        assert formatter.base_dir.is_absolute()
        assert formatter.warnings == []
        assert formatter.errors == []


class TestFormatAgentsMdImports:
    """CLAUDE.md files import their sibling AGENTS.md (#3939)."""

    @pytest.fixture
    def project(self, tmp_path):
        return tmp_path.resolve()

    @pytest.fixture
    def primitives(self, project):
        collection = PrimitiveCollection()
        collection.add_primitive(
            Instruction(
                name="style",
                file_path=project / ".apm" / "instructions" / "style.instructions.md",
                description="Style",
                apply_to="**",
                content="Always be kind.",
                author="test",
            )
        )
        collection.add_primitive(
            Chatmode(
                name="reviewer",
                file_path=project / ".apm" / "agents" / "reviewer.agent.md",
                description="Reviewer",
                apply_to=None,
                content="Review agent body",
                author="test",
            )
        )
        return collection

    def test_root_shim_imports_agents_md(self, project, primitives):
        formatter = ClaudeFormatter(str(project))
        result = formatter.format_agents_md_imports(primitives, {project / "AGENTS.md"})

        content = result.content_map[project / "CLAUDE.md"]
        lines = content.splitlines()
        assert lines[:3] == ["# CLAUDE.md", CLAUDE_HEADER, BUILD_ID_PLACEHOLDER]
        assert "@AGENTS.md" in lines
        assert sum(1 for line in lines if line.startswith("# ")) == 1
        # Instructions and agents live in AGENTS.md / .claude/, never in CLAUDE.md.
        assert "Always be kind." not in content
        assert "Review agent body" not in content

    def test_nested_shim_next_to_each_nested_agents_md(self, project, primitives):
        nested = project / "src" / "test" / "AGENTS.md"
        formatter = ClaudeFormatter(str(project))
        result = formatter.format_agents_md_imports(primitives, {nested})

        assert set(result.content_map) == {nested.parent / "CLAUDE.md"}
        nested_content = result.content_map[nested.parent / "CLAUDE.md"]
        assert "@AGENTS.md" in nested_content.splitlines()
        assert "## Dependencies" not in nested_content

    def test_no_claude_md_without_agents_md_dependencies_or_constitution(self, project, primitives):
        result = ClaudeFormatter(str(project)).format_agents_md_imports(primitives, set())

        assert result.success
        assert result.content_map == {}

    def test_existing_root_agents_md_gets_imported(self, project, primitives):
        (project / "AGENTS.md").write_text("# Team AGENTS.md\n", encoding="utf-8")

        result = ClaudeFormatter(str(project)).format_agents_md_imports(primitives, set())

        assert "@AGENTS.md" in result.content_map[project / "CLAUDE.md"].splitlines()

    def test_constitution_only_when_root_agents_md_does_not_carry_it(self, project, primitives):
        memory = project / ".specify" / "memory"
        memory.mkdir(parents=True)
        (memory / "constitution.md").write_text("Ship small changes.", encoding="utf-8")
        formatter = ClaudeFormatter(str(project))

        carried = formatter.format_agents_md_imports(
            primitives, {project / "AGENTS.md"}, {"include_constitution": False}
        )
        fallback = formatter.format_agents_md_imports(
            primitives, set(), {"include_constitution": True}
        )

        assert "Ship small changes." not in carried.content_map[project / "CLAUDE.md"]
        assert "Ship small changes." in fallback.content_map[project / "CLAUDE.md"]

    def test_source_attribution_adds_cosmetics(self, project, primitives):
        formatter = ClaudeFormatter(str(project))
        plain = formatter.format_agents_md_imports(primitives, {project / "AGENTS.md"})
        attributed = formatter.format_agents_md_imports(
            primitives, {project / "AGENTS.md"}, {"source_attribution": True}
        )

        version = f"<!-- APM Version: {get_version()} -->"
        assert version not in plain.content_map[project / "CLAUDE.md"]
        assert version in attributed.content_map[project / "CLAUDE.md"]

    def test_formatting_error_is_reported(self, project, primitives):
        formatter = ClaudeFormatter(str(project))
        with patch.object(formatter, "_collect_dependencies", side_effect=OSError("boom")):
            result = formatter.format_agents_md_imports(primitives, set())

        assert not result.success
        assert result.content_map == {}
        assert any("boom" in error for error in result.errors)


class TestDependenciesImportSyntax:
    """Tests for @import syntax for dependencies."""

    @pytest.fixture
    def temp_project_with_deps(self):
        """Create a temporary project with apm_modules containing packages with and without CLAUDE.md."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir).resolve()

        # Create apm_modules structure
        apm_modules = temp_path / "apm_modules"
        # Packages WITH CLAUDE.md - should be included
        (apm_modules / "owner1" / "package1").mkdir(parents=True)
        (apm_modules / "owner1" / "package1" / "CLAUDE.md").write_text(
            "# Package 1", encoding="utf-8"
        )
        (apm_modules / "owner2" / "package2").mkdir(parents=True)
        (apm_modules / "owner2" / "package2" / "CLAUDE.md").write_text(
            "# Package 2", encoding="utf-8"
        )
        # Packages WITHOUT CLAUDE.md - should be excluded
        (apm_modules / "owner1" / "no-claude").mkdir(parents=True)
        (apm_modules / "owner1" / "no-claude" / "README.md").write_text(
            "# No Claude", encoding="utf-8"
        )
        (apm_modules / "owner3" / "skills-only").mkdir(parents=True)

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_dependencies_use_import_syntax(self, temp_project_with_deps):
        """Test that dependencies are listed with @import syntax."""
        formatter = ClaudeFormatter(str(temp_project_with_deps))
        primitives = PrimitiveCollection()

        instruction = Instruction(
            name="test",
            file_path=temp_project_with_deps / "test.instructions.md",
            description="Test",
            apply_to="**/*.py",
            content="Test content",
            author="test",
        )
        primitives.add_primitive(instruction)

        result = formatter.format_agents_md_imports(primitives, set())

        # Get content using the resolved path
        content = result.content_map[formatter.base_dir / "CLAUDE.md"]

        # Check @import syntax
        assert "@apm_modules/owner1/package1/CLAUDE.md" in content
        assert "@apm_modules/owner2/package2/CLAUDE.md" in content
        assert "## Dependencies" in content.splitlines()

    def test_dependencies_are_sorted(self, temp_project_with_deps):
        """Test that dependencies are sorted alphabetically."""
        formatter = ClaudeFormatter(str(temp_project_with_deps))
        deps = formatter._collect_dependencies()

        assert deps == sorted(deps)

    def test_no_dependencies_section_when_no_apm_modules(self, tmp_path):
        """Test that no Dependencies section when apm_modules doesn't exist."""
        formatter = ClaudeFormatter(str(tmp_path))
        deps = formatter._collect_dependencies()

        assert deps == []

    def test_skips_packages_without_claude_md(self, temp_project_with_deps):
        """Test that packages without a CLAUDE.md file are not listed as dependencies."""
        formatter = ClaudeFormatter(str(temp_project_with_deps))
        deps = formatter._collect_dependencies()

        assert "@apm_modules/owner1/no-claude/CLAUDE.md" not in deps
        assert "@apm_modules/owner3/skills-only/CLAUDE.md" not in deps

    def test_ado_three_level_dependencies_included(self, tmp_path):
        """Azure DevOps packages live at apm_modules/org/project/repo (issue #2951)."""
        (tmp_path / "apm.yml").write_text(
            "name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n"
            "    - dev.azure.com/contoso/platform/standards\n",
            encoding="utf-8",
        )
        ado_pkg = tmp_path / "apm_modules" / "contoso" / "platform" / "standards"
        ado_pkg.mkdir(parents=True)
        (ado_pkg / "CLAUDE.md").write_text("# ADO package", encoding="utf-8")
        # Depth-3 tree without CLAUDE.md must stay omitted.
        empty = tmp_path / "apm_modules" / "contoso" / "platform" / "empty-repo"
        empty.mkdir(parents=True)
        (empty / "README.md").write_text("no claude", encoding="utf-8")

        formatter = ClaudeFormatter(str(tmp_path))
        deps = formatter._collect_dependencies()

        assert "@apm_modules/contoso/platform/standards/CLAUDE.md" in deps
        assert "@apm_modules/contoso/platform/empty-repo/CLAUDE.md" not in deps

    def test_nested_claude_md_under_github_package_not_imported(self, tmp_path):
        """Do not treat nested docs/CLAUDE.md as a package root when mid has CLAUDE.md."""
        pkg = tmp_path / "apm_modules" / "owner" / "repo"
        pkg.mkdir(parents=True)
        (pkg / "CLAUDE.md").write_text("# Package root", encoding="utf-8")
        nested = pkg / "docs"
        nested.mkdir()
        (nested / "CLAUDE.md").write_text("# Nested docs", encoding="utf-8")

        formatter = ClaudeFormatter(str(tmp_path))
        deps = formatter._collect_dependencies()

        assert deps == ["@apm_modules/owner/repo/CLAUDE.md"]

    def test_mixed_github_and_ado_dependencies_sorted(self, tmp_path):
        """Two-level and three-level roots can coexist and stay sorted."""
        (tmp_path / "apm.yml").write_text(
            "name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n"
            "    - zeta/pkg\n    - dev.azure.com/alpha/proj/repo\n",
            encoding="utf-8",
        )
        gh = tmp_path / "apm_modules" / "zeta" / "pkg"
        gh.mkdir(parents=True)
        (gh / "CLAUDE.md").write_text("# gh", encoding="utf-8")
        ado = tmp_path / "apm_modules" / "alpha" / "proj" / "repo"
        ado.mkdir(parents=True)
        (ado / "CLAUDE.md").write_text("# ado", encoding="utf-8")

        formatter = ClaudeFormatter(str(tmp_path))
        deps = formatter._collect_dependencies()

        assert deps == [
            "@apm_modules/alpha/proj/repo/CLAUDE.md",
            "@apm_modules/zeta/pkg/CLAUDE.md",
        ]

    def test_ado_dependency_appears_in_distributed_output(self, tmp_path):
        """The root CLAUDE.md Dependencies section includes ADO three-level imports."""
        (tmp_path / "apm.yml").write_text(
            "name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n"
            "    - dev.azure.com/org/project/repo\n",
            encoding="utf-8",
        )
        ado_pkg = tmp_path / "apm_modules" / "org" / "project" / "repo"
        ado_pkg.mkdir(parents=True)
        (ado_pkg / "CLAUDE.md").write_text("# ADO", encoding="utf-8")

        formatter = ClaudeFormatter(str(tmp_path))
        primitives = PrimitiveCollection()
        instruction = Instruction(
            name="test",
            file_path=tmp_path / "test.instructions.md",
            description="Test",
            apply_to="**/*.py",
            content="Test content",
            author="test",
        )
        primitives.add_primitive(instruction)
        result = formatter.format_agents_md_imports(primitives, set())
        content = result.content_map[formatter.base_dir / "CLAUDE.md"]

        assert "## Dependencies" in content
        assert "@apm_modules/org/project/repo/CLAUDE.md" in content


class TestDataclasses:
    """Tests for dataclass structures."""

    def test_claude_placement_defaults(self):
        """Test ClaudePlacement default values."""
        placement = ClaudePlacement(claude_path=Path("test/CLAUDE.md"), instructions=[])

        assert placement.agents == []
        assert placement.dependencies == []
        assert placement.coverage_patterns == set()
        assert placement.source_attribution == {}

    def test_claude_compilation_result_defaults(self):
        """Test ClaudeCompilationResult default values."""
        result = ClaudeCompilationResult(success=True, placements=[], content_map={})

        assert result.warnings == []
        assert result.errors == []
        assert result.stats == {}
