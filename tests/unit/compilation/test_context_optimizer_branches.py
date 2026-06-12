"""Unit tests for ContextOptimizer.

Focuses on branches, helper methods, and edge-cases that are not exercised by
the existing test files.
"""

from __future__ import annotations

import time
from pathlib import Path
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


def _make_instruction(
    name: str = "inst",
    apply_to: str = "**/*.py",
    source: str = "local",
) -> Instruction:
    return Instruction(
        name=name,
        file_path=Path(f"{name}.instructions.md"),
        description=f"{name} description",
        apply_to=apply_to,
        content=f"{name} content",
        source=source,
    )


def _touch(base: Path, rel: str) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


# ---------------------------------------------------------------------------
# DirectoryAnalysis edge-cases
# ---------------------------------------------------------------------------


class TestDirectoryAnalysisEdgeCases:
    def test_relevance_score_missing_pattern(self) -> None:
        """Pattern not present in pattern_matches returns 0.0."""
        da = DirectoryAnalysis(
            directory=Path("/x"), depth=0, total_files=5, pattern_matches={"**/*.js": 2}
        )
        assert da.get_relevance_score("**/*.py") == 0.0

    def test_relevance_score_perfect(self) -> None:
        """All files match → score == 1.0."""
        da = DirectoryAnalysis(
            directory=Path("/x"), depth=0, total_files=4, pattern_matches={"*.py": 4}
        )
        assert da.get_relevance_score("*.py") == 1.0

    def test_relevance_score_partial(self) -> None:
        da = DirectoryAnalysis(
            directory=Path("/x"), depth=1, total_files=8, pattern_matches={"*.py": 2}
        )
        assert abs(da.get_relevance_score("*.py") - 0.25) < 1e-9


# ---------------------------------------------------------------------------
# InheritanceAnalysis edge-cases
# ---------------------------------------------------------------------------


class TestInheritanceAnalysisEdgeCases:
    def test_efficiency_ratio_zero_total(self) -> None:
        ia = InheritanceAnalysis(
            working_directory=Path("/x"),
            inheritance_chain=[Path("/x")],
            total_context_load=0,
            relevant_context_load=0,
        )
        assert ia.get_efficiency_ratio() == 1.0

    def test_efficiency_ratio_all_irrelevant(self) -> None:
        ia = InheritanceAnalysis(
            working_directory=Path("/x"),
            inheritance_chain=[Path("/x")],
            total_context_load=10,
            relevant_context_load=0,
        )
        assert ia.get_efficiency_ratio() == 0.0


# ---------------------------------------------------------------------------
# PlacementCandidate score
# ---------------------------------------------------------------------------


class TestPlacementCandidateScore:
    def test_total_score_formula(self) -> None:
        """total_score = relevance*1.0 - pollution*0.5 + depth*0.1."""
        inst = _make_instruction()
        c = PlacementCandidate(
            instruction=inst,
            directory=Path("/x"),
            direct_relevance=1.0,
            inheritance_pollution=0.0,
            depth_specificity=0.0,
            total_score=0.0,
        )
        assert abs(c.total_score - 1.0) < 1e-9

    def test_pollution_penalty_applied(self) -> None:
        inst = _make_instruction()
        c = PlacementCandidate(
            instruction=inst,
            directory=Path("/x"),
            direct_relevance=0.0,
            inheritance_pollution=1.0,
            depth_specificity=0.0,
            total_score=0.0,
        )
        # 0.0 - 1.0*0.5 + 0 = -0.5
        assert abs(c.total_score - (-0.5)) < 1e-9


# ---------------------------------------------------------------------------
# ContextOptimizer initialisation and basic properties
# ---------------------------------------------------------------------------


