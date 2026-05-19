"""Professional CLI output formatters for APM compilation."""

from __future__ import annotations

from pathlib import Path

from rich import box
from rich.table import Table
from rich.text import Text

from .formatters import RICH_AVAILABLE
from .models import CompilationResults, OptimizationDecision


def _build_summary_line(results: CompilationResults) -> str:
    """Build the headline summary line for compilation results."""
    file_count = len(results.placement_summaries)
    target = results.target_name
    if results.is_dry_run:
        return (
            f"[DRY RUN] Would generate {file_count} {target} file{'s' if file_count != 1 else ''}"
        )
    return f"Generated {file_count} {target} file{'s' if file_count != 1 else ''}"


def _build_metrics_lines(stats) -> list[str]:
    """Build the optimisation metrics block for the summary."""
    metrics_lines = [f"+- Context efficiency:    {stats.efficiency_percentage:.1f}%"]
    if stats.efficiency_improvement is not None:
        improvement = (
            f"(baseline: {stats.baseline_efficiency * 100:.1f}%, improvement: +{stats.efficiency_improvement:.0f}%)"
            if stats.efficiency_improvement > 0
            else f"(baseline: {stats.baseline_efficiency * 100:.1f}%, change: {stats.efficiency_improvement:.0f}%)"
        )
        metrics_lines[0] += f" {improvement}"
    if stats.pollution_improvement is not None:
        pollution_pct = f"{(1.0 - stats.pollution_improvement) * 100:.1f}%"
        improvement_pct = (
            f"-{stats.pollution_improvement * 100:.0f}%"
            if stats.pollution_improvement > 0
            else f"+{abs(stats.pollution_improvement) * 100:.0f}%"
        )
        metrics_lines.append(
            f"|- Average pollution:     {pollution_pct} (improvement: {improvement_pct})"
        )
    if stats.placement_accuracy is not None:
        metrics_lines.append(
            f"|- Placement accuracy:    {stats.placement_accuracy * 100:.1f}% (mathematical optimum)"
        )
    if stats.generation_time_ms is not None:
        metrics_lines.append(f"+- Generation time:       {stats.generation_time_ms}ms")
    elif len(metrics_lines) > 1:
        metrics_lines[-1] = metrics_lines[-1].replace("|-", "+-")
    return metrics_lines


def _build_placement_distribution_lines(self, results: CompilationResults) -> list[str]:
    """Build the placement distribution section."""
    lines = [
        "",
        self._styled("Placement Distribution", "cyan bold")
        if self.use_color
        else "Placement Distribution",
    ]
    if not results.placement_summaries:
        return lines
    last_summary = results.placement_summaries[-1]
    for summary in results.placement_summaries:
        rel_path = str(summary.get_relative_path(Path.cwd()))
        content_text = self._get_placement_description(summary)
        source_text = f"{summary.source_count} source{'s' if summary.source_count != 1 else ''}"
        prefix = "|-" if summary != last_summary else "+-"
        line = f"{prefix} {rel_path:<30} {content_text} from {source_text}"
        lines.append(self._styled(line, "dim") if self.use_color else line)
    return lines


def _render_decision_source(decision) -> str:
    """Resolve a compact source display label for an optimisation decision."""
    if not (decision.instruction and hasattr(decision.instruction, "file_path")):
        return "unknown"
    try:
        return decision.instruction.file_path.name
    except Exception:
        return str(decision.instruction.file_path)[-20:]


def _render_single_decision_metric(self, decision) -> tuple[str, str]:
    """Return placement and metric strings for a single decision."""
    if len(decision.placement_directories) == 1:
        relevance = (
            getattr(decision, "relevance_score", 0.0)
            if hasattr(decision, "relevance_score")
            else 1.0
        )
        placement = self._get_relative_display_path(decision.placement_directories[0])
        return placement, f"rel: {relevance * 100:.0f}%"
    return f"{len(decision.placement_directories)} locations", "distributed"


def _render_rich_optimization_rows(self, table, decisions, analysis=None) -> None:
    """Populate the Rich optimisation table rows."""
    if analysis and analysis.constitution_detected:
        table.add_row("**", "constitution.md", "ALL", "./AGENTS.md", "rel: 100%")
    for decision in decisions:
        placement, metrics = _render_single_decision_metric(self, decision)
        table.add_row(
            decision.pattern if decision.pattern else "(global)",
            _render_decision_source(decision),
            f"{decision.matching_directories}/{decision.total_directories}",
            Text(placement, style=self._get_strategy_color(decision.strategy)),
            metrics,
        )


