"""Comprehensive unit tests for src/apm_cli/output/formatters.py.

Targeting ≥80% branch+statement coverage on the CompilationFormatter class.
All tests are hermetic — no filesystem writes, no network, no subprocess calls.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers to build model objects without touching external systems
# ---------------------------------------------------------------------------
from apm_cli.output.models import (
    CompilationResults,
    OptimizationDecision,
    OptimizationStats,
    PlacementStrategy,
    PlacementSummary,
    ProjectAnalysis,
)
from apm_cli.primitives.models import Instruction


def _make_instruction(
    name: str = "test-instruction",
    apply_to: str = "**/*.py",
    file_path: Path | None = None,
) -> Instruction:
    """Build a minimal Instruction for testing."""
    return Instruction(
        name=name,
        file_path=file_path or Path(f"/fake/project/{name}.md"),
        description="A test instruction",
        apply_to=apply_to,
        content="## Do something useful",
    )


def _make_decision(
    pattern: str = "**/*.py",
    matching_dirs: int = 3,
    total_dirs: int = 10,
    distribution_score: float = 0.2,
    strategy: PlacementStrategy = PlacementStrategy.SINGLE_POINT,
    placement_dirs: list[Path] | None = None,
    instruction: Instruction | None = None,
) -> OptimizationDecision:
    """Build a minimal OptimizationDecision."""
    return OptimizationDecision(
        instruction=instruction or _make_instruction(),
        pattern=pattern,
        matching_directories=matching_dirs,
        total_directories=total_dirs,
        distribution_score=distribution_score,
        strategy=strategy,
        placement_directories=placement_dirs or [Path("/fake/project")],
        reasoning="test reasoning",
        relevance_score=0.8,
    )


def _make_placement_summary(
    path: Path | None = None,
    instruction_count: int = 2,
    source_count: int = 1,
    sources: list[str] | None = None,
) -> PlacementSummary:
    """Build a minimal PlacementSummary."""
    return PlacementSummary(
        path=path or Path("/fake/project/AGENTS.md"),
        instruction_count=instruction_count,
        source_count=source_count,
        sources=sources or ["instructions/test.md"],
    )


def _make_stats(
    efficiency: float = 0.75,
    pollution_improvement: float | None = None,
    baseline_efficiency: float | None = None,
    placement_accuracy: float | None = None,
    generation_time_ms: int | None = None,
    total_agents_files: int = 1,
    directories_analyzed: int = 5,
) -> OptimizationStats:
    """Build a minimal OptimizationStats."""
    return OptimizationStats(
        average_context_efficiency=efficiency,
        pollution_improvement=pollution_improvement,
        baseline_efficiency=baseline_efficiency,
        placement_accuracy=placement_accuracy,
        generation_time_ms=generation_time_ms,
        total_agents_files=total_agents_files,
        directories_analyzed=directories_analyzed,
    )


def _make_analysis(
    directories_scanned: int = 5,
    files_analyzed: int = 20,
    file_types_detected: set[str] | None = None,
    instruction_patterns_detected: int = 3,
    max_depth: int = 3,
    constitution_detected: bool = False,
    constitution_path: str | None = None,
) -> ProjectAnalysis:
    """Build a minimal ProjectAnalysis."""
    if file_types_detected is None:
        file_types_detected = {".py", ".md"}
    return ProjectAnalysis(
        directories_scanned=directories_scanned,
        files_analyzed=files_analyzed,
        file_types_detected=file_types_detected,
        instruction_patterns_detected=instruction_patterns_detected,
        max_depth=max_depth,
        constitution_detected=constitution_detected,
        constitution_path=constitution_path,
    )


def _make_results(
    decisions: list[OptimizationDecision] | None = None,
    summaries: list[PlacementSummary] | None = None,
    stats: OptimizationStats | None = None,
    analysis: ProjectAnalysis | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    is_dry_run: bool = False,
    target_name: str = "AGENTS.md",
) -> CompilationResults:
    """Build a minimal CompilationResults."""
    return CompilationResults(
        project_analysis=analysis or _make_analysis(),
        optimization_decisions=decisions or [_make_decision()],
        placement_summaries=summaries or [_make_placement_summary()],
        optimization_stats=stats or _make_stats(),
        warnings=warnings or [],
        errors=errors or [],
        is_dry_run=is_dry_run,
        target_name=target_name,
    )


# ---------------------------------------------------------------------------
# Import the formatter
# ---------------------------------------------------------------------------
from apm_cli.output.formatters import CompilationFormatter  # noqa: E402

# ===========================================================================
# Tests: CompilationFormatter.__init__
# ===========================================================================


class TestCompilationFormatterInit(unittest.TestCase):
    """Tests for __init__ behavior."""

    def test_no_color_sets_console_none(self) -> None:
        """use_color=False leaves console as None."""
        formatter = CompilationFormatter(use_color=False)
        self.assertFalse(formatter.use_color)
        self.assertIsNone(formatter.console)

    def test_default_target_name(self) -> None:
        """Default target name is AGENTS.md."""
        formatter = CompilationFormatter(use_color=False)
        self.assertEqual(formatter._target_name, "AGENTS.md")


# ===========================================================================
# Tests: format_default
# ===========================================================================


class TestFormatDefault(unittest.TestCase):
    """Tests for format_default."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_returns_non_empty_string(self) -> None:
        results = _make_results()
        output = self.formatter.format_default(results)
        self.assertIsInstance(output, str)
        self.assertTrue(len(output) > 0)

    def test_contains_project_discovery_header(self) -> None:
        results = _make_results()
        output = self.formatter.format_default(results)
        self.assertIn("Analyzing project structure", output)

    def test_contains_optimization_header(self) -> None:
        results = _make_results()
        output = self.formatter.format_default(results)
        self.assertIn("Optimizing placements", output)

    def test_contains_generated_summary(self) -> None:
        results = _make_results()
        output = self.formatter.format_default(results)
        self.assertIn("Generated", output)

    def test_includes_warnings_when_present(self) -> None:
        results = _make_results(warnings=["Watch out!"])
        output = self.formatter.format_default(results)
        self.assertIn("Warning", output)
        self.assertIn("Watch out!", output)

    def test_includes_errors_when_present(self) -> None:
        results = _make_results(errors=["Something broke"])
        output = self.formatter.format_default(results)
        self.assertIn("Error", output)
        self.assertIn("Something broke", output)

    def test_no_issues_section_when_clean(self) -> None:
        results = _make_results(warnings=[], errors=[])
        output = self.formatter.format_default(results)
        self.assertNotIn("Warning", output)
        self.assertNotIn("Error:", output)

    def test_updates_target_name(self) -> None:
        results = _make_results(target_name="CUSTOM.md")
        self.formatter.format_default(results)
        self.assertEqual(self.formatter._target_name, "CUSTOM.md")

    def test_dry_run_label(self) -> None:
        results = _make_results(is_dry_run=True)
        output = self.formatter.format_default(results)
        self.assertIn("[DRY RUN]", output)

    def test_plural_files(self) -> None:
        summaries = [_make_placement_summary(), _make_placement_summary()]
        results = _make_results(summaries=summaries)
        output = self.formatter.format_default(results)
        self.assertIn("files", output)

    def test_singular_file(self) -> None:
        summaries = [_make_placement_summary()]
        results = _make_results(summaries=summaries)
        output = self.formatter.format_default(results)
        self.assertIn("file", output)


# ===========================================================================
# Tests: format_verbose
# ===========================================================================


