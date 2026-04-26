"""Unit tests for apm_cli.output module (models, formatters, script_formatters)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.output.models import (
    CompilationResults,
    OptimizationDecision,
    OptimizationStats,
    PlacementStrategy,
    PlacementSummary,
    ProjectAnalysis,
)
from apm_cli.output.formatters import CompilationFormatter
from apm_cli.output.script_formatters import ScriptExecutionFormatter
from apm_cli.primitives.models import Instruction


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_instruction(name: str = "test", apply_to: str = "**/*.py") -> Instruction:
    return Instruction(
        name=name,
        file_path=Path(f".apm/instructions/{name}.instructions.md"),
        description="Test instruction",
        apply_to=apply_to,
        content="# Test\nSome content",
    )


def _make_project_analysis(**kwargs) -> ProjectAnalysis:
    defaults = dict(
        directories_scanned=5,
        files_analyzed=20,
        file_types_detected={".py", ".md"},
        instruction_patterns_detected=3,
        max_depth=4,
    )
    defaults.update(kwargs)
    return ProjectAnalysis(**defaults)


def _make_optimization_decision(**kwargs) -> OptimizationDecision:
    defaults = dict(
        instruction=_make_instruction(),
        pattern="**/*.py",
        matching_directories=3,
        total_directories=5,
        distribution_score=0.6,
        strategy=PlacementStrategy.SINGLE_POINT,
        placement_directories=[Path(".")],
        reasoning="Global coverage",
        relevance_score=0.9,
    )
    defaults.update(kwargs)
    return OptimizationDecision(**defaults)


def _make_stats(**kwargs) -> OptimizationStats:
    defaults = dict(average_context_efficiency=0.75)
    defaults.update(kwargs)
    return OptimizationStats(**defaults)


def _make_placement_summary(**kwargs) -> PlacementSummary:
    defaults = dict(
        path=Path("AGENTS.md"),
        instruction_count=2,
        source_count=1,
        sources=["src/foo.instructions.md"],
    )
    defaults.update(kwargs)
    return PlacementSummary(**defaults)


def _make_results(**kwargs) -> CompilationResults:
    defaults = dict(
        project_analysis=_make_project_analysis(),
        optimization_decisions=[_make_optimization_decision()],
        placement_summaries=[_make_placement_summary()],
        optimization_stats=_make_stats(),
    )
    defaults.update(kwargs)
    return CompilationResults(**defaults)


# ---------------------------------------------------------------------------
# Tests: ProjectAnalysis
# ---------------------------------------------------------------------------

class TestProjectAnalysis:
    def test_get_file_types_summary_empty(self):
        analysis = _make_project_analysis(file_types_detected=set())
        assert analysis.get_file_types_summary() == "none"

    def test_get_file_types_summary_few(self):
        analysis = _make_project_analysis(file_types_detected={".py", ".md"})
        result = analysis.get_file_types_summary()
        assert "md" in result
        assert "py" in result

    def test_get_file_types_summary_many(self):
        analysis = _make_project_analysis(
            file_types_detected={".py", ".md", ".txt", ".json", ".yaml"}
        )
        result = analysis.get_file_types_summary()
        assert "more" in result

    def test_get_file_types_summary_strips_dots(self):
        analysis = _make_project_analysis(file_types_detected={".py"})
        result = analysis.get_file_types_summary()
        assert result == "py"

    def test_get_file_types_summary_exactly_three(self):
        analysis = _make_project_analysis(
            file_types_detected={".py", ".md", ".txt"}
        )
        result = analysis.get_file_types_summary()
        assert "more" not in result

    def test_constitution_defaults_to_false(self):
        analysis = _make_project_analysis()
        assert analysis.constitution_detected is False
        assert analysis.constitution_path is None


# ---------------------------------------------------------------------------
# Tests: OptimizationDecision
# ---------------------------------------------------------------------------

class TestOptimizationDecision:
    def test_distribution_ratio_normal(self):
        dec = _make_optimization_decision(matching_directories=3, total_directories=5)
        assert abs(dec.distribution_ratio - 0.6) < 1e-9

    def test_distribution_ratio_zero_total(self):
        dec = _make_optimization_decision(matching_directories=0, total_directories=0)
        assert dec.distribution_ratio == 0.0

    def test_distribution_ratio_full(self):
        dec = _make_optimization_decision(matching_directories=5, total_directories=5)
        assert abs(dec.distribution_ratio - 1.0) < 1e-9

    def test_strategy_enum_values(self):
        assert PlacementStrategy.SINGLE_POINT.value == "Single Point"
        assert PlacementStrategy.SELECTIVE_MULTI.value == "Selective Multi"
        assert PlacementStrategy.DISTRIBUTED.value == "Distributed"


# ---------------------------------------------------------------------------
# Tests: PlacementSummary
# ---------------------------------------------------------------------------

class TestPlacementSummary:
    def test_get_relative_path_same_dir(self, tmp_path):
        # When path == base_dir (a directory), relative_to returns '.' -> method returns '.'
        summary = _make_placement_summary(path=tmp_path)
        rel = summary.get_relative_path(tmp_path)
        assert rel == Path(".")

    def test_get_relative_path_subdir(self, tmp_path):
        subdir = tmp_path / "src"
        summary = _make_placement_summary(path=subdir / "AGENTS.md")
        rel = summary.get_relative_path(tmp_path)
        assert str(rel) == "src/AGENTS.md"

    def test_get_relative_path_unrelated(self):
        summary = _make_placement_summary(path=Path("/some/absolute/path/AGENTS.md"))
        rel = summary.get_relative_path(Path("/other/base"))
        assert rel == Path("/some/absolute/path/AGENTS.md")


# ---------------------------------------------------------------------------
# Tests: OptimizationStats
# ---------------------------------------------------------------------------

class TestOptimizationStats:
    def test_efficiency_percentage(self):
        stats = _make_stats(average_context_efficiency=0.75)
        assert abs(stats.efficiency_percentage - 75.0) < 1e-9

    def test_efficiency_improvement_none_when_no_baseline(self):
        stats = _make_stats()
        assert stats.efficiency_improvement is None

    def test_efficiency_improvement_positive(self):
        stats = _make_stats(
            average_context_efficiency=0.9, baseline_efficiency=0.6
        )
        improvement = stats.efficiency_improvement
        assert improvement is not None
        assert improvement > 0

    def test_efficiency_improvement_negative(self):
        stats = _make_stats(
            average_context_efficiency=0.4, baseline_efficiency=0.8
        )
        improvement = stats.efficiency_improvement
        assert improvement is not None
        assert improvement < 0


# ---------------------------------------------------------------------------
# Tests: CompilationResults
# ---------------------------------------------------------------------------

class TestCompilationResults:
    def test_total_instructions(self):
        summaries = [
            _make_placement_summary(instruction_count=2),
            _make_placement_summary(instruction_count=3),
        ]
        results = _make_results(placement_summaries=summaries)
        assert results.total_instructions == 5

    def test_total_instructions_empty(self):
        results = _make_results(placement_summaries=[])
        assert results.total_instructions == 0

    def test_has_issues_with_warnings(self):
        results = _make_results(warnings=["some warning"])
        assert results.has_issues is True

    def test_has_issues_with_errors(self):
        results = _make_results(errors=["some error"])
        assert results.has_issues is True

    def test_has_issues_false_when_clean(self):
        results = _make_results()
        assert results.has_issues is False

    def test_default_target_name(self):
        results = _make_results()
        assert results.target_name == "AGENTS.md"

    def test_is_dry_run_default(self):
        results = _make_results()
        assert results.is_dry_run is False


# ---------------------------------------------------------------------------
# Tests: CompilationFormatter (no-color path)
# ---------------------------------------------------------------------------

class TestCompilationFormatter:
    """Test CompilationFormatter using use_color=False to avoid Rich dependency."""

    def setup_method(self):
        self.fmt = CompilationFormatter(use_color=False)

    def test_format_default_returns_string(self):
        results = _make_results()
        output = self.fmt.format_default(results)
        assert isinstance(output, str)
        assert len(output) > 0

    def test_format_default_contains_generated_count(self):
        results = _make_results(placement_summaries=[_make_placement_summary()])
        output = self.fmt.format_default(results)
        assert "Generated" in output or "generate" in output.lower()

    def test_format_default_dry_run_label(self):
        results = _make_results(is_dry_run=True)
        output = self.fmt.format_default(results)
        assert "DRY RUN" in output

    def test_format_verbose_returns_string(self):
        results = _make_results()
        output = self.fmt.format_verbose(results)
        assert isinstance(output, str)
        assert len(output) > 0

    def test_format_dry_run_returns_string(self):
        results = _make_results(is_dry_run=True)
        output = self.fmt.format_dry_run(results)
        assert "DRY RUN" in output

    def test_format_default_includes_efficiency(self):
        results = _make_results()
        output = self.fmt.format_default(results)
        assert "%" in output  # efficiency percentage

    def test_format_default_includes_project_analysis(self):
        results = _make_results()
        output = self.fmt.format_default(results)
        assert "directories" in output or "files" in output or "Analyzing" in output

    def test_format_default_includes_warnings(self):
        results = _make_results(warnings=["watch out"])
        output = self.fmt.format_default(results)
        assert "watch out" in output

    def test_format_default_includes_errors(self):
        results = _make_results(errors=["something broke"])
        output = self.fmt.format_default(results)
        assert "something broke" in output

    def test_format_default_no_issues_no_issue_section(self):
        results = _make_results(warnings=[], errors=[])
        output = self.fmt.format_default(results)
        assert "Error:" not in output

    def test_format_multiple_placements(self):
        summaries = [
            _make_placement_summary(path=Path("AGENTS.md"), instruction_count=3, source_count=2),
            _make_placement_summary(path=Path("src/AGENTS.md"), instruction_count=1, source_count=1),
        ]
        results = _make_results(placement_summaries=summaries)
        output = self.fmt.format_default(results)
        assert isinstance(output, str)

    def test_format_with_constitution_detected(self):
        analysis = _make_project_analysis(
            constitution_detected=True, constitution_path="CONSTITUTION.md"
        )
        results = _make_results(project_analysis=analysis)
        output = self.fmt.format_default(results)
        assert "constitution" in output.lower()

    def test_strategy_symbol_all_variants(self):
        for strategy in PlacementStrategy:
            symbol = self.fmt._get_strategy_symbol(strategy)
            assert isinstance(symbol, str)

    def test_strategy_color_all_variants(self):
        for strategy in PlacementStrategy:
            color = self.fmt._get_strategy_color(strategy)
            assert isinstance(color, str)

    def test_get_relative_display_path_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = tmp_path
        display = self.fmt._get_relative_display_path(path)
        assert "AGENTS.md" in display

    def test_format_issues_errors_and_warnings(self):
        lines = self.fmt._format_issues(
            warnings=["be careful"], errors=["it broke"]
        )
        text = "\n".join(lines)
        assert "Error:" in text
        assert "Warning:" in text

    def test_format_issues_multiline_warning(self):
        lines = self.fmt._format_issues(
            warnings=["line1\nline2\nline3"], errors=[]
        )
        text = "\n".join(lines)
        assert "line1" in text
        assert "line2" in text

    def test_get_placement_description_with_constitution(self):
        summary = _make_placement_summary(
            instruction_count=1,
            sources=["path/to/constitution.md"],
        )
        desc = self.fmt._get_placement_description(summary)
        assert "Constitution" in desc

    def test_get_placement_description_instructions_only(self):
        summary = _make_placement_summary(instruction_count=2, sources=["foo.md"])
        desc = self.fmt._get_placement_description(summary)
        assert "2 instructions" in desc

    def test_get_placement_description_empty(self):
        summary = _make_placement_summary(instruction_count=0, sources=[])
        desc = self.fmt._get_placement_description(summary)
        assert desc == "content"

    def test_format_coverage_explanation_low(self):
        stats = _make_stats(average_context_efficiency=0.2)
        lines = self.fmt._format_coverage_explanation(stats)
        text = "\n".join(lines)
        assert "Low Efficiency" in text or "low" in text.lower()

    def test_format_coverage_explanation_moderate(self):
        stats = _make_stats(average_context_efficiency=0.5)
        lines = self.fmt._format_coverage_explanation(stats)
        text = "\n".join(lines)
        assert "Moderate" in text or "moderate" in text.lower()

    def test_format_coverage_explanation_high(self):
        stats = _make_stats(average_context_efficiency=0.9)
        lines = self.fmt._format_coverage_explanation(stats)
        text = "\n".join(lines)
        assert "High Efficiency" in text or "Excellent" in text

    def test_format_results_with_stats_optional_fields(self):
        stats = _make_stats(
            average_context_efficiency=0.8,
            pollution_improvement=0.3,
            placement_accuracy=0.95,
            generation_time_ms=42,
            baseline_efficiency=0.5,
            total_agents_files=3,
        )
        results = _make_results(optimization_stats=stats)
        output = self.fmt.format_default(results)
        assert "42ms" in output or "42" in output

    def test_format_verbose_mathematical_analysis(self):
        decisions = [
            _make_optimization_decision(
                strategy=PlacementStrategy.SELECTIVE_MULTI,
                placement_directories=[Path("."), Path("src")],
            )
        ]
        results = _make_results(optimization_decisions=decisions)
        output = self.fmt.format_verbose(results)
        assert isinstance(output, str)


# ---------------------------------------------------------------------------
# Tests: ScriptExecutionFormatter (no-color path)
# ---------------------------------------------------------------------------

class TestScriptExecutionFormatter:
    """Test ScriptExecutionFormatter using use_color=False."""

    def setup_method(self):
        self.fmt = ScriptExecutionFormatter(use_color=False)

    def test_format_script_header_no_params(self):
        lines = self.fmt.format_script_header("my-script", {})
        assert len(lines) == 1
        assert "my-script" in lines[0]

    def test_format_script_header_with_params(self):
        lines = self.fmt.format_script_header("my-script", {"key": "val"})
        text = "\n".join(lines)
        assert "key" in text
        assert "val" in text

    def test_format_compilation_progress_empty(self):
        lines = self.fmt.format_compilation_progress([])
        assert lines == []

    def test_format_compilation_progress_single_file(self):
        lines = self.fmt.format_compilation_progress(["prompt.md"])
        text = "\n".join(lines)
        assert "Compiling prompt" in text

    def test_format_compilation_progress_multiple_files(self):
        lines = self.fmt.format_compilation_progress(["a.md", "b.md", "c.md"])
        text = "\n".join(lines)
        assert "3 prompts" in text
        assert "a.md" in text
        assert "c.md" in text

    def test_format_compilation_progress_last_item_uses_plus(self):
        lines = self.fmt.format_compilation_progress(["a.md", "b.md"])
        # Last file line should use +-
        file_lines = [l for l in lines if "a.md" in l or "b.md" in l]
        assert any("+-" in l for l in file_lines)

    def test_format_runtime_execution(self):
        lines = self.fmt.format_runtime_execution("copilot", "gh copilot suggest", 500)
        text = "\n".join(lines)
        assert "copilot" in text.lower()
        assert "500" in text

    def test_format_runtime_execution_unknown_runtime(self):
        lines = self.fmt.format_runtime_execution("unknown", "cmd", 100)
        text = "\n".join(lines)
        assert "unknown" in text.lower()

    def test_format_content_preview_short_content(self):
        lines = self.fmt.format_content_preview("hello world", max_preview=200)
        text = "\n".join(lines)
        assert "hello world" in text

    def test_format_content_preview_truncates(self):
        long_content = "x" * 300
        lines = self.fmt.format_content_preview(long_content, max_preview=200)
        text = "\n".join(lines)
        assert "..." in text

    def test_format_content_preview_no_truncation_exact(self):
        content = "x" * 200
        lines = self.fmt.format_content_preview(content, max_preview=200)
        text = "\n".join(lines)
        assert "..." not in text

    def test_format_environment_setup_empty(self):
        lines = self.fmt.format_environment_setup("copilot", [])
        assert lines == []

    def test_format_environment_setup_with_vars(self):
        lines = self.fmt.format_environment_setup("copilot", ["MY_VAR", "OTHER_VAR"])
        text = "\n".join(lines)
        assert "MY_VAR" in text
        assert "OTHER_VAR" in text

    def test_format_environment_setup_last_uses_plus(self):
        lines = self.fmt.format_environment_setup("copilot", ["A", "B"])
        assert any("+-" in l for l in lines)

    def test_format_execution_success_no_time(self):
        lines = self.fmt.format_execution_success("copilot")
        text = "\n".join(lines)
        assert "[+]" in text
        assert "Copilot" in text

    def test_format_execution_success_with_time(self):
        lines = self.fmt.format_execution_success("copilot", execution_time=1.23)
        text = "\n".join(lines)
        assert "1.23s" in text

    def test_format_execution_error_no_msg(self):
        lines = self.fmt.format_execution_error("copilot", 1)
        text = "\n".join(lines)
        assert "exit code: 1" in text

    def test_format_execution_error_with_msg(self):
        lines = self.fmt.format_execution_error("codex", 2, "command not found")
        text = "\n".join(lines)
        assert "command not found" in text

    def test_format_execution_error_multiline_msg(self):
        lines = self.fmt.format_execution_error("llm", 1, "line1\nline2\n")
        text = "\n".join(lines)
        assert "line1" in text
        assert "line2" in text

    def test_format_subprocess_details(self):
        lines = self.fmt.format_subprocess_details(["gh", "copilot", "suggest"], 100)
        text = "\n".join(lines)
        assert "gh" in text
        assert "100" in text

    def test_format_subprocess_details_arg_with_space(self):
        lines = self.fmt.format_subprocess_details(["cmd", "arg with space"], 50)
        text = "\n".join(lines)
        assert '"arg with space"' in text

    def test_format_auto_discovery_message(self):
        msg = self.fmt.format_auto_discovery_message(
            "my-script", Path("prompts/my-script.md"), "copilot"
        )
        assert "prompts/my-script.md" in msg
        assert "copilot" in msg

    def test_styled_no_color(self):
        result = self.fmt._styled("hello", "red bold")
        assert result == "hello"
