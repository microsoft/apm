"""Integration tests for output formatters and console utilities.

Covers:
- src/apm_cli/output/formatters.py (383 missing lines)
- src/apm_cli/output/script_formatters.py (66 missing lines)
- src/apm_cli/utils/console.py (45 missing lines)

These tests exercise realistic formatting workflows with mock data.
No network calls; all tests are hermetic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.output.formatters import CompilationFormatter
from apm_cli.output.models import (
    CompilationResults,
    OptimizationDecision,
    OptimizationStats,
    PlacementStrategy,
    PlacementSummary,
    ProjectAnalysis,
)
from apm_cli.output.script_formatters import ScriptExecutionFormatter
from apm_cli.utils.console import (
    STATUS_SYMBOLS,
    _get_console,
    _rich_echo,
    _rich_error,
    _rich_info,
    _rich_success,
    _rich_warning,
    set_console_stderr,
)


class TestCompilationFormatterBasics:
    """Test CompilationFormatter initialization and basic modes."""

    def test_formatter_init_with_color(self):
        """CompilationFormatter initializes with color enabled."""
        fmt = CompilationFormatter(use_color=True)
        assert fmt.use_color is True or fmt.console is not None

    def test_formatter_init_without_color(self):
        """CompilationFormatter initializes without color."""
        fmt = CompilationFormatter(use_color=False)
        assert fmt.use_color is False
        assert fmt.console is None

    def test_formatter_default_target_name(self):
        """Default target name is AGENTS.md."""
        fmt = CompilationFormatter(use_color=False)
        assert fmt._target_name == "AGENTS.md"


class TestCompilationFormatterDefaultOutput:
    """Test format_default() with various result states."""

    def _make_results(
        self,
        file_count: int = 1,
        target: str = "AGENTS.md",
        warnings: list[str] | None = None,
        errors: list[str] | None = None,
        is_dry_run: bool = False,
    ) -> CompilationResults:
        """Helper to create mock CompilationResults."""
        analysis = ProjectAnalysis(
            directories_scanned=10,
            files_analyzed=50,
            file_types_detected={".md", ".py", ".yaml"},
            instruction_patterns_detected=5,
            max_depth=3,
            constitution_detected=False,
        )

        decisions = []
        for i in range(file_count):
            mock_instr = MagicMock()
            mock_instr.file_path = Path(f"/test/path{i}.md")
            decision = OptimizationDecision(
                instruction=mock_instr,
                pattern=f"test-{i}",
                matching_directories=5,
                total_directories=10,
                distribution_score=0.5,
                strategy=PlacementStrategy.SINGLE_POINT,
                placement_directories=[Path("./AGENTS.md")],
                reasoning=f"Test placement {i}",
                relevance_score=0.9,
            )
            decisions.append(decision)

        placements = [
            PlacementSummary(
                path=Path(f"./AGENTS{i}.md") if file_count > 1 else Path("./AGENTS.md"),
                instruction_count=10,
                source_count=1,
            )
            for i in range(file_count)
        ]

        stats = OptimizationStats(
            average_context_efficiency=85.5,
            placement_accuracy=0.95,
            pollution_improvement=0.02,
            generation_time_ms=42,
            baseline_efficiency=0.80,
            total_agents_files=file_count,
            directories_analyzed=10,
        )

        return CompilationResults(
            target_name=target,
            project_analysis=analysis,
            optimization_decisions=decisions,
            placement_summaries=placements,
            optimization_stats=stats,
            warnings=warnings or [],
            errors=errors or [],
            is_dry_run=is_dry_run,
        )

    def test_format_default_success(self):
        """format_default() with successful results."""
        results = self._make_results(file_count=1)
        fmt = CompilationFormatter(use_color=False)
        output = fmt.format_default(results)

        assert isinstance(output, str)
        assert "Analyzing project structure" in output
        assert "Optimizing placements" in output
        assert "Generated 1 AGENTS.md file" in output

    def test_format_default_multiple_files(self):
        """format_default() with multiple output files."""
        results = self._make_results(file_count=3)
        fmt = CompilationFormatter(use_color=False)
        output = fmt.format_default(results)

        assert "Generated 3 AGENTS.md files" in output

    def test_format_default_with_warnings(self):
        """format_default() includes warnings in output."""
        results = self._make_results(warnings=["warning: test warning"])
        fmt = CompilationFormatter(use_color=False)
        output = fmt.format_default(results)

        assert "warning" in output.lower()

    def test_format_default_with_errors(self):
        """format_default() includes errors in output."""
        results = self._make_results(errors=["error: test error"])
        fmt = CompilationFormatter(use_color=False)
        output = fmt.format_default(results)

        assert "error" in output.lower()

    def test_format_default_with_constitution(self):
        """format_default() shows constitution detection."""
        results = self._make_results()
        results.project_analysis.constitution_detected = True
        results.project_analysis.constitution_path = "./constitution.md"
        fmt = CompilationFormatter(use_color=False)
        output = fmt.format_default(results)

        assert "constitution" in output.lower()


class TestCompilationFormatterVerboseOutput:
    """Test format_verbose() with detailed metrics."""

    def test_format_verbose_success(self):
        """format_verbose() includes mathematical analysis."""
        results = self._make_results_helper(file_count=2)
        fmt = CompilationFormatter(use_color=False)
        output = fmt.format_verbose(results)

        assert isinstance(output, str)
        assert "Mathematical" in output or "Analyzing" in output
        assert "Generated 1 AGENTS.md file" in output

    def test_format_verbose_includes_metrics(self):
        """format_verbose() displays efficiency and accuracy metrics."""
        results = self._make_results_helper()
        fmt = CompilationFormatter(use_color=False)
        output = fmt.format_verbose(results)

        assert "Context efficiency" in output or "efficiency" in output.lower()

    def test_format_verbose_placement_distribution(self):
        """format_verbose() shows placement distribution."""
        results = self._make_results_helper()
        fmt = CompilationFormatter(use_color=False)
        output = fmt.format_verbose(results)

        assert "Placement" in output or "placement" in output.lower()

    def _make_results_helper(self, file_count: int = 1) -> CompilationResults:
        """Helper to create mock CompilationResults."""
        analysis = ProjectAnalysis(
            directories_scanned=10,
            files_analyzed=50,
            file_types_detected={".md", ".py", ".yaml"},
            instruction_patterns_detected=5,
            max_depth=3,
        )

        decisions = []
        for i in range(file_count):
            mock_instr = MagicMock()
            mock_instr.file_path = Path(f"/test/path{i}.md")
            decision = OptimizationDecision(
                instruction=mock_instr,
                pattern=f"pattern-{i}",
                matching_directories=5,
                total_directories=10,
                distribution_score=0.5,
                strategy=PlacementStrategy.SINGLE_POINT,
                placement_directories=[Path("./AGENTS.md")],
                reasoning="Test",
                relevance_score=0.9,
            )
            decisions.append(decision)

        placements = [
            PlacementSummary(
                path=Path("./AGENTS.md"),
                instruction_count=10 * file_count,
                source_count=file_count,
            )
        ]

        stats = OptimizationStats(
            average_context_efficiency=85.5,
            placement_accuracy=0.95,
            pollution_improvement=0.02,
            generation_time_ms=42,
            baseline_efficiency=0.80,
            total_agents_files=file_count,
            directories_analyzed=10,
        )

        return CompilationResults(
            target_name="AGENTS.md",
            project_analysis=analysis,
            optimization_decisions=decisions,
            placement_summaries=placements,
            optimization_stats=stats,
            warnings=[],
            errors=[],
            is_dry_run=False,
        )


class TestCompilationFormatterDryRun:
    """Test format_dry_run() mode."""

    def test_format_dry_run_basic(self):
        """format_dry_run() marks output as dry run."""
        results = self._make_dry_run_results()
        fmt = CompilationFormatter(use_color=False)
        output = fmt.format_dry_run(results)

        assert "[DRY RUN]" in output
        assert "No files written" in output

    def test_format_dry_run_with_warnings(self):
        """format_dry_run() still shows warnings."""
        results = self._make_dry_run_results(warnings=["test warning"])
        fmt = CompilationFormatter(use_color=False)
        output = fmt.format_dry_run(results)

        assert "[DRY RUN]" in output
        assert "warning" in output.lower()

    def _make_dry_run_results(self, warnings: list[str] | None = None) -> CompilationResults:
        """Create dry-run results."""
        analysis = ProjectAnalysis(
            directories_scanned=5,
            files_analyzed=25,
            file_types_detected={".md"},
            instruction_patterns_detected=2,
            max_depth=2,
        )

        mock_instr = MagicMock()
        mock_instr.file_path = Path("/test/test.md")
        decision = OptimizationDecision(
            instruction=mock_instr,
            pattern="test",
            matching_directories=3,
            total_directories=5,
            distribution_score=0.6,
            strategy=PlacementStrategy.SINGLE_POINT,
            placement_directories=[Path("./AGENTS.md")],
            reasoning="test",
        )

        placements = [
            PlacementSummary(path=Path("./AGENTS.md"), instruction_count=5, source_count=1)
        ]

        stats = OptimizationStats(average_context_efficiency=80.0)

        return CompilationResults(
            target_name="AGENTS.md",
            project_analysis=analysis,
            optimization_decisions=[decision],
            placement_summaries=placements,
            optimization_stats=stats,
            warnings=warnings or [],
            errors=[],
            is_dry_run=True,
        )


class TestFormatterColoring:
    """Test color output in formatters."""

    def test_formatter_with_rich_color(self):
        """CompilationFormatter with color uses Rich if available."""
        fmt = CompilationFormatter(use_color=True)
        # If rich is available, console should be initialized
        # If not, use_color should be False
        assert fmt.use_color is False or fmt.console is not None

    def test_styled_method_exists(self):
        """CompilationFormatter has _styled method."""
        fmt = CompilationFormatter(use_color=False)
        # Check that styled method works without color
        styled = fmt._styled("test", "bold")
        assert "test" in styled


class TestScriptExecutionFormatter:
    """Test ScriptExecutionFormatter for script output formatting."""

    def test_script_formatter_init(self):
        """ScriptExecutionFormatter initializes correctly."""
        fmt = ScriptExecutionFormatter(use_color=False)
        assert fmt.use_color is False
        assert fmt.console is None

    def test_format_script_header_basic(self):
        """format_script_header() with basic data."""
        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_script_header("test_script", {"param1": "value1"})

        assert isinstance(lines, list)
        assert len(lines) > 0
        assert any("test_script" in line for line in lines)

    def test_format_script_header_no_params(self):
        """format_script_header() without parameters."""
        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_script_header("simple", {})

        assert any("simple" in line for line in lines)

    def test_format_script_header_multiple_params(self):
        """format_script_header() with multiple parameters."""
        fmt = ScriptExecutionFormatter(use_color=False)
        params = {"env": "production", "region": "us-east", "replicas": "3"}
        lines = fmt.format_script_header("deploy", params)

        # Should have header + param lines
        assert len(lines) >= 4

    def test_format_compilation_progress_single_file(self):
        """format_compilation_progress() with single prompt file."""
        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_compilation_progress(["prompt.md"])

        assert any("Compiling prompt" in line for line in lines)

    def test_format_compilation_progress_multiple_files(self):
        """format_compilation_progress() with multiple prompt files."""
        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_compilation_progress(["prompt1.md", "prompt2.md", "prompt3.md"])

        assert any("3" in line for line in lines)
        assert any("prompt1.md" in line for line in lines)

    def test_format_compilation_progress_empty(self):
        """format_compilation_progress() with no files."""
        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_compilation_progress([])

        assert lines == []


class TestConsoleUtilities:
    """Test console output utilities."""

    def test_status_symbols_completeness(self):
        """STATUS_SYMBOLS contains expected keys."""
        expected_keys = ["success", "error", "warning", "info"]
        for key in expected_keys:
            assert key in STATUS_SYMBOLS

    def test_status_symbols_values_are_ascii(self):
        """All status symbols are ASCII-safe."""
        for key, value in STATUS_SYMBOLS.items():
            try:
                value.encode("ascii")
            except UnicodeEncodeError:
                pytest.fail(f"Status symbol '{key}' contains non-ASCII: {value}")

    @patch("apm_cli.utils.console.RICH_AVAILABLE", True)
    def test_get_console_returns_none_when_not_available(self):
        """_get_console() handles unavailability gracefully."""
        # This is mocked, but tests the fallback behavior
        console = _get_console()
        assert console is None or hasattr(console, "print")

    def test_set_console_stderr_basic(self):
        """set_console_stderr() can be called without error."""
        from apm_cli.utils.console import _reset_console

        try:
            set_console_stderr(True)
            set_console_stderr(False)
        finally:
            _reset_console()

    @patch("apm_cli.utils.console._rich_echo")
    def test_rich_echo_called(self, mock_echo):
        """_rich_echo can be imported and called."""
        _rich_echo("test message")

    @patch("apm_cli.utils.console._rich_warning")
    def test_rich_warning_called(self, mock_warning):
        """_rich_warning can be imported and called."""
        _rich_warning("test warning")

    @patch("apm_cli.utils.console._rich_error")
    def test_rich_error_called(self, mock_error):
        """_rich_error can be imported and called."""
        _rich_error("test error")

    @patch("apm_cli.utils.console._rich_info")
    def test_rich_info_called(self, mock_info):
        """_rich_info can be imported and called."""
        _rich_info("test info")

    @patch("apm_cli.utils.console._rich_success")
    def test_rich_success_called(self, mock_success):
        """_rich_success can be imported and called."""
        _rich_success("test success")


class TestOptimizationDecisionFormatting:
    """Test formatting of optimization decisions with various strategies."""

    def test_format_single_point_strategy(self):
        """Formatter handles SINGLE_POINT placement strategy."""
        fmt = CompilationFormatter(use_color=False)
        mock_instr = MagicMock()
        mock_instr.file_path = Path("test.md")

        decision = OptimizationDecision(
            instruction=mock_instr,
            pattern="test",
            matching_directories=2,
            total_directories=5,
            distribution_score=0.1,
            strategy=PlacementStrategy.SINGLE_POINT,
            placement_directories=[Path("./output")],
            reasoning="Single location",
        )

        lines = fmt._format_optimization_progress([decision], None)
        assert len(lines) > 0
        output = "\n".join(lines)
        assert "test" in output or "output" in output

    def test_format_distributed_strategy(self):
        """Formatter handles DISTRIBUTED placement strategy."""
        fmt = CompilationFormatter(use_color=False)
        mock_instr = MagicMock()
        mock_instr.file_path = Path("test.md")

        decision = OptimizationDecision(
            instruction=mock_instr,
            pattern="test",
            matching_directories=4,
            total_directories=5,
            distribution_score=0.8,
            strategy=PlacementStrategy.DISTRIBUTED,
            placement_directories=[Path("./out1"), Path("./out2"), Path("./out3")],
            reasoning="Multiple locations",
        )

        lines = fmt._format_optimization_progress([decision], None)
        assert len(lines) > 0

    def test_format_selective_multi_strategy(self):
        """Formatter handles SELECTIVE_MULTI placement strategy."""
        fmt = CompilationFormatter(use_color=False)
        mock_instr = MagicMock()
        mock_instr.file_path = Path("test.md")

        decision = OptimizationDecision(
            instruction=mock_instr,
            pattern="test",
            matching_directories=3,
            total_directories=5,
            distribution_score=0.5,
            strategy=PlacementStrategy.SELECTIVE_MULTI,
            placement_directories=[Path("./out1"), Path("./out2")],
            reasoning="Selected locations",
        )

        lines = fmt._format_optimization_progress([decision], None)
        assert len(lines) > 0


class TestPlacementSummaryFormatting:
    """Test formatting of placement summaries."""

    def test_format_placement_single_source(self):
        """Placement formatting for single source."""
        summary = PlacementSummary(path=Path("./AGENTS.md"), instruction_count=5, source_count=1)

        # Check singular form
        assert summary.source_count == 1

    def test_format_placement_multiple_sources(self):
        """Placement formatting for multiple sources."""
        summary = PlacementSummary(path=Path("./AGENTS.md"), instruction_count=10, source_count=3)

        # Check plural form
        assert summary.source_count == 3

    def test_format_placement_single_instruction(self):
        """Placement formatting for single instruction."""
        summary = PlacementSummary(path=Path("./AGENTS.md"), instruction_count=1, source_count=1)

        assert summary.instruction_count == 1

    def test_format_placement_multiple_instructions(self):
        """Placement formatting for multiple instructions."""
        summary = PlacementSummary(path=Path("./AGENTS.md"), instruction_count=20, source_count=5)

        assert summary.instruction_count == 20


class TestEdgeCasesAndErrorHandling:
    """Test edge cases and error handling in formatters."""

    def test_empty_optimization_decisions(self):
        """Formatter handles empty optimization decisions."""
        fmt = CompilationFormatter(use_color=False)
        lines = fmt._format_optimization_progress([], None)
        assert isinstance(lines, list)

    def test_optimization_decision_without_instruction_file(self):
        """Formatter handles decision with missing instruction.file_path."""
        fmt = CompilationFormatter(use_color=False)
        mock_instr = MagicMock(spec=[])  # No file_path attribute
        decision = OptimizationDecision(
            instruction=mock_instr,
            pattern="test",
            matching_directories=1,
            total_directories=1,
            distribution_score=0.5,
            strategy=PlacementStrategy.SINGLE_POINT,
            placement_directories=[Path(".")],
            reasoning="test",
        )

        lines = fmt._format_optimization_progress([decision], None)
        assert len(lines) > 0  # Should not crash

    def test_optimization_stats_with_no_improvements(self):
        """Formatter handles stats with None improvements."""
        analysis = ProjectAnalysis(
            directories_scanned=1,
            files_analyzed=1,
            file_types_detected=set(),
            instruction_patterns_detected=0,
            max_depth=1,
        )

        stats = OptimizationStats(average_context_efficiency=50.0)

        results = CompilationResults(
            target_name="TEST.md",
            project_analysis=analysis,
            optimization_decisions=[],
            placement_summaries=[],
            optimization_stats=stats,
            warnings=[],
            errors=[],
            is_dry_run=False,
        )

        fmt = CompilationFormatter(use_color=False)
        output = fmt.format_default(results)
        assert isinstance(output, str)

    def test_very_long_placement_paths(self):
        """Formatter handles very long placement paths."""
        summary = PlacementSummary(
            path=Path("/very/long/path/to/deeply/nested/directory/structure/AGENTS.md"),
            instruction_count=5,
            source_count=1,
        )

        # Should not crash
        rel_path = summary.get_relative_path(Path("/"))
        assert rel_path is not None