class TestFormatVerbose(unittest.TestCase):
    """Tests for format_verbose."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_returns_string(self) -> None:
        results = _make_results()
        output = self.formatter.format_verbose(results)
        self.assertIsInstance(output, str)

    def test_contains_math_analysis(self) -> None:
        results = _make_results()
        output = self.formatter.format_verbose(results)
        self.assertIn("Mathematical Optimization Analysis", output)

    def test_contains_coverage_analysis(self) -> None:
        results = _make_results()
        output = self.formatter.format_verbose(results)
        self.assertIn("Coverage vs. Efficiency Analysis", output)

    def test_contains_performance_metrics(self) -> None:
        results = _make_results()
        output = self.formatter.format_verbose(results)
        self.assertIn("Performance Metrics", output)

    def test_contains_placement_distribution(self) -> None:
        results = _make_results()
        output = self.formatter.format_verbose(results)
        self.assertIn("Placement Distribution", output)

    def test_includes_issues_when_present(self) -> None:
        results = _make_results(errors=["oops"])
        output = self.formatter.format_verbose(results)
        self.assertIn("oops", output)

    def test_updates_target_name(self) -> None:
        results = _make_results(target_name="RULES.md")
        self.formatter.format_verbose(results)
        self.assertEqual(self.formatter._target_name, "RULES.md")


# ===========================================================================
# Tests: format_dry_run
# ===========================================================================


class TestFormatDryRun(unittest.TestCase):
    """Tests for format_dry_run."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_returns_string(self) -> None:
        results = _make_results(is_dry_run=True)
        output = self.formatter.format_dry_run(results)
        self.assertIsInstance(output, str)

    def test_contains_dry_run_label(self) -> None:
        results = _make_results(is_dry_run=True)
        output = self.formatter.format_dry_run(results)
        self.assertIn("[DRY RUN]", output)

    def test_contains_file_preview(self) -> None:
        results = _make_results(is_dry_run=True)
        output = self.formatter.format_dry_run(results)
        self.assertIn("File generation preview", output)

    def test_contains_no_files_written_message(self) -> None:
        results = _make_results(is_dry_run=True)
        output = self.formatter.format_dry_run(results)
        self.assertIn("No files written", output)

    def test_includes_issues_when_present(self) -> None:
        results = _make_results(is_dry_run=True, warnings=["heads up"])
        output = self.formatter.format_dry_run(results)
        self.assertIn("heads up", output)


# ===========================================================================
# Tests: _format_project_discovery
# ===========================================================================


class TestFormatProjectDiscovery(unittest.TestCase):
    """Tests for _format_project_discovery."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_basic_header_present(self) -> None:
        analysis = _make_analysis()
        lines = self.formatter._format_project_discovery(analysis)
        text = "\n".join(lines)
        self.assertIn("Analyzing project structure", text)

    def test_directory_count_present(self) -> None:
        analysis = _make_analysis(directories_scanned=7)
        lines = self.formatter._format_project_discovery(analysis)
        text = "\n".join(lines)
        self.assertIn("7", text)

    def test_constitution_line_when_detected(self) -> None:
        analysis = _make_analysis(
            constitution_detected=True,
            constitution_path="CONSTITUTION.md",
        )
        lines = self.formatter._format_project_discovery(analysis)
        text = "\n".join(lines)
        self.assertIn("Constitution detected", text)
        self.assertIn("CONSTITUTION.md", text)

    def test_no_constitution_line_when_absent(self) -> None:
        analysis = _make_analysis(constitution_detected=False)
        lines = self.formatter._format_project_discovery(analysis)
        text = "\n".join(lines)
        self.assertNotIn("Constitution detected", text)

    def test_files_analyzed_count_present(self) -> None:
        analysis = _make_analysis(files_analyzed=42)
        lines = self.formatter._format_project_discovery(analysis)
        text = "\n".join(lines)
        self.assertIn("42", text)

    def test_max_depth_present(self) -> None:
        analysis = _make_analysis(max_depth=5)
        lines = self.formatter._format_project_discovery(analysis)
        text = "\n".join(lines)
        self.assertIn("max depth: 5", text)

    def test_instruction_patterns_present(self) -> None:
        analysis = _make_analysis(instruction_patterns_detected=9)
        lines = self.formatter._format_project_discovery(analysis)
        text = "\n".join(lines)
        self.assertIn("9", text)

    def test_returns_list(self) -> None:
        analysis = _make_analysis()
        result = self.formatter._format_project_discovery(analysis)
        self.assertIsInstance(result, list)


# ===========================================================================
# Tests: _format_project_discovery with use_color (branches at lines 238-249)
# ===========================================================================


class TestFormatProjectDiscoveryColor(unittest.TestCase):
    """Tests for _format_project_discovery use_color branches."""

    def test_color_branch_returns_nonempty_lines(self) -> None:
        formatter = CompilationFormatter(use_color=False)
        # Simulate use_color=True with a no-op styled
        formatter.use_color = True

        def _stub_styled(text: str, style: str) -> str:
            return text

        formatter._styled = _stub_styled  # type: ignore[method-assign]
        analysis = _make_analysis(constitution_detected=True, constitution_path="CONST.md")
        lines = formatter._format_project_discovery(analysis)
        text = "\n".join(lines)
        self.assertIn("Analyzing project structure", text)
        self.assertIn("Constitution detected", text)


# ===========================================================================
# Tests: _format_optimization_progress
# ===========================================================================


class TestFormatOptimizationProgress(unittest.TestCase):
    """Tests for _format_optimization_progress (plain-text path)."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_returns_list(self) -> None:
        decisions = [_make_decision()]
        result = self.formatter._format_optimization_progress(decisions)
        self.assertIsInstance(result, list)

    def test_header_present(self) -> None:
        decisions = [_make_decision()]
        lines = self.formatter._format_optimization_progress(decisions)
        text = "\n".join(lines)
        self.assertIn("Optimizing placements", text)

    def test_pattern_in_output(self) -> None:
        decisions = [_make_decision(pattern="**/*.ts")]
        lines = self.formatter._format_optimization_progress(decisions)
        text = "\n".join(lines)
        self.assertIn("**/*.ts", text)

    def test_empty_pattern_shows_global(self) -> None:
        decisions = [_make_decision(pattern="")]
        lines = self.formatter._format_optimization_progress(decisions)
        text = "\n".join(lines)
        self.assertIn("(global)", text)

    def test_multiple_placements_shows_locations(self) -> None:
        dirs = [Path("/fake/project/a"), Path("/fake/project/b")]
        decisions = [_make_decision(placement_dirs=dirs)]
        lines = self.formatter._format_optimization_progress(decisions)
        text = "\n".join(lines)
        self.assertIn("locations", text)

    def test_constitution_row_when_detected(self) -> None:
        analysis = _make_analysis(constitution_detected=True)
        decisions = [_make_decision()]
        lines = self.formatter._format_optimization_progress(decisions, analysis)
        text = "\n".join(lines)
        self.assertIn("constitution.md", text)

    def test_no_constitution_row_when_absent(self) -> None:
        analysis = _make_analysis(constitution_detected=False)
        decisions = [_make_decision()]
        lines = self.formatter._format_optimization_progress(decisions, analysis)
        text = "\n".join(lines)
        # constitution.md text should only appear with constitution
        self.assertNotIn("constitution.md", text)

    def test_instruction_with_exception_file_path(self) -> None:
        """When file_path.name raises, source_display stays 'unknown'."""
        from unittest.mock import PropertyMock

        decision = _make_decision()
        # Patch file_path.name as a property that raises on access
        mock_instruction = MagicMock()
        mock_fp = MagicMock()
        type(mock_fp).name = PropertyMock(side_effect=Exception("no name"))
        mock_instruction.file_path = mock_fp
        decision.instruction = mock_instruction
        lines = self.formatter._format_optimization_progress([decision])
        text = "\n".join(lines)
        self.assertIn("unknown", text)

    def test_no_analysis_arg_skips_constitution(self) -> None:
        """Passing no analysis means no constitution line."""
        decisions = [_make_decision()]
        lines = self.formatter._format_optimization_progress(decisions)
        text = "\n".join(lines)
        self.assertNotIn("constitution.md", text)


# ===========================================================================
# Tests: _format_results_summary
# ===========================================================================


