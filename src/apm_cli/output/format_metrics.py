"""Professional CLI output formatters for APM compilation."""

from __future__ import annotations

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ._metric_constants import (
    FOUNDATION_TEXT,
    METRICS_GUIDE_TEXT,
    _build_accuracy_assessment,
    _build_assessment,
    _build_efficiency_assessment,
    _build_pollution_assessment,
)
from .formatters import RICH_AVAILABLE
from .models import OptimizationDecision


def _append_heading(self, lines: list[str], title: str) -> None:
    """Append a styled or plain heading."""
    lines.append(self._styled(title, "cyan bold") if self.use_color else title)


def _append_captured_renderable(self, lines: list[str], renderable) -> None:
    """Capture a Rich renderable into output lines when possible."""
    if not self.console:
        return
    with self.console.capture() as capture:
        self.console.print(renderable)
    output = capture.get()
    if output.strip():
        lines.extend(output.split("\n"))


def _strategy_row(decision: OptimizationDecision) -> tuple[str, str, str, str, str]:
    """Build one row for the strategy analysis table."""
    pattern = decision.pattern if decision.pattern else "(global)"
    source_display = "unknown"
    if decision.instruction and hasattr(decision.instruction, "file_path"):
        try:
            source_display = decision.instruction.file_path.name
        except Exception:
            source_display = "unknown"

    score = decision.distribution_score
    if score < 0.3:
        return pattern, source_display, f"{score:.3f} (Low)", "Single Point", "[+] Perfect"
    if score > 0.7:
        return pattern, source_display, f"{score:.3f} (High)", "Distributed", "[+] Universal"

    coverage_status = "[!] Root Fallback"
    if not any(str(path) == "." or path.name == "" for path in decision.placement_directories):
        coverage_status = "[+] Verified"
    return pattern, source_display, f"{score:.3f} (Medium)", "Selective Multi", coverage_status


def _coverage_row(self, decision: OptimizationDecision) -> tuple[str, str, str, str]:
    """Build one row for the coverage analysis table."""
    pattern = decision.pattern if decision.pattern else "(global)"
    matching_files = f"{decision.matching_directories} dirs"
    if len(decision.placement_directories) != 1:
        return (
            pattern,
            matching_files,
            f"{len(decision.placement_directories)} locations",
            "Multi-point -> Full coverage",
        )

    placement_path = decision.placement_directories[0]
    placement = self._get_relative_display_path(placement_path)
    if str(placement_path).endswith("."):
        coverage_result = "Root -> All files inherit"
    elif decision.distribution_score < 0.3:
        coverage_result = "Local -> Perfect efficiency"
    else:
        coverage_result = "Selective -> Coverage verified"
    return pattern, matching_files, placement, coverage_result


def _format_mathematical_analysis(self, decisions: list[OptimizationDecision]) -> list[str]:
    """Format mathematical analysis for verbose mode with coverage-first principles."""
    lines: list[str] = []
    _append_heading(self, lines, "Mathematical Optimization Analysis")
    lines.append("")

    if self.use_color and RICH_AVAILABLE:
        strategy_table = Table(
            title="Three-Tier Coverage-First Strategy",
            show_header=True,
            header_style="bold cyan",
            box=box.SIMPLE_HEAD,
        )
        strategy_table.add_column("Pattern", style="white", width=25)
        strategy_table.add_column("Source", style="yellow", width=15)
        strategy_table.add_column("Distribution", style="yellow", width=12)
        strategy_table.add_column("Strategy", style="green", width=15)
        strategy_table.add_column("Coverage Guarantee", style="blue", width=20)
        for decision in decisions:
            strategy_table.add_row(*_strategy_row(decision))
        _append_captured_renderable(self, lines, strategy_table)
        lines.append("")

        coverage_table = Table(
            title="Hierarchical Coverage Analysis",
            show_header=True,
            header_style="bold cyan",
            box=box.SIMPLE_HEAD,
        )
        coverage_table.add_column("Pattern", style="white", width=25)
        coverage_table.add_column("Matching Files", style="yellow", width=15)
        coverage_table.add_column("Placement", style="green", width=20)
        coverage_table.add_column("Coverage Result", style="blue", width=25)
        for decision in decisions:
            coverage_table.add_row(*_coverage_row(self, decision))
        _append_captured_renderable(self, lines, coverage_table)
        lines.append("")

        try:
            _append_captured_renderable(
                self,
                lines,
                Panel(
                    FOUNDATION_TEXT,
                    title="Coverage-Constrained Optimization",
                    border_style="cyan",
                ),
            )
        except Exception:
            lines.append("Coverage-Constrained Optimization:")
            for line in FOUNDATION_TEXT.split("\n"):
                lines.append(f"  {line}")
        return lines

    lines.append("Coverage-First Strategy Analysis:")
    for decision in decisions:
        pattern = decision.pattern if decision.pattern else "(global)"
        score = f"{decision.distribution_score:.3f}"
        strategy = decision.strategy.value
        coverage = "[+] Verified" if decision.distribution_score < 0.7 else "[!] Root Fallback"
        lines.append(f"  {pattern:<30} {score:<8} {strategy:<15} {coverage}")

    lines.extend(
        [
            "",
            "Mathematical Foundation:",
            "  Objective: minimize sum(context_pollution x directory_weight)",
            "  Constraints: for_allfile_matching_pattern -> can_inherit_instruction",
            "  Algorithm: Three-tier strategy with coverage verification",
            "  Principle: Coverage guarantee takes priority over efficiency",
        ]
    )
    return lines


