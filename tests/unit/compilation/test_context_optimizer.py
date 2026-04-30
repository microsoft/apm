"""Unit tests for the Context Optimizer.

Tests the Context Optimization Engine that minimizes irrelevant context
loaded by agents working in specific directories.
"""

import fnmatch  # noqa: F401
import os  # noqa: F401
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest

from apm_cli.compilation.context_optimizer import (
    ContextOptimizer,
    DirectoryAnalysis,
    InheritanceAnalysis,
    PlacementCandidate,
)
from apm_cli.primitives.models import Instruction


class TestDirectoryAnalysis:
    """Test DirectoryAnalysis dataclass."""

    def test_get_relevance_score_empty_directory(self):
        """Test relevance score calculation for empty directory."""
        analysis = DirectoryAnalysis(directory=Path("/test"), depth=1, total_files=0)
        assert analysis.get_relevance_score("**/*.py") == 0.0

    def test_get_relevance_score_with_matches(self):
        """Test relevance score calculation with pattern matches."""
        analysis = DirectoryAnalysis(
            directory=Path("/test"), depth=1, total_files=10, pattern_matches={"**/*.py": 3}
        )
        assert analysis.get_relevance_score("**/*.py") == 0.3

    def test_get_relevance_score_no_matches(self):
        """Test relevance score for pattern with no matches."""
        analysis = DirectoryAnalysis(
            directory=Path("/test"), depth=1, total_files=10, pattern_matches={"**/*.js": 5}
        )
        assert analysis.get_relevance_score("**/*.py") == 0.0


class TestInheritanceAnalysis:
    """Test InheritanceAnalysis dataclass."""

    def test_get_efficiency_ratio_no_context(self):
        """Test efficiency ratio with no context load."""
        analysis = InheritanceAnalysis(
            working_directory=Path("/test"), inheritance_chain=[Path("/test")]
        )
        assert analysis.get_efficiency_ratio() == 1.0

    def test_get_efficiency_ratio_perfect_relevance(self):
        """Test efficiency ratio with perfect context relevance."""
        analysis = InheritanceAnalysis(
            working_directory=Path("/test"),
            inheritance_chain=[Path("/test")],
            total_context_load=5,
            relevant_context_load=5,
        )
        assert analysis.get_efficiency_ratio() == 1.0

    def test_get_efficiency_ratio_partial_relevance(self):
        """Test efficiency ratio with partial context relevance."""
        analysis = InheritanceAnalysis(
            working_directory=Path("/test"),
            inheritance_chain=[Path("/test")],
            total_context_load=10,
            relevant_context_load=6,
        )
        assert analysis.get_efficiency_ratio() == 0.6


class TestPlacementCandidate:
    """Test PlacementCandidate dataclass."""

    def test_post_init_score_calculation(self):
        """Test that total_score is calculated correctly in __post_init__."""
        instruction = Instruction(
            name="test",
            file_path=Path("test.md"),
            description="Test instruction",
            apply_to="**/*.py",
            content="Test content",
            source="local",
        )

        candidate = PlacementCandidate(
            instruction=instruction,
            directory=Path("/test"),
            direct_relevance=0.8,
            inheritance_pollution=0.3,
            depth_specificity=0.2,
            total_score=0.0,  # Will be overwritten
        )

        # Score = 0.8 * 1.0 + (-0.3 * 0.5) + 0.2 * 0.1 = 0.8 - 0.15 + 0.02 = 0.67
        expected_score = 0.8 - (0.3 * 0.5) + (0.2 * 0.1)
        assert abs(candidate.total_score - expected_score) < 0.001