class TestFormatResultsSummary(unittest.TestCase):
    """Tests for _format_results_summary."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_returns_list(self) -> None:
        results = _make_results()
        out = self.formatter._format_results_summary(results)
        self.assertIsInstance(out, list)

    def test_generated_line_present(self) -> None:
        results = _make_results()
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        self.assertIn("Generated", text)

    def test_dry_run_label_present(self) -> None:
        results = _make_results(is_dry_run=True)
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        self.assertIn("[DRY RUN]", text)

    def test_efficiency_percentage_present(self) -> None:
        stats = _make_stats(efficiency=0.65)
        results = _make_results(stats=stats)
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        self.assertIn("65.0%", text)

    def test_efficiency_improvement_positive(self) -> None:
        stats = _make_stats(efficiency=0.80, baseline_efficiency=0.50)
        results = _make_results(stats=stats)
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        self.assertIn("baseline:", text)
        self.assertIn("improvement:", text)

    def test_efficiency_improvement_negative(self) -> None:
        stats = _make_stats(efficiency=0.45, baseline_efficiency=0.60)
        results = _make_results(stats=stats)
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        self.assertIn("change:", text)

    def test_pollution_improvement_positive(self) -> None:
        stats = _make_stats(efficiency=0.70, pollution_improvement=0.30)
        results = _make_results(stats=stats)
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        self.assertIn("pollution", text.lower())

    def test_pollution_improvement_negative(self) -> None:
        stats = _make_stats(efficiency=0.70, pollution_improvement=-0.10)
        results = _make_results(stats=stats)
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        self.assertIn("pollution", text.lower())

    def test_placement_accuracy_present(self) -> None:
        stats = _make_stats(efficiency=0.70, placement_accuracy=0.95)
        results = _make_results(stats=stats)
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        self.assertIn("Placement accuracy", text)
        self.assertIn("95.0%", text)

    def test_generation_time_present(self) -> None:
        stats = _make_stats(efficiency=0.70, generation_time_ms=123)
        results = _make_results(stats=stats)
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        self.assertIn("123ms", text)

    def test_generation_time_none_changes_last_pipe(self) -> None:
        """When generation_time_ms is None but there are multiple metric lines,
        the last |- is replaced with +-."""
        stats = _make_stats(
            efficiency=0.70,
            placement_accuracy=0.90,
            pollution_improvement=0.10,
            generation_time_ms=None,
        )
        results = _make_results(stats=stats)
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        # At least one +- should appear somewhere
        self.assertIn("+-", text)

    def test_placement_distribution_header(self) -> None:
        results = _make_results()
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        self.assertIn("Placement Distribution", text)

    def test_multiple_summaries_tree_formatting(self) -> None:
        summaries = [
            _make_placement_summary(path=Path("/fake/project/a/AGENTS.md")),
            _make_placement_summary(path=Path("/fake/project/b/AGENTS.md")),
        ]
        results = _make_results(summaries=summaries)
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        # Last entry gets +-, intermediate gets |-
        self.assertIn("|-", text)
        self.assertIn("+-", text)

    def test_single_summary_uses_plus_prefix(self) -> None:
        summaries = [_make_placement_summary()]
        results = _make_results(summaries=summaries)
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        self.assertIn("+-", text)

    def test_source_count_singular(self) -> None:
        summaries = [_make_placement_summary(source_count=1)]
        results = _make_results(summaries=summaries)
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        self.assertIn("1 source", text)
        self.assertNotIn("1 sources", text)

    def test_source_count_plural(self) -> None:
        summaries = [_make_placement_summary(source_count=3)]
        results = _make_results(summaries=summaries)
        out = self.formatter._format_results_summary(results)
        text = "\n".join(out)
        self.assertIn("3 sources", text)


# ===========================================================================
# Tests: _format_dry_run_summary
# ===========================================================================


class TestFormatDryRunSummary(unittest.TestCase):
    """Tests for _format_dry_run_summary."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_returns_list(self) -> None:
        results = _make_results(is_dry_run=True)
        out = self.formatter._format_dry_run_summary(results)
        self.assertIsInstance(out, list)

    def test_dry_run_header_present(self) -> None:
        results = _make_results(is_dry_run=True)
        out = self.formatter._format_dry_run_summary(results)
        text = "\n".join(out)
        self.assertIn("[DRY RUN]", text)

    def test_instruction_count_singular(self) -> None:
        summaries = [_make_placement_summary(instruction_count=1)]
        results = _make_results(summaries=summaries)
        out = self.formatter._format_dry_run_summary(results)
        text = "\n".join(out)
        self.assertIn("1 instruction", text)
        self.assertNotIn("1 instructions", text)

    def test_instruction_count_plural(self) -> None:
        summaries = [_make_placement_summary(instruction_count=5)]
        results = _make_results(summaries=summaries)
        out = self.formatter._format_dry_run_summary(results)
        text = "\n".join(out)
        self.assertIn("5 instructions", text)

    def test_last_entry_plus_prefix(self) -> None:
        summaries = [_make_placement_summary(), _make_placement_summary()]
        results = _make_results(summaries=summaries)
        out = self.formatter._format_dry_run_summary(results)
        text = "\n".join(out)
        self.assertIn("+-", text)

    def test_no_files_written_message(self) -> None:
        results = _make_results(is_dry_run=True)
        out = self.formatter._format_dry_run_summary(results)
        text = "\n".join(out)
        self.assertIn("No files written", text)


# ===========================================================================
# Tests: _format_mathematical_analysis
# ===========================================================================


class TestFormatMathematicalAnalysis(unittest.TestCase):
    """Tests for _format_mathematical_analysis (plain-text path)."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_returns_list(self) -> None:
        decisions = [_make_decision()]
        out = self.formatter._format_mathematical_analysis(decisions)
        self.assertIsInstance(out, list)

    def test_header_present(self) -> None:
        decisions = [_make_decision()]
        out = self.formatter._format_mathematical_analysis(decisions)
        text = "\n".join(out)
        self.assertIn("Mathematical Optimization Analysis", text)

    def test_low_distribution_score_shows_single_point(self) -> None:
        decisions = [
            _make_decision(distribution_score=0.1, strategy=PlacementStrategy.SINGLE_POINT)
        ]
        out = self.formatter._format_mathematical_analysis(decisions)
        text = "\n".join(out)
        self.assertIn("Single Point", text)

    def test_high_distribution_score_shows_distributed(self) -> None:
        decisions = [_make_decision(distribution_score=0.8, strategy=PlacementStrategy.DISTRIBUTED)]
        out = self.formatter._format_mathematical_analysis(decisions)
        text = "\n".join(out)
        self.assertIn("Distributed", text)

    def test_medium_distribution_score_shows_selective(self) -> None:
        decisions = [
            _make_decision(distribution_score=0.5, strategy=PlacementStrategy.SELECTIVE_MULTI)
        ]
        out = self.formatter._format_mathematical_analysis(decisions)
        text = "\n".join(out)
        self.assertIn("Selective Multi", text)

    def test_mathematical_foundation_section(self) -> None:
        decisions = [_make_decision()]
        out = self.formatter._format_mathematical_analysis(decisions)
        text = "\n".join(out)
        self.assertIn("Mathematical Foundation", text)

    def test_objective_function_present(self) -> None:
        decisions = [_make_decision()]
        out = self.formatter._format_mathematical_analysis(decisions)
        text = "\n".join(out)
        self.assertIn("minimize", text)

    def test_empty_decisions_still_has_header(self) -> None:
        out = self.formatter._format_mathematical_analysis([])
        text = "\n".join(out)
        self.assertIn("Mathematical Optimization Analysis", text)


# ===========================================================================
# Tests: _format_mathematical_analysis — Rich path (medium score branches)
# ===========================================================================


class TestFormatMathematicalAnalysisRichBranches(unittest.TestCase):
    """Test medium-score branches in _format_mathematical_analysis."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_medium_score_with_root_placement(self) -> None:
        """Root placement path: placement dir is '.' — plain-text uses strategy.value."""
        dirs = [Path(".")]
        decision = _make_decision(
            distribution_score=0.5,
            placement_dirs=dirs,
            strategy=PlacementStrategy.SELECTIVE_MULTI,
        )
        out = self.formatter._format_mathematical_analysis([decision])
        text = "\n".join(out)
        # The plain-text path uses decision.strategy.value
        self.assertIn("Selective Multi", text)

    def test_multi_placement_dirs(self) -> None:
        """Multiple placement dirs -> shows 'locations' in coverage table fallback."""
        dirs = [Path("/fake/a"), Path("/fake/b")]
        decision = _make_decision(distribution_score=0.5, placement_dirs=dirs)
        out = self.formatter._format_mathematical_analysis([decision])
        # Plain text path checks distribution_score < 0.7 for "[+] Verified"
        text = "\n".join(out)
        self.assertIn("[+] Verified", text)

    def test_high_distribution_score_fallback(self) -> None:
        """High score (>0.7) shows '[!] Root Fallback' in plain-text path."""
        decision = _make_decision(distribution_score=0.9)
        out = self.formatter._format_mathematical_analysis([decision])
        text = "\n".join(out)
        self.assertIn("[!] Root Fallback", text)


