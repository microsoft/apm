"""Tests for distributed compilation system (Task 7)."""

import shutil  # noqa: F401
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig
from apm_cli.compilation.distributed_compiler import DirectoryMap, DistributedAgentsCompiler
from apm_cli.primitives.models import Instruction, PrimitiveCollection


class TestDistributedCompiler:
    """Test distributed AGENTS.md compilation."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory with test structure."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)

            # Create directory structure
            (base_dir / "src").mkdir()
            (base_dir / "src" / "components").mkdir()
            (base_dir / "docs").mkdir()
            (base_dir / "tests").mkdir()

            # Create test files to match the patterns
            (base_dir / "main.py").touch()  # For **/*.py pattern
            (base_dir / "src" / "app.py").touch()  # For src/**/*.py pattern
            (
                base_dir / "src" / "components" / "button.py"
            ).touch()  # For src/components/**/*.py pattern
            (base_dir / "docs" / "readme.md").touch()  # For docs/**/*.md pattern
            (base_dir / "tests" / "test_main.py").touch()  # Additional Python file

            yield base_dir

    @pytest.fixture
    def sample_instructions(self, temp_project):
        """Create sample instructions with different patterns."""
        instructions = []

        # Global instruction
        global_inst = Instruction(
            name="global-python",
            file_path=temp_project / ".apm" / "instructions" / "global.instructions.md",
            description="Global Python standards",
            apply_to="**/*.py",
            content="# Global Python Standards\n- Use type hints\n- Follow PEP 8",
        )
        global_inst.source = "local"
        instructions.append(global_inst)

        # Source-specific instruction
        src_inst = Instruction(
            name="source-code",
            file_path=temp_project / ".apm" / "instructions" / "source.instructions.md",
            description="Source code standards",
            apply_to="src/**/*.py",
            content="# Source Code Standards\n- Add docstrings\n- Use logging",
        )
        src_inst.source = "local"
        instructions.append(src_inst)

        # Component-specific instruction
        comp_inst = Instruction(
            name="components",
            file_path=temp_project / ".apm" / "instructions" / "components.instructions.md",
            description="Component standards",
            apply_to="src/components/**/*.py",
            content="# Component Standards\n- Use React patterns\n- Add prop types",
        )
        comp_inst.source = "local"
        instructions.append(comp_inst)

        # Documentation instruction
        docs_inst = Instruction(
            name="documentation",
            file_path=temp_project / ".apm" / "instructions" / "docs.instructions.md",
            description="Documentation standards",
            apply_to="docs/**/*.md",
            content="# Documentation Standards\n- Use clear titles\n- Add examples",
        )
        docs_inst.source = "local"
        instructions.append(docs_inst)

        return instructions

    def test_analyze_directory_structure(self, temp_project, sample_instructions):
        """Test directory structure analysis."""
        compiler = DistributedAgentsCompiler(str(temp_project))

        directory_map = compiler.analyze_directory_structure(sample_instructions)

        # Should have detected directories
        assert len(directory_map.directories) >= 3
        # Normalize paths for comparison (handle symlink resolution differences)
        dir_paths = {p.resolve() for p in directory_map.directories.keys()}  # noqa: SIM118
        assert temp_project.resolve() in dir_paths
        assert (temp_project / "src").resolve() in dir_paths
        assert (temp_project / "docs").resolve() in dir_paths

        # Check depth calculations
        resolved_temp_project = temp_project.resolve()
        assert directory_map.depth_map[resolved_temp_project] == 0
        assert directory_map.depth_map[resolved_temp_project / "src"] == 1

        # Check patterns are assigned
        resolved_temp_project = temp_project.resolve()
        assert "**/*.py" in directory_map.directories[resolved_temp_project]
        assert "src/**/*.py" in directory_map.directories[resolved_temp_project / "src"]

    def test_determine_agents_placement(self, temp_project, sample_instructions):
        """Test AGENTS.md placement logic."""
        compiler = DistributedAgentsCompiler(str(temp_project))

        directory_map = compiler.analyze_directory_structure(sample_instructions)
        placement_map = compiler.determine_agents_placement(
            sample_instructions, directory_map, min_instructions=1, debug=False
        )

        # Should have placements in multiple directories
        assert len(placement_map) >= 2

        # Root should have global instructions (normalize paths for comparison)
        placement_paths = {p.resolve() for p in placement_map.keys()}  # noqa: SIM118
        resolved_temp_project = temp_project.resolve()
        assert resolved_temp_project in placement_paths

        # Find the resolved path key to access placement_map
        root_key = None
        for p in placement_map.keys():  # noqa: SIM118
            if p.resolve() == resolved_temp_project:
                root_key = p
                break
        assert root_key is not None
        root_instructions = placement_map[root_key]
        assert any(inst.apply_to == "**/*.py" for inst in root_instructions)

    def test_generate_distributed_agents_files(self, temp_project, sample_instructions):
        """Test distributed AGENTS.md content generation."""
        compiler = DistributedAgentsCompiler(str(temp_project))

        directory_map = compiler.analyze_directory_structure(sample_instructions)
        placement_map = compiler.determine_agents_placement(sample_instructions, directory_map)

        # Create a primitive collection
        collection = PrimitiveCollection()
        for inst in sample_instructions:
            collection.add_primitive(inst)

        placements = compiler.generate_distributed_agents_files(
            placement_map, collection, source_attribution=True
        )

        assert len(placements) > 0

        # Check first placement has proper structure
        placement = placements[0]
        assert placement.agents_path.name == "AGENTS.md"
        assert len(placement.instructions) > 0
        assert len(placement.coverage_patterns) > 0
        assert placement.source_attribution  # Should have source attribution

    def test_compile_distributed_integration(self, temp_project, sample_instructions):
        """Test full distributed compilation flow."""
        compiler = DistributedAgentsCompiler(str(temp_project))

        # Create a primitive collection
        collection = PrimitiveCollection()
        for inst in sample_instructions:
            collection.add_primitive(inst)

        # Run distributed compilation
        result = compiler.compile_distributed(collection)

        assert result.success
        assert len(result.placements) > 0
        assert len(result.content_map) > 0
        assert result.stats["agents_files_generated"] > 0

        # Check generated content
        for agents_path, content in result.content_map.items():  # noqa: B007
            assert "# AGENTS.md" in content
            assert "Generated by APM CLI" in content
            assert "Files matching" in content


class TestAgentsCompilerIntegration:
    """Test integration with existing AgentsCompiler."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield Path(temp_dir)

    def test_distributed_compilation_config(self, temp_project):
        """Test configuration for distributed compilation."""
        config = CompilationConfig()

        # Default should be distributed
        assert config.strategy == "distributed"
        assert not config.single_agents
        assert config.source_attribution

        # Single agents flag should override strategy
        config = CompilationConfig(single_agents=True)
        assert config.strategy == "single-file"
        assert config.single_agents

        # from_apm_yml should also work with single_agents override
        config = CompilationConfig.from_apm_yml(single_agents=True)
        assert config.strategy == "single-file"

    @patch("apm_cli.primitives.discovery.discover_primitives_with_dependencies")
    def test_agents_compiler_distributed_mode(self, mock_discovery, temp_project):
        """Test AgentsCompiler calls distributed compiler in distributed mode."""
        # Mock primitives
        mock_primitives = MagicMock()
        mock_primitives.instructions = []
        mock_primitives.chatmodes = []
        mock_primitives.contexts = []
        mock_primitives.count.return_value = 0
        mock_discovery.return_value = mock_primitives

        compiler = AgentsCompiler(str(temp_project))
        config = CompilationConfig(strategy="distributed")

        # This should call the distributed compilation path
        with patch.object(compiler, "_compile_distributed") as mock_distributed:
            mock_distributed.return_value = MagicMock(success=True, warnings=[], errors=[])

            result = compiler.compile(config)  # noqa: F841

            mock_distributed.assert_called_once_with(config, mock_primitives)

    @patch("apm_cli.primitives.discovery.discover_primitives")
    def test_agents_compiler_single_file_mode(self, mock_discovery, temp_project):
        """Test AgentsCompiler uses single-file mode when requested."""
        # Mock primitives
        mock_primitives = MagicMock()
        mock_primitives.instructions = []
        mock_primitives.chatmodes = []
        mock_primitives.contexts = []
        mock_primitives.count.return_value = 0
        mock_discovery.return_value = mock_primitives

        compiler = AgentsCompiler(str(temp_project))
        config = CompilationConfig(strategy="single-file", single_agents=True)

        # This should call the single-file compilation path
        with patch.object(compiler, "_compile_single_file") as mock_single_file:
            mock_single_file.return_value = MagicMock(success=True, warnings=[], errors=[])

            result = compiler.compile(config)  # noqa: F841

            # Check that single-file compilation was called (verify call count, not exact args)
            assert mock_single_file.call_count == 1
            call_args = mock_single_file.call_args[0]
            assert call_args[0] == config  # First argument should be the config


class TestDirectoryAnalysis:
    """Test directory analysis and pattern extraction."""

    def test_extract_directories_from_pattern(self):
        """Test pattern to directory extraction."""
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            compiler = DistributedAgentsCompiler(temp_dir)

        # Test various patterns
        assert compiler._extract_directories_from_pattern("**/*.py") == [Path(".")]
        assert compiler._extract_directories_from_pattern("src/**/*.py") == [Path("src")]
        assert compiler._extract_directories_from_pattern("docs/*.md") == [Path("docs")]
        assert compiler._extract_directories_from_pattern("*.py") == [Path(".")]
        assert compiler._extract_directories_from_pattern("tests/unit/**/*.py") == [Path("tests")]

    def test_directory_map_structure(self):
        """Test DirectoryMap data structure."""
        directory_map = DirectoryMap(
            directories={Path("."): {"**/*.py"}, Path("src"): {"src/**/*.py"}},
            depth_map={Path("."): 0, Path("src"): 1},
            parent_map={Path("."): None, Path("src"): Path(".")},
        )

        assert directory_map.get_max_depth() == 1
        assert Path(".") in directory_map.directories
        assert "**/*.py" in directory_map.directories[Path(".")]


if __name__ == "__main__":
    pytest.main([__file__])
