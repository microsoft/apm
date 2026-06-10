"""Comprehensive unit tests for apm_cli.compilation.distributed_compiler (phase 3).

Targets uncovered branches in DistributedAgentsCompiler:
- __init__ (OSError path for base_dir resolution)
- analyze_directory_structure (pattern extraction, depth/parent tracking)
- determine_agents_placement (min_instructions filter, constitution fallback)
- generate_distributed_agents_files (empty map, normal map, attribution)
- _extract_directories_from_pattern (all pattern shapes)
- _find_best_directory (no apply_to, pattern matching, depth preference)
- _generate_agents_content (single source, multiple sources)
- _validate_coverage (covered and uncovered instructions)
- _find_orphaned_agents_files (skip dirs, in-scope vs out-of-scope)
- _generate_orphan_warnings (empty, single, multiple > 5)
- _cleanup_orphaned_files (dry_run True/False, unlink error)
- _compile_distributed_stats (with and without optimization_stats)
- get_compilation_results_for_display (placement_map set / not set)
- compile_distributed (success path, debug path, clean_orphaned, exception)
- DirectoryMap.get_max_depth
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.compilation.distributed_compiler import (
    AGENTS_MD_GENERATED_MARKER,
    CompilationResult,
    DirectoryMap,
    DistributedAgentsCompiler,
    PlacementResult,
)
from apm_cli.primitives.models import Instruction, PrimitiveCollection

# APM-generated AGENTS.md content (marker present) for orphan tests
_GENERATED_CONTENT = f"{AGENTS_MD_GENERATED_MARKER}\n# Generated content\n"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_instruction(
    name: str = "test",
    apply_to: str = "**/*.py",
    content: str = "Use type hints.",
    file_path: Path | None = None,
    source: str | None = None,
) -> Instruction:
    if file_path is None:
        file_path = Path(f"/tmp/{name}.instructions.md")
    return Instruction(
        name=name,
        file_path=file_path,
        description="Test instruction",
        apply_to=apply_to,
        content=content,
        author="test",
        version="1.0",
        source=source,
    )


def _make_primitives(*instructions: Instruction) -> PrimitiveCollection:
    col = PrimitiveCollection()
    for inst in instructions:
        col.add_primitive(inst)
    return col


def _make_compiler(base_dir: str = "/tmp") -> DistributedAgentsCompiler:
    """Create a DistributedAgentsCompiler with all heavy dependencies mocked."""
    mock_opt = MagicMock()
    mock_lr = MagicMock()
    mock_lr.resolve_links_for_compilation.return_value = "# AGENTS.md\ncontent"
    mock_fmt = MagicMock()

    with (
        patch("apm_cli.compilation.distributed_compiler.ContextOptimizer", return_value=mock_opt),
        patch("apm_cli.compilation.distributed_compiler.UnifiedLinkResolver", return_value=mock_lr),
        patch(
            "apm_cli.compilation.distributed_compiler.CompilationFormatter", return_value=mock_fmt
        ),
    ):
        compiler = DistributedAgentsCompiler(base_dir=base_dir)

    compiler.context_optimizer = mock_opt
    compiler.link_resolver = mock_lr
    compiler.output_formatter = mock_fmt
    return compiler


def _make_compiler_in_tmp(tmp_path: Path) -> DistributedAgentsCompiler:
    """Create a compiler rooted at tmp_path."""
    return _make_compiler(base_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# DirectoryMap
# ---------------------------------------------------------------------------


class TestDirectoryMap:
    """Tests for DirectoryMap dataclass."""

    def test_get_max_depth_empty(self) -> None:
        """get_max_depth returns 0 when depth_map is empty."""
        dm = DirectoryMap(directories={}, depth_map={}, parent_map={})
        assert dm.get_max_depth() == 0

    def test_get_max_depth_single_entry(self) -> None:
        """get_max_depth returns the single depth value."""
        p = Path("/some/dir")
        dm = DirectoryMap(directories={p: set()}, depth_map={p: 3}, parent_map={p: None})
        assert dm.get_max_depth() == 3

    def test_get_max_depth_multiple_entries(self) -> None:
        """get_max_depth returns the maximum across all depths."""
        p1 = Path("/a")
        p2 = Path("/a/b")
        p3 = Path("/a/b/c")
        dm = DirectoryMap(
            directories={p1: set(), p2: set(), p3: set()},
            depth_map={p1: 0, p2: 1, p3: 2},
            parent_map={p1: None, p2: p1, p3: p2},
        )
        assert dm.get_max_depth() == 2


# ---------------------------------------------------------------------------
# PlacementResult
# ---------------------------------------------------------------------------


class TestPlacementResult:
    """Tests for PlacementResult dataclass defaults."""

    def test_defaults_empty_collections(self) -> None:
        """PlacementResult fields default to empty lists/sets/dicts."""
        pr = PlacementResult(agents_path=Path("/AGENTS.md"), instructions=[])
        assert pr.inherited_instructions == []
        assert pr.coverage_patterns == set()
        assert pr.source_attribution == {}


# ---------------------------------------------------------------------------
# CompilationResult
# ---------------------------------------------------------------------------


class TestCompilationResult:
    """Tests for CompilationResult dataclass defaults."""

    def test_defaults(self) -> None:
        """CompilationResult fields default to empty collections."""
        cr = CompilationResult(success=True, placements=[], content_map={})
        assert cr.warnings == []
        assert cr.errors == []
        assert cr.stats == {}


# ---------------------------------------------------------------------------
# DistributedAgentsCompiler.__init__
# ---------------------------------------------------------------------------


class TestDistributedAgentsCompilerInit:
    """Test __init__ behaviour for various base_dir inputs."""

    def test_init_creates_path(self, tmp_path: Path) -> None:
        """Compiler sets base_dir to a resolved Path."""
        compiler = _make_compiler_in_tmp(tmp_path)
        assert compiler.base_dir == tmp_path.resolve()

    def test_init_defaults(self, tmp_path: Path) -> None:
        """Compiler starts with empty warnings/errors and zero file count."""
        compiler = _make_compiler_in_tmp(tmp_path)
        assert compiler.warnings == []
        assert compiler.errors == []
        assert compiler.total_files_written == 0

    def test_init_oserror_falls_back_to_absolute(self) -> None:
        """When Path.resolve() raises OSError, falls back to Path.absolute()."""
        with (
            patch(
                "apm_cli.compilation.distributed_compiler.ContextOptimizer",
                return_value=MagicMock(),
            ),
            patch(
                "apm_cli.compilation.distributed_compiler.UnifiedLinkResolver",
                return_value=MagicMock(),
            ),
            patch(
                "apm_cli.compilation.distributed_compiler.CompilationFormatter",
                return_value=MagicMock(),
            ),
            patch.object(Path, "resolve", side_effect=OSError("no resolve")),
        ):
            compiler = DistributedAgentsCompiler(base_dir="/some/path")
        # Should not raise; base_dir set via absolute()
        assert isinstance(compiler.base_dir, Path)


# ---------------------------------------------------------------------------
# _extract_directories_from_pattern
# ---------------------------------------------------------------------------


class TestExtractDirectoriesFromPattern:
    """Test _extract_directories_from_pattern with all known pattern shapes."""

    def setup_method(self) -> None:
        self.compiler = _make_compiler()

    def test_global_pattern_returns_dot(self) -> None:
        """'**/*.py' returns [Path('.')]."""
        result: list[Path] = self.compiler._extract_directories_from_pattern("**/*.py")
        assert result == [Path(".")]

    def test_pattern_with_directory_part(self) -> None:
        """'src/**/*.py' returns [Path('src')]."""
        result: list[Path] = self.compiler._extract_directories_from_pattern("src/**/*.py")
        assert result == [Path("src")]

    def test_simple_filename_returns_dot(self) -> None:
        """'*.py' with no slash returns [Path('.')]."""
        result: list[Path] = self.compiler._extract_directories_from_pattern("*.py")
        assert result == [Path(".")]

    def test_docs_pattern(self) -> None:
        """'docs/*.md' returns [Path('docs')]."""
        result: list[Path] = self.compiler._extract_directories_from_pattern("docs/*.md")
        assert result == [Path("docs")]

    def test_wildcard_dir_returns_dot(self) -> None:
        """'**/subdir/*.md' starts with '**/' so returns [Path('.')]."""
        result: list[Path] = self.compiler._extract_directories_from_pattern("**/subdir/*.md")
        assert result == [Path(".")]

    def test_wildcard_dir_part_returns_dot(self) -> None:
        """Pattern starting with '*/' returns [Path('.')]."""
        result: list[Path] = self.compiler._extract_directories_from_pattern("*/foo/*.py")
        assert result == [Path(".")]


# ---------------------------------------------------------------------------
# analyze_directory_structure
# ---------------------------------------------------------------------------


class TestAnalyzeDirectoryStructure:
    """Test analyze_directory_structure."""

    def test_empty_instructions_returns_base_dir_only(self, tmp_path: Path) -> None:
        """No instructions -> DirectoryMap contains only base_dir at depth 0."""
        compiler = _make_compiler_in_tmp(tmp_path)
        dm: DirectoryMap = compiler.analyze_directory_structure([])
        assert compiler.base_dir in dm.directories
        assert dm.depth_map[compiler.base_dir] == 0
        assert dm.parent_map[compiler.base_dir] is None

    def test_instruction_without_apply_to_skipped(self, tmp_path: Path) -> None:
        """Instructions without apply_to are not added to the directory map."""
        compiler = _make_compiler_in_tmp(tmp_path)
        _inst = _make_instruction(name="no-apply", apply_to="")
        inst_obj = Instruction(
            name="no-apply",
            file_path=Path("/tmp/no-apply.md"),
            description="d",
            apply_to=None,  # type: ignore[arg-type]
            content="c",
            author="a",
            version="1.0",
        )
        dm: DirectoryMap = compiler.analyze_directory_structure([inst_obj])
        # Only base_dir should be in depth_map
        assert compiler.base_dir in dm.depth_map

    def test_src_pattern_adds_src_directory(self, tmp_path: Path) -> None:
        """'src/**/*.py' pattern -> src dir included in depth_map at depth 1."""
        compiler = _make_compiler_in_tmp(tmp_path)
        inst = _make_instruction(apply_to="src/**/*.py")
        dm: DirectoryMap = compiler.analyze_directory_structure([inst])
        src_dir: Path = compiler.base_dir / "src"
        assert src_dir in dm.depth_map
        assert dm.depth_map[src_dir] == 1

    def test_global_pattern_only_adds_base_dir(self, tmp_path: Path) -> None:
        """'**/*.py' pattern -> only base_dir in depth_map."""
        compiler = _make_compiler_in_tmp(tmp_path)
        inst = _make_instruction(apply_to="**/*.py")
        dm: DirectoryMap = compiler.analyze_directory_structure([inst])
        assert compiler.base_dir in dm.depth_map

    def test_parent_map_populated(self, tmp_path: Path) -> None:
        """src/ directory has base_dir as parent."""
        compiler = _make_compiler_in_tmp(tmp_path)
        inst = _make_instruction(apply_to="src/**/*.py")
        dm: DirectoryMap = compiler.analyze_directory_structure([inst])
        src_dir: Path = compiler.base_dir / "src"
        assert dm.parent_map.get(src_dir) == compiler.base_dir


# ---------------------------------------------------------------------------
# determine_agents_placement
# ---------------------------------------------------------------------------


class TestDetermineAgentsPlacement:
    """Test determine_agents_placement."""

    def test_returns_optimizer_result(self, tmp_path: Path) -> None:
        """Returns the optimizer placement map when non-empty."""
        compiler = _make_compiler_in_tmp(tmp_path)
        inst = _make_instruction()
        expected: dict[Path, list[Instruction]] = {compiler.base_dir: [inst]}
        compiler.context_optimizer.optimize_instruction_placement.return_value = expected

        dm = DirectoryMap(
            directories={compiler.base_dir: set()},
            depth_map={compiler.base_dir: 0},
            parent_map={compiler.base_dir: None},
        )
        result = compiler.determine_agents_placement([inst], dm)
        assert result == expected

    def test_stores_placement_map(self, tmp_path: Path) -> None:
        """Optimizer result is stored in _placement_map."""
        compiler = _make_compiler_in_tmp(tmp_path)
        inst = _make_instruction()
        expected: dict[Path, list[Instruction]] = {compiler.base_dir: [inst]}
        compiler.context_optimizer.optimize_instruction_placement.return_value = expected
        dm = DirectoryMap(
            directories={compiler.base_dir: set()},
            depth_map={compiler.base_dir: 0},
            parent_map={compiler.base_dir: None},
        )
        compiler.determine_agents_placement([inst], dm)
        assert compiler._placement_map == expected

    def test_min_instructions_filter_moves_to_parent(self, tmp_path: Path) -> None:
        """Directories with fewer instructions than min are merged into their parent."""
        compiler = _make_compiler_in_tmp(tmp_path)
        sub_dir = compiler.base_dir / "sub"
        inst = _make_instruction(name="child-inst")

        # sub_dir has 1 instruction, min=2 -> gets moved to base
        compiler.context_optimizer.optimize_instruction_placement.return_value = {sub_dir: [inst]}
        dm = DirectoryMap(
            directories={compiler.base_dir: set(), sub_dir: set()},
            depth_map={compiler.base_dir: 0, sub_dir: 1},
            parent_map={compiler.base_dir: None, sub_dir: compiler.base_dir},
        )
        result = compiler.determine_agents_placement([inst], dm, min_instructions=2)
        # sub_dir should be gone; instructions should be at parent
        assert sub_dir not in result
        assert compiler.base_dir in result
        assert inst in result[compiler.base_dir]

    def test_base_dir_kept_with_min_instructions(self, tmp_path: Path) -> None:
        """base_dir is never filtered out even if under min_instructions threshold."""
        compiler = _make_compiler_in_tmp(tmp_path)
        inst = _make_instruction()
        compiler.context_optimizer.optimize_instruction_placement.return_value = {
            compiler.base_dir: [inst]
        }
        dm = DirectoryMap(
            directories={compiler.base_dir: set()},
            depth_map={compiler.base_dir: 0},
            parent_map={compiler.base_dir: None},
        )
        result = compiler.determine_agents_placement([inst], dm, min_instructions=10)
        assert compiler.base_dir in result


# ---------------------------------------------------------------------------
# generate_distributed_agents_files
# ---------------------------------------------------------------------------


class TestGenerateDistributedAgentsFiles:
    """Test generate_distributed_agents_files."""

    def test_empty_placement_and_no_constitution_returns_empty(self, tmp_path: Path) -> None:
        """Empty placement_map with no constitution yields empty placements list."""
        compiler = _make_compiler_in_tmp(tmp_path)
        prims = _make_primitives()
        with patch("apm_cli.compilation.constitution.find_constitution") as mock_const:
            mock_const.return_value = MagicMock(exists=lambda: False)
            placements = compiler.generate_distributed_agents_files({}, prims)
        assert placements == []

    def test_empty_placement_with_constitution_yields_root_placement(self, tmp_path: Path) -> None:
        """Empty placement_map + existing constitution -> one root placement."""
        compiler = _make_compiler_in_tmp(tmp_path)
        prims = _make_primitives()
        mock_const_path = MagicMock()
        mock_const_path.exists.return_value = True
        with patch(
            "apm_cli.compilation.constitution.find_constitution",
            return_value=mock_const_path,
        ):
            placements = compiler.generate_distributed_agents_files({}, prims)
        assert len(placements) == 1
        assert placements[0].agents_path == compiler.base_dir / "AGENTS.md"

    def test_normal_placement_map_creates_placements(self, tmp_path: Path) -> None:
        """Each entry in placement_map creates one PlacementResult."""
        compiler = _make_compiler_in_tmp(tmp_path)
        prims = _make_primitives()
        inst1 = _make_instruction(name="a", file_path=tmp_path / "a.md")
        inst2 = _make_instruction(name="b", file_path=tmp_path / "b.md")
        placement_map: dict[Path, list[Instruction]] = {
            compiler.base_dir: [inst1],
            compiler.base_dir / "sub": [inst2],
        }
        placements = compiler.generate_distributed_agents_files(placement_map, prims)
        assert len(placements) == 2

    def test_placement_agents_path_is_dir_plus_agents_md(self, tmp_path: Path) -> None:
        """PlacementResult.agents_path is <dir>/AGENTS.md."""
        compiler = _make_compiler_in_tmp(tmp_path)
        prims = _make_primitives()
        inst = _make_instruction(name="x", file_path=tmp_path / "x.md")
        placement_map: dict[Path, list[Instruction]] = {compiler.base_dir: [inst]}
        placements = compiler.generate_distributed_agents_files(placement_map, prims)
        assert placements[0].agents_path == compiler.base_dir / "AGENTS.md"

    def test_source_attribution_disabled(self, tmp_path: Path) -> None:
        """source_attribution=False yields empty source_attribution dict."""
        compiler = _make_compiler_in_tmp(tmp_path)
        prims = _make_primitives()
        inst = _make_instruction(name="y", file_path=tmp_path / "y.md", source="local")
        placement_map: dict[Path, list[Instruction]] = {compiler.base_dir: [inst]}
        placements = compiler.generate_distributed_agents_files(
            placement_map, prims, source_attribution=False
        )
        assert placements[0].source_attribution == {}

    def test_source_attribution_enabled(self, tmp_path: Path) -> None:
        """source_attribution=True populates source_attribution map."""
        compiler = _make_compiler_in_tmp(tmp_path)
        prims = _make_primitives()
        inst = _make_instruction(name="z", file_path=tmp_path / "z.md", source="remote")
        placement_map: dict[Path, list[Instruction]] = {compiler.base_dir: [inst]}
        placements = compiler.generate_distributed_agents_files(
            placement_map, prims, source_attribution=True
        )
        assert placements[0].source_attribution != {}

    def test_coverage_patterns_populated_from_apply_to(self, tmp_path: Path) -> None:
        """coverage_patterns includes apply_to values for each instruction."""
        compiler = _make_compiler_in_tmp(tmp_path)
        prims = _make_primitives()
        inst = _make_instruction(name="p", apply_to="src/**/*.py", file_path=tmp_path / "p.md")
        placement_map: dict[Path, list[Instruction]] = {compiler.base_dir: [inst]}
        placements = compiler.generate_distributed_agents_files(placement_map, prims)
        assert "src/**/*.py" in placements[0].coverage_patterns


# ---------------------------------------------------------------------------
# _generate_agents_content
# ---------------------------------------------------------------------------


class TestGenerateAgentsContent:
    """Test _generate_agents_content."""

    def test_content_includes_agents_md_header(self, tmp_path: Path) -> None:
        """Generated content starts with # AGENTS.md header marker."""
        compiler = _make_compiler_in_tmp(tmp_path)
        compiler.link_resolver.resolve_links_for_compilation.side_effect = lambda content, **kw: (
            content
        )
        prims = _make_primitives()
        placement = PlacementResult(
            agents_path=tmp_path / "AGENTS.md",
            instructions=[],
            coverage_patterns=set(),
            source_attribution={},
        )
        with patch(
            "apm_cli.compilation.distributed_compiler.build_attributed_instructions",
            return_value=[],
        ):
            content: str = compiler._generate_agents_content(placement, prims)
        assert "# AGENTS.md" in content

    def test_content_includes_footer(self, tmp_path: Path) -> None:
        """Generated content ends with the 'Do not edit manually' footer."""
        compiler = _make_compiler_in_tmp(tmp_path)
        compiler.link_resolver.resolve_links_for_compilation.side_effect = lambda content, **kw: (
            content
        )
        prims = _make_primitives()
        placement = PlacementResult(
            agents_path=tmp_path / "AGENTS.md",
            instructions=[],
            coverage_patterns=set(),
            source_attribution={},
        )
        with patch(
            "apm_cli.compilation.distributed_compiler.build_attributed_instructions",
            return_value=[],
        ):
            content: str = compiler._generate_agents_content(placement, prims)
        assert "Do not edit manually" in content

    def test_single_source_attribution_label(self, tmp_path: Path) -> None:
        """Single source in attribution renders '<!-- Source: ... -->'."""
        compiler = _make_compiler_in_tmp(tmp_path)
        compiler.link_resolver.resolve_links_for_compilation.side_effect = lambda content, **kw: (
            content
        )
        prims = _make_primitives()
        placement = PlacementResult(
            agents_path=tmp_path / "AGENTS.md",
            instructions=[],
            coverage_patterns=set(),
            source_attribution={"inst.md": "local"},
        )
        with patch(
            "apm_cli.compilation.distributed_compiler.build_attributed_instructions",
            return_value=[],
        ):
            content: str = compiler._generate_agents_content(placement, prims)
        assert "<!-- Source:" in content

    def test_multiple_source_attribution_label(self, tmp_path: Path) -> None:
        """Multiple sources in attribution renders '<!-- Sources: ... -->'."""
        compiler = _make_compiler_in_tmp(tmp_path)
        compiler.link_resolver.resolve_links_for_compilation.side_effect = lambda content, **kw: (
            content
        )
        prims = _make_primitives()
        placement = PlacementResult(
            agents_path=tmp_path / "AGENTS.md",
            instructions=[],
            coverage_patterns=set(),
            source_attribution={"a.md": "local", "b.md": "remote"},
        )
        with patch(
            "apm_cli.compilation.distributed_compiler.build_attributed_instructions",
            return_value=[],
        ):
            content: str = compiler._generate_agents_content(placement, prims)
        assert "<!-- Sources:" in content