# ===========================================================================
# Tests: _format_detailed_metrics — all efficiency/pollution branches
# ===========================================================================


class TestFormatDetailedMetrics(unittest.TestCase):
    """Tests for _format_detailed_metrics (plain-text path)."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def _run(self, efficiency: float, placement_accuracy: float | None = None) -> str:
        stats = _make_stats(efficiency=efficiency, placement_accuracy=placement_accuracy)
        return "\n".join(self.formatter._format_detailed_metrics(stats))

    def test_header_present(self) -> None:
        text = self._run(0.70)
        self.assertIn("Performance Metrics", text)

    def test_efficiency_excellent(self) -> None:
        text = self._run(0.90)
        self.assertIn("Excellent", text)

    def test_efficiency_good(self) -> None:
        text = self._run(0.65)
        self.assertIn("Good", text)

    def test_efficiency_fair(self) -> None:
        text = self._run(0.50)
        self.assertIn("Fair", text)

    def test_efficiency_poor(self) -> None:
        text = self._run(0.30)
        self.assertIn("Poor", text)

    def test_efficiency_very_poor(self) -> None:
        text = self._run(0.10)
        self.assertIn("Very Poor", text)

    def test_pollution_excellent(self) -> None:
        # Pollution ≤ 10 → Excellent
        text = self._run(0.95)  # pollution = 5%
        self.assertIn("Excellent", text)

    def test_pollution_good(self) -> None:
        # Pollution ≤ 25 → Good
        text = self._run(0.80)  # pollution = 20%
        self.assertIn("Good", text)

    def test_pollution_fair(self) -> None:
        # Pollution ≤ 50 → Fair
        text = self._run(0.60)  # pollution = 40%
        self.assertIn("Fair", text)

    def test_pollution_poor(self) -> None:
        # Pollution > 50 → Poor
        text = self._run(0.40)  # pollution = 60%
        self.assertIn("Poor", text)

    def test_guide_line_present(self) -> None:
        text = self._run(0.70)
        self.assertIn("Guide:", text)

    def test_returns_list(self) -> None:
        stats = _make_stats(efficiency=0.70)
        result = self.formatter._format_detailed_metrics(stats)
        self.assertIsInstance(result, list)


# ===========================================================================
# Tests: _format_issues
# ===========================================================================


class TestFormatIssues(unittest.TestCase):
    """Tests for _format_issues."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_error_formatted(self) -> None:
        lines = self.formatter._format_issues([], ["broken pipe"])
        text = "\n".join(lines)
        self.assertIn("x Error: broken pipe", text)

    def test_single_line_warning_formatted(self) -> None:
        lines = self.formatter._format_issues(["beware"], [])
        text = "\n".join(lines)
        self.assertIn("[!] Warning: beware", text)

    def test_multi_line_warning_first_line_has_header(self) -> None:
        warning = "first line\nsecond line\nthird line"
        lines = self.formatter._format_issues([warning], [])
        text = "\n".join(lines)
        self.assertIn("[!] Warning: first line", text)

    def test_multi_line_warning_subsequent_lines_indented(self) -> None:
        warning = "main\ndetail one\ndetail two"
        lines = self.formatter._format_issues([warning], [])
        text = "\n".join(lines)
        self.assertIn("detail one", text)
        self.assertIn("detail two", text)

    def test_multi_line_warning_empty_lines_skipped(self) -> None:
        """Empty lines in multi-line warning are not emitted."""
        warning = "main\n\ndetail"
        lines = self.formatter._format_issues([warning], [])
        # The "" empty line is skipped by the `if line.strip()` guard
        text = "\n".join(lines)
        self.assertIn("detail", text)

    def test_returns_empty_for_no_issues(self) -> None:
        lines = self.formatter._format_issues([], [])
        self.assertEqual(lines, [])

    def test_both_errors_and_warnings(self) -> None:
        lines = self.formatter._format_issues(["warn"], ["err"])
        text = "\n".join(lines)
        self.assertIn("Error", text)
        self.assertIn("Warning", text)


# ===========================================================================
# Tests: _format_coverage_explanation
# ===========================================================================


class TestFormatCoverageExplanation(unittest.TestCase):
    """Tests for _format_coverage_explanation."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_header_present(self) -> None:
        stats = _make_stats(efficiency=0.70)
        out = self.formatter._format_coverage_explanation(stats)
        text = "\n".join(out)
        self.assertIn("Coverage vs. Efficiency Analysis", text)

    def test_low_efficiency_message(self) -> None:
        stats = _make_stats(efficiency=0.20)  # < 30%
        out = self.formatter._format_coverage_explanation(stats)
        text = "\n".join(out)
        self.assertIn("Low Efficiency Detected", text)

    def test_moderate_efficiency_message(self) -> None:
        stats = _make_stats(efficiency=0.45)  # 30% ≤ x < 60%
        out = self.formatter._format_coverage_explanation(stats)
        text = "\n".join(out)
        self.assertIn("Moderate Efficiency", text)

    def test_high_efficiency_message(self) -> None:
        stats = _make_stats(efficiency=0.80)  # ≥ 60%
        out = self.formatter._format_coverage_explanation(stats)
        text = "\n".join(out)
        self.assertIn("High Efficiency", text)

    def test_coverage_priority_explanation_always_present(self) -> None:
        for eff in [0.20, 0.45, 0.80]:
            stats = _make_stats(efficiency=eff)
            out = self.formatter._format_coverage_explanation(stats)
            text = "\n".join(out)
            self.assertIn("Why Coverage Takes Priority", text)

    def test_returns_list(self) -> None:
        stats = _make_stats(efficiency=0.50)
        result = self.formatter._format_coverage_explanation(stats)
        self.assertIsInstance(result, list)


# ===========================================================================
# Tests: _get_placement_description
# ===========================================================================


class TestGetPlacementDescription(unittest.TestCase):
    """Tests for _get_placement_description."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_with_constitution_and_instructions(self) -> None:
        summary = _make_placement_summary(
            instruction_count=2,
            sources=["constitution.md", "other.md"],
        )
        desc = self.formatter._get_placement_description(summary)
        self.assertIn("Constitution", desc)
        self.assertIn("instruction", desc)

    def test_without_constitution(self) -> None:
        summary = _make_placement_summary(
            instruction_count=3,
            sources=["instructions/a.md"],
        )
        desc = self.formatter._get_placement_description(summary)
        self.assertNotIn("Constitution", desc)
        self.assertIn("instruction", desc)

    def test_constitution_only_no_instructions(self) -> None:
        summary = _make_placement_summary(
            instruction_count=0,
            sources=["constitution.md"],
        )
        desc = self.formatter._get_placement_description(summary)
        self.assertIn("Constitution", desc)
        self.assertNotIn("instruction", desc)

    def test_no_constitution_no_instructions_returns_content(self) -> None:
        summary = _make_placement_summary(
            instruction_count=0,
            sources=[],
        )
        desc = self.formatter._get_placement_description(summary)
        self.assertEqual(desc, "content")

    def test_single_instruction_singular(self) -> None:
        summary = _make_placement_summary(instruction_count=1, sources=["x.md"])
        desc = self.formatter._get_placement_description(summary)
        self.assertIn("1 instruction", desc)
        self.assertNotIn("1 instructions", desc)

    def test_multiple_instructions_plural(self) -> None:
        summary = _make_placement_summary(instruction_count=4, sources=["x.md"])
        desc = self.formatter._get_placement_description(summary)
        self.assertIn("4 instructions", desc)


# ===========================================================================
# Tests: _get_strategy_symbol & _get_strategy_color
# ===========================================================================


