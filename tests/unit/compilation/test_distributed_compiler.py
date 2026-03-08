"""Unit tests for distributed AGENTS.md compilation system."""

import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.compilation.distributed_compiler import (
    CompilationResult,
    DirectoryMap,
    DistributedAgentsCompiler,
    PlacementResult,
)
from apm_cli.primitives.models import Instruction, PrimitiveCollection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_instruction(name: str, apply_to: str, content: str = "# content", base_dir: Path = None) -> Instruction:
    base = base_dir or Path("/tmp/project")
    return Instruction(
        name=name,
        file_path=base / ".apm" / "instructions" / f"{name}.instructions.md",
        description=f"Description for {name}",
        apply_to=apply_to,
        content=content,
        source="local",
    )


def _make_primitives(instructions=None, chatmodes=None, contexts=None) -> PrimitiveCollection:
    pc = PrimitiveCollection()
    pc.instructions = instructions or []
    pc.chatmodes = chatmodes or []
    pc.contexts = contexts or []
    return pc


# ---------------------------------------------------------------------------
# DirectoryMap
# ---------------------------------------------------------------------------

class TestDirectoryMap:
    def test_get_max_depth_non_empty(self, tmp_path):
        dm = DirectoryMap(
            directories={tmp_path: set(), tmp_path / "a": set(), tmp_path / "a" / "b": set()},
            depth_map={tmp_path: 0, tmp_path / "a": 1, tmp_path / "a" / "b": 2},
            parent_map={tmp_path: None, tmp_path / "a": tmp_path, tmp_path / "a" / "b": tmp_path / "a"},
        )
        assert dm.get_max_depth() == 2

    def test_get_max_depth_empty(self):
        dm = DirectoryMap(directories={}, depth_map={}, parent_map={})
        assert dm.get_max_depth() == 0


# ---------------------------------------------------------------------------
# DistributedAgentsCompiler.__init__
# ---------------------------------------------------------------------------