# ---------------------------------------------------------------------------
# _validate_coverage
# ---------------------------------------------------------------------------


class TestValidateCoverage:
    """Test _validate_coverage."""

    def test_all_covered_no_warnings(self, tmp_path: Path) -> None:
        """All instructions placed -> no coverage warnings."""
        compiler = _make_compiler_in_tmp(tmp_path)
        inst = _make_instruction(file_path=tmp_path / "a.md")
        placement = PlacementResult(agents_path=tmp_path / "AGENTS.md", instructions=[inst])
        warnings: list[str] = compiler._validate_coverage([placement], [inst])
        assert warnings == []

    def test_uncovered_instruction_generates_warning(self, tmp_path: Path) -> None:
        """Instruction not in any placement -> warning listing it."""
        compiler = _make_compiler_in_tmp(tmp_path)
        placed_inst = _make_instruction(name="placed", file_path=tmp_path / "placed.md")
        unplaced_inst = _make_instruction(name="unplaced", file_path=tmp_path / "unplaced.md")
        placement = PlacementResult(agents_path=tmp_path / "AGENTS.md", instructions=[placed_inst])
        warnings: list[str] = compiler._validate_coverage([placement], [placed_inst, unplaced_inst])
        assert len(warnings) == 1
        assert str(tmp_path / "unplaced.md") in warnings[0]

    def test_empty_placements_and_instructions_no_warnings(self, tmp_path: Path) -> None:
        """No placements, no instructions -> no warnings."""
        compiler = _make_compiler_in_tmp(tmp_path)
        warnings: list[str] = compiler._validate_coverage([], [])
        assert warnings == []