class TestStrategyHelpers(unittest.TestCase):
    """Tests for _get_strategy_symbol and _get_strategy_color."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_symbol_single_point(self) -> None:
        sym = self.formatter._get_strategy_symbol(PlacementStrategy.SINGLE_POINT)
        self.assertEqual(sym, "*")

    def test_symbol_selective_multi(self) -> None:
        sym = self.formatter._get_strategy_symbol(PlacementStrategy.SELECTIVE_MULTI)
        self.assertEqual(sym, "*")

    def test_symbol_distributed(self) -> None:
        sym = self.formatter._get_strategy_symbol(PlacementStrategy.DISTRIBUTED)
        self.assertEqual(sym, "*")

    def test_symbol_unknown_returns_star(self) -> None:
        sym = self.formatter._get_strategy_symbol(MagicMock())
        self.assertEqual(sym, "*")

    def test_color_single_point(self) -> None:
        color = self.formatter._get_strategy_color(PlacementStrategy.SINGLE_POINT)
        self.assertEqual(color, "green")

    def test_color_selective_multi(self) -> None:
        color = self.formatter._get_strategy_color(PlacementStrategy.SELECTIVE_MULTI)
        self.assertEqual(color, "yellow")

    def test_color_distributed(self) -> None:
        color = self.formatter._get_strategy_color(PlacementStrategy.DISTRIBUTED)
        self.assertEqual(color, "blue")

    def test_color_unknown_returns_white(self) -> None:
        color = self.formatter._get_strategy_color(MagicMock())
        self.assertEqual(color, "white")


# ===========================================================================
# Tests: _get_relative_display_path
# ===========================================================================


class TestGetRelativeDisplayPath(unittest.TestCase):
    """Tests for _get_relative_display_path."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)
        self.formatter._target_name = "AGENTS.md"

    def test_path_relative_to_cwd_root(self) -> None:
        """Path equal to cwd returns ./AGENTS.md."""
        with patch("apm_cli.output.formatters.Path") as MockPath:
            mock_cwd = MagicMock()
            MockPath.cwd.return_value = mock_cwd

            # relative_to returns Path(".")
            mock_rel = MagicMock()
            mock_rel.__eq__ = lambda self, other: str(other) == "."
            mock_rel.__str__ = lambda s: "."

            fake_path = MagicMock()
            fake_path.relative_to.return_value = mock_rel

            # Set up Path(".") comparison
            mock_dot = MagicMock()
            mock_dot.__eq__ = lambda s, o: True

            result = self.formatter._get_relative_display_path(Path.cwd())
        # Just ensure no exception is raised; result is a string
        self.assertIsInstance(result, str)

    def test_path_outside_cwd_returns_absolute(self) -> None:
        """ValueError from relative_to falls back to absolute path."""
        fake_path = MagicMock(spec=Path)
        fake_path.relative_to.side_effect = ValueError("not relative")
        fake_path.__truediv__ = lambda s, o: MagicMock(__str__=lambda x: f"/abs/{o}")

        result = self.formatter._get_relative_display_path(fake_path)
        self.assertIsInstance(result, str)

    def test_returns_string(self) -> None:
        path = Path("/some/project/subdir")
        result = self.formatter._get_relative_display_path(path)
        self.assertIsInstance(result, str)


# ===========================================================================
# Tests: _styled
# ===========================================================================


class TestStyled(unittest.TestCase):
    """Tests for _styled method."""

    def test_no_color_returns_text_unchanged(self) -> None:
        formatter = CompilationFormatter(use_color=False)
        result = formatter._styled("hello world", "bold red")
        self.assertEqual(result, "hello world")

    def test_use_color_false_never_calls_console(self) -> None:
        formatter = CompilationFormatter(use_color=False)
        formatter.console = MagicMock()  # Should never be called
        formatter._styled("text", "dim")
        formatter.console.print.assert_not_called()


# ===========================================================================
# Tests: RICH_AVAILABLE import guard (lines 17-19)
# ===========================================================================


class TestRichImportGuard(unittest.TestCase):
    """Verify RICH_AVAILABLE flag is correctly set."""

    def test_rich_available_is_bool(self) -> None:
        from apm_cli.output.formatters import RICH_AVAILABLE

        self.assertIsInstance(RICH_AVAILABLE, bool)

    def test_formatter_color_off_when_no_rich(self) -> None:
        """If RICH_AVAILABLE were False, use_color would remain False."""
        with patch("apm_cli.output.formatters.RICH_AVAILABLE", False):
            formatter = CompilationFormatter(use_color=True)
            # use_color = use_color AND RICH_AVAILABLE = True AND False = False
            self.assertFalse(formatter.use_color)


# ===========================================================================
# Tests: _format_final_summary (verbose path)
# ===========================================================================