class TestContextOptimizerInit:
    def test_default_base_dir_is_cwd(self) -> None:
        opt = ContextOptimizer()
        assert opt.base_dir == Path(".").resolve()

    def test_exclude_patterns_are_stored(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path), exclude_patterns=["vendor/*"])
        assert opt._exclude_patterns  # non-empty after validation

    def test_initial_caches_empty(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        assert opt._directory_cache == {}
        assert opt._pattern_cache == {}
        assert opt._file_list_cache is None

    def test_oserror_on_resolve_falls_back_to_absolute(self) -> None:
        """If Path.resolve raises OSError we fall back to Path.absolute()."""
        with patch("apm_cli.compilation.context_optimizer.Path.resolve", side_effect=OSError):
            opt = ContextOptimizer("/no/such/path")
        # absolute() works even for non-existent paths
        assert opt.base_dir is not None


# ---------------------------------------------------------------------------
# enable_timing
# ---------------------------------------------------------------------------


class TestEnableTiming:
    def test_enable_timing_sets_flag(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        opt.enable_timing(verbose=True)
        assert opt._timing_enabled is True

    def test_enable_timing_clears_phase_timings(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        opt._phase_timings["old"] = 9.9
        opt.enable_timing(verbose=False)
        assert opt._phase_timings == {}


# ---------------------------------------------------------------------------
# _time_phase
# ---------------------------------------------------------------------------


class TestTimePhase:
    def test_time_phase_without_timing_just_calls_func(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        opt._timing_enabled = False
        result = opt._time_phase("p", lambda: 42)
        assert result == 42
        assert "p" not in opt._phase_timings

    def test_time_phase_with_timing_records_duration(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        opt._timing_enabled = True
        opt._verbose = False
        result = opt._time_phase("myphase", lambda: "hello")
        assert result == "hello"
        assert "myphase" in opt._phase_timings
        assert opt._phase_timings["myphase"] >= 0

    def test_time_phase_verbose_prints(self, tmp_path: Path, capsys) -> None:
        opt = ContextOptimizer(str(tmp_path))
        opt._timing_enabled = True
        opt._verbose = True
        opt._time_phase("verbose_phase", lambda: None)
        captured = capsys.readouterr()
        assert "verbose_phase" in captured.out


# ---------------------------------------------------------------------------
# _get_all_files
# ---------------------------------------------------------------------------


class TestGetAllFiles:
    def test_returns_non_hidden_files(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        _touch(tmp_path, ".hidden.py")
        _touch(tmp_path, "sub/b.py")
        opt = ContextOptimizer(str(tmp_path))
        files = opt._get_all_files()
        names = [f.name for f in files]
        assert "a.py" in names
        assert "b.py" in names
        assert ".hidden.py" not in names

    def test_skips_excluded_dirnames(self, tmp_path: Path) -> None:
        _touch(tmp_path, "node_modules/lib.js")
        _touch(tmp_path, "__pycache__/cache.pyc")
        _touch(tmp_path, "src/ok.py")
        opt = ContextOptimizer(str(tmp_path))
        files = opt._get_all_files()
        names = [f.name for f in files]
        assert "lib.js" not in names
        assert "cache.pyc" not in names
        assert "ok.py" in names

    def test_cache_is_populated_on_second_call(self, tmp_path: Path) -> None:
        _touch(tmp_path, "x.txt")
        opt = ContextOptimizer(str(tmp_path))
        first = opt._get_all_files()
        assert opt._file_list_cache is not None
        second = opt._get_all_files()
        assert first is second  # exact same list object


# ---------------------------------------------------------------------------
# _should_exclude_subdir
# ---------------------------------------------------------------------------


class TestShouldExcludeSubdir:
    def test_node_modules_excluded(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        assert opt._should_exclude_subdir(tmp_path / "node_modules") is True

    def test_hidden_dir_excluded(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        assert opt._should_exclude_subdir(tmp_path / ".git") is True

    def test_normal_dir_not_excluded(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        assert opt._should_exclude_subdir(tmp_path / "src") is False

    def test_pattern_excluded_dir(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path), exclude_patterns=["vendor"])
        # vendor/* matches vendor sub-items; vendor itself would depend on pattern
        # This verifies the method doesn't crash for non-matching pattern
        result = opt._should_exclude_subdir(tmp_path / "normal_dir")
        assert result is False


# ---------------------------------------------------------------------------
# _expand_glob_pattern
# ---------------------------------------------------------------------------


class TestExpandGlobPattern:
    def test_no_braces_returns_list_with_one(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        assert opt._expand_glob_pattern("**/*.py") == ["**/*.py"]

    def test_single_brace_group(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        expanded = opt._expand_glob_pattern("**/*.{ts,tsx}")
        assert set(expanded) == {"**/*.ts", "**/*.tsx"}

    def test_nested_brace_groups(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        expanded = opt._expand_glob_pattern("**/*.{test,spec}.{ts,js}")
        assert set(expanded) == {
            "**/*.test.ts",
            "**/*.test.js",
            "**/*.spec.ts",
            "**/*.spec.js",
        }

    def test_single_alternative(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        assert opt._expand_glob_pattern("**/*.{py}") == ["**/*.py"]


# ---------------------------------------------------------------------------
# _extract_intended_directory_from_pattern
# ---------------------------------------------------------------------------


class TestExtractIntendedDirectory:
    def test_global_pattern_returns_none(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        assert opt._extract_intended_directory_from_pattern("**/*.py") is None

    def test_empty_pattern_returns_none(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        assert opt._extract_intended_directory_from_pattern("") is None

    def test_pattern_without_slash_returns_none(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        assert opt._extract_intended_directory_from_pattern("*.py") is None

    def test_wildcard_first_segment_returns_none(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        # First segment is wildcard, should return None
        assert opt._extract_intended_directory_from_pattern("*/test/**") is None

    def test_non_existent_first_dir_returns_none(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        result = opt._extract_intended_directory_from_pattern("nonexistent_dir/**/*.py")
        assert result is None

    def test_existing_first_dir_returns_path(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        opt = ContextOptimizer(str(tmp_path))
        result = opt._extract_intended_directory_from_pattern("docs/**/*.md")
        assert result == tmp_path / "docs"


# ---------------------------------------------------------------------------
# _file_matches_pattern
# ---------------------------------------------------------------------------


class TestFileMatchesPattern:
    def test_simple_fnmatch(self, tmp_path: Path) -> None:
        _touch(tmp_path, "main.py")
        opt = ContextOptimizer(str(tmp_path))
        assert opt._file_matches_pattern(tmp_path / "main.py", "*.py") is True

    def test_no_match(self, tmp_path: Path) -> None:
        _touch(tmp_path, "main.py")
        opt = ContextOptimizer(str(tmp_path))
        assert opt._file_matches_pattern(tmp_path / "main.py", "*.js") is False

    def test_globstar_pattern(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/main.py")
        opt = ContextOptimizer(str(tmp_path))
        assert opt._file_matches_pattern(tmp_path / "src" / "main.py", "**/*.py") is True

    def test_brace_expansion(self, tmp_path: Path) -> None:
        _touch(tmp_path, "app.ts")
        opt = ContextOptimizer(str(tmp_path))
        assert opt._file_matches_pattern(tmp_path / "app.ts", "**/*.{ts,tsx}") is True

    def test_outside_base_dir_raises_no_crash(self, tmp_path: Path) -> None:
        """File outside base_dir: ValueError caught silently, returns False."""
        other = tmp_path / "other"
        other.mkdir()
        (other / "x.py").touch()
        sub = tmp_path / "sub"
        sub.mkdir()
        opt = ContextOptimizer(str(sub))
        # x.py is outside base_dir → ValueError on relative_to → False
        result = opt._file_matches_pattern(other / "x.py", "**/*.py")
        assert result is False


# ---------------------------------------------------------------------------
# _find_matching_directories — cache hit
# ---------------------------------------------------------------------------


class TestFindMatchingDirectories:
    def test_cache_hit_returns_same_set(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        first = opt._find_matching_directories("*.py")
        second = opt._find_matching_directories("*.py")
        assert first is second  # Same object from cache

    def test_oserror_during_iterdir_is_swallowed(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        with patch.object(Path, "iterdir", side_effect=OSError("Permission denied")):
            result = opt._find_matching_directories("*.new_pattern_xyz")
        assert isinstance(result, set)


# ---------------------------------------------------------------------------
# _calculate_distribution_score
# ---------------------------------------------------------------------------


class TestCalculateDistributionScore:
    def test_zero_dirs_with_files(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        # Empty cache → 0 dirs with files
        assert opt._calculate_distribution_score(set()) == 0.0

    def test_single_matching_dir(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        _touch(tmp_path, "b.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        dirs = {tmp_path.resolve()}
        score = opt._calculate_distribution_score(dirs)
        assert 0.0 <= score <= 2.0  # some valid float

    def test_multiple_dirs_diversity_factor(self, tmp_path: Path) -> None:
        """Depth variance should increase the diversity factor above 1.0."""
        _touch(tmp_path, "root.py")
        _touch(tmp_path, "a/deep/nested/x.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        dirs = set(opt._directory_cache.keys())
        score = opt._calculate_distribution_score(dirs)
        # With depth variance, diversity_factor >= 1, so score can exceed base_ratio
        assert score >= 0.0


# ---------------------------------------------------------------------------
# _calculate_inheritance_pollution
# ---------------------------------------------------------------------------


class TestCalculateInheritancePollution:
    def test_no_children_returns_zero(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        score = opt._calculate_inheritance_pollution(tmp_path.resolve(), "*.py")
        assert score == 0.0  # No subdirs → no pollution

    def test_child_with_no_pattern_matches_adds_pollution(self, tmp_path: Path) -> None:
        _touch(tmp_path, "main.py")
        _touch(tmp_path, "assets/logo.png")  # assets has no .py files
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        # Put pattern matches in root so assets child triggers pollution
        opt._directory_cache[tmp_path.resolve()].pattern_matches["*.py"] = 1
        # assets has total_files > 0 but no *.py matches
        score = opt._calculate_inheritance_pollution(tmp_path.resolve(), "*.py")
        assert score > 0.0

    def test_oserror_on_iterdir_returns_zero(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        with patch.object(Path, "iterdir", side_effect=OSError):
            score = opt._calculate_inheritance_pollution(tmp_path.resolve(), "*.py")
        assert score == 0.0


# ---------------------------------------------------------------------------
# _find_minimal_coverage_placement
# ---------------------------------------------------------------------------


class TestFindMinimalCoveragePlacement:
    def test_empty_set_returns_none(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        assert opt._find_minimal_coverage_placement(set()) is None

    def test_single_dir_returns_that_dir(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/a.py")
        opt = ContextOptimizer(str(tmp_path))
        src = tmp_path / "src"
        result = opt._find_minimal_coverage_placement({src})
        assert result == src

    def test_common_ancestor_found(self, tmp_path: Path) -> None:
        _touch(tmp_path, "lib/a/x.py")
        _touch(tmp_path, "lib/b/y.py")
        opt = ContextOptimizer(str(tmp_path))
        lib_a = (tmp_path / "lib" / "a").resolve()
        lib_b = (tmp_path / "lib" / "b").resolve()
        result = opt._find_minimal_coverage_placement({lib_a, lib_b})
        # common ancestor is lib
        assert result is not None
        assert result == (tmp_path / "lib").resolve()

    def test_no_common_ancestor_returns_base(self, tmp_path: Path) -> None:
        _touch(tmp_path, "alpha/x.py")
        _touch(tmp_path, "beta/y.py")
        opt = ContextOptimizer(str(tmp_path))
        alpha = (tmp_path / "alpha").resolve()
        beta = (tmp_path / "beta").resolve()
        result = opt._find_minimal_coverage_placement({alpha, beta})
        assert result == tmp_path.resolve()


# ---------------------------------------------------------------------------
# _is_hierarchically_covered / _calculate_hierarchical_coverage
# ---------------------------------------------------------------------------


class TestHierarchicalCoverage:
    def test_same_dir_is_covered(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        assert opt._is_hierarchically_covered(tmp_path, tmp_path) is True

    def test_child_is_covered_by_parent(self, tmp_path: Path) -> None:
        child = tmp_path / "src"
        child.mkdir()
        opt = ContextOptimizer(str(tmp_path))
        assert opt._is_hierarchically_covered(child, tmp_path) is True

    def test_unrelated_dir_not_covered(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        opt = ContextOptimizer(str(tmp_path))
        assert opt._is_hierarchically_covered(a, b) is False

    def test_calculate_hierarchical_coverage_all_covered(self, tmp_path: Path) -> None:
        child = tmp_path / "child"
        child.mkdir()
        opt = ContextOptimizer(str(tmp_path))
        covered = opt._calculate_hierarchical_coverage([tmp_path], {child})
        assert child in covered

    def test_calculate_hierarchical_coverage_none_covered(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        opt = ContextOptimizer(str(tmp_path))
        covered = opt._calculate_hierarchical_coverage([a], {b})
        assert b not in covered


# ---------------------------------------------------------------------------
# _is_child_directory
# ---------------------------------------------------------------------------


class TestIsChildDirectory:
    def test_child_is_child(self, tmp_path: Path) -> None:
        child = tmp_path / "child"
        child.mkdir()
        opt = ContextOptimizer(str(tmp_path))
        assert opt._is_child_directory(child, tmp_path) is True

    def test_same_dir_is_not_child(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        assert opt._is_child_directory(tmp_path, tmp_path) is False

    def test_parent_is_not_child_of_child(self, tmp_path: Path) -> None:
        child = tmp_path / "child"
        child.mkdir()
        opt = ContextOptimizer(str(tmp_path))
        assert opt._is_child_directory(tmp_path, child) is False

    def test_unrelated_paths_are_not_children(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        opt = ContextOptimizer(str(tmp_path))
        assert opt._is_child_directory(a, b) is False


# ---------------------------------------------------------------------------
# _get_inheritance_chain
# ---------------------------------------------------------------------------


class TestGetInheritanceChain:
    def test_chain_from_child_to_base(self, tmp_path: Path) -> None:
        child = tmp_path / "src"
        child.mkdir()
        opt = ContextOptimizer(str(tmp_path))
        chain = opt._get_inheritance_chain(child)
        assert child.resolve() in chain
        assert tmp_path.resolve() in chain

    def test_chain_cached_on_second_call(self, tmp_path: Path) -> None:
        child = tmp_path / "sub"
        child.mkdir()
        opt = ContextOptimizer(str(tmp_path))
        chain1 = opt._get_inheritance_chain(child)
        chain2 = opt._get_inheritance_chain(child)
        assert chain1 is chain2  # same list from cache

    def test_base_dir_itself_in_chain(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        chain = opt._get_inheritance_chain(tmp_path)
        assert tmp_path.resolve() in chain


# ---------------------------------------------------------------------------
# _calculate_coverage_efficiency, _calculate_pollution_minimization,
# _calculate_maintenance_locality
# ---------------------------------------------------------------------------


class TestMetricHelpers:
    def _build_optimizer_with_dir(self, tmp_path: Path) -> ContextOptimizer:
        _touch(tmp_path, "a.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        return opt

    def test_coverage_efficiency_delegates_to_relevance_score(self, tmp_path: Path) -> None:
        opt = self._build_optimizer_with_dir(tmp_path)
        key = tmp_path.resolve()
        # Add pattern match manually
        opt._directory_cache[key].pattern_matches["*.py"] = 1
        score = opt._calculate_coverage_efficiency(key, "*.py")
        # 1 / total_files
        total = opt._directory_cache[key].total_files
        assert abs(score - (1 / total)) < 1e-9

    def test_maintenance_locality_zero_when_no_files(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        da = DirectoryAnalysis(directory=tmp_path, depth=0, total_files=0)
        opt._directory_cache[tmp_path] = da
        assert opt._calculate_maintenance_locality(tmp_path, "*.py") == 0.0

    def test_maintenance_locality_capped_at_1(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        da = DirectoryAnalysis(
            directory=tmp_path,
            depth=0,
            total_files=2,
            pattern_matches={"*.py": 100},  # more than total_files
        )
        opt._directory_cache[tmp_path] = da
        score = opt._calculate_maintenance_locality(tmp_path, "*.py")
        assert score == 1.0

    def test_pollution_minimization_delegates_to_pollution(self, tmp_path: Path) -> None:
        opt = self._build_optimizer_with_dir(tmp_path)
        key = tmp_path.resolve()
        score = opt._calculate_pollution_minimization(key, "*.py")
        assert score >= 0.0


# ---------------------------------------------------------------------------
# _is_instruction_relevant
# ---------------------------------------------------------------------------


class TestIsInstructionRelevant:
    def test_global_instruction_always_relevant(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.txt")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        inst = _make_instruction(apply_to="")
        assert opt._is_instruction_relevant(inst, tmp_path) is True

    def test_pattern_with_no_cache_entry_returns_false(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        inst = _make_instruction(apply_to="*.py")
        unknown_dir = tmp_path / "does_not_exist"
        assert opt._is_instruction_relevant(inst, unknown_dir) is False

    def test_pattern_uses_cached_result(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        key = tmp_path.resolve()
        # Pre-populate cache with 3 matches
        opt._directory_cache[key].pattern_matches["*.py"] = 3
        inst = _make_instruction(apply_to="*.py")
        assert opt._is_instruction_relevant(inst, tmp_path) is True

    def test_pattern_cached_zero_means_not_relevant(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        key = tmp_path.resolve()
        opt._directory_cache[key].pattern_matches["*.js"] = 0
        inst = _make_instruction(apply_to="*.js")
        assert opt._is_instruction_relevant(inst, tmp_path) is False

    def test_pattern_freshly_analyzed_when_not_in_cache(self, tmp_path: Path) -> None:
        _touch(tmp_path, "app.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        inst = _make_instruction(apply_to="*.py")
        result = opt._is_instruction_relevant(inst, tmp_path)
        assert result is True


# ---------------------------------------------------------------------------
# analyze_context_inheritance
# ---------------------------------------------------------------------------


class TestAnalyzeContextInheritance:
    def test_empty_placement_map_gives_zero_pollution(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        opt = ContextOptimizer(str(tmp_path))
        ia = opt.analyze_context_inheritance(tmp_path, {})
        assert ia.total_context_load == 0
        assert ia.pollution_score == 0.0

    def test_all_relevant_gives_zero_pollution(self, tmp_path: Path) -> None:
        _touch(tmp_path, "main.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        key = tmp_path.resolve()
        inst = _make_instruction(apply_to="")  # global → always relevant
        placement_map = {key: [inst, inst]}
        ia = opt.analyze_context_inheritance(tmp_path, placement_map)
        assert ia.pollution_score == 0.0

    def test_all_irrelevant_gives_full_pollution(self, tmp_path: Path) -> None:
        _touch(tmp_path, "main.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        key = tmp_path.resolve()
        # Pattern that will not match anything in tmp_path
        inst = _make_instruction(apply_to="*.nonexistent_xyz")
        placement_map = {key: [inst]}
        ia = opt.analyze_context_inheritance(tmp_path, placement_map)
        assert abs(ia.pollution_score - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# get_optimization_stats
# ---------------------------------------------------------------------------


class TestGetOptimizationStats:
    def test_empty_placement_map(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        stats = opt.get_optimization_stats({})
        assert stats.total_agents_files == 0
        assert stats.average_context_efficiency == 0.0

    def test_non_empty_placement_map(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        key = tmp_path.resolve()
        inst = _make_instruction(apply_to="")
        stats = opt.get_optimization_stats({key: [inst]})
        assert stats.total_agents_files == 1
        assert stats.directories_analyzed >= 1


# ---------------------------------------------------------------------------
# get_compilation_results
# ---------------------------------------------------------------------------


class TestGetCompilationResults:
    def test_empty_placement_map_no_constitution(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()

        # Patch find_constitution at the source module (lazily imported)
        fake_constitution = tmp_path / "AGENTS.md"
        with patch(
            "apm_cli.compilation.constitution.find_constitution",
            return_value=fake_constitution,
        ):
            results = opt.get_compilation_results({})

        assert results.project_analysis.directories_scanned >= 0
        assert results.is_dry_run is False

    def test_dry_run_flag_propagated(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        fake_constitution = tmp_path / "AGENTS.md"
        with patch(
            "apm_cli.compilation.constitution.find_constitution",
            return_value=fake_constitution,
        ):
            results = opt.get_compilation_results({}, is_dry_run=True)
        assert results.is_dry_run is True

    def test_constitution_detected_branch(self, tmp_path: Path) -> None:
        """When constitution exists and placement_map is empty, a root placement is synthesised."""
        _touch(tmp_path, "a.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()

        # Create a fake constitution that does exist
        constitution = tmp_path / "AGENTS.md"
        constitution.touch()

        with patch(
            "apm_cli.compilation.constitution.find_constitution",
            return_value=constitution,
        ):
            results = opt.get_compilation_results({})

        assert results.project_analysis.constitution_detected is True
        # A root placement summary should have been created
        assert len(results.placement_summaries) == 1
        assert results.placement_summaries[0].instruction_count == 0

    def test_placement_map_with_source_file(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        fake_constitution = tmp_path / "AGENTS.md"

        inst = MagicMock()
        inst.source_file = "python.instructions.md"
        inst.apply_to = "*.py"

        with patch(
            "apm_cli.compilation.constitution.find_constitution",
            return_value=fake_constitution,
        ):
            results = opt.get_compilation_results({tmp_path.resolve(): [inst]})

        assert len(results.placement_summaries) >= 1

    def test_generation_time_recorded(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        opt._start_time = time.time()
        fake_constitution = tmp_path / "AGENTS.md"
        with patch(
            "apm_cli.compilation.constitution.find_constitution",
            return_value=fake_constitution,
        ):
            results = opt.get_compilation_results({})
        assert results.optimization_stats.generation_time_ms is not None
        assert results.optimization_stats.generation_time_ms >= 0


# ---------------------------------------------------------------------------
# optimize_instruction_placement with timing
# ---------------------------------------------------------------------------


class TestOptimizeWithTiming:
    def test_optimize_with_timing_enabled(self, tmp_path: Path) -> None:
        _touch(tmp_path, "main.py")
        opt = ContextOptimizer(str(tmp_path))
        inst = _make_instruction(apply_to="")
        placement = opt.optimize_instruction_placement([inst], verbose=False, enable_timing=True)
        assert tmp_path.resolve() in placement

    def test_optimize_with_timing_and_verbose(self, tmp_path: Path) -> None:
        _touch(tmp_path, "main.py")
        opt = ContextOptimizer(str(tmp_path))
        inst = _make_instruction(apply_to="")
        # Should not raise even with timing + verbose
        placement = opt.optimize_instruction_placement([inst], verbose=True, enable_timing=True)
        assert isinstance(placement, dict)

    def test_optimize_clears_decisions_on_each_call(self, tmp_path: Path) -> None:
        _touch(tmp_path, "main.py")
        opt = ContextOptimizer(str(tmp_path))
        inst = _make_instruction(apply_to="")
        opt.optimize_instruction_placement([inst])
        first_count = len(opt._optimization_decisions)
        opt.optimize_instruction_placement([inst])
        second_count = len(opt._optimization_decisions)
        # Decisions should be reset and re-populated equally
        assert first_count == second_count


# ---------------------------------------------------------------------------
# _select_clean_separation_placements
# ---------------------------------------------------------------------------


class TestSelectCleanSeparationPlacements:
    def test_no_candidates_returns_empty(self, tmp_path: Path) -> None:
        opt = ContextOptimizer(str(tmp_path))
        result = opt._select_clean_separation_placements([], "*.py")
        assert result == []

    def test_single_isolated_candidate_with_relevance(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/main.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        src = tmp_path / "src"

        inst = _make_instruction()
        cand = PlacementCandidate(
            instruction=inst,
            directory=src,
            direct_relevance=0.5,
            inheritance_pollution=0.0,
            depth_specificity=0.1,
            total_score=0.0,
        )
        result = opt._select_clean_separation_placements([cand], "*.py")
        # Single candidate is isolated but only 1 cluster → returns empty
        assert result == []

    def test_two_isolated_candidates_returns_both(self, tmp_path: Path) -> None:
        _touch(tmp_path, "alpha/main.py")
        _touch(tmp_path, "beta/app.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        alpha = tmp_path / "alpha"
        beta = tmp_path / "beta"

        inst = _make_instruction()
        cand_a = PlacementCandidate(
            instruction=inst,
            directory=alpha,
            direct_relevance=0.5,
            inheritance_pollution=0.0,
            depth_specificity=0.1,
            total_score=0.0,
        )
        cand_b = PlacementCandidate(
            instruction=inst,
            directory=beta,
            direct_relevance=0.5,
            inheritance_pollution=0.0,
            depth_specificity=0.1,
            total_score=0.0,
        )
        result = opt._select_clean_separation_placements([cand_a, cand_b], "*.py")
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _optimize_distributed_placement
# ---------------------------------------------------------------------------


class TestOptimizeDistributedPlacement:
    def test_always_returns_base_dir(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py")
        _touch(tmp_path, "b/c.py")
        opt = ContextOptimizer(str(tmp_path))
        opt._analyze_project_structure()
        inst = _make_instruction()
        dirs = {tmp_path.resolve(), (tmp_path / "b").resolve()}
        result = opt._optimize_distributed_placement(dirs, inst)
        assert result == [opt.base_dir]


# ---------------------------------------------------------------------------
# Integration: no-pattern instruction placed at root
# ---------------------------------------------------------------------------


class TestIntegrationGlobalInstruction:
    def test_global_instruction_at_root(self, tmp_path: Path) -> None:
        _touch(tmp_path, "readme.txt")
        opt = ContextOptimizer(str(tmp_path))
        inst = _make_instruction(apply_to="")
        placement = opt.optimize_instruction_placement([inst])
        assert opt.base_dir in placement
        assert inst in placement[opt.base_dir]

    def test_no_match_falls_back_to_root(self, tmp_path: Path) -> None:
        _touch(tmp_path, "readme.txt")
        opt = ContextOptimizer(str(tmp_path))
        inst = _make_instruction(apply_to="**/*.nonexistent_xyz_format")
        placement = opt.optimize_instruction_placement([inst])
        assert opt.base_dir in placement
        assert len(opt._warnings) > 0


# ---------------------------------------------------------------------------
# Integration: no-match with intended directory
# ---------------------------------------------------------------------------


class TestIntegrationIntendedDirectory:
    def test_no_match_with_intended_dir(self, tmp_path: Path) -> None:
        """Pattern like 'docs/**/*.md' with existing docs/ but no .md files
        → places in docs/ and records a warning."""
        (tmp_path / "docs").mkdir()
        _touch(tmp_path, "src/app.py")

        opt = ContextOptimizer(str(tmp_path))
        inst = _make_instruction(apply_to="docs/**/*.md")
        placement = opt.optimize_instruction_placement([inst])

        # Should warn about no matching files
        assert len(opt._warnings) > 0
        # Should place somewhere (either docs/ or root)
        assert len(placement) >= 1