def _render_rich_detailed_metrics(
    self, lines: list[str], stats, efficiency: float, pollution_level: float
) -> None:
    table = Table(box=box.SIMPLE)
    table.add_column("Metric", style="white", width=20)
    table.add_column("Value", style="white", width=12)
    table.add_column("Assessment", style="blue", width=35)

    efficiency_assessment, efficiency_colour = _build_efficiency_assessment(efficiency)
    pollution_assessment, pollution_colour = _build_pollution_assessment(pollution_level)
    table.add_row(
        "Context Efficiency",
        Text(f"{efficiency:.1f}%", style=efficiency_colour),
        Text(efficiency_assessment, style=efficiency_colour),
    )
    table.add_row(
        "Pollution Level",
        Text(f"{pollution_level:.1f}%", style=pollution_colour),
        Text(pollution_assessment, style=pollution_colour),
    )

    if stats.placement_accuracy:
        accuracy = stats.placement_accuracy * 100
        accuracy_assessment, accuracy_colour = _build_accuracy_assessment(accuracy)
        table.add_row(
            "Placement Accuracy",
            Text(f"{accuracy:.1f}%", style=accuracy_colour),
            Text(accuracy_assessment, style=accuracy_colour),
        )

    _append_captured_renderable(self, lines, table)
    lines.append("")
    try:
        _append_captured_renderable(
            self,
            lines,
            Panel(
                METRICS_GUIDE_TEXT,
                title="Metrics Guide",
                border_style="dim",
                title_align="left",
            ),
        )
    except Exception:
        lines.extend(
            [
                "Metrics Guide:",
                "* Context Efficiency 80-100%: Excellent | 60-80%: Good | 40-60%: Fair | <40%: Poor",
                "* Pollution 0-10%: Excellent | 10-25%: Good | 25-50%: Fair | >50%: Poor",
            ]
        )


def _render_plain_detailed_metrics(
    lines: list[str], efficiency: float, pollution_level: float
) -> None:
    efficiency_assessment = "Very Poor"
    if efficiency >= 80:
        efficiency_assessment = "Excellent"
    elif efficiency >= 60:
        efficiency_assessment = "Good"
    elif efficiency >= 40:
        efficiency_assessment = "Fair"
    elif efficiency >= 20:
        efficiency_assessment = "Poor"

    pollution_assessment = "Poor"
    if pollution_level <= 10:
        pollution_assessment = "Excellent"
    elif pollution_level <= 25:
        pollution_assessment = "Good"
    elif pollution_level <= 50:
        pollution_assessment = "Fair"

    lines.extend(
        [
            f"Context Efficiency: {efficiency:.1f}% ({efficiency_assessment})",
            f"Pollution Level: {pollution_level:.1f}% ({pollution_assessment})",
            "Guide: 80-100% Excellent | 60-80% Good | 40-60% Fair | 20-40% Poor | <20% Very Poor",
        ]
    )


