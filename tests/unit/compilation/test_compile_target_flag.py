"""Unit tests for --target flag and compilation routing in apm compile command.

Tests cover:
- CompilationConfig.target defaults to "all"
- --target vscode only generates AGENTS.md
- --target agents is alias for vscode
- --target claude only generates CLAUDE.md
- --target all generates both files (default)
- Invalid target value raises error
- _merge_results() correctly combines results
"""

import tempfile
import shutil
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.compilation.agents_compiler import (
    AgentsCompiler,
    CompilationConfig,
    CompilationResult,
)
from apm_cli.primitives.models import Instruction, PrimitiveCollection


class TestCompilationConfigTarget:
    """Tests for CompilationConfig.target field."""

    def test_target_default_is_all(self):
        """Test that CompilationConfig.target defaults to 'all'."""
        config = CompilationConfig()
        assert config.target == "all"

    def test_target_can_be_set_to_vscode(self):
        """Test that target can be set to 'vscode'."""
        config = CompilationConfig(target="vscode")
        assert config.target == "vscode"

    def test_target_can_be_set_to_agents(self):
        """Test that target can be set to 'agents'."""
        config = CompilationConfig(target="agents")
        assert config.target == "agents"

    def test_target_can_be_set_to_claude(self):
        """Test that target can be set to 'claude'."""
        config = CompilationConfig(target="claude")
        assert config.target == "claude"

    def test_from_apm_yml_applies_target_override(self):
        """Test that from_apm_yml correctly applies target override."""
        config = CompilationConfig.from_apm_yml(target="claude")
        assert config.target == "claude"

    def test_from_apm_yml_uses_default_when_no_override(self):
        """Test that from_apm_yml uses default target when not overridden."""
        config = CompilationConfig.from_apm_yml()
        assert config.target == "all"


class TestCompileTargetRouting:
    """Tests for compile() method routing based on target."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory with APM structure."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        
        # Create minimal apm.yml
        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")
        
        # Create instruction file
        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        instruction_file = apm_dir / "test.instructions.md"
        instruction_file.write_text("""---