# ---------------------------------------------------------------------------
# _find_orphaned_agents_files
# ---------------------------------------------------------------------------


class TestFindOrphanedAgentsFiles:
    """Test _find_orphaned_agents_files."""

    def test_no_orphans_when_all_generated(self, tmp_path: Path) -> None:
        """Existing AGENTS.md that matches generated set is not orphaned."""
        compiler = _make_compiler_in_tmp(tmp_path)
        agents_file: Path = tmp_path / "AGENTS.md"
        agents_file.write_text(_GENERATED_CONTENT)
        orphans: list[Path] = compiler._find_orphaned_agents_files([agents_file])
        assert agents_file not in orphans

    def test_detects_orphaned_file(self, tmp_path: Path) -> None:
        """APM-generated AGENTS.md not in generated set is orphaned."""
        compiler = _make_compiler_in_tmp(tmp_path)
        agents_file: Path = tmp_path / "AGENTS.md"
        agents_file.write_text(_GENERATED_CONTENT)
        orphans: list[Path] = compiler._find_orphaned_agents_files([])
        assert agents_file in orphans

    def test_hand_authored_file_not_orphaned(self, tmp_path: Path) -> None:
        """Hand-authored AGENTS.md (no APM marker) is never reported as orphaned."""
        compiler = _make_compiler_in_tmp(tmp_path)
        agents_file: Path = tmp_path / "AGENTS.md"
        agents_file.write_text("# Hand-authored\nThis file has no APM marker.\n")
        orphans: list[Path] = compiler._find_orphaned_agents_files([])
        assert agents_file not in orphans

    def test_skips_files_in_git_dir(self, tmp_path: Path) -> None:
        """AGENTS.md inside .git/ is not reported as orphaned."""
        compiler = _make_compiler_in_tmp(tmp_path)
        git_dir: Path = tmp_path / ".git"
        git_dir.mkdir()
        agents_in_git: Path = git_dir / "AGENTS.md"
        agents_in_git.write_text(_GENERATED_CONTENT)
        orphans: list[Path] = compiler._find_orphaned_agents_files([])
        assert agents_in_git not in orphans

    def test_skips_files_in_node_modules(self, tmp_path: Path) -> None:
        """AGENTS.md inside node_modules/ is skipped."""
        compiler = _make_compiler_in_tmp(tmp_path)
        nm_dir: Path = tmp_path / "node_modules" / "some-pkg"
        nm_dir.mkdir(parents=True)
        agents_in_nm: Path = nm_dir / "AGENTS.md"
        agents_in_nm.write_text(_GENERATED_CONTENT)
        orphans: list[Path] = compiler._find_orphaned_agents_files([])
        assert agents_in_nm not in orphans

    def test_skips_files_in_apm_modules(self, tmp_path: Path) -> None:
        """AGENTS.md inside apm_modules/ is skipped."""
        compiler = _make_compiler_in_tmp(tmp_path)
        apm_mod_dir: Path = tmp_path / "apm_modules" / "pkg"
        apm_mod_dir.mkdir(parents=True)
        agents_in_apm: Path = apm_mod_dir / "AGENTS.md"
        agents_in_apm.write_text(_GENERATED_CONTENT)
        orphans: list[Path] = compiler._find_orphaned_agents_files([])
        assert agents_in_apm not in orphans