class TestFormatFinalSummary(unittest.TestCase):
    """Tests for _format_final_summary."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def test_returns_list(self) -> None:
        results = _make_results()
        out = self.formatter._format_final_summary(results)
        self.assertIsInstance(out, list)

    def test_dry_run_label_present(self) -> None:
        results = _make_results(is_dry_run=True)
        out = self.formatter._format_final_summary(results)
        text = "\n".join(out)
        self.assertIn("[DRY RUN]", text)

    def test_generated_label_when_not_dry_run(self) -> None:
        results = _make_results(is_dry_run=False)
        out = self.formatter._format_final_summary(results)
        text = "\n".join(out)
        self.assertIn("Generated", text)

    def test_efficiency_percentage_in_output(self) -> None:
        stats = _make_stats(efficiency=0.55)
        results = _make_results(stats=stats)
        out = self.formatter._format_final_summary(results)
        text = "\n".join(out)
        self.assertIn("55.0%", text)

    def test_placement_distribution_header(self) -> None:
        results = _make_results()
        out = self.formatter._format_final_summary(results)
        text = "\n".join(out)
        self.assertIn("Placement Distribution", text)

    def test_efficiency_improvement_positive_in_final(self) -> None:
        stats = _make_stats(efficiency=0.80, baseline_efficiency=0.50)
        results = _make_results(stats=stats)
        out = self.formatter._format_final_summary(results)
        text = "\n".join(out)
        self.assertIn("improvement:", text)

    def test_pollution_improvement_in_final(self) -> None:
        stats = _make_stats(efficiency=0.70, pollution_improvement=0.20)
        results = _make_results(stats=stats)
        out = self.formatter._format_final_summary(results)
        text = "\n".join(out)
        self.assertIn("pollution", text.lower())

    def test_placement_accuracy_in_final(self) -> None:
        stats = _make_stats(efficiency=0.70, placement_accuracy=0.92)
        results = _make_results(stats=stats)
        out = self.formatter._format_final_summary(results)
        text = "\n".join(out)
        self.assertIn("Placement accuracy", text)

    def test_generation_time_in_final(self) -> None:
        stats = _make_stats(efficiency=0.70, generation_time_ms=456)
        results = _make_results(stats=stats)
        out = self.formatter._format_final_summary(results)
        text = "\n".join(out)
        self.assertIn("456ms", text)

    def test_generation_time_none_pipe_change_final(self) -> None:
        stats = _make_stats(
            efficiency=0.70,
            pollution_improvement=0.10,
            placement_accuracy=0.90,
            generation_time_ms=None,
        )
        results = _make_results(stats=stats)
        out = self.formatter._format_final_summary(results)
        text = "\n".join(out)
        self.assertIn("+-", text)

    def test_pollution_improvement_negative_in_final(self) -> None:
        stats = _make_stats(efficiency=0.70, pollution_improvement=-0.05)
        results = _make_results(stats=stats)
        out = self.formatter._format_final_summary(results)
        text = "\n".join(out)
        self.assertIn("pollution", text.lower())

    def test_efficiency_improvement_negative_in_final(self) -> None:
        stats = _make_stats(efficiency=0.45, baseline_efficiency=0.60)
        results = _make_results(stats=stats)
        out = self.formatter._format_final_summary(results)
        text = "\n".join(out)
        self.assertIn("change:", text)


# ===========================================================================
# Integration smoke-tests: round-trip all three public format methods
# ===========================================================================


class TestFormatRoundTrips(unittest.TestCase):
    """Smoke-tests: all three public format methods return valid, non-empty strings."""

    def setUp(self) -> None:
        self.formatter = CompilationFormatter(use_color=False)

    def _rich_results(self) -> CompilationResults:
        """Results with multiple decisions, summaries, and all stats populated."""
        decisions = [
            _make_decision(
                pattern="**/*.py",
                distribution_score=0.15,
                strategy=PlacementStrategy.SINGLE_POINT,
                placement_dirs=[Path("/fake/project/src")],
            ),
            _make_decision(
                pattern="**/*.ts",
                distribution_score=0.5,
                strategy=PlacementStrategy.SELECTIVE_MULTI,
                placement_dirs=[Path("/fake/project/frontend"), Path("/fake/project/api")],
            ),
            _make_decision(
                pattern="",
                distribution_score=0.85,
                strategy=PlacementStrategy.DISTRIBUTED,
                placement_dirs=[
                    Path("/fake/project"),
                    Path("/fake/project/a"),
                    Path("/fake/project/b"),
                ],
            ),
        ]
        summaries = [
            _make_placement_summary(
                path=Path("/fake/project/AGENTS.md"),
                instruction_count=5,
                source_count=3,
                sources=["constitution.md", "instr/a.md", "instr/b.md"],
            ),
            _make_placement_summary(
                path=Path("/fake/project/src/AGENTS.md"),
                instruction_count=2,
                source_count=1,
                sources=["instr/c.md"],
            ),
        ]
        stats = _make_stats(
            efficiency=0.72,
            pollution_improvement=0.18,
            baseline_efficiency=0.55,
            placement_accuracy=0.91,
            generation_time_ms=89,
        )
        analysis = _make_analysis(
            directories_scanned=12,
            files_analyzed=150,
            file_types_detected={".py", ".md", ".ts", ".json", ".yaml"},
            instruction_patterns_detected=8,
            max_depth=4,
            constitution_detected=True,
            constitution_path="CONSTITUTION.md",
        )
        return _make_results(
            decisions=decisions,
            summaries=summaries,
            stats=stats,
            analysis=analysis,
            warnings=["a warning"],
            errors=[],
        )

    def test_format_default_round_trip(self) -> None:
        results = self._rich_results()
        output = self.formatter.format_default(results)
        self.assertIsInstance(output, str)
        self.assertGreater(len(output), 100)

    def test_format_verbose_round_trip(self) -> None:
        results = self._rich_results()
        output = self.formatter.format_verbose(results)
        self.assertIsInstance(output, str)
        self.assertGreater(len(output), 200)

    def test_format_dry_run_round_trip(self) -> None:
        results = self._rich_results()
        results.is_dry_run = True
        output = self.formatter.format_dry_run(results)
        self.assertIsInstance(output, str)
        self.assertIn("[DRY RUN]", output)

    def test_format_default_no_decisions(self) -> None:
        results = _make_results(decisions=[], summaries=[])
        output = self.formatter.format_default(results)
        self.assertIsInstance(output, str)

    def test_format_verbose_no_decisions(self) -> None:
        results = _make_results(decisions=[], summaries=[])
        output = self.formatter.format_verbose(results)
        self.assertIsInstance(output, str)


# ===========================================================================
# Tests: ProjectAnalysis.get_file_types_summary (exercise model helper)
# ===========================================================================


class TestProjectAnalysisGetFileTypesSummary(unittest.TestCase):
    """Tests for ProjectAnalysis.get_file_types_summary."""

    def test_empty_returns_none_string(self) -> None:
        analysis = _make_analysis(file_types_detected=set())
        self.assertEqual(analysis.get_file_types_summary(), "none")

    def test_one_type(self) -> None:
        analysis = _make_analysis(file_types_detected={".py"})
        summary = analysis.get_file_types_summary()
        self.assertIn("py", summary)

    def test_three_types(self) -> None:
        analysis = _make_analysis(file_types_detected={".py", ".md", ".ts"})
        summary = analysis.get_file_types_summary()
        # Should have all three, comma separated
        self.assertIn(",", summary)

    def test_more_than_three_shows_and_more(self) -> None:
        analysis = _make_analysis(file_types_detected={".py", ".md", ".ts", ".json", ".yaml"})
        summary = analysis.get_file_types_summary()
        self.assertIn("more", summary)


# ===========================================================================
# Tests: use_color=True paths (exercises Rich branches)
# All internal output is captured via console.capture() so no stdout noise.
# ===========================================================================


def _color_formatter() -> CompilationFormatter:
    """Return a formatter with use_color=True (Rich must be available).

    Thin alias kept for test readability; the real construction (incl. the
    pinned Console width that prevents column truncation under narrow
    Windows CI terminals) lives in ``tests/unit/_formatter_helpers.py``
    so the two formatter test modules cannot drift.
    """
    from tests.unit._formatter_helpers import make_color_formatter

    return make_color_formatter()


class TestFormatDefaultColor(unittest.TestCase):
    """Tests for format_default with use_color=True."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def test_returns_non_empty_string(self) -> None:
        results = _make_results()
        output = self.formatter.format_default(results)
        self.assertIsInstance(output, str)
        self.assertTrue(len(output) > 0)

    def test_includes_project_discovery(self) -> None:
        results = _make_results()
        output = self.formatter.format_default(results)
        # Rich strips ANSI but text should still appear
        self.assertTrue(len(output) > 0)

    def test_includes_warnings_colored(self) -> None:
        results = _make_results(warnings=["caution!"])
        output = self.formatter.format_default(results)
        self.assertIn("caution!", output)

    def test_includes_errors_colored(self) -> None:
        results = _make_results(errors=["broken"])
        output = self.formatter.format_default(results)
        self.assertIn("broken", output)

    def test_dry_run_colored(self) -> None:
        results = _make_results(is_dry_run=True)
        output = self.formatter.format_default(results)
        self.assertIn("DRY RUN", output)


class TestFormatVerboseColor(unittest.TestCase):
    """Tests for format_verbose with use_color=True."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def test_returns_non_empty_string(self) -> None:
        results = _make_results()
        output = self.formatter.format_verbose(results)
        self.assertIsInstance(output, str)
        self.assertTrue(len(output) > 0)

    def test_includes_issues(self) -> None:
        results = _make_results(errors=["oops"])
        output = self.formatter.format_verbose(results)
        self.assertIn("oops", output)

    def test_dry_run_verbose_colored(self) -> None:
        results = _make_results(is_dry_run=True)
        output = self.formatter.format_verbose(results)
        self.assertIn("DRY RUN", output)


class TestFormatDryRunColor(unittest.TestCase):
    """Tests for format_dry_run with use_color=True."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def test_returns_non_empty_string(self) -> None:
        results = _make_results(is_dry_run=True)
        output = self.formatter.format_dry_run(results)
        self.assertIsInstance(output, str)
        self.assertTrue(len(output) > 0)

    def test_contains_dry_run_label(self) -> None:
        results = _make_results(is_dry_run=True)
        output = self.formatter.format_dry_run(results)
        self.assertIn("DRY RUN", output)


class TestFormatProjectDiscoveryColorBranches(unittest.TestCase):
    """Tests for _format_project_discovery with use_color=True."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def test_header_present_colored(self) -> None:
        analysis = _make_analysis()
        lines = self.formatter._format_project_discovery(analysis)
        text = "\n".join(lines)
        self.assertIn("Analyzing project structure", text)

    def test_constitution_detected_colored(self) -> None:
        analysis = _make_analysis(constitution_detected=True, constitution_path="CONST.md")
        lines = self.formatter._format_project_discovery(analysis)
        text = "\n".join(lines)
        self.assertIn("Constitution detected", text)
        self.assertIn("CONST.md", text)

    def test_no_constitution_colored(self) -> None:
        analysis = _make_analysis(constitution_detected=False)
        lines = self.formatter._format_project_discovery(analysis)
        text = "\n".join(lines)
        self.assertNotIn("Constitution detected", text)


class TestFormatOptimizationProgressColorBranches(unittest.TestCase):
    """Tests for _format_optimization_progress with use_color=True (Rich table path)."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def test_returns_list_colored(self) -> None:
        decisions = [_make_decision()]
        result = self.formatter._format_optimization_progress(decisions)
        self.assertIsInstance(result, list)

    def test_header_present_colored(self) -> None:
        decisions = [_make_decision()]
        lines = self.formatter._format_optimization_progress(decisions)
        text = "\n".join(lines)
        self.assertIn("Optimizing placements", text)

    def test_constitution_row_colored(self) -> None:
        analysis = _make_analysis(constitution_detected=True)
        decisions = [_make_decision()]
        lines = self.formatter._format_optimization_progress(decisions, analysis)
        text = "\n".join(lines)
        # Rich may truncate column text; check for prefix
        self.assertIn("constitution", text)

    def test_multiple_placements_colored(self) -> None:
        dirs = [Path("/fake/project/a"), Path("/fake/project/b")]
        decisions = [_make_decision(placement_dirs=dirs)]
        lines = self.formatter._format_optimization_progress(decisions)
        text = "\n".join(lines)
        self.assertIn("2 locations", text)

    def test_empty_pattern_global_colored(self) -> None:
        decisions = [_make_decision(pattern="")]
        lines = self.formatter._format_optimization_progress(decisions)
        text = "\n".join(lines)
        self.assertIn("(global)", text)

    def test_instruction_exception_colored(self) -> None:
        """Exception in file_path.name falls back to str(file_path)[-20:]."""
        from unittest.mock import PropertyMock

        decision = _make_decision()
        mock_instruction = MagicMock()
        mock_fp = MagicMock()
        type(mock_fp).name = PropertyMock(side_effect=Exception("no name"))
        mock_fp.__str__ = MagicMock(return_value="/fake/project/some-instruction.md")
        mock_instruction.file_path = mock_fp
        decision.instruction = mock_instruction
        lines = self.formatter._format_optimization_progress([decision])
        # Should not raise; source_display falls back to str(file_path)[-20:]
        self.assertIsInstance(lines, list)