def _format_detailed_metrics(self, stats) -> list[str]:
    """Format detailed performance metrics table with interpretations."""
    lines: list[str] = []
    _append_heading(self, lines, "Performance Metrics")

    efficiency = stats.efficiency_percentage
    pollution_level = 100 - efficiency
    if self.use_color and RICH_AVAILABLE:
        _render_rich_detailed_metrics(self, lines, stats, efficiency, pollution_level)
        return lines

    _render_plain_detailed_metrics(lines, efficiency, pollution_level)
    return lines


def _format_issues(self, warnings: list[str], errors: list[str]) -> list[str]:
    """Format warnings and errors as professional blocks."""
    lines = []

    # Errors first
    for error in errors:
        if self.use_color:
            lines.append(self._styled(f"x Error: {error}", "red"))
        else:
            lines.append(f"x Error: {error}")

    # Then warnings - handle multi-line warnings as cohesive blocks
    for warning in warnings:
        if "\n" in warning:
            # Multi-line warning - format as a professional block
            warning_lines = warning.split("\n")
            # First line gets the warning symbol and styling
            if self.use_color:
                lines.append(self._styled(f"[!] Warning: {warning_lines[0]}", "yellow"))
            else:
                lines.append(f"[!] Warning: {warning_lines[0]}")

            # Subsequent lines are indented and styled consistently
            for line in warning_lines[1:]:
                if line.strip():  # Skip empty lines
                    if self.use_color:
                        lines.append(self._styled(f"           {line}", "yellow"))
                    else:
                        lines.append(f"           {line}")
        # Single-line warning - standard format
        elif self.use_color:
            lines.append(self._styled(f"[!] Warning: {warning}", "yellow"))
        else:
            lines.append(f"[!] Warning: {warning}")

    return lines


def _format_coverage_explanation(self, stats) -> list[str]:
    """Explain the coverage vs. efficiency trade-off."""
    lines = []

    if self.use_color:
        lines.append(self._styled("Coverage vs. Efficiency Analysis", "cyan bold"))
    else:
        lines.append("Coverage vs. Efficiency Analysis")

    lines.append("")

    efficiency = stats.efficiency_percentage

    if efficiency < 30:
        lines.append("[!] Low Efficiency Detected:")
        lines.append("   * Coverage guarantee requires some instructions at root level")
        lines.append("   * This creates pollution for specialized directories")
        lines.append("   * Trade-off: Guaranteed coverage vs. optimal efficiency")
        lines.append("   * Alternative: Higher efficiency with coverage violations (data loss)")
        lines.append("")
        lines.append("This may be mathematically optimal given coverage constraints")
    elif efficiency < 60:
        lines.append("[+] Moderate Efficiency:")
        lines.append("   * Good balance between coverage and efficiency")
        lines.append("   * Some coverage-driven pollution is acceptable")
        lines.append("   * Most patterns are well-localized")
    else:
        lines.append("High Efficiency:")
        lines.append("   * Excellent pattern locality achieved")
        lines.append("   * Minimal coverage conflicts")
        lines.append("   * Instructions are optimally placed")

    lines.append("")
    lines.append("Why Coverage Takes Priority:")
    lines.append("   * Every file must access applicable instructions")
    lines.append("   * Hierarchical inheritance prevents data loss")
    lines.append("   * Better low efficiency than missing instructions")

    return lines


def _get_placement_description(self, summary) -> str:
    """Get description of what's included in a placement summary.

    Args:
        summary: PlacementSummary object

    Returns:
        str: Description like "Constitution and 1 instruction" or "Constitution"
    """
    # Check if constitution is included
    has_constitution = any("constitution.md" in source for source in summary.sources)

    # Build the description based on what's included
    parts = []
    if has_constitution:
        parts.append("Constitution")

    if summary.instruction_count > 0:
        instruction_text = f"{summary.instruction_count} instruction{'s' if summary.instruction_count != 1 else ''}"
        parts.append(instruction_text)

    if parts:
        return " and ".join(parts)
    else:
        return "content"