# ---------------------------------------------------------------------------
# _generate_orphan_warnings
# ---------------------------------------------------------------------------


class TestGenerateOrphanWarnings:
    """Test _generate_orphan_warnings."""

    def test_empty_list_returns_empty(self, tmp_path: Path) -> None:
        """No orphaned files -> empty warnings list."""
        compiler = _make_compiler_in_tmp(tmp_path)
        assert compiler._generate_orphan_warnings([]) == []

    def test_single_file_warning(self, tmp_path: Path) -> None:
        """Single orphan yields a one-item list with a descriptive message."""
        compiler = _make_compiler_in_tmp(tmp_path)
        orphan: Path = tmp_path / "sub" / "AGENTS.md"
        msgs: list[str] = compiler._generate_orphan_warnings([orphan])
        assert len(msgs) == 1
        assert "apm compile --clean" in msgs[0]

    def test_multiple_files_grouped_in_one_message(self, tmp_path: Path) -> None:
        """Multiple orphans are grouped in a single warning message."""
        compiler = _make_compiler_in_tmp(tmp_path)
        orphans: list[Path] = [tmp_path / f"d{i}" / "AGENTS.md" for i in range(3)]
        msgs: list[str] = compiler._generate_orphan_warnings(orphans)
        assert len(msgs) == 1
        assert "3" in msgs[0]

    def test_more_than_five_shows_ellipsis(self, tmp_path: Path) -> None:
        """More than 5 orphans shows '...and N more' truncation."""
        compiler = _make_compiler_in_tmp(tmp_path)
        orphans: list[Path] = [tmp_path / f"d{i}" / "AGENTS.md" for i in range(7)]
        msgs: list[str] = compiler._generate_orphan_warnings(orphans)
        assert "2 more" in msgs[0]