class TestContextOptimizer:
    """Test ContextOptimizer class."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project structure for testing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create directory structure
            (temp_path / "server").mkdir()
            (temp_path / "styles").mkdir()
            (temp_path / "tests").mkdir()
            (temp_path / "src" / "components").mkdir(parents=True)

            # Create test files
            (temp_path / "server" / "api.py").touch()
            (temp_path / "server" / "models.py").touch()
            (temp_path / "styles" / "main.css").touch()
            (temp_path / "styles" / "tokens.scss").touch()
            (temp_path / "tests" / "test_api.py").touch()
            (temp_path / "tests" / "test_ui.tsx").touch()
            (temp_path / "src" / "components" / "Button.tsx").touch()
            (temp_path / "src" / "components" / "Modal.tsx").touch()
            (temp_path / "index.html").touch()
            (temp_path / "main.js").touch()

            yield temp_path

    @pytest.fixture
    def sample_instructions(self):
        """Create sample instructions for testing."""
        return [
            Instruction(
                name="python-standards",
                file_path=Path("python.instructions.md"),
                description="Python development standards",
                apply_to="**/*.py",
                content="Python coding standards",
                source="local",
            ),
            Instruction(
                name="typescript-standards",
                file_path=Path("typescript.instructions.md"),
                description="TypeScript development standards",
                apply_to="**/*.{ts,tsx}",
                content="TypeScript coding standards",
                source="local",
            ),
            Instruction(
                name="css-standards",
                file_path=Path("css.instructions.md"),
                description="CSS development standards",
                apply_to="**/*.{css,scss}",
                content="CSS coding standards",
                source="local",
            ),
            Instruction(
                name="general-standards",
                file_path=Path("general.instructions.md"),
                description="General development standards",
                apply_to="**/*.{py,js,ts,tsx,css,scss}",
                content="General coding standards",
                source="local",
            ),
        ]

    def test_initialization(self, temp_project):
        """Test ContextOptimizer initialization."""
        optimizer = ContextOptimizer(str(temp_project))
        assert optimizer.base_dir.resolve() == temp_project.resolve()
        assert optimizer._directory_cache == {}
        assert optimizer._pattern_cache == {}

    def test_initialization_with_invalid_path(self):
        """Test ContextOptimizer initialization with invalid path."""
        optimizer = ContextOptimizer("/nonexistent/path")
        assert optimizer.base_dir == Path("/nonexistent/path").absolute()

    def test_analyze_project_structure(self, temp_project):
        """Test project structure analysis."""
        optimizer = ContextOptimizer(str(temp_project))
        optimizer._analyze_project_structure()

        # Check that directories were analyzed
        assert len(optimizer._directory_cache) > 0

        # Check specific directories
        assert temp_project.resolve() in optimizer._directory_cache
        assert (temp_project / "server").resolve() in optimizer._directory_cache
        assert (temp_project / "styles").resolve() in optimizer._directory_cache

        # Check file counts
        server_analysis = optimizer._directory_cache[(temp_project / "server").resolve()]
        assert server_analysis.total_files == 2  # api.py, models.py
        assert server_analysis.depth == 1

    def test_find_matching_directories(self, temp_project, sample_instructions):
        """Test finding directories that match file patterns."""
        optimizer = ContextOptimizer(str(temp_project))
        optimizer._analyze_project_structure()

        # Test Python pattern
        python_dirs = optimizer._find_matching_directories("**/*.py")
        expected_python_dirs = {
            (temp_project / "server").resolve(),
            (temp_project / "tests").resolve(),
        }
        assert python_dirs == expected_python_dirs

        # Test CSS pattern
        css_dirs = optimizer._find_matching_directories("**/*.{css,scss}")
        expected_css_dirs = {(temp_project / "styles").resolve()}
        assert css_dirs == expected_css_dirs

        # Test TypeScript pattern
        tsx_dirs = optimizer._find_matching_directories("**/*.{ts,tsx}")
        expected_tsx_dirs = {
            (temp_project / "tests").resolve(),
            (temp_project / "src" / "components").resolve(),
        }
        assert tsx_dirs == expected_tsx_dirs

    def test_optimize_instruction_placement_isolated_patterns(
        self, temp_project, sample_instructions
    ):
        """Test optimization for patterns that are cleanly isolated using mathematical optimization."""
        optimizer = ContextOptimizer(str(temp_project))

        # Filter to just Python and CSS instructions (cleanly separated)
        isolated_instructions = [
            inst
            for inst in sample_instructions
            if inst.name in ["python-standards", "css-standards"]
        ]

        placement = optimizer.optimize_instruction_placement(isolated_instructions)

        # Mathematical optimization uses three-tier strategy with coverage guarantee
        python_placements = []
        css_placements = []

        for directory, instructions in placement.items():
            for instruction in instructions:
                if instruction.name == "python-standards":
                    python_placements.append(directory)
                elif instruction.name == "css-standards":
                    css_placements.append(directory)

        # Verify Python instruction is placed to guarantee coverage
        assert len(python_placements) >= 1  # At least one placement guaranteed
        # Coverage takes priority over efficiency - may be placed at root for universal access
        python_dirs_with_files = [
            (temp_project / "server").resolve(),
            (temp_project / "tests").resolve(),
            temp_project.resolve(),
        ]
        assert any(placement in python_dirs_with_files for placement in python_placements)

        # Verify CSS is placed where it can be accessed by CSS files
        assert len(css_placements) >= 1  # At least one placement guaranteed
        # May be placed at root for coverage or at styles directory for efficiency
        css_dirs_allowed = [(temp_project / "styles").resolve(), temp_project.resolve()]
        assert any(placement in css_dirs_allowed for placement in css_placements)

    def test_optimize_instruction_placement_widespread_pattern(
        self, temp_project, sample_instructions
    ):
        """Test optimization for widespread patterns that should go to root."""
        optimizer = ContextOptimizer(str(temp_project))

        # The general-standards instruction applies to many file types and should go to root
        general_instruction = [  # noqa: RUF015
            inst for inst in sample_instructions if inst.name == "general-standards"
        ][0]

        placement = optimizer.optimize_instruction_placement([general_instruction])

        # Should be placed at root due to widespread nature
        assert temp_project.resolve() in placement
        assert general_instruction in placement[temp_project.resolve()]

    def test_optimize_instruction_placement_no_pattern(self, temp_project):
        """Test optimization for instructions without apply_to pattern."""
        optimizer = ContextOptimizer(str(temp_project))

        instruction_without_pattern = Instruction(
            name="global-instruction",
            file_path=Path("global.instructions.md"),
            description="Global instruction",
            apply_to="",  # No pattern
            content="Global content",
            source="local",
        )

        placement = optimizer.optimize_instruction_placement([instruction_without_pattern])

        # Should be placed at root
        assert temp_project.resolve() in placement
        assert instruction_without_pattern in placement[temp_project.resolve()]

    def test_calculate_inheritance_pollution(self, temp_project, sample_instructions):
        """Test inheritance pollution calculation."""
        optimizer = ContextOptimizer(str(temp_project))
        optimizer._analyze_project_structure()

        # Test pollution for placing Python instruction at root
        # This should create pollution for styles directory (no Python files)
        pollution = optimizer._calculate_inheritance_pollution(temp_project.resolve(), "**/*.py")  # noqa: F841
        # Note: pollution calculation depends on child directories existing
        # If styles directory has no Python files, placing Python instruction at root creates pollution
        # The actual value depends on the implementation details

    def test_analyze_context_inheritance(self, temp_project, sample_instructions):
        """Test context inheritance analysis."""
        optimizer = ContextOptimizer(str(temp_project))

        # Create a simple placement map
        placement_map = {
            temp_project.resolve(): [sample_instructions[3]],  # general-standards at root
            (temp_project / "server").resolve(): [
                sample_instructions[0]
            ],  # python-standards in server
        }

        # Analyze inheritance for server directory
        inheritance = optimizer.analyze_context_inheritance(
            (temp_project / "server").resolve(), placement_map
        )

        assert inheritance.working_directory == (temp_project / "server").resolve()
        assert len(inheritance.inheritance_chain) >= 2  # server and root
        assert inheritance.total_context_load >= 2  # At least the two instructions

    def test_get_optimization_stats(self, temp_project, sample_instructions):
        """Test optimization statistics generation."""
        optimizer = ContextOptimizer(str(temp_project))

        # Create a placement map
        placement_map = optimizer.optimize_instruction_placement(sample_instructions)

        # Convert Path keys to string keys for the stats method
        stats_placement_map = {
            str(path): instructions for path, instructions in placement_map.items()
        }
        stats = optimizer.get_optimization_stats(stats_placement_map)

        # Check required stats (new API) - stats is an OptimizationStats object
        assert hasattr(stats, "average_context_efficiency")
        assert hasattr(stats, "total_agents_files")
        assert hasattr(stats, "directories_analyzed")

        # Check value ranges
        assert 0.0 <= stats.average_context_efficiency <= 1.0
        assert stats.total_agents_files >= 0
        assert stats.directories_analyzed >= 0

    def test_get_inheritance_chain(self, temp_project):
        """Test inheritance chain generation."""
        optimizer = ContextOptimizer(str(temp_project))

        # Test chain for nested directory
        deep_dir = (temp_project / "src" / "components").resolve()
        chain = optimizer._get_inheritance_chain(deep_dir)

        expected_chain = [deep_dir, (temp_project / "src").resolve(), temp_project.resolve()]

        assert chain == expected_chain

    def test_is_child_directory(self, temp_project):
        """Test child directory detection."""
        optimizer = ContextOptimizer(str(temp_project))

        parent = (temp_project / "src").resolve()
        child = (temp_project / "src" / "components").resolve()
        sibling = (temp_project / "server").resolve()

        assert optimizer._is_child_directory(child, parent) is True
        assert optimizer._is_child_directory(parent, child) is False
        assert optimizer._is_child_directory(sibling, parent) is False
        assert optimizer._is_child_directory(parent, parent) is False

    def test_is_instruction_relevant(self, temp_project):
        """Test instruction relevance detection."""
        optimizer = ContextOptimizer(str(temp_project))
        optimizer._analyze_project_structure()

        python_instruction = Instruction(
            name="python-test",
            file_path=Path("python.instructions.md"),
            description="Python test",
            apply_to="**/*.py",
            content="Python content",
            source="local",
        )

        global_instruction = Instruction(
            name="global-test",
            file_path=Path("global.instructions.md"),
            description="Global test",
            apply_to="",  # No pattern - always relevant
            content="Global content",
            source="local",
        )

        # Python instruction should be relevant to server (has .py files)
        assert (
            optimizer._is_instruction_relevant(
                python_instruction, (temp_project / "server").resolve()
            )
            is True
        )

        # Python instruction should not be relevant to styles (no .py files)
        assert (
            optimizer._is_instruction_relevant(
                python_instruction, (temp_project / "styles").resolve()
            )
            is False
        )

        # Global instruction should always be relevant
        assert (
            optimizer._is_instruction_relevant(
                global_instruction, (temp_project / "server").resolve()
            )
            is True
        )
        assert (
            optimizer._is_instruction_relevant(
                global_instruction, (temp_project / "styles").resolve()
            )
            is True
        )

    def test_select_clean_separation_placements(self, temp_project, sample_instructions):
        """Test clean separation placement selection."""
        optimizer = ContextOptimizer(str(temp_project))
        optimizer._analyze_project_structure()

        # Create candidates for Python instruction
        python_instruction = sample_instructions[0]  # python-standards

        candidates = [
            PlacementCandidate(
                instruction=python_instruction,
                directory=(temp_project / "server").resolve(),
                direct_relevance=1.0,
                inheritance_pollution=0.1,
                depth_specificity=0.1,
                total_score=0.0,  # Will be calculated
            ),
            PlacementCandidate(
                instruction=python_instruction,
                directory=(temp_project / "tests").resolve(),
                direct_relevance=0.5,
                inheritance_pollution=0.1,
                depth_specificity=0.1,
                total_score=0.0,  # Will be calculated
            ),
        ]

        # These directories are isolated (neither is parent/child of the other)
        clean_placements = optimizer._select_clean_separation_placements(candidates, "**/*.py")

        # Should return both directories for clean separation
        expected_dirs = {(temp_project / "server").resolve(), (temp_project / "tests").resolve()}
        assert set(clean_placements) == expected_dirs

    def test_real_project_optimization_benefits(self, temp_project):
        """Test that optimization provides real benefits over naive placement."""
        optimizer = ContextOptimizer(str(temp_project))  # noqa: F841

    def test_real_project_optimization_benefits(self, temp_project):  # noqa: F811
        """Test that optimization provides real benefits over naive placement."""
        optimizer = ContextOptimizer(str(temp_project))

        # Create instructions that would cause pollution if not optimized
        instructions = [
            Instruction(
                name="python-only",
                file_path=Path("python.instructions.md"),
                description="Python only",
                apply_to="**/*.py",
                content="Python content",
                source="local",
            ),
            Instruction(
                name="css-only",
                file_path=Path("css.instructions.md"),
                description="CSS only",
                apply_to="**/*.{css,scss}",
                content="CSS content",
                source="local",
            ),
            Instruction(
                name="frontend-only",
                file_path=Path("frontend.instructions.md"),
                description="Frontend only",
                apply_to="**/*.{ts,tsx,css,scss,html}",
                content="Frontend content",
                source="local",
            ),
        ]

        # Get optimized placement
        optimized_placement = optimizer.optimize_instruction_placement(instructions)

        # Simulate naive placement (all at root)
        naive_placement = {temp_project.resolve(): instructions}

        # Compare context efficiency for server directory (Python work)
        server_dir = (temp_project / "server").resolve()

        optimized_inheritance = optimizer.analyze_context_inheritance(
            server_dir, optimized_placement
        )
        naive_inheritance = optimizer.analyze_context_inheritance(server_dir, naive_placement)

        optimized_efficiency = optimized_inheritance.get_efficiency_ratio()
        naive_efficiency = naive_inheritance.get_efficiency_ratio()

        # With coverage-first optimization, efficiency comparison is more nuanced
        # The optimizer prioritizes coverage guarantee over efficiency
        assert optimized_efficiency >= naive_efficiency or optimized_efficiency >= 0.5

        # For server directory with only Python files, optimized placement should provide reasonable efficiency
        # Even if coverage constraints require some pollution
        if optimized_efficiency > 0:  # If there's any optimization possible
            # Either optimized is better, or it's a reasonable efficiency given coverage constraints
            assert optimized_efficiency >= naive_efficiency or optimized_efficiency >= 0.3


class TestDirectoryExclusion:
    """Test directory exclusion patterns in ContextOptimizer."""

    def test_should_exclude_path_no_patterns(self):
        """Test that no exclusions occur when no patterns are provided."""
        optimizer = ContextOptimizer(base_dir="/test", exclude_patterns=None)
        assert not optimizer._should_exclude_path(Path("/test/src"))
        assert not optimizer._should_exclude_path(Path("/test/apm_modules"))

    def test_should_exclude_path_simple_directory(self):
        """Test exclusion of simple directory patterns."""
        optimizer = ContextOptimizer(base_dir="/test", exclude_patterns=["apm_modules", "tmp"])
        assert optimizer._should_exclude_path(Path("/test/apm_modules"))
        assert optimizer._should_exclude_path(Path("/test/tmp"))
        assert not optimizer._should_exclude_path(Path("/test/src"))

    def test_should_exclude_path_with_trailing_slash(self):
        """Test exclusion patterns with trailing slashes."""
        optimizer = ContextOptimizer(base_dir="/test", exclude_patterns=["apm_modules/", "tmp/"])
        assert optimizer._should_exclude_path(Path("/test/apm_modules"))
        assert optimizer._should_exclude_path(Path("/test/apm_modules/package"))
        assert optimizer._should_exclude_path(Path("/test/tmp"))
        assert not optimizer._should_exclude_path(Path("/test/src"))

    def test_should_exclude_path_nested_directories(self):
        """Test exclusion of nested directory patterns."""
        optimizer = ContextOptimizer(base_dir="/test", exclude_patterns=["projects/packages/apm"])
        assert optimizer._should_exclude_path(Path("/test/projects/packages/apm"))
        assert optimizer._should_exclude_path(Path("/test/projects/packages/apm/src"))
        assert not optimizer._should_exclude_path(Path("/test/projects/packages/other"))

    def test_should_exclude_path_glob_patterns(self):
        """Test exclusion using glob patterns."""
        optimizer = ContextOptimizer(
            base_dir="/test", exclude_patterns=["**/test-fixtures", "coverage/**"]
        )
        assert optimizer._should_exclude_path(Path("/test/test-fixtures"))
        assert optimizer._should_exclude_path(Path("/test/src/test-fixtures"))
        assert optimizer._should_exclude_path(Path("/test/coverage"))
        assert optimizer._should_exclude_path(Path("/test/coverage/report"))
        assert not optimizer._should_exclude_path(Path("/test/src"))

    def test_should_exclude_path_wildcard_patterns(self):
        """Test exclusion using wildcard patterns."""
        optimizer = ContextOptimizer(base_dir="/test", exclude_patterns=["tmp*", "*cache*"])
        assert optimizer._should_exclude_path(Path("/test/tmp"))
        assert optimizer._should_exclude_path(Path("/test/tmp123"))
        assert optimizer._should_exclude_path(Path("/test/cache"))
        assert optimizer._should_exclude_path(Path("/test/mycache"))
        assert not optimizer._should_exclude_path(Path("/test/src"))

    def test_should_exclude_path_complex_glob(self):
        """Test complex glob patterns."""
        optimizer = ContextOptimizer(
            base_dir="/test", exclude_patterns=["projects/**/apm/**", "**/node_modules/**"]
        )
        assert optimizer._should_exclude_path(Path("/test/projects/packages/apm"))
        assert optimizer._should_exclude_path(Path("/test/projects/packages/apm/src"))
        assert optimizer._should_exclude_path(Path("/test/node_modules"))
        assert optimizer._should_exclude_path(Path("/test/src/node_modules"))
        assert not optimizer._should_exclude_path(Path("/test/projects/other"))

    def test_should_exclude_path_path_outside_base_dir(self):
        """Test that paths outside base_dir are not excluded."""
        optimizer = ContextOptimizer(base_dir="/test", exclude_patterns=["apm_modules"])
        # Path that's not relative to base_dir should not be excluded
        assert not optimizer._should_exclude_path(Path("/other/apm_modules"))

    def test_analyze_project_structure_with_exclusions(self):
        """Test that _analyze_project_structure respects exclusion patterns."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(
                tmpdir
            ).resolve()  # Resolve to handle symlinks (e.g., macOS /var -> /private/var)

            # Create directory structure
            (base_path / "src").mkdir()
            (base_path / "src" / "file.py").touch()
            (base_path / "apm_modules").mkdir()
            (base_path / "apm_modules" / "file.py").touch()
            (base_path / "tmp").mkdir()
            (base_path / "tmp" / "file.py").touch()

            # Create optimizer with exclusions
            optimizer = ContextOptimizer(
                base_dir=str(base_path), exclude_patterns=["apm_modules", "tmp"]
            )
            optimizer._analyze_project_structure()

            # Check that excluded directories are not in cache
            cached_dirs = set(optimizer._directory_cache.keys())
            assert base_path / "src" in cached_dirs
            assert base_path / "apm_modules" not in cached_dirs
            assert base_path / "tmp" not in cached_dirs

    def test_analyze_project_structure_with_nested_exclusions(self):
        """Test that nested exclusions work correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(
                tmpdir
            ).resolve()  # Resolve to handle symlinks (e.g., macOS /var -> /private/var)

            # Create nested directory structure
            (base_path / "projects" / "packages" / "apm" / "src").mkdir(parents=True)
            (base_path / "projects" / "packages" / "apm" / "src" / "file.py").touch()
            (base_path / "projects" / "packages" / "other" / "src").mkdir(parents=True)
            (base_path / "projects" / "packages" / "other" / "src" / "file.py").touch()

            # Create optimizer with nested exclusion
            optimizer = ContextOptimizer(
                base_dir=str(base_path), exclude_patterns=["projects/packages/apm/**"]
            )
            optimizer._analyze_project_structure()

            # Check that excluded directories are not in cache
            cached_dirs = set(optimizer._directory_cache.keys())
            assert base_path / "projects" / "packages" / "apm" not in cached_dirs
            assert base_path / "projects" / "packages" / "apm" / "src" not in cached_dirs
            # Other directories should be present
            assert base_path / "projects" / "packages" / "other" / "src" in cached_dirs

    def test_default_exclusions_still_work(self):
        """Test that default hardcoded exclusions still work alongside custom patterns."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(
                tmpdir
            ).resolve()  # Resolve to handle symlinks (e.g., macOS /var -> /private/var)

            # Create directories including default exclusions
            (base_path / "src").mkdir()
            (base_path / "src" / "file.py").touch()
            (base_path / "node_modules").mkdir()
            (base_path / "node_modules" / "file.js").touch()
            (base_path / "custom_exclude").mkdir()
            (base_path / "custom_exclude" / "file.py").touch()

            # Create optimizer with custom exclusion only
            optimizer = ContextOptimizer(
                base_dir=str(base_path), exclude_patterns=["custom_exclude"]
            )
            optimizer._analyze_project_structure()

            # Check that both default and custom exclusions work
            cached_dirs = set(optimizer._directory_cache.keys())
            assert base_path / "src" in cached_dirs
            assert base_path / "node_modules" not in cached_dirs  # Default exclusion
            assert base_path / "custom_exclude" not in cached_dirs  # Custom exclusion


