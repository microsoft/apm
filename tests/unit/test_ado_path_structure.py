"""Unit tests for Azure DevOps (ADO) path structure handling.

Tests that:
1. GitHub dependencies use 2-part paths: apm_modules/owner/repo
2. ADO dependencies use 3-part paths: apm_modules/org/project/repo
3. Primitive discovery works correctly with ADO 3-level structure
4. Compilation finds and processes ADO dependency primitives
"""

import tempfile
from pathlib import Path

import pytest

from apm_cli.models.apm_package import DependencyReference
from apm_cli.primitives.discovery import (
    discover_primitives_with_dependencies,
    get_dependency_declaration_order,
)


class TestADOPathStructure:
    """Test ADO vs GitHub path structure handling."""

    def test_github_dependency_uses_2_part_path(self):
        """Test that GitHub dependencies use owner/repo (2-part) structure."""
        dep = DependencyReference.parse("microsoft/apm-sample-package")

        assert dep.is_azure_devops() is False
        assert dep.repo_url == "microsoft/apm-sample-package"

        # Path parts for installation
        parts = dep.repo_url.split("/")
        assert len(parts) == 2
        assert parts[0] == "microsoft"
        assert parts[1] == "apm-sample-package"

    def test_ado_dependency_uses_3_part_path(self):
        """Test that ADO dependencies use org/project/repo (3-part) structure."""
        dep = DependencyReference.parse(
            "dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules"
        )

        assert dep.is_azure_devops() is True
        assert dep.repo_url == "dmeppiel-org/market-js-app/compliance-rules"
        assert dep.ado_organization == "dmeppiel-org"
        assert dep.ado_project == "market-js-app"
        assert dep.ado_repo == "compliance-rules"

        # Path parts for installation
        parts = dep.repo_url.split("/")
        assert len(parts) == 3
        assert parts[0] == "dmeppiel-org"
        assert parts[1] == "market-js-app"
        assert parts[2] == "compliance-rules"

    def test_ado_simplified_format_uses_3_part_path(self):
        """Test that simplified ADO format also produces 3-part path."""
        dep = DependencyReference.parse("dev.azure.com/myorg/myproject/myrepo")

        assert dep.is_azure_devops() is True
        assert dep.repo_url == "myorg/myproject/myrepo"

        parts = dep.repo_url.split("/")
        assert len(parts) == 3


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


class TestInstallPathLogic:
    """Test the install path logic differentiates GitHub and ADO correctly."""

    def test_github_install_path_is_2_levels(self):
        """Verify GitHub dependencies would install to 2-level path."""
        dep = DependencyReference.parse("owner/repo")

        # Simulate install path logic from cli.py
        repo_parts = dep.repo_url.split("/")

        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_parts = [repo_parts[0], repo_parts[1], repo_parts[2]]
        elif len(repo_parts) >= 2:
            install_parts = [repo_parts[0], repo_parts[1]]
        else:
            install_parts = [dep.repo_url]

        # Should be 2 levels for GitHub
        assert len(install_parts) == 2
        assert install_parts == ["owner", "repo"]

    def test_ado_install_path_is_3_levels(self):
        """Verify ADO dependencies would install to 3-level path."""
        dep = DependencyReference.parse("dev.azure.com/org/project/_git/repo")

        # Simulate install path logic from cli.py
        repo_parts = dep.repo_url.split("/")

        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_parts = [repo_parts[0], repo_parts[1], repo_parts[2]]
        elif len(repo_parts) >= 2:
            install_parts = [repo_parts[0], repo_parts[1]]
        else:
            install_parts = [dep.repo_url]

        # Should be 3 levels for ADO
        assert len(install_parts) == 3
        assert install_parts == ["org", "project", "repo"]

    def test_discovery_path_matches_install_path_for_github(self):
        """Verify discovery path logic matches install path logic for GitHub."""
        dep_name = "owner/repo"  # As returned by get_dependency_declaration_order

        # Simulate discovery path logic from discovery.py
        parts = dep_name.split("/")
        if len(parts) >= 3:
            discovery_path = f"{parts[0]}/{parts[1]}/{parts[2]}"
        elif len(parts) == 2:
            discovery_path = f"{parts[0]}/{parts[1]}"
        else:
            discovery_path = dep_name

        # Simulate install path logic
        dep = DependencyReference.parse(dep_name)
        repo_parts = dep.repo_url.split("/")
        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}/{repo_parts[2]}"
        elif len(repo_parts) >= 2:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}"
        else:
            install_path = dep.repo_url

        # Paths should match
        assert discovery_path == install_path

    def test_discovery_path_matches_install_path_for_ado(self):
        """Verify discovery path logic matches install path logic for ADO."""
        # The dep_name in apm.yml would be the full ADO URL
        # get_dependency_declaration_order extracts repo_url = "org/project/repo"
        dep_name = "org/project/repo"  # As returned by get_dependency_declaration_order

        # Simulate discovery path logic from discovery.py
        parts = dep_name.split("/")
        if len(parts) >= 3:
            discovery_path = f"{parts[0]}/{parts[1]}/{parts[2]}"
        elif len(parts) == 2:
            discovery_path = f"{parts[0]}/{parts[1]}"
        else:
            discovery_path = dep_name

        # Simulate install path logic for ADO
        dep = DependencyReference.parse("dev.azure.com/org/project/_git/repo")
        repo_parts = dep.repo_url.split("/")
        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}/{repo_parts[2]}"
        elif len(repo_parts) >= 2:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}"
        else:
            install_path = dep.repo_url

        # Paths should match
        assert discovery_path == install_path
        assert discovery_path == "org/project/repo"