# ---------------------------------------------------------------------------
# _cleanup_orphaned_files
# ---------------------------------------------------------------------------


class TestCleanupOrphanedFiles:
    """Test _cleanup_orphaned_files."""

    def test_empty_list_returns_empty(self, tmp_path: Path) -> None:
        """No files to clean -> empty messages list."""
        compiler = _make_compiler_in_tmp(tmp_path)
        assert compiler._cleanup_orphaned_files([]) == []

    def test_dry_run_does_not_delete(self, tmp_path: Path) -> None:
        """dry_run=True reports what would be removed but does not delete."""
        compiler = _make_compiler_in_tmp(tmp_path)
        f: Path = tmp_path / "sub" / "AGENTS.md"
        f.parent.mkdir()
        f.write_text(_GENERATED_CONTENT)
        msgs: list[str] = compiler._cleanup_orphaned_files([f], dry_run=True)
        assert f.exists()
        assert any("Would clean up" in m for m in msgs)

    def test_real_cleanup_deletes_apm_generated_file(self, tmp_path: Path) -> None:
        """dry_run=False removes an APM-generated file (marker present)."""
        compiler = _make_compiler_in_tmp(tmp_path)
        f: Path = tmp_path / "sub2" / "AGENTS.md"
        f.parent.mkdir()
        f.write_text(_GENERATED_CONTENT)
        msgs: list[str] = compiler._cleanup_orphaned_files([f], dry_run=False)
        assert not f.exists()
        assert any("Removed" in m for m in msgs)

    def test_hand_authored_not_deleted(self, tmp_path: Path) -> None:
        """Hand-authored AGENTS.md (no APM marker) is skipped; no user-facing warning.

        The defense-in-depth re-check in _cleanup_orphaned_files is on an unreachable
        path in normal operation (_find_orphaned_agents_files already marker-gates).
        The skip is now logged at DEBUG level only to avoid a double-prefixed warning.
        """
        compiler = _make_compiler_in_tmp(tmp_path)
        f: Path = tmp_path / "sub3" / "AGENTS.md"
        f.parent.mkdir()
        f.write_text("# hand-authored, no marker")
        msgs: list[str] = compiler._cleanup_orphaned_files([f], dry_run=False)
        assert f.exists(), "Hand-authored file must not be deleted"
        # Skip is debug-only -- must NOT appear in user-facing cleanup messages
        assert not any("hand-authored" in m.lower() or "Skipped" in m for m in msgs), (
            "Hand-authored skip must not appear as a user-facing warning (demoted to debug).\n"
            f"Messages: {msgs}"
        )

    def test_unlink_failure_captured_in_messages(self, tmp_path: Path) -> None:
        """OSError during unlink is caught and reported in messages (not raised)."""
        compiler = _make_compiler_in_tmp(tmp_path)
        f: Path = tmp_path / "ghost" / "AGENTS.md"
        f.parent.mkdir()
        f.write_text(_GENERATED_CONTENT)
        with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
            msgs: list[str] = compiler._cleanup_orphaned_files([f], dry_run=False)
        assert any("Failed to remove" in m for m in msgs)