class TestFormatResultsSummaryColorBranches(unittest.TestCase):
    """Tests for _format_results_summary with use_color=True."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def test_generated_colored(self) -> None:
        results = _make_results()
        lines = self.formatter._format_results_summary(results)
        text = "\n".join(lines)
        self.assertIn("Generated", text)

    def test_dry_run_yellow_colored(self) -> None:
        results = _make_results(is_dry_run=True)
        lines = self.formatter._format_results_summary(results)
        text = "\n".join(lines)
        self.assertIn("DRY RUN", text)

    def test_efficiency_colored(self) -> None:
        stats = _make_stats(efficiency=0.75)
        results = _make_results(stats=stats)
        lines = self.formatter._format_results_summary(results)
        text = "\n".join(lines)
        self.assertIn("75.0%", text)

    def test_placement_distribution_colored(self) -> None:
        results = _make_results()
        lines = self.formatter._format_results_summary(results)
        text = "\n".join(lines)
        self.assertIn("Placement Distribution", text)

    def test_with_full_stats_colored(self) -> None:
        stats = _make_stats(
            efficiency=0.80,
            baseline_efficiency=0.60,
            pollution_improvement=0.15,
            placement_accuracy=0.93,
            generation_time_ms=120,
        )
        results = _make_results(stats=stats)
        lines = self.formatter._format_results_summary(results)
        text = "\n".join(lines)
        self.assertIn("120ms", text)


class TestFormatDryRunSummaryColorBranches(unittest.TestCase):
    """Tests for _format_dry_run_summary with use_color=True."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def test_dry_run_header_colored(self) -> None:
        results = _make_results(is_dry_run=True)
        lines = self.formatter._format_dry_run_summary(results)
        text = "\n".join(lines)
        self.assertIn("DRY RUN", text)

    def test_no_files_written_colored(self) -> None:
        results = _make_results(is_dry_run=True)
        lines = self.formatter._format_dry_run_summary(results)
        text = "\n".join(lines)
        self.assertIn("No files written", text)

    def test_last_entry_plus_colored(self) -> None:
        summaries = [_make_placement_summary(), _make_placement_summary()]
        results = _make_results(summaries=summaries)
        lines = self.formatter._format_dry_run_summary(results)
        text = "\n".join(lines)
        self.assertIn("+-", text)


class TestFormatMathematicalAnalysisColorBranches(unittest.TestCase):
    """Tests for _format_mathematical_analysis with use_color=True (Rich table path)."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def test_header_present_colored(self) -> None:
        decisions = [_make_decision()]
        lines = self.formatter._format_mathematical_analysis(decisions)
        text = "\n".join(lines)
        self.assertIn("Mathematical Optimization Analysis", text)

    def test_low_score_colored(self) -> None:
        decisions = [_make_decision(distribution_score=0.15)]
        lines = self.formatter._format_mathematical_analysis(decisions)
        self.assertIsInstance(lines, list)

    def test_high_score_colored(self) -> None:
        decisions = [_make_decision(distribution_score=0.85)]
        lines = self.formatter._format_mathematical_analysis(decisions)
        self.assertIsInstance(lines, list)

    def test_medium_score_root_colored(self) -> None:
        """Medium score with root dir as placement."""
        dirs = [Path(".")]
        decision = _make_decision(distribution_score=0.5, placement_dirs=dirs)
        lines = self.formatter._format_mathematical_analysis([decision])
        self.assertIsInstance(lines, list)

    def test_medium_score_non_root_colored(self) -> None:
        """Medium score with non-root dir."""
        dirs = [Path("/fake/project/src")]
        decision = _make_decision(distribution_score=0.5, placement_dirs=dirs)
        lines = self.formatter._format_mathematical_analysis([decision])
        self.assertIsInstance(lines, list)

    def test_multi_placement_dirs_colored(self) -> None:
        """Multiple placement dirs → 'locations' path in coverage table."""
        dirs = [Path("/fake/a"), Path("/fake/b")]
        decision = _make_decision(distribution_score=0.5, placement_dirs=dirs)
        lines = self.formatter._format_mathematical_analysis([decision])
        self.assertIsInstance(lines, list)

    def test_single_dir_local_coverage_colored(self) -> None:
        """Single dir + low distribution score → local coverage path."""
        dirs = [Path("/fake/project/src")]
        decision = _make_decision(distribution_score=0.2, placement_dirs=dirs)
        lines = self.formatter._format_mathematical_analysis([decision])
        self.assertIsInstance(lines, list)

    def test_instruction_exception_colored(self) -> None:
        """Exception in instruction.file_path.name in Rich path → 'unknown'."""
        from unittest.mock import PropertyMock

        decision = _make_decision()
        mock_instruction = MagicMock()
        mock_fp = MagicMock()
        type(mock_fp).name = PropertyMock(side_effect=Exception("no name"))
        mock_instruction.file_path = mock_fp
        decision.instruction = mock_instruction
        lines = self.formatter._format_mathematical_analysis([decision])
        self.assertIsInstance(lines, list)

    def test_empty_decisions_colored(self) -> None:
        lines = self.formatter._format_mathematical_analysis([])
        text = "\n".join(lines)
        self.assertIn("Mathematical Optimization Analysis", text)


class TestFormatDetailedMetricsColorBranches(unittest.TestCase):
    """Tests for _format_detailed_metrics with use_color=True (Rich table path)."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def _run(self, efficiency: float, placement_accuracy: float | None = None) -> str:
        stats = _make_stats(efficiency=efficiency, placement_accuracy=placement_accuracy)
        return "\n".join(self.formatter._format_detailed_metrics(stats))

    def test_header_colored(self) -> None:
        text = self._run(0.70)
        self.assertIn("Performance Metrics", text)

    def test_excellent_efficiency_colored(self) -> None:
        text = self._run(0.90)
        self.assertIn("Excellent", text)

    def test_good_efficiency_colored(self) -> None:
        text = self._run(0.65)
        self.assertIn("Good", text)

    def test_fair_efficiency_colored(self) -> None:
        text = self._run(0.50)
        self.assertIn("Fair", text)

    def test_poor_efficiency_colored(self) -> None:
        text = self._run(0.30)
        self.assertIn("Poor", text)

    def test_very_poor_efficiency_colored(self) -> None:
        text = self._run(0.10)
        self.assertIn("Very Poor", text)

    def test_placement_accuracy_excellent_colored(self) -> None:
        text = self._run(0.90, placement_accuracy=0.97)
        self.assertIn("97.0%", text)

    def test_placement_accuracy_good_colored(self) -> None:
        text = self._run(0.90, placement_accuracy=0.88)
        self.assertIn("88.0%", text)

    def test_placement_accuracy_fair_colored(self) -> None:
        text = self._run(0.90, placement_accuracy=0.75)
        self.assertIn("75.0%", text)

    def test_placement_accuracy_poor_colored(self) -> None:
        text = self._run(0.90, placement_accuracy=0.60)
        self.assertIn("60.0%", text)

    def test_returns_list_colored(self) -> None:
        stats = _make_stats(efficiency=0.70)
        result = self.formatter._format_detailed_metrics(stats)
        self.assertIsInstance(result, list)