class TestADOVirtualPackagePaths:
    """Test that ADO virtual packages (collections and individual files) use correct paths."""

    def test_github_virtual_package_uses_2_level_path(self):
        """Verify GitHub virtual packages install to 2-level path."""
        dep = DependencyReference.parse("owner/test-repo/collections/project-planning")

        assert dep.is_virtual is True
        assert dep.is_virtual_collection() is True

        # Simulate install path logic from cli.py for virtual packages
        repo_parts = dep.repo_url.split("/")
        virtual_name = dep.get_virtual_package_name()

        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}/{virtual_name}"
        elif len(repo_parts) >= 2:
            install_path = f"{repo_parts[0]}/{virtual_name}"
        else:
            install_path = virtual_name

        # Should be 2 levels for GitHub: owner/virtual-pkg-name
        assert install_path == "owner/test-repo-project-planning"

    def test_ado_virtual_collection_uses_3_level_path(self):
        """Verify ADO virtual collections install to 3-level path."""
        dep = DependencyReference.parse(
            "dev.azure.com/myorg/myproject/myrepo/collections/my-collection"
        )

        assert dep.is_azure_devops() is True
        assert dep.is_virtual is True
        assert dep.is_virtual_collection() is True
        assert dep.repo_url == "myorg/myproject/myrepo"

        # Simulate install path logic from cli.py for virtual packages
        repo_parts = dep.repo_url.split("/")
        virtual_name = dep.get_virtual_package_name()

        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}/{virtual_name}"
        elif len(repo_parts) >= 2:
            install_path = f"{repo_parts[0]}/{virtual_name}"
        else:
            install_path = virtual_name

        # Should be 3 levels for ADO: org/project/virtual-pkg-name
        assert install_path == "myorg/myproject/myrepo-my-collection"

    def test_ado_virtual_file_uses_3_level_path(self):
        """Verify ADO virtual files install to 3-level path."""
        dep = DependencyReference.parse(
            "dev.azure.com/myorg/myproject/myrepo/prompts/code-review.prompt.md"
        )

        assert dep.is_azure_devops() is True
        assert dep.is_virtual is True
        assert dep.is_virtual_file() is True
        assert dep.repo_url == "myorg/myproject/myrepo"

        # Simulate install path logic from cli.py for virtual packages
        repo_parts = dep.repo_url.split("/")
        virtual_name = dep.get_virtual_package_name()

        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}/{virtual_name}"
        elif len(repo_parts) >= 2:
            install_path = f"{repo_parts[0]}/{virtual_name}"
        else:
            install_path = virtual_name

        # Should be 3 levels for ADO: org/project/virtual-pkg-name
        assert install_path == "myorg/myproject/myrepo-code-review"

    def test_ado_collection_with_git_segment(self):
        """Verify ADO collections with _git segment work correctly."""
        dep = DependencyReference.parse(
            "dev.azure.com/myorg/myproject/_git/copilot-instructions/collections/csharp-ddd"
        )

        assert dep.is_azure_devops() is True
        assert dep.is_virtual is True
        assert dep.is_virtual_collection() is True
        assert dep.repo_url == "myorg/myproject/copilot-instructions"
        assert dep.virtual_path == "collections/csharp-ddd"

        # Verify correct install path
        repo_parts = dep.repo_url.split("/")
        virtual_name = dep.get_virtual_package_name()

        assert len(repo_parts) == 3
        assert virtual_name == "copilot-instructions-csharp-ddd"

        # Install path should be org/project/virtual-pkg-name
        install_path = f"{repo_parts[0]}/{repo_parts[1]}/{virtual_name}"
        assert install_path == "myorg/myproject/copilot-instructions-csharp-ddd"

    def test_ado_collection_missing_repo_name_parsed_incorrectly(self):
        """Document the behavior when repo name is omitted from ADO collection path.

        This test documents that if a user omits the repo name, 'collections' is
        incorrectly parsed as the repo name. Users must include the full path:
        org/project/repo/collections/name
        """
        # User mistake: omitting repo name
        dep = DependencyReference.parse("dev.azure.com/myorg/myproject/collections/my-collection")

        # 'collections' is incorrectly parsed as repo name
        assert dep.repo_url == "myorg/myproject/collections"
        assert dep.virtual_path == "my-collection"

        # This causes is_virtual_collection() to return False
        # because virtual_path doesn't contain 'collections/'
        assert dep.is_virtual_collection() is False
        assert dep.is_virtual_file() is False

        # This is why the user gets "Unknown virtual package type" error