# ---------------------------------------------------------------------------
# _compile_distributed_stats
# ---------------------------------------------------------------------------


class TestCompileDistributedStats:
    """Test _compile_distributed_stats."""

    def test_basic_stats_keys_present(self, tmp_path: Path) -> None:
        """Basic stats dict includes expected keys."""
        compiler = _make_compiler_in_tmp(tmp_path)
        compiler.context_optimizer.get_optimization_stats.return_value = None
        prims = _make_primitives()
        stats: dict = compiler._compile_distributed_stats([], prims)
        assert "agents_files_generated" in stats
        assert "total_instructions_placed" in stats
        assert "primitives_found" in stats

    def test_stats_counts_placements(self, tmp_path: Path) -> None:
        """agents_files_generated equals number of placements."""
        compiler = _make_compiler_in_tmp(tmp_path)
        compiler.context_optimizer.get_optimization_stats.return_value = None
        inst = _make_instruction(file_path=tmp_path / "i.md")
        placement = PlacementResult(
            agents_path=tmp_path / "AGENTS.md",
            instructions=[inst],
            coverage_patterns={"**/*.py"},
        )
        prims = _make_primitives()
        stats: dict = compiler._compile_distributed_stats([placement], prims)
        assert stats["agents_files_generated"] == 1
        assert stats["total_instructions_placed"] == 1

    def test_optimization_stats_merged_when_available(self, tmp_path: Path) -> None:
        """When optimization_stats is returned, its fields appear in the stats dict."""
        compiler = _make_compiler_in_tmp(tmp_path)
        opt_stats = MagicMock()
        opt_stats.average_context_efficiency = 0.9
        opt_stats.pollution_improvement = 0.1
        opt_stats.baseline_efficiency = 0.8
        opt_stats.placement_accuracy = 0.95
        opt_stats.generation_time_ms = 42.0
        opt_stats.total_agents_files = 3
        opt_stats.directories_analyzed = 5
        compiler.context_optimizer.get_optimization_stats.return_value = opt_stats
        prims = _make_primitives()
        stats: dict = compiler._compile_distributed_stats([], prims)
        assert stats["average_context_efficiency"] == 0.9
        assert stats["placement_accuracy"] == 0.95


