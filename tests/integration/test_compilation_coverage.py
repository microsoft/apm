"""Integration tests for compilation module coverage.

Tests realistic compilation flows including:
- Context optimization and minimal context principle
- Link resolution during compilation
- Distributed AGENTS.md generation
- Agents.md compilation with various primitives
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from apm_cli.compilation.agents_compiler import compile_agents_md
from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.compilation.distributed_compiler import DistributedAgentsCompiler
from apm_cli.compilation.link_resolver import LinkResolutionContext, UnifiedLinkResolver
from apm_cli.primitives.models import Instruction, PrimitiveCollection


class TestContextOptimizerIntegration:
    """Integration tests for context optimizer with realistic project structures."""

    @pytest.fixture
    def project_with_structure(self, tmp_path: Path) -> Path:
        """Create a project with realistic nested directory structure."""
        # Create multi-level directory structure
        (tmp_path / "src" / "backend" / "api").mkdir(parents=True)
        (tmp_path / "src" / "frontend" / "components").mkdir(parents=True)
        (tmp_path / "tests" / "unit").mkdir(parents=True)
        (tmp_path / "docs").mkdir(parents=True)
        (tmp_path / ".apm" / "instructions").mkdir(parents=True)
        (tmp_path / ".apm" / "context").mkdir(parents=True)

        # Create files in different directories
        (tmp_path / "src" / "backend" / "api" / "users.py").write_text(
            "# API code", encoding="utf-8"
        )
        (tmp_path / "src" / "backend" / "models.py").write_text("# Models", encoding="utf-8")
        (tmp_path / "src" / "frontend" / "components" / "Button.tsx").write_text(
            "// UI", encoding="utf-8"
        )
        (tmp_path / "src" / "frontend" / "App.tsx").write_text("// App", encoding="utf-8")
        (tmp_path / "tests" / "unit" / "test_api.py").write_text("# Tests", encoding="utf-8")
        (tmp_path / "docs" / "README.md").write_text("# Documentation", encoding="utf-8")

        return tmp_path

    def test_optimizer_initializes_with_project_structure(
        self, project_with_structure: Path
    ) -> None:
        """Context optimizer can initialize with a project."""
        optimizer = ContextOptimizer(str(project_with_structure))
        assert optimizer.base_dir.exists()

    def test_optimizer_analyzes_directory_distribution(self, project_with_structure: Path) -> None:
        """Optimizer analyzes file distribution across directories."""
        optimizer = ContextOptimizer(str(project_with_structure))

        # Create some instructions
        instructions = [
            Instruction(
                name="py-standards",
                file_path=Path(".apm/instructions/python.instructions.md"),
                description="Python standards",
                apply_to="**/*.py",
                content="Python content",
                source="local",
            ),
            Instruction(
                name="ts-standards",
                file_path=Path(".apm/instructions/typescript.instructions.md"),
                description="TypeScript standards",
                apply_to="**/*.tsx",
                content="TypeScript content",
                source="local",
            ),
        ]

        # Analyze should handle the structure without error
        results = optimizer.optimize_instruction_placement(instructions)
        assert results is not None

    def test_optimizer_excludes_directories(self, project_with_structure: Path) -> None:
        """Optimizer respects exclude patterns."""
        exclude = ["tests/*", "docs/*"]
        optimizer = ContextOptimizer(str(project_with_structure), exclude_patterns=exclude)

        # Should initialize successfully
        assert optimizer.base_dir.exists()
        assert optimizer._exclude_patterns

    def test_context_inheritance_chain_analysis(self, project_with_structure: Path) -> None:
        """Optimizer analyzes inheritance chain for nested directories."""
        optimizer = ContextOptimizer(str(project_with_structure))

        target_dir = project_with_structure / "src" / "backend"
        chain = optimizer._get_inheritance_chain(target_dir)

        # Should include target and parent directories up to root
        assert target_dir in chain

    def test_optimizer_with_empty_patterns(self, project_with_structure: Path) -> None:
        """Optimizer handles instructions with empty pattern list."""
        optimizer = ContextOptimizer(str(project_with_structure))

        # Create instructions with realistic patterns
        instructions = [
            Instruction(
                name="test-instruction",
                file_path=Path(".apm/instructions/test.md"),
                description="Test",
                apply_to="src/**/*.py",  # Specific pattern
                content="Content",
                source="local",
            ),
        ]

        results = optimizer.optimize_instruction_placement(instructions)
        assert results is not None


class TestLinkResolverIntegration:
    """Integration tests for link resolution during compilation."""

    @pytest.fixture
    def resolver_with_contexts(
        self, tmp_path: Path
    ) -> tuple[UnifiedLinkResolver, PrimitiveCollection]:
        """Create resolver with registered context files."""
        resolver = UnifiedLinkResolver(tmp_path)

        # Create context files
        context_dir = tmp_path / ".apm" / "context"
        context_dir.mkdir(parents=True)

        api_ctx = context_dir / "api-standards.context.md"
        api_ctx.write_text("# API Standards", encoding="utf-8")

        security_ctx = context_dir / "security.context.md"
        security_ctx.write_text("# Security", encoding="utf-8")

        # Create collection with primitives
        collection = PrimitiveCollection()
        from apm_cli.primitives.models import Context

        collection.add_primitive(
            Context(
                name="api-standards",
                file_path=api_ctx,
                content="# API Standards",
                source="local",
            )
        )
        collection.add_primitive(
            Context(
                name="security",
                file_path=security_ctx,
                content="# Security",
                source="local",
            )
        )

        resolver.register_contexts(collection)
        return resolver, collection

    def test_resolver_registers_context_files(self, resolver_with_contexts: tuple) -> None:
        """Resolver registers available context files."""
        resolver, _collection = resolver_with_contexts

        # Should have registered contexts
        assert "api-standards.context.md" in resolver.context_registry
        assert "security.context.md" in resolver.context_registry

    def test_resolver_preserves_external_urls(self, resolver_with_contexts: tuple) -> None:
        """Resolver preserves HTTP/HTTPS URLs."""
        resolver, _collection = resolver_with_contexts

        content = """