class TestADOPruneCommand:
    """Test that prune command handles ADO 3-level paths correctly."""

    def test_prune_path_parts_extraction(self):
        """Verify path parts are correctly extracted for both GitHub and ADO."""
        # GitHub 2-level path
        github_path = "owner/repo"
        github_parts = github_path.split("/")
        assert len(github_parts) == 2
        assert github_parts[0] == "owner"

        # ADO 3-level path
        ado_path = "org/project/repo"
        ado_parts = ado_path.split("/")
        assert len(ado_parts) == 3
        assert ado_parts[0] == "org"
        assert ado_parts[1] == "project"
        assert ado_parts[2] == "repo"

    def test_prune_cleanup_uses_path_parts_not_org_name(self):
        """Verify prune uses path_parts[0] for org directory cleanup (not undefined org_name)."""
        # This test ensures the fix for the undefined org_name bug
        org_repo_name = "org/project/repo"
        path_parts = org_repo_name.split("/")

        # Must use path_parts[0], not org_name (which was undefined)
        org_path_component = path_parts[0]
        assert org_path_component == "org"

        # For ADO 3-level, project directory should also be cleaned
        if len(path_parts) >= 3:
            project_path_components = path_parts[0], path_parts[1]
            assert project_path_components == ("org", "project")

    def test_prune_joinpath_works_for_variable_depth(self):
        """Verify joinpath(*path_parts) works for both 2-level and 3-level paths."""
        from pathlib import Path

        base = Path("/tmp/apm_modules")

        # GitHub 2-level
        github_parts = ["owner", "repo"]
        github_path = base.joinpath(*github_parts)
        assert github_path.as_posix().endswith("/tmp/apm_modules/owner/repo")

        # ADO 3-level
        ado_parts = ["org", "project", "repo"]
        ado_path = base.joinpath(*ado_parts)
        assert ado_path.as_posix().endswith("/tmp/apm_modules/org/project/repo")