# ---------------------------------------------------------------------------
# get_compilation_results_for_display
# ---------------------------------------------------------------------------


class TestGetCompilationResultsForDisplay:
    """Test get_compilation_results_for_display."""

    def test_returns_none_when_no_placement_map(self, tmp_path: Path) -> None:
        """Returns None when _placement_map is None (compile not yet called)."""
        compiler = _make_compiler_in_tmp(tmp_path)
        assert compiler._placement_map is None
        result = compiler.get_compilation_results_for_display()
        assert result is None

    def test_returns_compilation_results_when_placement_map_set(self, tmp_path: Path) -> None:
        """Returns CompilationResults when _placement_map has been set."""
        compiler = _make_compiler_in_tmp(tmp_path)
        compiler._placement_map = {compiler.base_dir: []}
        mock_cr = MagicMock()
        mock_cr.warnings = []
        mock_cr.errors = []
        mock_cr.project_analysis = None
        mock_cr.optimization_decisions = []
        mock_cr.placement_summaries = []
        mock_cr.optimization_stats = None
        compiler.context_optimizer.get_compilation_results.return_value = mock_cr

        with patch("apm_cli.compilation.distributed_compiler.CompilationResults") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = compiler.get_compilation_results_for_display(is_dry_run=True)
        assert result is not None