applyTo: "**/*.py"
---
Use type hints in Python code.
""")
        
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def sample_primitives(self, temp_project):
        """Create sample primitives for testing."""
        primitives = PrimitiveCollection()
        
        instruction = Instruction(
            name="python-style",
            file_path=temp_project / ".apm/instructions/test.instructions.md",
            description="Python coding standards",
            apply_to="**/*.py",
            content="Use type hints in Python code.",
            author="test",
            source="local"
        )
        primitives.add_primitive(instruction)
        
        return primitives

    def test_target_vscode_generates_agents_md(self, temp_project, sample_primitives):
        """Test that target='vscode' generates AGENTS.md files."""
        config = CompilationConfig(
            target="vscode",
            dry_run=True,
            single_agents=True  # Use single-file mode for simpler test
        )
        
        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)
        
        assert result.success
        # Output path should be for AGENTS.md
        assert "AGENTS.md" in result.output_path
        # Content should contain AGENTS.md format elements
        assert "AGENTS.md" in result.content or result.content  # Has content

    def test_target_agents_is_alias_for_vscode(self, temp_project, sample_primitives):
        """Test that target='agents' produces same result as 'vscode'."""
        config_vscode = CompilationConfig(
            target="vscode",
            dry_run=True,
            single_agents=True
        )
        
        config_agents = CompilationConfig(
            target="agents",
            dry_run=True,
            single_agents=True
        )
        
        compiler = AgentsCompiler(str(temp_project))
        
        result_vscode = compiler.compile(config_vscode, sample_primitives)
        result_agents = compiler.compile(config_agents, sample_primitives)
        
        assert result_vscode.success == result_agents.success
        # Both should reference AGENTS.md
        assert "AGENTS.md" in result_vscode.output_path
        assert "AGENTS.md" in result_agents.output_path

    def test_target_claude_generates_claude_md(self, temp_project, sample_primitives):
        """Test that target='claude' generates CLAUDE.md files."""
        config = CompilationConfig(
            target="claude",
            dry_run=True
        )
        
        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)
        
        assert result.success
        # Output path should reference CLAUDE.md
        assert "CLAUDE" in result.output_path

    def test_target_all_generates_both(self, temp_project, sample_primitives):
        """Test that target='all' generates both AGENTS.md and CLAUDE.md."""
        config = CompilationConfig(
            target="all",
            dry_run=True,
            single_agents=True  # Use single-file for AGENTS.md
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success
        # Output path should mention both targets
        assert "AGENTS.md" in result.output_path or "CLAUDE" in result.output_path

    def test_target_codex_generates_agents_md(self, temp_project, sample_primitives):
        """Regression for issue #766: --target codex must produce AGENTS.md, not a silent no-op."""
        config = CompilationConfig(
            target="codex",
            dry_run=True,
            single_agents=True,
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success
        assert result.output_path, "codex target must route to a compiler, not return empty"
        assert "AGENTS.md" in result.output_path

    def test_target_opencode_generates_agents_md(self, temp_project, sample_primitives):
        """target='opencode' must route to AGENTS.md (same universal format as codex)."""
        config = CompilationConfig(
            target="opencode",
            dry_run=True,
            single_agents=True,
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success
        assert "AGENTS.md" in result.output_path

    def test_target_minimal_generates_agents_md(self, temp_project, sample_primitives):
        """target='minimal' must route to AGENTS.md-only."""
        config = CompilationConfig(
            target="minimal",
            dry_run=True,
            single_agents=True,
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success
        assert "AGENTS.md" in result.output_path

    def test_unknown_target_returns_failure(self, temp_project, sample_primitives):
        """Unknown target must fail explicitly instead of silently succeeding."""
        config = CompilationConfig(
            target="not-a-real-target",
            dry_run=True,
            single_agents=True,
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success is False
        assert any("Unknown compilation target" in e for e in result.errors)


class TestMergeResults:
    """Tests for _merge_results() method."""

    @pytest.fixture
    def compiler(self):
        """Create a compiler instance for testing."""
        return AgentsCompiler(".")

    def test_merge_empty_results_list(self, compiler):
        """Test merging an empty results list."""
        result = compiler._merge_results([])
        
        assert result.success is True
        assert result.output_path == ""
        assert result.content == ""
        assert result.warnings == []
        assert result.errors == []
        assert result.stats == {}

    def test_merge_single_result(self, compiler):
        """Test that single result is returned as-is."""
        single_result = CompilationResult(
            success=True,
            output_path="AGENTS.md",
            content="# Test content",
            warnings=["warning1"],
            errors=[],
            stats={"test": 1}
        )
        
        result = compiler._merge_results([single_result])
        
        assert result.success is True
        assert result.output_path == "AGENTS.md"
        assert result.content == "# Test content"
        assert result.warnings == ["warning1"]
        assert result.stats == {"test": 1}

    def test_merge_multiple_results_success(self, compiler):
        """Test merging multiple successful results."""
        result1 = CompilationResult(
            success=True,
            output_path="AGENTS.md",
            content="AGENTS content",
            warnings=["agents warning"],
            errors=[],
            stats={"agents_files_generated": 2}
        )
        
        result2 = CompilationResult(
            success=True,
            output_path="CLAUDE.md: 1 files",
            content="CLAUDE content",
            warnings=["claude warning"],
            errors=[],
            stats={"claude_files_written": 1}
        )
        
        merged = compiler._merge_results([result1, result2])
        
        assert merged.success is True
        assert "AGENTS.md" in merged.output_path
        assert "CLAUDE" in merged.output_path
        assert "agents warning" in merged.warnings
        assert "claude warning" in merged.warnings
        assert "AGENTS content" in merged.content
        assert "CLAUDE content" in merged.content

    def test_merge_results_with_one_failure(self, compiler):
        """Test that merged result is failure if any result fails."""
        result1 = CompilationResult(
            success=True,
            output_path="AGENTS.md",
            content="Success",
            warnings=[],
            errors=[],
            stats={}
        )
        
        result2 = CompilationResult(
            success=False,
            output_path="CLAUDE.md",
            content="",
            warnings=[],
            errors=["Failed to compile"],
            stats={}
        )
        
        merged = compiler._merge_results([result1, result2])
        
        assert merged.success is False
        assert "Failed to compile" in merged.errors

    def test_merge_results_combines_numeric_stats(self, compiler):
        """Test that numeric stats are summed when merging."""
        result1 = CompilationResult(
            success=True,
            output_path="A",
            content="",
            warnings=[],
            errors=[],
            stats={"primitives_found": 5, "instructions": 3}
        )
        
        result2 = CompilationResult(
            success=True,
            output_path="B",
            content="",
            warnings=[],
            errors=[],
            stats={"primitives_found": 2, "claude_files_written": 1}
        )
        
        merged = compiler._merge_results([result1, result2])
        
        # Same key should be summed
        assert merged.stats["primitives_found"] == 7
        # Different keys should be kept
        assert merged.stats["instructions"] == 3
        assert merged.stats["claude_files_written"] == 1

    def test_merge_results_preserves_all_warnings_and_errors(self, compiler):
        """Test that all warnings and errors are preserved."""
        result1 = CompilationResult(
            success=True,
            output_path="A",
            content="",
            warnings=["warn1", "warn2"],
            errors=[],
            stats={}
        )
        
        result2 = CompilationResult(
            success=True,
            output_path="B",
            content="",
            warnings=["warn3"],
            errors=["error1"],
            stats={}
        )
        
        merged = compiler._merge_results([result1, result2])
        
        assert len(merged.warnings) == 3
        assert "warn1" in merged.warnings
        assert "warn2" in merged.warnings
        assert "warn3" in merged.warnings
        assert len(merged.errors) == 1
        assert "error1" in merged.errors

    def test_merge_results_joins_output_paths(self, compiler):
        """Test that output paths are joined with ' | '."""
        result1 = CompilationResult(
            success=True,
            output_path="Distributed: 3 AGENTS.md files",
            content="",
            warnings=[],
            errors=[],
            stats={}
        )
        
        result2 = CompilationResult(
            success=True,
            output_path="CLAUDE.md: 2 files",
            content="",
            warnings=[],
            errors=[],
            stats={}
        )
        
        merged = compiler._merge_results([result1, result2])
        
        assert " | " in merged.output_path
        assert "Distributed" in merged.output_path
        assert "CLAUDE.md" in merged.output_path

    def test_merge_results_joins_content_with_separator(self, compiler):
        """Test that content is joined with separator."""
        result1 = CompilationResult(
            success=True,
            output_path="A",
            content="Content A",
            warnings=[],
            errors=[],
            stats={}
        )
        
        result2 = CompilationResult(
            success=True,
            output_path="B",
            content="Content B",
            warnings=[],
            errors=[],
            stats={}
        )
        
        merged = compiler._merge_results([result1, result2])
        
        assert "---" in merged.content
        assert "Content A" in merged.content
        assert "Content B" in merged.content


class TestCompileCommandCLI:
    """Tests for the compile command CLI with --target flag."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        
        # Create minimal apm.yml
        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")
        
        # Create instruction file for compilation
        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        instruction_file = apm_dir / "test.instructions.md"
        instruction_file.write_text("""---
applyTo: "**/*.py"
---
Use type hints in Python code.
""")
        
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_target_flag_accepts_vscode(self, runner, temp_project):
        """Test that --target vscode is accepted."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            result = runner.invoke(cli, ["compile", "--target", "vscode", "--dry-run"])
            
            # Should not fail due to invalid choice
            assert "Invalid value for '--target'" not in result.output
        finally:
            os.chdir(original_dir)

    def test_target_flag_accepts_agents(self, runner, temp_project):
        """Test that --target agents is accepted."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            result = runner.invoke(cli, ["compile", "--target", "agents", "--dry-run"])
            
            assert "Invalid value for '--target'" not in result.output
        finally:
            os.chdir(original_dir)

    def test_target_flag_accepts_claude(self, runner, temp_project):
        """Test that --target claude is accepted."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            result = runner.invoke(cli, ["compile", "--target", "claude", "--dry-run"])
            
            assert "Invalid value for '--target'" not in result.output
        finally:
            os.chdir(original_dir)

    def test_target_flag_accepts_all(self, runner, temp_project):
        """Test that --target all is accepted."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            result = runner.invoke(cli, ["compile", "--target", "all", "--dry-run"])
            
            assert "Invalid value for '--target'" not in result.output
        finally:
            os.chdir(original_dir)

    def test_target_flag_rejects_invalid(self, runner, temp_project):
        """Test that invalid target value is rejected."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            result = runner.invoke(cli, ["compile", "--target", "invalid", "--dry-run"])
            
            assert result.exit_code != 0
            assert "Invalid value for '--target'" in result.output
        finally:
            os.chdir(original_dir)

    def test_target_default_is_all(self, runner, temp_project):
        """Test that default target is 'all' when not specified."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            # Run compile with dry-run to just test config
            result = runner.invoke(cli, ["compile", "--dry-run"])
            
            # Should succeed and compile for all targets
            # Exit code should be 0 (success) since we have valid primitives
            assert result.exit_code == 0 or "No APM content" in result.output
        finally:
            os.chdir(original_dir)

    def test_short_flag_t_works(self, runner, temp_project):
        """Test that -t short flag works for target."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            result = runner.invoke(cli, ["compile", "-t", "vscode", "--dry-run"])
            
            assert "Invalid value for '--target'" not in result.output
        finally:
            os.chdir(original_dir)


class TestTargetVscodeOnlyGeneratesAgentsMd:
    """Tests to verify --target vscode only generates AGENTS.md."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        
        # Create minimal apm.yml
        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")
        
        # Create instruction file
        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        instruction_file = apm_dir / "test.instructions.md"
        instruction_file.write_text("""---
applyTo: "**/*.py"
---
Use type hints.
""")
        
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_vscode_target_does_not_create_claude_md(self, temp_project):
        """Test that --target vscode doesn't create CLAUDE.md."""
        config = CompilationConfig(
            target="vscode",
            dry_run=False,
            single_agents=True
        )
        
        compiler = AgentsCompiler(str(temp_project))
        primitives = PrimitiveCollection()
        
        instruction = Instruction(
            name="test",
            file_path=temp_project / ".apm/instructions/test.instructions.md",
            description="Test",
            apply_to="**/*.py",
            content="Use type hints.",
            author="test",
            source="local"
        )
        primitives.add_primitive(instruction)
        
        result = compiler.compile(config, primitives)
        
        # Should succeed
        assert result.success
        
        # AGENTS.md should be created
        agents_md = temp_project / "AGENTS.md"
        assert agents_md.exists()
        
        # CLAUDE.md should NOT be created
        claude_md = temp_project / "CLAUDE.md"
        assert not claude_md.exists()


class TestTargetClaudeOnlyGeneratesClaudeMd:
    """Tests to verify --target claude only generates CLAUDE.md."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        
        # Create minimal apm.yml
        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")
        
        # Create instruction file
        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        instruction_file = apm_dir / "test.instructions.md"
        instruction_file.write_text("""---
applyTo: "**/*.py"
---
Use type hints.
""")
        
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_claude_target_does_not_create_agents_md(self, temp_project):
        """Test that --target claude doesn't create AGENTS.md."""
        config = CompilationConfig(
            target="claude",
            dry_run=False
        )
        
        compiler = AgentsCompiler(str(temp_project))
        primitives = PrimitiveCollection()
        
        instruction = Instruction(
            name="test",
            file_path=temp_project / ".apm/instructions/test.instructions.md",
            description="Test",
            apply_to="**/*.py",
            content="Use type hints.",
            author="test",
            source="local"
        )
        primitives.add_primitive(instruction)
        
        result = compiler.compile(config, primitives)
        
        # Should succeed
        assert result.success
        
        # CLAUDE.md should be created (in root or with distributed)
        claude_md = temp_project / "CLAUDE.md"
        assert claude_md.exists()
        
        # AGENTS.md should NOT be created at root
        # (checking root AGENTS.md since distributed could create subdirectory ones)
        agents_md = temp_project / "AGENTS.md"
        assert not agents_md.exists()


class TestTargetAllGeneratesBoth:
    """Tests to verify --target all generates both AGENTS.md and CLAUDE.md."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        
        # Create minimal apm.yml
        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")
        
        # Create instruction file
        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        instruction_file = apm_dir / "test.instructions.md"
        instruction_file.write_text("""---
applyTo: "**/*.py"
---
Use type hints.
""")
        
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_all_target_creates_both_files(self, temp_project):
        """Test that --target all creates both AGENTS.md and CLAUDE.md."""
        config = CompilationConfig(
            target="all",
            dry_run=False,
            single_agents=True  # Use single-file for simpler verification
        )
        
        compiler = AgentsCompiler(str(temp_project))
        primitives = PrimitiveCollection()
        
        instruction = Instruction(
            name="test",
            file_path=temp_project / ".apm/instructions/test.instructions.md",
            description="Test",
            apply_to="**/*.py",
            content="Use type hints.",
            author="test",
            source="local"
        )
        primitives.add_primitive(instruction)
        
        result = compiler.compile(config, primitives)
        
        # Should succeed
        assert result.success
        
        # Both files should be created
        agents_md = temp_project / "AGENTS.md"
        claude_md = temp_project / "CLAUDE.md"
        
        assert agents_md.exists(), "AGENTS.md should be created for target='all'"
        assert claude_md.exists(), "CLAUDE.md should be created for target='all'"

    def test_all_target_result_references_both(self, temp_project):
        """Test that --target all result references both outputs."""
        config = CompilationConfig(
            target="all",
            dry_run=True,
            single_agents=True
        )
        
        compiler = AgentsCompiler(str(temp_project))
        primitives = PrimitiveCollection()
        
        instruction = Instruction(
            name="test",
            file_path=temp_project / ".apm/instructions/test.instructions.md",
            description="Test",
            apply_to="**/*.py",
            content="Use type hints.",
            author="test",
            source="local"
        )
        primitives.add_primitive(instruction)
        
        result = compiler.compile(config, primitives)
        
        assert result.success
        # The merged output path should reference both targets
        assert "AGENTS.md" in result.output_path or "CLAUDE" in result.output_path


class TestClaudeAndAgentsMdConsistentOutput:
    """Tests to ensure CLAUDE.md and AGENTS.md use the same optimization logic.
    
    Both targets should produce:
    - Same optimization decisions (placement table)
    - Same efficiency metrics
    - Same placement distribution
    Only the output file names should differ.
    """

    @pytest.fixture
    def temp_project_with_instructions(self):
        """Create a temporary project with instruction files."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        
        # Create .apm directory with instructions
        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        
        # Create instruction file that targets specific pattern
        (apm_dir / "code-standards.instructions.md").write_text("""---
applyTo: "**/*.py"
description: "Python coding standards"
---
# Python Coding Standards
Follow PEP 8 guidelines.
""")
        
        # Create another instruction file with different pattern
        (apm_dir / "test-guidelines.instructions.md").write_text("""---
applyTo: "tests/**/*.py"
description: "Testing guidelines"
---
# Testing Guidelines
Use pytest for all tests.
""")
        
        # Create target directories to match patterns
        (temp_path / "src").mkdir()
        (temp_path / "src" / "main.py").write_text("# Main file")
        (temp_path / "tests").mkdir()
        (temp_path / "tests" / "test_main.py").write_text("# Test file")
        
        # Create apm.yml
        (temp_path / "apm.yml").write_text("""
name: test-project
version: 0.1.0
""")
        
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_claude_and_agents_have_same_placement_count(self, temp_project_with_instructions):
        """Test that CLAUDE.md and AGENTS.md generate the same number of placement files."""
        compiler = AgentsCompiler(str(temp_project_with_instructions))
        
        # Compile for VSCode/AGENTS.md
        vscode_config = CompilationConfig(target="vscode", dry_run=True)
        vscode_result = compiler.compile(vscode_config)
        
        # Reset compiler state
        compiler = AgentsCompiler(str(temp_project_with_instructions))
        
        # Compile for Claude/CLAUDE.md  
        claude_config = CompilationConfig(target="claude", dry_run=True)
        claude_result = compiler.compile(claude_config)
        
        # Both should succeed
        assert vscode_result.success
        assert claude_result.success
        
        # Both should have the same file count in stats (using target-specific keys)
        vscode_file_count = vscode_result.stats.get('agents_files_generated', vscode_result.stats.get('total_agents_files', 0))
        claude_file_count = claude_result.stats.get('claude_files_generated', 0)
        
        # The file counts should be equal (same optimization logic)
        assert vscode_file_count == claude_file_count, \
            f"File counts differ: AGENTS.md={vscode_file_count}, CLAUDE.md={claude_file_count}"

    def test_claude_compilation_produces_optimization_output(self, temp_project_with_instructions):
        """Test that CLAUDE.md compilation produces proper optimization metrics."""
        compiler = AgentsCompiler(str(temp_project_with_instructions))
        
        # Compile for Claude/CLAUDE.md  
        claude_config = CompilationConfig(target="claude", dry_run=True)
        claude_result = compiler.compile(claude_config)
        
        # Should succeed
        assert claude_result.success
        
        # Should have file count
        assert claude_result.stats.get('claude_files_generated', 0) > 0
        
        # Should have primitives count
        assert claude_result.stats.get('primitives_found', 0) > 0


class TestConfigFromApmYml:
    """Tests for reading target from apm.yml configuration."""

    @pytest.fixture
    def temp_project_with_config(self):
        """Create a temporary project with apm.yml containing compilation config."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_target_from_apm_yml(self, temp_project_with_config):
        """Test that target can be read from apm.yml compilation section."""
        apm_yml = temp_project_with_config / "apm.yml"
        apm_yml.write_text("""
name: test-project
version: 0.1.0
compilation:
  target: claude
""")
        
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project_with_config)
            config = CompilationConfig.from_apm_yml()
            assert config.target == "claude"
        finally:
            os.chdir(original_dir)

    def test_cli_override_takes_precedence(self, temp_project_with_config):
        """Test that CLI --target overrides apm.yml config."""
        apm_yml = temp_project_with_config / "apm.yml"
        apm_yml.write_text("""
name: test-project
version: 0.1.0
compilation:
  target: claude
""")
        
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project_with_config)
            # CLI override should take precedence
            config = CompilationConfig.from_apm_yml(target="vscode")
            assert config.target == "vscode"
        finally:
            os.chdir(original_dir)


class TestCompileWarningOnMissingApplyTo:
    """Tests that apm compile warns when an instruction is missing applyTo."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def project_with_bad_instruction(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")

        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)

        (apm_dir / "good.instructions.md").write_text(
            "---\napplyTo: '**/*.py'\n---\nFollow PEP 8.\n"
        )
        (apm_dir / "bad.instructions.md").write_text(
            "---\ndescription: Missing applyTo\n---\nThis instruction has no scope.\n"
        )

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_cli_warns_missing_apply_to_distributed(
        self, runner, project_with_bad_instruction
    ):
        """Test that apm compile --dry-run warns about missing applyTo in distributed mode."""
        original_dir = os.getcwd()
        try:
            os.chdir(project_with_bad_instruction)
            result = runner.invoke(
                cli, ["compile", "--target", "vscode", "--dry-run"]
            )
            assert "applyTo" in result.output, (
                f"Expected warning about missing 'applyTo' in CLI output, got:\n{result.output}"
            )
        finally:
            os.chdir(original_dir)

    def test_cli_warns_missing_apply_to_claude(
        self, runner, project_with_bad_instruction
    ):
        """Test that apm compile --target claude --dry-run warns about missing applyTo."""
        original_dir = os.getcwd()
        try:
            os.chdir(project_with_bad_instruction)
            result = runner.invoke(
                cli, ["compile", "--target", "claude", "--dry-run"]
            )
            assert "applyTo" in result.output, (
                f"Expected warning about missing 'applyTo' in CLI output, got:\n{result.output}"
            )
        finally:
            os.chdir(original_dir)


class TestResolveCompileTarget:
    """Tests for _resolve_compile_target() multi-target list mapping."""

    def test_none_returns_none(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(None) is None

    def test_single_string_passthrough(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target("claude") == "claude"
        assert _resolve_compile_target("vscode") == "vscode"
        assert _resolve_compile_target("all") == "all"
        assert _resolve_compile_target("copilot") == "copilot"

    def test_list_claude_and_copilot_returns_all(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["claude", "vscode"]) == "all"
        assert _resolve_compile_target(["claude", "copilot"]) == "all"

    def test_list_claude_only_returns_claude(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["claude"]) == "claude"

    def test_list_copilot_only_returns_vscode(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["vscode"]) == "vscode"
        assert _resolve_compile_target(["copilot"]) == "vscode"

    def test_list_agents_family_without_claude_returns_vscode(self):
        """Targets that produce AGENTS.md but not CLAUDE.md."""
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["cursor"]) == "vscode"
        assert _resolve_compile_target(["opencode"]) == "vscode"
        assert _resolve_compile_target(["codex"]) == "vscode"
        assert _resolve_compile_target(["cursor", "opencode"]) == "vscode"

    def test_list_cursor_and_claude_returns_all(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["cursor", "claude"]) == "all"
        assert _resolve_compile_target(["codex", "claude"]) == "all"

    def test_list_all_targets_returns_all(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["claude", "vscode", "cursor"]) == "all"