class TestFormatIssuesColorBranches(unittest.TestCase):
    """Tests for _format_issues with use_color=True."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def test_error_colored(self) -> None:
        lines = self.formatter._format_issues([], ["critical failure"])
        text = "\n".join(lines)
        self.assertIn("critical failure", text)

    def test_single_line_warning_colored(self) -> None:
        lines = self.formatter._format_issues(["watch out!"], [])
        text = "\n".join(lines)
        self.assertIn("watch out!", text)

    def test_multi_line_warning_colored(self) -> None:
        lines = self.formatter._format_issues(["line1\nline2\nline3"], [])
        text = "\n".join(lines)
        self.assertIn("line1", text)
        self.assertIn("line2", text)

    def test_multi_line_warning_empty_line_skipped_colored(self) -> None:
        lines = self.formatter._format_issues(["head\n\nbody"], [])
        text = "\n".join(lines)
        self.assertIn("body", text)

    def test_both_errors_and_warnings_colored(self) -> None:
        lines = self.formatter._format_issues(["w"], ["e"])
        text = "\n".join(lines)
        self.assertTrue(len(text) > 0)


class TestFormatCoverageExplanationColorBranches(unittest.TestCase):
    """Tests for _format_coverage_explanation with use_color=True."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def test_header_colored(self) -> None:
        stats = _make_stats(efficiency=0.70)
        lines = self.formatter._format_coverage_explanation(stats)
        text = "\n".join(lines)
        self.assertIn("Coverage vs. Efficiency Analysis", text)

    def test_low_efficiency_colored(self) -> None:
        stats = _make_stats(efficiency=0.20)
        lines = self.formatter._format_coverage_explanation(stats)
        text = "\n".join(lines)
        self.assertIn("Low Efficiency Detected", text)

    def test_moderate_efficiency_colored(self) -> None:
        stats = _make_stats(efficiency=0.45)
        lines = self.formatter._format_coverage_explanation(stats)
        text = "\n".join(lines)
        self.assertIn("Moderate Efficiency", text)

    def test_high_efficiency_colored(self) -> None:
        stats = _make_stats(efficiency=0.80)
        lines = self.formatter._format_coverage_explanation(stats)
        text = "\n".join(lines)
        self.assertIn("High Efficiency", text)


class TestStyledWithColor(unittest.TestCase):
    """Tests for _styled with use_color=True."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def test_styled_returns_string(self) -> None:
        result = self.formatter._styled("hello", "bold")
        self.assertIsInstance(result, str)

    def test_styled_contains_original_text(self) -> None:
        result = self.formatter._styled("my text", "dim")
        self.assertIn("my text", result)

    def test_styled_empty_string(self) -> None:
        result = self.formatter._styled("", "red")
        self.assertIsInstance(result, str)


class TestFormatFinalSummaryColorBranches(unittest.TestCase):
    """Tests for _format_final_summary with use_color=True."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def test_dry_run_label_colored(self) -> None:
        results = _make_results(is_dry_run=True)
        lines = self.formatter._format_final_summary(results)
        text = "\n".join(lines)
        self.assertIn("DRY RUN", text)

    def test_generated_label_colored(self) -> None:
        results = _make_results(is_dry_run=False)
        lines = self.formatter._format_final_summary(results)
        text = "\n".join(lines)
        self.assertIn("Generated", text)

    def test_efficiency_colored(self) -> None:
        stats = _make_stats(efficiency=0.65)
        results = _make_results(stats=stats)
        lines = self.formatter._format_final_summary(results)
        text = "\n".join(lines)
        self.assertIn("65.0%", text)

    def test_placement_distribution_colored(self) -> None:
        results = _make_results()
        lines = self.formatter._format_final_summary(results)
        text = "\n".join(lines)
        self.assertIn("Placement Distribution", text)

    def test_with_all_metrics_colored(self) -> None:
        stats = _make_stats(
            efficiency=0.80,
            baseline_efficiency=0.55,
            pollution_improvement=0.20,
            placement_accuracy=0.90,
            generation_time_ms=75,
        )
        results = _make_results(stats=stats)
        lines = self.formatter._format_final_summary(results)
        text = "\n".join(lines)
        self.assertIn("75ms", text)

    def test_efficiency_improvement_negative_colored(self) -> None:
        stats = _make_stats(efficiency=0.40, baseline_efficiency=0.60)
        results = _make_results(stats=stats)
        lines = self.formatter._format_final_summary(results)
        text = "\n".join(lines)
        self.assertIn("change:", text)

    def test_pollution_improvement_negative_colored(self) -> None:
        stats = _make_stats(efficiency=0.70, pollution_improvement=-0.05)
        results = _make_results(stats=stats)
        lines = self.formatter._format_final_summary(results)
        text = "\n".join(lines)
        self.assertTrue(len(text) > 0)

    def test_generation_time_none_pipe_change_colored(self) -> None:
        stats = _make_stats(
            efficiency=0.70,
            pollution_improvement=0.10,
            placement_accuracy=0.90,
            generation_time_ms=None,
        )
        results = _make_results(stats=stats)
        lines = self.formatter._format_final_summary(results)
        text = "\n".join(lines)
        self.assertIn("+-", text)


class TestRichColorIntegrationRoundTrips(unittest.TestCase):
    """Round-trip integration tests with use_color=True."""

    def setUp(self) -> None:
        try:
            self.formatter = _color_formatter()
        except unittest.SkipTest as exc:
            self.skipTest(str(exc))

    def _full_results(self) -> CompilationResults:
        decisions = [
            _make_decision(
                pattern="**/*.py",
                distribution_score=0.15,
                strategy=PlacementStrategy.SINGLE_POINT,
                placement_dirs=[Path("/fake/project/src")],
            ),
            _make_decision(
                pattern="**/*.ts",
                distribution_score=0.5,
                strategy=PlacementStrategy.SELECTIVE_MULTI,
                placement_dirs=[Path("/fake/project/fe"), Path("/fake/project/api")],
            ),
            _make_decision(
                pattern="",
                distribution_score=0.85,
                strategy=PlacementStrategy.DISTRIBUTED,
                placement_dirs=[Path("/fake"), Path("/fake/a"), Path("/fake/b")],
            ),
        ]
        summaries = [
            _make_placement_summary(
                path=Path("/fake/project/AGENTS.md"),
                instruction_count=5,
                source_count=3,
                sources=["constitution.md", "instr/a.md", "instr/b.md"],
            ),
            _make_placement_summary(
                path=Path("/fake/project/src/AGENTS.md"),
                instruction_count=2,
                source_count=1,
                sources=["instr/c.md"],
            ),
        ]
        stats = _make_stats(
            efficiency=0.72,
            pollution_improvement=0.18,
            baseline_efficiency=0.55,
            placement_accuracy=0.91,
            generation_time_ms=89,
        )
        analysis = _make_analysis(
            directories_scanned=12,
            files_analyzed=150,
            file_types_detected={".py", ".md", ".ts", ".json", ".yaml"},
            instruction_patterns_detected=8,
            max_depth=4,
            constitution_detected=True,
            constitution_path="CONSTITUTION.md",
        )
        return _make_results(
            decisions=decisions,
            summaries=summaries,
            stats=stats,
            analysis=analysis,
            warnings=["a warning"],
            errors=[],
        )

    def test_format_default_colored_round_trip(self) -> None:
        output = self.formatter.format_default(self._full_results())
        self.assertIsInstance(output, str)
        self.assertGreater(len(output), 50)

    def test_format_verbose_colored_round_trip(self) -> None:
        output = self.formatter.format_verbose(self._full_results())
        self.assertIsInstance(output, str)
        self.assertGreater(len(output), 100)

    def test_format_dry_run_colored_round_trip(self) -> None:
        results = self._full_results()
        results.is_dry_run = True
        output = self.formatter.format_dry_run(results)
        self.assertIsInstance(output, str)
        self.assertIn("DRY RUN", output)

    def test_format_verbose_with_errors_colored(self) -> None:
        results = self._full_results()
        results.errors = ["something went wrong"]
        output = self.formatter.format_verbose(results)
        self.assertIn("something went wrong", output)


if __name__ == "__main__":
    unittest.main()