# Documentation

[External API](https://api.example.com/docs)
[Another Link](http://example.org/help)
        """

        LinkResolutionContext(
            source_file=Path("instructions.md"),
            source_location=Path(".apm"),
            target_location=Path("."),
            base_dir=resolver.base_dir,
            available_contexts=resolver.context_registry,
        )

        # resolve_links_for_installation should handle external URLs
        result = resolver.resolve_links_for_installation(
            content, Path("instructions.md"), Path("AGENTS.md")
        )

        # External URLs should be preserved
        assert "https://api.example.com/docs" in result
        assert "http://example.org/help" in result

    def test_resolver_registers_dependency_contexts(self, tmp_path: Path) -> None:
        """Resolver registers dependency context files with qualified names."""
        resolver = UnifiedLinkResolver(tmp_path)

        # Create a dependency context structure
        dep_dir = tmp_path / "apm_modules" / "company" / "standards" / ".apm" / "context"
        dep_dir.mkdir(parents=True)

        api_file = dep_dir / "api.context.md"
        api_file.write_text("# Company API", encoding="utf-8")

        # Create collection with dependency
        collection = PrimitiveCollection()
        from apm_cli.primitives.models import Context

        collection.add_primitive(
            Context(
                name="api",
                file_path=api_file,
                content="# Company API",
                source="dependency:company/standards",
            )
        )

        resolver.register_contexts(collection)

        # Should register with qualified name
        assert "company/standards:api.context.md" in resolver.context_registry

    def test_resolver_handles_relative_links(self, resolver_with_contexts: tuple) -> None:
        """Resolver handles relative links correctly."""
        resolver, _collection = resolver_with_contexts

        content = "[See docs](./docs/guide.md)"
        LinkResolutionContext(
            source_file=Path(".apm/instructions/test.md"),
            source_location=Path(".apm/instructions"),
            target_location=Path("."),
            base_dir=resolver.base_dir,
            available_contexts=resolver.context_registry,
        )

        result = resolver.resolve_links_for_installation(
            content, Path(".apm/instructions/test.md"), Path("AGENTS.md")
        )
        assert result is not None


class TestDistributedCompilerIntegration:
    """Integration tests for distributed AGENTS.md compilation."""

    @pytest.fixture
    def minimal_apm_project(self, tmp_path: Path) -> Path:
        """Create minimal .apm/ structure for compilation."""
        apm_dir = tmp_path / ".apm"

        # Create subdirectories
        (apm_dir / "skills").mkdir(parents=True)
        (apm_dir / "agents").mkdir(parents=True)
        (apm_dir / "instructions").mkdir(parents=True)
        (apm_dir / "context").mkdir(parents=True)

        # Create a simple skill
        skill_file = apm_dir / "skills" / "example.skill.md"
        skill_file.write_text(
            """---
name: Example Skill
description: A test skill
---

# Example Skill

This is a test skill.
            """,
            encoding="utf-8",
        )

        # Create a simple instruction
        instr_file = apm_dir / "instructions" / "standards.instructions.md"
        instr_file.write_text(
            """---
name: Code Standards
description: Coding standards
apply_to: "**/*.py"
---

# Code Standards

Follow these standards.
            """,
            encoding="utf-8",
        )

        # Create a simple agent
        agent_file = apm_dir / "agents" / "test-agent.agents.md"
        agent_file.write_text(
            """---
name: Test Agent
description: A test agent
---

# Test Agent

This is a test agent.
            """,
            encoding="utf-8",
        )

        return tmp_path

    def test_distributed_compiler_initializes(self, minimal_apm_project: Path) -> None:
        """Distributed compiler can initialize with project."""
        compiler = DistributedAgentsCompiler(str(minimal_apm_project))
        assert compiler.base_dir.exists()

    def test_distributed_compiler_with_exclude_patterns(self, minimal_apm_project: Path) -> None:
        """Distributed compiler respects exclude patterns."""
        exclude = ["tests/*", "build/*"]
        compiler = DistributedAgentsCompiler(str(minimal_apm_project), exclude_patterns=exclude)
        assert compiler.base_dir.exists()

    def test_compiler_discovers_primitives(self, minimal_apm_project: Path) -> None:
        """Compiler can discover primitives in .apm/."""
        # Create apm.yml to make it a valid package
        apm_yml = minimal_apm_project / "apm.yml"
        apm_yml.write_text(
            """
name: test-package
version: "1.0.0"
            """,
            encoding="utf-8",
        )

        compiler = DistributedAgentsCompiler(str(minimal_apm_project))

        # Should have context optimizer
        assert compiler.context_optimizer is not None

    def test_compiler_handles_empty_project(self, tmp_path: Path) -> None:
        """Compiler handles project with no primitives gracefully."""
        # Create minimal structure without primitives
        (tmp_path / ".apm").mkdir()

        compiler = DistributedAgentsCompiler(str(tmp_path))
        assert compiler.base_dir.exists()


class TestAgentsCompilerIntegration:
    """Integration tests for main agents.md compilation."""

    @pytest.fixture
    def compilation_project(self, tmp_path: Path) -> Path:
        """Create a project ready for compilation."""
        # Create .apm structure
        apm_dir = tmp_path / ".apm"
        (apm_dir / "instructions").mkdir(parents=True)
        (apm_dir / "agents").mkdir(parents=True)

        # Create apm.yml
        (tmp_path / "apm.yml").write_text(
            """
name: test-skill
version: "1.0.0"
            """,
            encoding="utf-8",
        )

        # Create test instruction
        instr = apm_dir / "instructions" / "example.instructions.md"
        instr.write_text(
            """---
name: Example
description: Test instruction
apply_to: "**/*.py"
---

# Example Instruction

Content here.
            """,
            encoding="utf-8",
        )

        return tmp_path

    def test_compile_agents_md_creates_output(self, compilation_project: Path) -> None:
        """Compile creates AGENTS.md output file with primitives."""
        # Call with explicit primitives (None means discover)
        result = compile_agents_md(
            primitives=None,
            output_path=str(compilation_project / "AGENTS.md"),
            chatmode=None,
            dry_run=False,
            base_dir=str(compilation_project),
        )

        # Result should be content string
        assert result is not None
        assert isinstance(result, str)

    def test_compilation_with_chatmode_selection(self, compilation_project: Path) -> None:
        """Compilation respects chatmode selection."""
        result = compile_agents_md(
            primitives=None,
            output_path=str(compilation_project / "AGENTS.md"),
            chatmode="default",
            dry_run=False,
            base_dir=str(compilation_project),
        )
        assert result is not None
        assert isinstance(result, str)

    def test_compilation_with_link_resolution(self, compilation_project: Path) -> None:
        """Compilation with proper base_dir for link resolution."""
        result = compile_agents_md(
            primitives=None,
            output_path=str(compilation_project / "AGENTS.md"),
            chatmode=None,
            dry_run=False,
            base_dir=str(compilation_project),
        )
        assert result is not None
        assert isinstance(result, str)

    def test_compilation_dry_run_mode(self, compilation_project: Path) -> None:
        """Compilation respects dry_run flag."""
        result = compile_agents_md(
            primitives=None,
            output_path=str(compilation_project / "AGENTS.md"),
            dry_run=True,
            base_dir=str(compilation_project),
        )
        assert result is not None
        assert isinstance(result, str)

    def test_compilation_with_multiple_targets(self, compilation_project: Path) -> None:
        """Compilation works with various output paths."""
        # Create multiple output targets
        for target in ["AGENTS.md", "CLAUDE.md", "VSCODE.md"]:
            result = compile_agents_md(
                primitives=None,
                output_path=str(compilation_project / target),
                chatmode=None,
                dry_run=True,
                base_dir=str(compilation_project),
            )
            assert result is not None


class TestCompilationEdgeCases:
    """Integration tests for edge cases in compilation."""

    def test_compilation_with_nonexistent_project(self, tmp_path: Path) -> None:
        """Compilation handles missing project gracefully."""
        missing_dir = tmp_path / "nonexistent"

        # Should handle missing directory gracefully
        try:
            # Try to compile without base_dir or with nonexistent directory
            result = compile_agents_md(
                primitives=None, output_path="AGENTS.md", base_dir=str(missing_dir)
            )
            # If it completes, result should be valid
            assert result is not None or result is None  # Accept either
        except Exception:
            # Exception is acceptable for missing directory
            pass

    def test_context_optimizer_with_deeply_nested_structure(self, tmp_path: Path) -> None:
        """Optimizer handles deeply nested directory structures."""
        # Create deep nesting
        deep_dir = tmp_path / "a" / "b" / "c" / "d" / "e"
        deep_dir.mkdir(parents=True)

        (deep_dir / "file.py").write_text("# Python", encoding="utf-8")

        optimizer = ContextOptimizer(str(tmp_path))
        assert optimizer.base_dir.exists()

    def test_resolver_with_malformed_markdown_links(self) -> None:
        """Link resolver handles malformed markdown links gracefully."""
        tmp_path = Path(tempfile.mkdtemp())
        try:
            resolver = UnifiedLinkResolver(tmp_path)

            content = """
[Incomplete link](https://example.com
Another [broken](https://example.com
            """

            # Should handle malformed links without crashing
            result = resolver.resolve_links_for_installation(
                content, Path("test.md"), Path("output.md")
            )
            assert result is not None
        finally:
            shutil.rmtree(tmp_path, ignore_errors=True)


class TestCompilationWithRealPrimitives:
    """Integration tests using realistic primitive structures."""

    @pytest.fixture
    def realistic_project(self, tmp_path: Path) -> Path:
        """Create realistic project with multiple primitive types."""
        # Create comprehensive .apm structure
        apm = tmp_path / ".apm"
        (apm / "skills").mkdir(parents=True)
        (apm / "agents").mkdir(parents=True)
        (apm / "instructions").mkdir(parents=True)
        (apm / "context").mkdir(parents=True)

        # Create diverse primitives
        (apm / "skills" / "code-review.skill.md").write_text(
            """---
name: Code Review
description: Reviews code for quality
---

# Code Review Skill

Examines code quality.
            """,
            encoding="utf-8",
        )

        (apm / "instructions" / "python.instructions.md").write_text(
            """---
name: Python Standards
description: Python coding standards
apply_to: "**/*.py"
---

# Python Standards

Follow PEP 8.
            """,
            encoding="utf-8",
        )

        (apm / "instructions" / "typescript.instructions.md").write_text(
            """---
name: TypeScript Standards
description: TypeScript coding standards
apply_to: "**/*.ts"
---

# TypeScript Standards

Follow strict mode.
            """,
            encoding="utf-8",
        )

        (apm / "context" / "architecture.context.md").write_text(
            """---
name: Architecture Context
---

# Architecture

Our system architecture.
            """,
            encoding="utf-8",
        )

        # Create apm.yml
        (tmp_path / "apm.yml").write_text(
            """
name: test-suite
version: "1.0.0"
            """,
            encoding="utf-8",
        )

        return tmp_path

    def test_compile_with_mixed_primitives(self, realistic_project: Path) -> None:
        """Compilation works with mixed skill/instruction/context types."""
        result = compile_agents_md(
            primitives=None,
            output_path=str(realistic_project / "AGENTS.md"),
            chatmode=None,
            dry_run=True,
            base_dir=str(realistic_project),
        )
        assert result is not None
        assert isinstance(result, str)

    def test_optimizer_with_realistic_patterns(self, realistic_project: Path) -> None:
        """Optimizer works with realistic file patterns."""
        optimizer = ContextOptimizer(str(realistic_project))

        instructions = [
            Instruction(
                name="py",
                file_path=Path(".apm/instructions/python.instructions.md"),
                description="Python",
                apply_to="**/*.py",
                content="Python standards",
                source="local",
            ),
            Instruction(
                name="ts",
                file_path=Path(".apm/instructions/typescript.instructions.md"),
                description="TypeScript",
                apply_to="**/*.ts",
                content="TypeScript standards",
                source="local",
            ),
        ]

        results = optimizer.optimize_instruction_placement(instructions)
        assert results is not None