def _render_plain_optimization_lines(self, decisions, analysis=None) -> list[str]:
    """Render the plain-text optimisation summary lines."""
    lines: list[str] = []
    if analysis and analysis.constitution_detected:
        lines.append(
            "**                        constitution.md     ALL        -> ./AGENTS.md                (rel: 100%)"
        )
    for decision in decisions:
        placement, metrics = _render_single_decision_metric(self, decision)
        ratio_display = f"{decision.matching_directories}/{decision.total_directories} dirs"
        suffix = f"({metrics})" if metrics != "distributed" else placement
        if metrics == "distributed":
            line = f"{(decision.pattern or '(global)'):<25} {_render_decision_source(decision):<15} {ratio_display:<10} -> {suffix}"
        else:
            line = f"{(decision.pattern or '(global)'):<25} {_render_decision_source(decision):<15} {ratio_display:<10} -> {placement:<25} {suffix}"
        lines.append(line)
    return lines


def _format_final_summary(self, results: CompilationResults) -> list[str]:
    """Format final summary for verbose mode: Generated files + placement distribution."""
    lines = []
    summary_line = _build_summary_line(results)
    if self.use_color:
        color = "yellow" if results.is_dry_run else "green"
        lines.append(self._styled(summary_line, f"{color} bold"))
    else:
        lines.append(summary_line)
    for line in _build_metrics_lines(results.optimization_stats):
        lines.append(self._styled(line, "dim") if self.use_color else line)
    lines.extend(_build_placement_distribution_lines(self, results))
    return lines


def _format_project_discovery(self, analysis) -> list[str]:
    """Format project discovery phase output."""
    lines = []

    if self.use_color:
        lines.append(self._styled("Analyzing project structure...", "cyan bold"))
    else:
        lines.append("Analyzing project structure...")

    # Constitution detection (first priority)
    if analysis.constitution_detected:
        constitution_line = f"|- Constitution detected: {analysis.constitution_path}"
        if self.use_color:
            lines.append(self._styled(constitution_line, "dim"))
        else:
            lines.append(constitution_line)

    # Structure tree with more detailed information
    file_types_summary = (
        analysis.get_file_types_summary()
        if hasattr(analysis, "get_file_types_summary")
        else "various"
    )
    tree_lines = [
        f"|- {analysis.directories_scanned} directories scanned (max depth: {analysis.max_depth})",
        f"|- {analysis.files_analyzed} files analyzed across {len(analysis.file_types_detected)} file types ({file_types_summary})",
        f"+- {analysis.instruction_patterns_detected} instruction patterns detected",
    ]

    for line in tree_lines:
        if self.use_color:
            lines.append(self._styled(line, "dim"))
        else:
            lines.append(line)

    return lines


def _format_optimization_progress(
    self, decisions: list[OptimizationDecision], analysis=None
) -> list[str]:
    """Format optimization progress display using Rich table for better readability."""
    lines = [
        self._styled("Optimizing placements...", "cyan bold")
        if self.use_color
        else "Optimizing placements..."
    ]
    if self.use_color and RICH_AVAILABLE:
        table = Table(show_header=True, header_style="bold cyan", box=box.SIMPLE_HEAD)
        table.add_column("Pattern", style="white", width=25)
        table.add_column("Source", style="yellow", width=20)
        table.add_column("Coverage", style="dim", width=10)
        table.add_column("Placement", style="green", width=25)
        table.add_column("Metrics", style="dim", width=20)
        _render_rich_optimization_rows(self, table, decisions, analysis)
        if self.console:
            with self.console.capture() as capture:
                self.console.print(table)
            table_output = capture.get()
            if table_output.strip():
                lines.extend(table_output.split("\n"))
        return lines
    lines.extend(_render_plain_optimization_lines(self, decisions, analysis))
    return lines


def _format_results_summary(self, results: CompilationResults) -> list[str]:
    """Format final results summary."""
    return _format_final_summary(self, results)


def _format_dry_run_summary(self, results: CompilationResults) -> list[str]:
    """Format dry run specific summary."""
    lines = []

    if self.use_color:
        lines.append(self._styled("[DRY RUN] File generation preview:", "yellow bold"))
    else:
        lines.append("[DRY RUN] File generation preview:")

    # List files that would be generated
    for summary in results.placement_summaries:
        rel_path = str(summary.get_relative_path(Path.cwd()))
        instruction_text = f"{summary.instruction_count} instruction{'s' if summary.instruction_count != 1 else ''}"
        source_text = f"{summary.source_count} source{'s' if summary.source_count != 1 else ''}"

        line = f"|- {rel_path:<30} {instruction_text}, {source_text}"

        if self.use_color:
            lines.append(self._styled(line, "dim"))
        else:
            lines.append(line)

    # Change last |- to +-
    if lines and len(lines) > 1:
        lines[-1] = lines[-1].replace("|-", "+-")

    lines.append("")

    # Call to action
    if self.use_color:
        lines.append(
            self._styled(
                "[DRY RUN] No files written. Run 'apm compile' to apply changes.", "yellow"
            )
        )
    else:
        lines.append("[DRY RUN] No files written. Run 'apm compile' to apply changes.")

    return lines
