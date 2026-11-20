"""Unit tests for Azure DevOps (ADO) path structure handling (filesystem, process and CLI boundary cases)."""

import tempfile
from pathlib import Path

import pytest

from apm_cli.primitives.discovery import (
    discover_primitives_with_dependencies,
    get_dependency_declaration_order,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise(
        "P-OWN-distributed-agents-output-preparation", "P-OWN-dependency-identity-materialization"
    ),
]


class TestADOPrimitiveDiscovery:
    """Test primitive discovery with ADO 3-level directory structure."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def _create_apm_yml(self, project_dir: Path, dependencies: list):
        """Create an apm.yml file with the specified dependencies."""
        import yaml

        content = {
            "name": "test-project",
            "version": "1.0.0",
            "dependencies": {"apm": dependencies},
        }

        apm_yml = project_dir / "apm.yml"
        with open(apm_yml, "w") as f:
            yaml.dump(content, f)

    def _create_instruction_file(self, file_path: Path, apply_to: str, content: str):
        """Create an instruction file with frontmatter."""
        file_path.parent.mkdir(parents=True, exist_ok=True)

        instruction_content = f"""---
applyTo: "{apply_to}"
description: "Test instruction"
---

{content}
"""
        file_path.write_text(instruction_content)

    def test_discovery_finds_github_2_level_deps(self, temp_project):
        """Test discovery finds primitives in GitHub 2-level structure."""
        # Create apm.yml with GitHub dependency
        self._create_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        # Create GitHub-style 2-level directory structure
        dep_path = (
            temp_project
            / "apm_modules"
            / "microsoft"
            / "apm-sample-package"
            / ".apm"
            / "instructions"
        )
        dep_path.mkdir(parents=True)
        self._create_instruction_file(
            dep_path / "style.instructions.md",
            "**/*.css",
            "# Design Guidelines\nFollow these styles.",
        )

        # Discover primitives
        collection = discover_primitives_with_dependencies(str(temp_project))

        # Should find the instruction
        assert len(collection.instructions) == 1
        assert collection.instructions[0].apply_to == "**/*.css"
        assert "dependency:microsoft/apm-sample-package" in collection.instructions[0].source

    def test_discovery_finds_ado_3_level_deps(self, temp_project):
        """Test discovery finds primitives in ADO 3-level structure."""
        # Create apm.yml with ADO dependency
        self._create_apm_yml(
            temp_project, ["dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules"]
        )

        # Create ADO-style 3-level directory structure
        dep_path = (
            temp_project
            / "apm_modules"
            / "dmeppiel-org"
            / "market-js-app"
            / "compliance-rules"
            / ".apm"
            / "instructions"
        )
        dep_path.mkdir(parents=True)
        self._create_instruction_file(
            dep_path / "compliance.instructions.md",
            "**/*.{py,js,ts}",
            "# Compliance Rules\nFollow GDPR requirements.",
        )

        # Discover primitives
        collection = discover_primitives_with_dependencies(str(temp_project))

        # Should find the instruction
        assert len(collection.instructions) == 1
        assert collection.instructions[0].apply_to == "**/*.{py,js,ts}"
        assert (
            "dependency:dmeppiel-org/market-js-app/compliance-rules"
            in collection.instructions[0].source
        )

    def test_discovery_mixed_github_and_ado_deps(self, temp_project):
        """Test discovery works with both GitHub and ADO dependencies."""
        # Create apm.yml with both types
        self._create_apm_yml(
            temp_project,
            [
                "microsoft/apm-sample-package",
                "dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules",
            ],
        )

        # Create GitHub 2-level structure
        github_path = (
            temp_project
            / "apm_modules"
            / "microsoft"
            / "apm-sample-package"
            / ".apm"
            / "instructions"
        )
        github_path.mkdir(parents=True)
        self._create_instruction_file(
            github_path / "style.instructions.md", "**/*.css", "# Design styles"
        )

        # Create ADO 3-level structure
        ado_path = (
            temp_project
            / "apm_modules"
            / "dmeppiel-org"
            / "market-js-app"
            / "compliance-rules"
            / ".apm"
            / "instructions"
        )
        ado_path.mkdir(parents=True)
        self._create_instruction_file(
            ado_path / "compliance.instructions.md", "**/*.py", "# Compliance rules"
        )

        # Discover primitives
        collection = discover_primitives_with_dependencies(str(temp_project))

        # Should find both instructions
        assert len(collection.instructions) == 2

        # Verify sources
        sources = [inst.source for inst in collection.instructions]
        assert any("microsoft/apm-sample-package" in s for s in sources)
        assert any("dmeppiel-org/market-js-app/compliance-rules" in s for s in sources)

    def test_get_dependency_order_returns_full_ado_path(self, temp_project):
        """Test that get_dependency_declaration_order returns full 3-part ADO paths."""
        self._create_apm_yml(
            temp_project, ["dev.azure.com/org1/proj1/_git/repo1", "acme/simple-repo"]
        )

        dep_order = get_dependency_declaration_order(str(temp_project))

        assert len(dep_order) == 2
        # ADO should have 3 parts
        assert dep_order[0] == "org1/proj1/repo1"
        # GitHub should have 2 parts
        assert dep_order[1] == "acme/simple-repo"


class TestADOCompilation:
    """Test that compilation works correctly with ADO dependencies."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory with source files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)

            # Create source files that match applyTo patterns
            src_dir = project / "src"
            src_dir.mkdir()
            (src_dir / "app.py").write_text("# Python app")
            (src_dir / "main.js").write_text("// JS main")

            yield project

    def _create_apm_yml(self, project_dir: Path, dependencies: list):
        """Create an apm.yml file."""
        import yaml

        content = {
            "name": "test-project",
            "version": "1.0.0",
            "dependencies": {"apm": dependencies},
        }

        apm_yml = project_dir / "apm.yml"
        with open(apm_yml, "w") as f:
            yaml.dump(content, f)

    def _create_instruction_file(self, file_path: Path, apply_to: str, content: str):
        """Create an instruction file with frontmatter."""
        file_path.parent.mkdir(parents=True, exist_ok=True)

        instruction_content = f"""---
applyTo: "{apply_to}"
description: "Test instruction"
---

{content}
"""
        file_path.write_text(instruction_content)

    def test_compile_generates_agents_md_from_ado_deps(self, temp_project):
        """Test that compile generates AGENTS.md with content from ADO dependencies."""
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig

        # Create apm.yml with ADO dependency
        self._create_apm_yml(temp_project, ["dev.azure.com/myorg/myproj/_git/compliance"])

        # Create ADO 3-level structure with instruction
        dep_path = (
            temp_project
            / "apm_modules"
            / "myorg"
            / "myproj"
            / "compliance"
            / ".apm"
            / "instructions"
        )
        dep_path.mkdir(parents=True)
        self._create_instruction_file(
            dep_path / "security.instructions.md",
            "**/*.py",
            "# Security Rules\nNever store passwords in plain text.",
        )

        # Compile
        config = CompilationConfig(
            strategy="distributed",
            dry_run=True,  # Don't write files
        )
        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config)

        # Should compile successfully and find the instruction
        assert result.success
        # Stats should show instructions found
        assert (
            result.stats.get("total_instructions_placed", 0) >= 1
            or result.stats.get("instructions", 0) >= 1
        )

    def test_compile_with_both_github_and_ado_deps(self, temp_project):
        """Test compilation with both GitHub and ADO dependencies."""
        from apm_cli.compilation.agents_compiler import (
            AgentsCompiler,  # noqa: F401
            CompilationConfig,  # noqa: F401
        )

        # Create apm.yml with both
        self._create_apm_yml(
            temp_project, ["owner/github-pkg", "dev.azure.com/org/proj/_git/ado-pkg"]
        )

        # GitHub 2-level
        github_path = (
            temp_project / "apm_modules" / "owner" / "github-pkg" / ".apm" / "instructions"
        )
        github_path.mkdir(parents=True)
        self._create_instruction_file(
            github_path / "github-rules.instructions.md", "**/*.js", "# GitHub Rules"
        )

        # ADO 3-level
        ado_path = (
            temp_project / "apm_modules" / "org" / "proj" / "ado-pkg" / ".apm" / "instructions"
        )
        ado_path.mkdir(parents=True)
        self._create_instruction_file(
            ado_path / "ado-rules.instructions.md", "**/*.py", "# ADO Rules"
        )

        # Discover primitives
        collection = discover_primitives_with_dependencies(str(temp_project))

        # Should find both instructions
        assert len(collection.instructions) == 2