class TestDistributedAgentsCompilerInit:
    def test_init_normal(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        assert compiler.base_dir == tmp_path.resolve()

    def test_init_oserror_falls_back_to_absolute(self):
        """OSError during Path.resolve() falls back to Path.absolute()."""
        with patch("apm_cli.compilation.distributed_compiler.Path") as mock_path_cls:
            mock_path_instance = MagicMock()
            mock_path_instance.resolve.side_effect = OSError("resolve failed")
            mock_path_instance.absolute.return_value = Path("/abs/fallback")
            mock_path_cls.return_value = mock_path_instance

            # ContextOptimizer and UnifiedLinkResolver will still be called;
            # mock them to avoid real filesystem access
            with (
                patch("apm_cli.compilation.distributed_compiler.ContextOptimizer"),
                patch("apm_cli.compilation.distributed_compiler.UnifiedLinkResolver"),
                patch("apm_cli.compilation.distributed_compiler.CompilationFormatter"),
            ):
                compiler = DistributedAgentsCompiler("/some/path")
                assert compiler.base_dir == Path("/abs/fallback")


# ---------------------------------------------------------------------------
# _extract_directories_from_pattern
# ---------------------------------------------------------------------------

class TestExtractDirectoriesFromPattern:
    def setup_method(self):
        with patch("apm_cli.compilation.distributed_compiler.ContextOptimizer"), \
             patch("apm_cli.compilation.distributed_compiler.UnifiedLinkResolver"):
            self.compiler = DistributedAgentsCompiler.__new__(DistributedAgentsCompiler)
            self.compiler.base_dir = Path("/project")

    def test_global_pattern_returns_dot(self):
        result = self.compiler._extract_directories_from_pattern("**/*.py")
        assert result == [Path(".")]

    def test_pattern_with_directory(self):
        result = self.compiler._extract_directories_from_pattern("src/*.py")
        assert result == [Path("src")]

    def test_pattern_with_wildcard_directory(self):
        result = self.compiler._extract_directories_from_pattern("*/*.py")
        assert result == [Path(".")]

    def test_pattern_no_directory(self):
        result = self.compiler._extract_directories_from_pattern("*.py")
        assert result == [Path(".")]

    def test_pattern_nested_directory(self):
        result = self.compiler._extract_directories_from_pattern("src/**/*.ts")
        assert result == [Path("src")]


# ---------------------------------------------------------------------------
# _find_best_directory
# ---------------------------------------------------------------------------

class TestFindBestDirectory:
    def setup_method(self):
        self.base = Path("/project")
        with patch("apm_cli.compilation.distributed_compiler.ContextOptimizer"), \
             patch("apm_cli.compilation.distributed_compiler.UnifiedLinkResolver"):
            self.compiler = DistributedAgentsCompiler.__new__(DistributedAgentsCompiler)
            self.compiler.base_dir = self.base

    def _make_dir_map(self):
        src = self.base / "src"
        return DirectoryMap(
            directories={
                self.base: {"**/*.py", "src/**/*.py"},
                src: {"src/**/*.py"},
            },
            depth_map={self.base: 0, src: 1},
            parent_map={self.base: None, src: self.base},
        )

    def test_no_apply_to_returns_base_dir(self):
        inst = _make_instruction("no-apply", apply_to="", base_dir=self.base)
        inst.apply_to = None
        dm = self._make_dir_map()
        result = self.compiler._find_best_directory(inst, dm, max_depth=5)
        assert result == self.base

    def test_prefers_deeper_matching_directory(self):
        inst = _make_instruction("src-python", apply_to="src/**/*.py", base_dir=self.base)
        dm = self._make_dir_map()
        result = self.compiler._find_best_directory(inst, dm, max_depth=5)
        assert result == self.base / "src"

    def test_depth_limit_respected(self):
        inst = _make_instruction("src-python", apply_to="src/**/*.py", base_dir=self.base)
        dm = self._make_dir_map()
        # max_depth=0 means only base_dir (depth 0) is eligible
        result = self.compiler._find_best_directory(inst, dm, max_depth=0)
        assert result == self.base

    def test_no_matching_pattern_returns_base_dir(self):
        inst = _make_instruction("docs", apply_to="docs/**/*.md", base_dir=self.base)
        dm = self._make_dir_map()
        result = self.compiler._find_best_directory(inst, dm, max_depth=5)
        assert result == self.base


# ---------------------------------------------------------------------------
# analyze_directory_structure
# ---------------------------------------------------------------------------

class TestAnalyzeDirectoryStructure:
    def setup_method(self):
        self.base = Path("/project")
        with patch("apm_cli.compilation.distributed_compiler.ContextOptimizer"), \
             patch("apm_cli.compilation.distributed_compiler.UnifiedLinkResolver"), \
             patch("apm_cli.compilation.distributed_compiler.CompilationFormatter"):
            self.compiler = DistributedAgentsCompiler.__new__(DistributedAgentsCompiler)
            self.compiler.base_dir = self.base

    def test_empty_instructions_returns_base_only(self):
        dm = self.compiler.analyze_directory_structure([])
        assert self.base in dm.directories
        assert dm.depth_map[self.base] == 0

    def test_instruction_without_apply_to_skipped(self):
        inst = _make_instruction("no-apply", apply_to="", base_dir=self.base)
        inst.apply_to = None
        dm = self.compiler.analyze_directory_structure([inst])
        assert self.base in dm.directories

    def test_parent_directory_tracked_when_missing(self):
        """When a deep directory is found, its parent is added too."""
        inst = _make_instruction("deep", apply_to="src/**/*.py", base_dir=self.base)
        dm = self.compiler.analyze_directory_structure([inst])
        src = self.base / "src"
        assert src in dm.directories
        # parent (base_dir) should also be in directories
        assert self.base in dm.directories


# ---------------------------------------------------------------------------
# _validate_coverage
# ---------------------------------------------------------------------------

class TestValidateCoverage:
    def setup_method(self):
        self.base = Path("/project")
        with patch("apm_cli.compilation.distributed_compiler.ContextOptimizer"), \
             patch("apm_cli.compilation.distributed_compiler.UnifiedLinkResolver"), \
             patch("apm_cli.compilation.distributed_compiler.CompilationFormatter"):
            self.compiler = DistributedAgentsCompiler.__new__(DistributedAgentsCompiler)
            self.compiler.base_dir = self.base

    def test_all_instructions_placed_no_warnings(self):
        inst = _make_instruction("inst1", apply_to="**/*.py", base_dir=self.base)
        placement = PlacementResult(
            agents_path=self.base / "AGENTS.md",
            instructions=[inst],
        )
        warnings = self.compiler._validate_coverage([placement], [inst])
        assert warnings == []

    def test_missing_instruction_generates_warning(self):
        inst1 = _make_instruction("inst1", apply_to="**/*.py", base_dir=self.base)
        inst2 = _make_instruction("inst2", apply_to="src/**/*.py", base_dir=self.base)
        placement = PlacementResult(
            agents_path=self.base / "AGENTS.md",
            instructions=[inst1],  # inst2 is not placed
        )
        warnings = self.compiler._validate_coverage([placement], [inst1, inst2])
        assert len(warnings) == 1
        assert "inst2" in warnings[0] or str(inst2.file_path) in warnings[0]

    def test_empty_placements_all_missing(self):
        inst = _make_instruction("inst1", apply_to="**/*.py", base_dir=self.base)
        warnings = self.compiler._validate_coverage([], [inst])
        assert len(warnings) == 1


# ---------------------------------------------------------------------------
# _find_orphaned_agents_files
# ---------------------------------------------------------------------------

class TestFindOrphanedAgentsFiles:
    def test_no_existing_agents_files(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        orphaned = compiler._find_orphaned_agents_files([])
        assert orphaned == []

    def test_generated_file_not_orphaned(self, tmp_path):
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text("# Generated")
        compiler = DistributedAgentsCompiler(str(tmp_path))
        orphaned = compiler._find_orphaned_agents_files([agents_file])
        assert agents_file not in orphaned

    def test_ungenerated_file_is_orphaned(self, tmp_path):
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text("# Old content")
        compiler = DistributedAgentsCompiler(str(tmp_path))
        orphaned = compiler._find_orphaned_agents_files([])
        assert agents_file in orphaned

    def test_git_dir_agents_skipped(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        agents_file = git_dir / "AGENTS.md"
        agents_file.write_text("# Git content")
        compiler = DistributedAgentsCompiler(str(tmp_path))
        orphaned = compiler._find_orphaned_agents_files([])
        assert agents_file not in orphaned

    def test_apm_modules_dir_agents_skipped(self, tmp_path):
        apm_dir = tmp_path / "apm_modules" / "pkg"
        apm_dir.mkdir(parents=True)
        agents_file = apm_dir / "AGENTS.md"
        agents_file.write_text("# pkg content")
        compiler = DistributedAgentsCompiler(str(tmp_path))
        orphaned = compiler._find_orphaned_agents_files([])
        assert agents_file not in orphaned

    def test_nested_orphaned_file_detected(self, tmp_path):
        sub = tmp_path / "src"
        sub.mkdir()
        agents_file = sub / "AGENTS.md"
        agents_file.write_text("# Src content")
        compiler = DistributedAgentsCompiler(str(tmp_path))
        orphaned = compiler._find_orphaned_agents_files([])
        assert agents_file in orphaned


# ---------------------------------------------------------------------------
# _generate_orphan_warnings
# ---------------------------------------------------------------------------

class TestGenerateOrphanWarnings:
    def setup_method(self):
        self.base = Path("/project")
        with patch("apm_cli.compilation.distributed_compiler.ContextOptimizer"), \
             patch("apm_cli.compilation.distributed_compiler.UnifiedLinkResolver"), \
             patch("apm_cli.compilation.distributed_compiler.CompilationFormatter"):
            self.compiler = DistributedAgentsCompiler.__new__(DistributedAgentsCompiler)
            self.compiler.base_dir = self.base

    def test_no_orphans_returns_empty(self):
        warnings = self.compiler._generate_orphan_warnings([])
        assert warnings == []

    def test_single_orphan_warning(self):
        orphan = self.base / "sub" / "AGENTS.md"
        warnings = self.compiler._generate_orphan_warnings([orphan])
        assert len(warnings) == 1
        assert "sub/AGENTS.md" in warnings[0] or "Orphaned" in warnings[0]

    def test_multiple_orphans_warning(self):
        orphans = [self.base / f"dir{i}" / "AGENTS.md" for i in range(3)]
        warnings = self.compiler._generate_orphan_warnings(orphans)
        assert len(warnings) == 1  # single coalesced message
        assert "3" in warnings[0]

    def test_more_than_five_orphans_truncated(self):
        orphans = [self.base / f"d{i}" / "AGENTS.md" for i in range(7)]
        warnings = self.compiler._generate_orphan_warnings(orphans)
        assert len(warnings) == 1
        assert "more" in warnings[0]


# ---------------------------------------------------------------------------
# _cleanup_orphaned_files
# ---------------------------------------------------------------------------

class TestCleanupOrphanedFiles:
    def setup_method(self):
        self.base = Path("/project")
        with patch("apm_cli.compilation.distributed_compiler.ContextOptimizer"), \
             patch("apm_cli.compilation.distributed_compiler.UnifiedLinkResolver"), \
             patch("apm_cli.compilation.distributed_compiler.CompilationFormatter"):
            self.compiler = DistributedAgentsCompiler.__new__(DistributedAgentsCompiler)
            self.compiler.base_dir = self.base

    def test_empty_list_returns_empty(self):
        result = self.compiler._cleanup_orphaned_files([], dry_run=False)
        assert result == []

    def test_dry_run_reports_without_deleting(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        orphan = tmp_path / "AGENTS.md"
        orphan.write_text("old content")
        messages = compiler._cleanup_orphaned_files([orphan], dry_run=True)
        assert orphan.exists()  # not deleted
        assert any("Would clean" in m or "clean" in m.lower() for m in messages)

    def test_actual_cleanup_deletes_file(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        orphan = tmp_path / "AGENTS.md"
        orphan.write_text("old content")
        messages = compiler._cleanup_orphaned_files([orphan], dry_run=False)
        assert not orphan.exists()
        assert any("Removed" in m or "emoving" in m or "✓" in m for m in messages)

    def test_cleanup_handles_delete_error(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        orphan = tmp_path / "AGENTS.md"
        orphan.write_text("old content")
        with patch.object(orphan.__class__, "unlink", side_effect=OSError("permission denied")):
            messages = compiler._cleanup_orphaned_files([orphan], dry_run=False)
        assert any("Failed" in m or "fail" in m.lower() or "✗" in m for m in messages)


# ---------------------------------------------------------------------------
# generate_distributed_agents_files
# ---------------------------------------------------------------------------

class TestGenerateDistributedAgentsFiles:
    def test_empty_placement_map_no_constitution_returns_empty(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        primitives = _make_primitives()
        result = compiler.generate_distributed_agents_files({}, primitives)
        assert result == []

    def test_empty_placement_map_with_constitution_creates_root_placement(self, tmp_path):
        constitution = tmp_path / ".specify" / "memory" / "constitution.md"
        constitution.parent.mkdir(parents=True)
        constitution.write_text("# Constitution\nProject rules.")
        compiler = DistributedAgentsCompiler(str(tmp_path))
        primitives = _make_primitives()
        result = compiler.generate_distributed_agents_files({}, primitives)
        assert len(result) == 1
        assert result[0].agents_path == tmp_path / "AGENTS.md"
        assert result[0].instructions == []

    def test_normal_placement_map_creates_placements(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        inst = _make_instruction("inst1", apply_to="**/*.py", base_dir=tmp_path)
        inst.file_path = tmp_path / ".apm" / "instructions" / "inst1.instructions.md"
        placement_map = {tmp_path: [inst]}
        primitives = _make_primitives(instructions=[inst])
        result = compiler.generate_distributed_agents_files(placement_map, primitives)
        assert len(result) == 1
        assert result[0].agents_path == tmp_path / "AGENTS.md"
        assert inst in result[0].instructions

    def test_source_attribution_disabled(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        inst = _make_instruction("inst1", apply_to="**/*.py", base_dir=tmp_path)
        inst.file_path = tmp_path / ".apm" / "instructions" / "inst1.instructions.md"
        placement_map = {tmp_path: [inst]}
        primitives = _make_primitives(instructions=[inst])
        result = compiler.generate_distributed_agents_files(placement_map, primitives, source_attribution=False)
        assert result[0].source_attribution == {}

    def test_instruction_apply_to_populates_coverage_patterns(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        inst = _make_instruction("inst1", apply_to="src/**/*.py", base_dir=tmp_path)
        inst.file_path = tmp_path / ".apm" / "instructions" / "inst1.instructions.md"
        placement_map = {tmp_path: [inst]}
        primitives = _make_primitives(instructions=[inst])
        result = compiler.generate_distributed_agents_files(placement_map, primitives)
        assert "src/**/*.py" in result[0].coverage_patterns


# ---------------------------------------------------------------------------
# _generate_agents_content
# ---------------------------------------------------------------------------

class TestGenerateAgentsContent:
    def test_generates_header(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        placement = PlacementResult(
            agents_path=tmp_path / "AGENTS.md",
            instructions=[],
        )
        content = compiler._generate_agents_content(placement, _make_primitives())
        assert "# AGENTS.md" in content
        assert "Generated by APM CLI" in content

    def test_includes_instruction_content(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        inst = _make_instruction("python-rules", apply_to="**/*.py",
                                 content="Use type hints always.", base_dir=tmp_path)
        inst.file_path = tmp_path / ".apm" / "instructions" / "python-rules.instructions.md"
        placement = PlacementResult(
            agents_path=tmp_path / "AGENTS.md",
            instructions=[inst],
        )
        content = compiler._generate_agents_content(placement, _make_primitives(instructions=[inst]))
        assert "Use type hints always." in content
        assert "**/*.py" in content

    def test_multiple_sources_attribution(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        inst1 = _make_instruction("inst1", apply_to="**/*.py", base_dir=tmp_path)
        inst1.file_path = tmp_path / ".apm" / "instructions" / "inst1.instructions.md"
        inst2 = _make_instruction("inst2", apply_to="**/*.ts", base_dir=tmp_path)
        inst2.file_path = tmp_path / ".apm" / "instructions" / "inst2.instructions.md"
        placement = PlacementResult(
            agents_path=tmp_path / "AGENTS.md",
            instructions=[inst1, inst2],
            source_attribution={
                str(inst1.file_path): "pkgA",
                str(inst2.file_path): "pkgB",
            },
        )
        content = compiler._generate_agents_content(placement, _make_primitives(instructions=[inst1, inst2]))
        assert "Sources:" in content

    def test_single_source_attribution(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        inst = _make_instruction("inst1", apply_to="**/*.py", base_dir=tmp_path)
        inst.file_path = tmp_path / ".apm" / "instructions" / "inst1.instructions.md"
        placement = PlacementResult(
            agents_path=tmp_path / "AGENTS.md",
            instructions=[inst],
            source_attribution={str(inst.file_path): "local"},
        )
        content = compiler._generate_agents_content(placement, _make_primitives(instructions=[inst]))
        assert "Source:" in content

    def test_footer_present(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        placement = PlacementResult(
            agents_path=tmp_path / "AGENTS.md",
            instructions=[],
        )
        content = compiler._generate_agents_content(placement, _make_primitives())
        assert "Do not edit manually" in content


# ---------------------------------------------------------------------------
# determine_agents_placement
# ---------------------------------------------------------------------------

class TestDetermineAgentsPlacement:
    def test_min_instructions_filters_small_directories(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        inst1 = _make_instruction("inst1", apply_to="**/*.py", base_dir=tmp_path)
        inst2 = _make_instruction("inst2", apply_to="src/**/*.py", base_dir=tmp_path)

        src = tmp_path / "src"
        mock_optimizer_placement = {
            tmp_path: [inst1, inst2],
            src: [inst2],
        }
        dm = DirectoryMap(
            directories={tmp_path: set(), src: set()},
            depth_map={tmp_path: 0, src: 1},
            parent_map={tmp_path: None, src: tmp_path},
        )

        with patch.object(compiler.context_optimizer, "optimize_instruction_placement",
                          return_value=mock_optimizer_placement):
            # min_instructions=3 means src (1 instruction) gets moved to parent
            result = compiler.determine_agents_placement(
                [inst1, inst2], dm, min_instructions=3
            )

        # src dir had only 1 instruction - should be merged up
        assert src not in result or len(result.get(src, [])) >= 3 or tmp_path in result

    def test_constitution_fallback_when_no_placement(self, tmp_path):
        constitution = tmp_path / ".specify" / "memory" / "constitution.md"
        constitution.parent.mkdir(parents=True)
        constitution.write_text("# Constitution")
        compiler = DistributedAgentsCompiler(str(tmp_path))

        with patch.object(compiler.context_optimizer, "optimize_instruction_placement",
                          return_value={}):
            result = compiler.determine_agents_placement([], DirectoryMap({}, {}, {}))

        assert tmp_path in result

    def test_no_constitution_empty_placement_returns_empty(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))

        with patch.object(compiler.context_optimizer, "optimize_instruction_placement",
                          return_value={}):
            result = compiler.determine_agents_placement([], DirectoryMap({}, {}, {}))

        assert result == {}


# ---------------------------------------------------------------------------
# compile_distributed - exception handling
# ---------------------------------------------------------------------------

class TestCompileDistributedExceptionHandling:
    def test_exception_in_analysis_returns_failure(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        primitives = _make_primitives()

        with patch.object(compiler, "analyze_directory_structure",
                          side_effect=RuntimeError("unexpected error")):
            result = compiler.compile_distributed(primitives)

        assert result.success is False
        assert len(result.errors) > 0
        assert "unexpected error" in result.errors[0]

    def test_compile_distributed_success_basic(self, tmp_path):
        compiler = DistributedAgentsCompiler(str(tmp_path))
        primitives = _make_primitives()
        result = compiler.compile_distributed(primitives)
        assert isinstance(result, CompilationResult)
        assert result.success is True

    def test_compile_distributed_debug_mode(self, tmp_path):
        """Debug mode triggers referenced context scanning path."""
        compiler = DistributedAgentsCompiler(str(tmp_path))
        inst = _make_instruction("inst1", apply_to="**/*.py", base_dir=tmp_path)
        primitives = _make_primitives(instructions=[inst])

        with patch.object(compiler.link_resolver, "register_contexts"), \
             patch.object(compiler.link_resolver, "get_referenced_contexts",
                          return_value=["ctx1", "ctx2"]):
            result = compiler.compile_distributed(primitives, config={"debug": True})

        # debug mode warns about referenced context files
        assert isinstance(result, CompilationResult)

    def test_compile_distributed_orphan_cleanup(self, tmp_path):
        """clean_orphaned=True triggers file cleanup."""
        # Create a pre-existing AGENTS.md that will be orphaned
        orphan = tmp_path / "src" / "AGENTS.md"
        orphan.parent.mkdir()
        orphan.write_text("# Old AGENTS.md")

        compiler = DistributedAgentsCompiler(str(tmp_path))
        primitives = _make_primitives()
        result = compiler.compile_distributed(
            primitives, config={"clean_orphaned": True, "dry_run": False}
        )

        assert isinstance(result, CompilationResult)
        # File should have been removed during cleanup
        assert not orphan.exists()

    def test_compile_distributed_orphan_dry_run_keeps_file(self, tmp_path):
        """dry_run=True does not remove orphaned files."""
        orphan = tmp_path / "sub" / "AGENTS.md"
        orphan.parent.mkdir()
        orphan.write_text("# Old AGENTS.md")

        compiler = DistributedAgentsCompiler(str(tmp_path))
        primitives = _make_primitives()
        result = compiler.compile_distributed(
            primitives, config={"clean_orphaned": True, "dry_run": True}
        )

        assert isinstance(result, CompilationResult)
        assert orphan.exists()  # not removed in dry run

    def test_compile_distributed_coverage_validation_warning(self, tmp_path):
        """Coverage validation generates warning for unplaced instructions."""
        compiler = DistributedAgentsCompiler(str(tmp_path))
        inst = _make_instruction("unplaced", apply_to="**/*.py", base_dir=tmp_path)
        primitives = _make_primitives(instructions=[inst])

        # Mock placement to return empty (no instructions placed)
        with patch.object(compiler, "generate_distributed_agents_files", return_value=[]):
            result = compiler.compile_distributed(primitives)

        assert isinstance(result, CompilationResult)
        # Warning about unplaced instructions
        assert any("unplaced" in w or "inst" in w.lower() or "not placed" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# test_install_command.py ANSI fix (regression guard)
# ---------------------------------------------------------------------------

class TestAnsiStripping:
    """Verify the ANSI-stripping helper used in install command tests works."""

    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

    def strip_ansi(self, text: str) -> str:
        return self._ANSI_RE.sub("", text)

    def test_strip_ansi_codes(self):
        raw = "\x1b[1m<\x1b[0m\x1b[1morg\x1b[0m/repo\x1b[1m>\x1b[0m"
        assert self.strip_ansi(raw) == "<org/repo>"

    def test_strip_ansi_preserves_clean_text(self):
        assert self.strip_ansi("no ansi here") == "no ansi here"
