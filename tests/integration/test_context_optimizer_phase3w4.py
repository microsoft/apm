"""Phase-3w4 integration tests for src/apm_cli/compilation/context_optimizer.py.

Targets the gap of ~182 lines at 76.1% coverage.

Covered branches / lines:
- ContextOptimizer.__init__: OSError path, exclude_patterns
- enable_timing / _time_phase with/without verbose
- _get_all_files caching
- optimize_instruction_placement: global instructions, per-instruction path
- analyze_context_inheritance: pollution calculation
- get_optimization_stats: empty placement map, non-empty
- get_compilation_results: with/without placement_map, with/without constitution
- _analyze_project_structure: skip hidden, DEFAULT_EXCLUDED_DIRNAMES, exclusion patterns
- _should_exclude_subdir: hidden dir, default excluded, pattern match
- _solve_placement_optimization: no matching dirs -> root fallback, intended dir
- _extract_intended_directory_from_pattern: global pattern, with dir, wildcard first part
- _expand_glob_pattern: brace expansion, nested braces
- _file_matches_pattern: ** pattern, non-recursive, filename match
- _calculate_distribution_score: empty, diversity factor
- DirectoryAnalysis.get_relevance_score: zero total files, non-zero
- InheritanceAnalysis.get_efficiency_ratio: zero load, non-zero
- PlacementCandidate.__post_init__: total_score calculation
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apm_cli.compilation.context_optimizer import (
    ContextOptimizer,
    DirectoryAnalysis,
    InheritanceAnalysis,
    PlacementCandidate,
)
from apm_cli.primitives.models import Instruction

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_instruction(apply_to: str | None = "**/*.py", content: str = "content") -> Instruction:
    inst = MagicMock(spec=Instruction)
    inst.apply_to = apply_to
    inst.content = content
    inst.source_file = None
    inst.source = None
    return inst


# ---------------------------------------------------------------------------
# DirectoryAnalysis.get_relevance_score
# ---------------------------------------------------------------------------


class TestDirectoryAnalysis:
    def test_returns_zero_when_no_files(self, tmp_path):
        analysis = DirectoryAnalysis(directory=tmp_path, depth=0, total_files=0)
        assert analysis.get_relevance_score("*.py") == 0.0

    def test_returns_ratio_when_files_present(self, tmp_path):
        analysis = DirectoryAnalysis(
            directory=tmp_path, depth=0, total_files=10, pattern_matches={"*.py": 5}
        )
        assert analysis.get_relevance_score("*.py") == 0.5

    def test_returns_zero_when_pattern_not_in_matches(self, tmp_path):
        analysis = DirectoryAnalysis(directory=tmp_path, depth=0, total_files=10)
        assert analysis.get_relevance_score("*.rb") == 0.0


# ---------------------------------------------------------------------------
# InheritanceAnalysis.get_efficiency_ratio
# ---------------------------------------------------------------------------


class TestInheritanceAnalysis:
    def test_returns_one_when_no_context_load(self, tmp_path):
        analysis = InheritanceAnalysis(
            working_directory=tmp_path,
            inheritance_chain=[],
            total_context_load=0,
        )
        assert analysis.get_efficiency_ratio() == 1.0

    def test_returns_ratio_when_context_loaded(self, tmp_path):
        analysis = InheritanceAnalysis(
            working_directory=tmp_path,
            inheritance_chain=[],
            total_context_load=10,
            relevant_context_load=7,
        )
        assert abs(analysis.get_efficiency_ratio() - 0.7) < 1e-6


# ---------------------------------------------------------------------------
# PlacementCandidate.__post_init__
# ---------------------------------------------------------------------------


class TestPlacementCandidate:
    def test_total_score_computed_correctly(self, tmp_path):
        inst = _make_instruction()
        cand = PlacementCandidate(
            instruction=inst,
            directory=tmp_path,
            direct_relevance=1.0,
            inheritance_pollution=0.5,
            depth_specificity=2.0,
            total_score=0.0,
        )
        expected = 1.0 * 1.0 + (-0.5 * 0.5) + 2.0 * 0.1
        assert abs(cand.total_score - expected) < 1e-6


# ---------------------------------------------------------------------------
# ContextOptimizer.__init__
# ---------------------------------------------------------------------------


class TestContextOptimizerInit:
    def test_init_with_base_dir(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        assert opt.base_dir == tmp_path.resolve()

    def test_init_with_oserror_falls_back_to_absolute(self):
        with patch("pathlib.Path.resolve", side_effect=OSError("mock error")):
            opt = ContextOptimizer(base_dir=".")
        assert opt.base_dir is not None

    def test_init_with_exclude_patterns(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path), exclude_patterns=["*.log"])
        assert opt._exclude_patterns is not None

    def test_init_creates_empty_caches(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        assert opt._directory_cache == {}
        assert opt._glob_cache == {}
        assert opt._file_list_cache is None


# ---------------------------------------------------------------------------
# enable_timing / _time_phase
# ---------------------------------------------------------------------------


class TestTimingMethods:
    def test_enable_timing_clears_phase_timings(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._phase_timings["old_phase"] = 1.0
        opt.enable_timing(verbose=True)
        assert "old_phase" not in opt._phase_timings

    def test_time_phase_without_timing_enabled_calls_function(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._timing_enabled = False
        called = []

        def operation():
            called.append(True)
            return "result"

        result = opt._time_phase("test", operation)
        assert result == "result"
        assert called

    def test_time_phase_with_timing_enabled_records_timing(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._timing_enabled = True

        def operation():
            return 42

        result = opt._time_phase("my_phase", operation)
        assert result == 42
        assert "my_phase" in opt._phase_timings


# ---------------------------------------------------------------------------
# _get_all_files
# ---------------------------------------------------------------------------


class TestGetAllFiles:
    def test_returns_files_excluding_hidden(self, tmp_path):
        (tmp_path / "visible.py").write_text("x")
        (tmp_path / ".hidden.py").write_text("x")

        opt = ContextOptimizer(base_dir=str(tmp_path))
        files = opt._get_all_files()
        names = [f.name for f in files]
        assert "visible.py" in names
        assert ".hidden.py" not in names

    def test_excludes_default_excluded_dirs(self, tmp_path):
        excluded = tmp_path / "node_modules"
        excluded.mkdir()
        (excluded / "file.js").write_text("x")

        opt = ContextOptimizer(base_dir=str(tmp_path))
        files = opt._get_all_files()
        assert not any("node_modules" in str(f) for f in files)

    def test_caches_result_on_second_call(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        result1 = opt._get_all_files()
        result2 = opt._get_all_files()
        assert result1 is result2  # Same list object (cached)


# ---------------------------------------------------------------------------
# _expand_glob_pattern
# ---------------------------------------------------------------------------


class TestExpandGlobPattern:
    def test_no_braces_returns_single_pattern(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        result = opt._expand_glob_pattern("**/*.py")
        assert result == ["**/*.py"]

    def test_single_brace_group_expands(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        result = opt._expand_glob_pattern("**/*.{py,js}")
        assert "**/*.py" in result
        assert "**/*.js" in result
        assert len(result) == 2

    def test_nested_brace_expansion(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        result = opt._expand_glob_pattern("**/*.{test,spec}.{ts,js}")
        assert "**/*.test.ts" in result
        assert "**/*.test.js" in result
        assert "**/*.spec.ts" in result
        assert "**/*.spec.js" in result


# ---------------------------------------------------------------------------
# _extract_intended_directory_from_pattern
# ---------------------------------------------------------------------------


class TestExtractIntendedDirectory:
    def test_returns_none_for_global_pattern(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        assert opt._extract_intended_directory_from_pattern("**/*.py") is None

    def test_returns_none_when_no_slash(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        assert opt._extract_intended_directory_from_pattern("*.py") is None

    def test_returns_dir_when_exists(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        opt = ContextOptimizer(base_dir=str(tmp_path))
        result = opt._extract_intended_directory_from_pattern("src/**/*.py")
        assert result == src

    def test_returns_none_when_dir_missing(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        result = opt._extract_intended_directory_from_pattern("nonexistent_dir/**/*.py")
        assert result is None

    def test_returns_none_when_first_part_is_wildcard(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        result = opt._extract_intended_directory_from_pattern("**/tests/*.py")
        assert result is None


# ---------------------------------------------------------------------------
# _file_matches_pattern
# ---------------------------------------------------------------------------


class TestFileMatchesPattern:
    def test_matches_simple_glob(self, tmp_path):
        py_file = tmp_path / "hello.py"
        py_file.write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        assert opt._file_matches_pattern(py_file, "*.py") is True

    def test_does_not_match_wrong_extension(self, tmp_path):
        js_file = tmp_path / "hello.js"
        js_file.write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        assert opt._file_matches_pattern(js_file, "*.py") is False

    def test_matches_double_star_pattern(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        py_file = sub / "foo.py"
        py_file.write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        assert opt._file_matches_pattern(py_file, "**/*.py") is True

    def test_brace_pattern_expands(self, tmp_path):
        ts_file = tmp_path / "comp.ts"
        ts_file.write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        assert opt._file_matches_pattern(ts_file, "*.{ts,tsx}") is True


# ---------------------------------------------------------------------------
# _should_exclude_subdir
# ---------------------------------------------------------------------------


class TestShouldExcludeSubdir:
    def test_excludes_hidden_dir(self, tmp_path):
        hidden = tmp_path / ".git"
        hidden.mkdir()
        opt = ContextOptimizer(base_dir=str(tmp_path))
        assert opt._should_exclude_subdir(hidden) is True

    def test_excludes_node_modules(self, tmp_path):
        nm = tmp_path / "node_modules"
        nm.mkdir()
        opt = ContextOptimizer(base_dir=str(tmp_path))
        assert opt._should_exclude_subdir(nm) is True

    def test_does_not_exclude_normal_dir(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        opt = ContextOptimizer(base_dir=str(tmp_path))
        assert opt._should_exclude_subdir(src) is False


# ---------------------------------------------------------------------------
# _analyze_project_structure
# ---------------------------------------------------------------------------


class TestAnalyzeProjectStructure:
    def test_populates_directory_cache(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        assert tmp_path.resolve() in opt._directory_cache

    def test_skips_hidden_directories(self, tmp_path):
        hidden = tmp_path / ".hidden_dir"
        hidden.mkdir()
        (hidden / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        # Hidden dir should not be in cache
        assert all(".hidden_dir" not in str(k) for k in opt._directory_cache)

    def test_skips_default_excluded_dirs(self, tmp_path):
        excluded = tmp_path / "node_modules"
        excluded.mkdir()
        (excluded / "file.js").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        assert all("node_modules" not in str(k) for k in opt._directory_cache)

    def test_skips_empty_directories(self, tmp_path):
        empty = tmp_path / "empty_dir"
        empty.mkdir()
        # Put a file in root so structure is populated
        (tmp_path / "root.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        assert empty.resolve() not in opt._directory_cache


# ---------------------------------------------------------------------------
# optimize_instruction_placement
# ---------------------------------------------------------------------------


class TestOptimizeInstructionPlacement:
    def test_global_instruction_goes_to_root(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        inst = _make_instruction(apply_to=None)
        result = opt.optimize_instruction_placement([inst])
        assert opt.base_dir in result
        assert inst in result[opt.base_dir]

    def test_instruction_with_pattern_placed(self, tmp_path):
        py_file = tmp_path / "hello.py"
        py_file.write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        inst = _make_instruction(apply_to="*.py")
        result = opt.optimize_instruction_placement([inst])
        # Should produce a non-empty placement map
        assert len(result) > 0

    def test_empty_instructions_returns_empty_map(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        result = opt.optimize_instruction_placement([])
        assert result == {}

    def test_enable_timing_records_phases(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        inst = _make_instruction(apply_to=None)
        opt.optimize_instruction_placement([inst], enable_timing=True, verbose=True)
        assert len(opt._phase_timings) > 0


# ---------------------------------------------------------------------------
# analyze_context_inheritance
# ---------------------------------------------------------------------------


class TestAnalyzeContextInheritance:
    def test_empty_placement_map_gives_zero_pollution(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        result = opt.analyze_context_inheritance(tmp_path, {})
        assert result.pollution_score == 0.0

    def test_relevant_instructions_reduce_pollution(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        inst = _make_instruction(apply_to="*.py")
        placement_map = {opt.base_dir: [inst]}
        result = opt.analyze_context_inheritance(tmp_path, placement_map)
        # Pollution should be between 0 and 1
        assert 0.0 <= result.pollution_score <= 1.0


# ---------------------------------------------------------------------------
# get_optimization_stats
# ---------------------------------------------------------------------------


class TestGetOptimizationStats:
    def test_empty_placement_map_returns_zeros(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        stats = opt.get_optimization_stats({})
        assert stats.average_context_efficiency == 0.0
        assert stats.total_agents_files == 0

    def test_non_empty_map_returns_stats(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        inst = _make_instruction(apply_to=None)
        placement_map = {opt.base_dir: [inst]}
        stats = opt.get_optimization_stats(placement_map)
        assert stats.total_agents_files == 1


# ---------------------------------------------------------------------------
# get_compilation_results
# ---------------------------------------------------------------------------


class TestGetCompilationResults:
    def test_empty_map_no_constitution(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        opt._start_time = None

        with patch("apm_cli.compilation.constitution.find_constitution") as mock_find:
            mock_const = MagicMock()
            mock_const.exists.return_value = False
            mock_find.return_value = mock_const
            result = opt.get_compilation_results({})

        assert result.project_analysis.constitution_detected is False

    def test_empty_map_with_constitution_creates_root_placement(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()

        with patch("apm_cli.compilation.constitution.find_constitution") as mock_find:
            mock_const = MagicMock()
            mock_const.exists.return_value = True
            mock_find.return_value = mock_const
            result = opt.get_compilation_results({})

        assert len(result.placement_summaries) == 1

    def test_non_empty_map_creates_summaries(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        opt._start_time = None

        inst = _make_instruction(apply_to=None)
        placement_map = {opt.base_dir: [inst]}

        with patch("apm_cli.compilation.constitution.find_constitution") as mock_find:
            mock_const = MagicMock()
            mock_const.exists.return_value = False
            mock_find.return_value = mock_const
            result = opt.get_compilation_results(placement_map)

        assert len(result.placement_summaries) == 1
        assert result.placement_summaries[0].instruction_count == 1

    def test_generation_time_recorded_when_start_time_set(self, tmp_path):
        import time

        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        opt._start_time = time.time() - 0.1  # 100ms ago

        with patch("apm_cli.compilation.constitution.find_constitution") as mock_find:
            mock_const = MagicMock()
            mock_const.exists.return_value = False
            mock_find.return_value = mock_const
            result = opt.get_compilation_results({})

        assert result.optimization_stats.generation_time_ms is not None
        assert result.optimization_stats.generation_time_ms > 0

    def test_dry_run_flag_propagated(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()

        with patch("apm_cli.compilation.constitution.find_constitution") as mock_find:
            mock_const = MagicMock()
            mock_const.exists.return_value = False
            mock_find.return_value = mock_const
            result = opt.get_compilation_results({}, is_dry_run=True)

        assert result.is_dry_run is True


# ---------------------------------------------------------------------------
# _solve_placement_optimization fallback paths
# ---------------------------------------------------------------------------


class TestSolvePlacementOptimization:
    def test_no_matching_dirs_falls_back_to_root(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        inst = _make_instruction(apply_to="**/*.nonexistent_ext_xyz")
        placements = opt._solve_placement_optimization(inst)
        assert opt.base_dir in placements

    def test_no_matching_dirs_with_intended_dir_uses_it(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.py").write_text("x")  # Need file in src to be in cache
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        inst = _make_instruction(apply_to="src/**/*.nonexistent_ext_xyz")
        placements = opt._solve_placement_optimization(inst)
        # Should place in src/ as intended dir
        assert src.resolve() in placements

    def test_records_warning_when_no_matching_files(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        opt._warnings.clear()
        inst = _make_instruction(apply_to="**/*.nonexistent_xyz")
        opt._solve_placement_optimization(inst)
        assert len(opt._warnings) > 0


# ---------------------------------------------------------------------------
# _calculate_distribution_score
# ---------------------------------------------------------------------------


class TestCalculateDistributionScore:
    def test_returns_zero_when_no_dirs_with_files(self, tmp_path):
        opt = ContextOptimizer(base_dir=str(tmp_path))
        # Empty cache
        score = opt._calculate_distribution_score(set())
        assert score == 0.0

    def test_returns_non_zero_when_dirs_present(self, tmp_path):
        (tmp_path / "file.py").write_text("x")
        opt = ContextOptimizer(base_dir=str(tmp_path))
        opt._analyze_project_structure()
        matching = {opt.base_dir}
        score = opt._calculate_distribution_score(matching)
        assert 0.0 < score <= 1.0 or score > 0  # diversity factor may push > 1