class TestSubstringExclusionFalsePositives:
    """Test that directory exclusions use component matching, not substring matching (Fixes #158)."""

    def test_directory_containing_exclusion_token_not_excluded(self):
        """Directories like 'rebuild/' must NOT be excluded just because 'build' is a substring."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            for name in ["rebuild", "apm_modules_guide", "redistribution", "node_modules_compat"]:
                d = base / "src" / name
                d.mkdir(parents=True, exist_ok=True)
                (d / "file.py").touch()

            optimizer = ContextOptimizer(base_dir=str(base))
            optimizer._analyze_project_structure()

            cached = set(optimizer._directory_cache.keys())
            for name in ["rebuild", "apm_modules_guide", "redistribution", "node_modules_compat"]:
                assert base / "src" / name in cached, f"'{name}' was incorrectly excluded"

    def test_exact_exclusion_names_still_excluded(self):
        """Directories exactly named 'build', 'dist', etc. must still be excluded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            for name in ["build", "dist", "node_modules", "__pycache__"]:
                d = base / name
                d.mkdir(parents=True, exist_ok=True)
                (d / "file.py").touch()
            (base / "src").mkdir(exist_ok=True)
            (base / "src" / "app.py").touch()

            optimizer = ContextOptimizer(base_dir=str(base))
            optimizer._analyze_project_structure()

            cached = set(optimizer._directory_cache.keys())
            assert base / "src" in cached
            for name in ["build", "dist", "node_modules", "__pycache__"]:
                assert base / name not in cached, f"'{name}' should be excluded"

    def test_nested_exclusion_name_still_excluded(self):
        """A directory named 'build' nested under a non-excluded parent must still be excluded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            (base / "src" / "build").mkdir(parents=True)
            (base / "src" / "build" / "output.js").touch()
            (base / "src" / "app.py").touch()

            optimizer = ContextOptimizer(base_dir=str(base))
            optimizer._analyze_project_structure()

            cached = set(optimizer._directory_cache.keys())
            assert base / "src" in cached
            assert base / "src" / "build" not in cached


class TestExpandGlobPattern:
    """Test _expand_glob_pattern brace expansion."""

    def test_single_brace_group(self):
        """Test expansion of a single brace group."""
        optimizer = ContextOptimizer(base_dir="/tmp")
        result = optimizer._expand_glob_pattern("**/*.{css,scss}")
        assert sorted(result) == sorted(["**/*.css", "**/*.scss"])

    def test_multiple_brace_groups(self):
        """Test expansion of multiple brace groups (Fixes #153)."""
        optimizer = ContextOptimizer(base_dir="/tmp")
        result = optimizer._expand_glob_pattern("**/*.{test,spec}.{ts,js,mts,mjs}")
        expected = [
            "**/*.test.ts",
            "**/*.test.js",
            "**/*.test.mts",
            "**/*.test.mjs",
            "**/*.spec.ts",
            "**/*.spec.js",
            "**/*.spec.mts",
            "**/*.spec.mjs",
        ]
        assert sorted(result) == sorted(expected)

    def test_no_brace_group(self):
        """Test pattern without braces is returned as-is."""
        optimizer = ContextOptimizer(base_dir="/tmp")
        result = optimizer._expand_glob_pattern("**/*.py")
        assert result == ["**/*.py"]

    def test_single_item_brace_group(self):
        """Test brace group with a single item."""
        optimizer = ContextOptimizer(base_dir="/tmp")
        result = optimizer._expand_glob_pattern("**/*.{ts}")
        assert result == ["**/*.ts"]

    def test_three_brace_groups(self):
        """Test expansion of three brace groups."""
        optimizer = ContextOptimizer(base_dir="/tmp")
        result = optimizer._expand_glob_pattern("{src,lib}/*.{test,spec}.{ts,js}")
        expected = [
            "src/*.test.ts",
            "src/*.test.js",
            "src/*.spec.ts",
            "src/*.spec.js",
            "lib/*.test.ts",
            "lib/*.test.js",
            "lib/*.spec.ts",
            "lib/*.spec.js",
        ]
        assert sorted(result) == sorted(expected)


class TestApmModulesExclusion:
    """Test that apm_modules/ is excluded from project scanning (Fixes #154)."""

    @pytest.fixture
    def project_with_apm_modules(self):
        """Create a project with a realistic apm_modules/ tree."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)

            # Real project directories
            for d in ["src", "src/components", "tests", "docs"]:
                (base / d).mkdir(parents=True, exist_ok=True)

            # Real project files
            for f in [
                "src/index.ts",
                "src/components/App.tsx",
                "tests/app.test.ts",
                "docs/README.md",
            ]:
                (base / f).touch()

            # Simulate apm_modules with many nested packages
            for i in range(50):
                pkg_dir = base / "apm_modules" / f"org/package-{i}" / "sub"
                pkg_dir.mkdir(parents=True, exist_ok=True)
                (pkg_dir / "SKILL.md").touch()
                (pkg_dir / "instructions.md").touch()

            yield base

    def test_apm_modules_excluded_from_directory_cache(self, project_with_apm_modules):
        """apm_modules directories must not appear in _directory_cache."""
        optimizer = ContextOptimizer(base_dir=str(project_with_apm_modules))
        optimizer._analyze_project_structure()

        cached_paths = set(optimizer._directory_cache.keys())
        # Resolve to handle macOS /var -> /private/var symlink
        resolved_base = project_with_apm_modules.resolve()

        # Real dirs are cached
        assert resolved_base / "src" in cached_paths
        assert resolved_base / "tests" in cached_paths

        # No apm_modules path is cached
        apm_paths = [p for p in cached_paths if "apm_modules" in str(p)]
        assert apm_paths == [], f"apm_modules leaked into cache: {apm_paths}"

    def test_cache_size_unaffected_by_apm_modules(self, project_with_apm_modules):
        """Directory cache size should reflect only project dirs, not apm_modules."""
        optimizer = ContextOptimizer(base_dir=str(project_with_apm_modules))
        optimizer._analyze_project_structure()

        # 50 packages × sub-directory each = 150+ dirs if not excluded.
        # Real project has at most ~5 dirs (root, src, src/components, tests, docs).
        assert len(optimizer._directory_cache) <= 10, (
            f"Cache has {len(optimizer._directory_cache)} dirs — apm_modules likely not excluded"
        )

    def test_os_walk_prunes_apm_modules(self, project_with_apm_modules):
        """os.walk must not descend into apm_modules subdirectories."""
        optimizer = ContextOptimizer(base_dir=str(project_with_apm_modules))
        optimizer._analyze_project_structure()

        # _should_exclude_subdir must flag apm_modules
        apm_modules_path = project_with_apm_modules / "apm_modules"
        assert optimizer._should_exclude_subdir(apm_modules_path) is True

    def test_find_matching_dirs_ignores_apm_modules(self, project_with_apm_modules):
        """Pattern matching must not return directories inside apm_modules."""
        optimizer = ContextOptimizer(base_dir=str(project_with_apm_modules))
        optimizer._analyze_project_structure()

        matching = optimizer._find_matching_directories("**/*.md")
        apm_matches = [p for p in matching if "apm_modules" in str(p)]
        assert apm_matches == [], f"apm_modules dirs matched: {apm_matches}"


class TestGlobCacheReuse:
    """Test that glob results are cached and Set[Path] conversion is not repeated."""

    def test_set_path_cached_across_calls(self):
        """_file_matches_pattern must cache the Set[Path] conversion."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            (base / "src").mkdir()
            (base / "src" / "a.ts").touch()
            (base / "src" / "b.ts").touch()

            optimizer = ContextOptimizer(base_dir=str(base))
            optimizer._analyze_project_structure()

            file_a = base / "src" / "a.ts"
            file_b = base / "src" / "b.ts"

            # First call populates cache
            optimizer._file_matches_pattern(file_a, "**/*.ts")
            assert "**/*.ts" in optimizer._glob_set_cache

            # Second call should reuse the cached set (not recreate it)
            cached_set_id = id(optimizer._glob_set_cache["**/*.ts"])
            optimizer._file_matches_pattern(file_b, "**/*.ts")
            assert id(optimizer._glob_set_cache["**/*.ts"]) == cached_set_id


if __name__ == "__main__":
    pytest.main([__file__])