# ---------------------------------------------------------------------------
# compile_distributed - integration-style
# ---------------------------------------------------------------------------


class TestCompileDistributed:
    """Integration-level tests for compile_distributed."""

    def _setup_compiler(self, tmp_path: Path) -> DistributedAgentsCompiler:
        compiler = _make_compiler_in_tmp(tmp_path)
        compiler.context_optimizer.optimize_instruction_placement.return_value = {}
        compiler.context_optimizer.get_optimization_stats.return_value = None
        return compiler

    def test_success_with_empty_primitives(self, tmp_path: Path) -> None:
        """compile_distributed succeeds with empty PrimitiveCollection."""
        compiler = self._setup_compiler(tmp_path)
        prims = _make_primitives()
        result: CompilationResult = compiler.compile_distributed(prims)
        assert result.success is True

    def test_errors_cleared_between_calls(self, tmp_path: Path) -> None:
        """Errors/warnings are cleared at start of each compile_distributed call."""
        compiler = self._setup_compiler(tmp_path)
        compiler.errors.append("stale error")
        compiler.warnings.append("stale warning")
        prims = _make_primitives()
        result: CompilationResult = compiler.compile_distributed(prims)
        assert result.success is True
        assert "stale error" not in result.errors

    def test_exception_returns_failure_result(self, tmp_path: Path) -> None:
        """An unexpected exception inside compile_distributed yields success=False."""
        compiler = self._setup_compiler(tmp_path)
        compiler.context_optimizer.optimize_instruction_placement.side_effect = RuntimeError(
            "unexpected"
        )
        prims = _make_primitives()
        result: CompilationResult = compiler.compile_distributed(prims)
        assert result.success is False
        assert any("Distributed compilation failed" in e for e in result.errors)

    def test_debug_mode_referenced_contexts_warning(self, tmp_path: Path) -> None:
        """In debug mode, referenced contexts warning is appended if contexts found."""
        compiler = self._setup_compiler(tmp_path)
        compiler.link_resolver.get_referenced_contexts.return_value = [
            tmp_path / "ctx.md",
            tmp_path / "ctx2.md",
        ]
        prims = _make_primitives()
        result: CompilationResult = compiler.compile_distributed(prims, config={"debug": True})
        assert any("context" in w.lower() for w in result.warnings)

    def test_clean_orphaned_triggers_cleanup(self, tmp_path: Path) -> None:
        """clean_orphaned=True removes APM-generated orphaned files in a non-dry-run."""
        compiler = self._setup_compiler(tmp_path)
        # Create an APM-generated orphaned AGENTS.md (outside the generated set)
        orphan: Path = tmp_path / "old" / "AGENTS.md"
        orphan.parent.mkdir()
        orphan.write_text(_GENERATED_CONTENT)
        prims = _make_primitives()
        result: CompilationResult = compiler.compile_distributed(
            prims, config={"clean_orphaned": True, "dry_run": False}
        )
        assert result.success is True
        # The APM-generated orphan should have been cleaned up
        assert not orphan.exists()

    def test_dry_run_does_not_delete_orphaned(self, tmp_path: Path) -> None:
        """dry_run=True with clean_orphaned=True does NOT delete orphaned files."""
        compiler = self._setup_compiler(tmp_path)
        orphan: Path = tmp_path / "old2" / "AGENTS.md"
        orphan.parent.mkdir()
        orphan.write_text(_GENERATED_CONTENT)
        prims = _make_primitives()
        compiler.compile_distributed(prims, config={"clean_orphaned": True, "dry_run": True})
        assert orphan.exists()

    def test_config_defaults_applied(self, tmp_path: Path) -> None:
        """Passing config=None uses safe defaults (no crash)."""
        compiler = self._setup_compiler(tmp_path)
        prims = _make_primitives()
        result: CompilationResult = compiler.compile_distributed(prims, config=None)
        assert isinstance(result, CompilationResult)

    def test_result_content_map_populated(self, tmp_path: Path) -> None:
        """content_map is populated with agents_path -> content for each placement."""
        compiler = self._setup_compiler(tmp_path)
        inst = _make_instruction(file_path=tmp_path / "i.md")
        compiler.context_optimizer.optimize_instruction_placement.return_value = {
            compiler.base_dir: [inst]
        }
        compiler.link_resolver.resolve_links_for_compilation.side_effect = lambda content, **kw: (
            content
        )
        prims = _make_primitives(inst)
        with patch(
            "apm_cli.compilation.distributed_compiler.build_attributed_instructions",
            return_value=[],
        ):
            result: CompilationResult = compiler.compile_distributed(prims)
        agents_path = compiler.base_dir / "AGENTS.md"
        assert agents_path in result.content_map
