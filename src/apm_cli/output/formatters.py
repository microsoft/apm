"""Professional CLI output formatters for APM compilation."""

from pathlib import Path

try:
    from rich import box
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from ._formatters_detail import _FormattersDetailMixin
from .models import CompilationResults, OptimizationDecision, PlacementStrategy


class CompilationFormatter(_FormattersDetailMixin):
    """Professional formatter for compilation output with fallback for no-rich environments."""

    def __init__(self, use_color: bool = True):
        """Initialize formatter.

        Args:
            use_color: Whether to use colors and rich formatting.
        """
        self.use_color = use_color and RICH_AVAILABLE
        self.console = Console() if self.use_color else None
        self._target_name = "AGENTS.md"  # Default, updated per format call

    def format_default(self, results: CompilationResults) -> str:
        """Format default compilation output.

        Args:
            results: Compilation results to format.

        Returns:
            Formatted output string.
        """
        self._target_name = results.target_name
        lines = []

        # Phase 1: Project Discovery
        lines.extend(self._format_project_discovery(results.project_analysis))
        lines.append("")

        # Phase 2: Optimization Progress
        lines.extend(
            self._format_optimization_progress(
                results.optimization_decisions, results.project_analysis
            )
        )
        lines.append("")

        # Phase 3: Results Summary
        lines.extend(self._format_results_summary(results))

        # Issues (warnings/errors)
        if results.has_issues:
            lines.append("")
            lines.extend(self._format_issues(results.warnings, results.errors))

        return "\n".join(lines)

    def format_verbose(self, results: CompilationResults) -> str:
        """Format verbose compilation output with mathematical details.

        Args:
            results: Compilation results to format.

        Returns:
            Formatted verbose output string.
        """
        self._target_name = results.target_name
        lines = []

        # Phase 1: Project Discovery
        lines.extend(self._format_project_discovery(results.project_analysis))
        lines.append("")

        # Phase 2: Optimization Progress
        lines.extend(
            self._format_optimization_progress(
                results.optimization_decisions, results.project_analysis
            )
        )
        lines.append("")

        # Phase 3: Mathematical Analysis Section (verbose only)
        lines.extend(self._format_mathematical_analysis(results.optimization_decisions))
        lines.append("")

        # Phase 4: Coverage vs. Efficiency Explanation (verbose only)
        lines.extend(self._format_coverage_explanation(results.optimization_stats))
        lines.append("")

        # Phase 5: Detailed Performance Metrics (verbose only)
        lines.extend(self._format_detailed_metrics(results.optimization_stats))
        lines.append("")

        # Phase 6: Final Summary (Generated X files + placement distribution)
        lines.extend(self._format_results_summary(results))

        # Issues (warnings/errors)
        if results.has_issues:
            lines.append("")
            lines.extend(self._format_issues(results.warnings, results.errors))

        return "\n".join(lines)

    def format_dry_run(self, results: CompilationResults) -> str:
        """Format dry run output.

        Args:
            results: Compilation results to format.

        Returns:
            Formatted dry run output string.
        """
        self._target_name = results.target_name
        lines = []

        # Standard analysis
        lines.extend(self._format_project_discovery(results.project_analysis))
        lines.append("")
        lines.extend(
            self._format_optimization_progress(
                results.optimization_decisions, results.project_analysis
            )
        )
        lines.append("")

        # Dry run specific output
        lines.extend(self._format_dry_run_summary(results))

        # Issues (warnings/errors) - important for dry run too!
        if results.has_issues:
            lines.append("")
            lines.extend(self._format_issues(results.warnings, results.errors))

        return "\n".join(lines)

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
        lines = []

        if self.use_color:
            lines.append(self._styled("Optimizing placements...", "cyan bold"))
        else:
            lines.append("Optimizing placements...")

        if self.use_color and RICH_AVAILABLE:
            # Create a Rich table for professional display
            table = Table(show_header=True, header_style="bold cyan", box=box.SIMPLE_HEAD)
            table.add_column("Pattern", style="white", width=25)
            table.add_column("Source", style="yellow", width=20)
            table.add_column("Coverage", style="dim", width=10)
            table.add_column("Placement", style="green", width=25)
            table.add_column("Metrics", style="dim", width=20)

            # Add constitution row first if detected
            if analysis and analysis.constitution_detected:
                table.add_row("**", "constitution.md", "ALL", "./AGENTS.md", "rel: 100%")

            for decision in decisions:
                pattern_display = decision.pattern if decision.pattern else "(global)"

                # Extract source information from the instruction
                source_display = "unknown"
                if decision.instruction and hasattr(decision.instruction, "file_path"):
                    try:
                        # Get relative path from base directory if possible
                        rel_path = decision.instruction.file_path.name  # Just filename for brevity
                        source_display = rel_path
                    except Exception:
                        source_display = str(decision.instruction.file_path)[-20:]  # Last 20 chars

                ratio_display = f"{decision.matching_directories}/{decision.total_directories}"

                if len(decision.placement_directories) == 1:
                    placement = self._get_relative_display_path(decision.placement_directories[0])
                    # Add efficiency details for single placement
                    relevance = (
                        getattr(decision, "relevance_score", 0.0)
                        if hasattr(decision, "relevance_score")
                        else 1.0
                    )
                    pollution = (
                        getattr(decision, "pollution_score", 0.0)
                        if hasattr(decision, "pollution_score")
                        else 0.0
                    )
                    metrics = f"rel: {relevance * 100:.0f}%"
                else:
                    placement_count = len(decision.placement_directories)
                    placement = f"{placement_count} locations"
                    metrics = "distributed"

                # Color code the placement by strategy
                placement_style = self._get_strategy_color(decision.strategy)
                placement_text = Text(placement, style=placement_style)

                table.add_row(
                    pattern_display, source_display, ratio_display, placement_text, metrics
                )

            # Render table to lines
            if self.console:
                with self.console.capture() as capture:
                    self.console.print(table)
                table_output = capture.get()
                if table_output.strip():
                    lines.extend(table_output.split("\n"))
        else:
            # Fallback to simplified text display for non-Rich environments
            # Add constitution first if detected
            if analysis and analysis.constitution_detected:
                lines.append(
                    "**                        constitution.md     ALL        -> ./AGENTS.md                (rel: 100%)"
                )

            for decision in decisions:
                pattern_display = decision.pattern if decision.pattern else "(global)"

                # Extract source information
                source_display = "unknown"
                if decision.instruction and hasattr(decision.instruction, "file_path"):
                    try:
                        source_display = decision.instruction.file_path.name
                    except Exception:
                        source_display = "unknown"

                ratio_display = f"{decision.matching_directories}/{decision.total_directories} dirs"

                if len(decision.placement_directories) == 1:
                    placement = self._get_relative_display_path(decision.placement_directories[0])
                    relevance = (
                        getattr(decision, "relevance_score", 0.0)
                        if hasattr(decision, "relevance_score")
                        else 1.0
                    )
                    pollution = (  # noqa: F841
                        getattr(decision, "pollution_score", 0.0)
                        if hasattr(decision, "pollution_score")
                        else 0.0
                    )
                    line = f"{pattern_display:<25} {source_display:<15} {ratio_display:<10} -> {placement:<25} (rel: {relevance * 100:.0f}%)"
                else:
                    placement_count = len(decision.placement_directories)
                    line = f"{pattern_display:<25} {source_display:<15} {ratio_display:<10} -> {placement_count} locations"

                lines.append(line)

        return lines

    def _format_results_summary(self, results: CompilationResults) -> list[str]:
        """Format final results summary."""
        lines = []

        # Main result
        file_count = len(results.placement_summaries)
        target = results.target_name
        summary_line = f"Generated {file_count} {target} file{'s' if file_count != 1 else ''}"

        if results.is_dry_run:
            summary_line = f"[DRY RUN] Would generate {file_count} {target} file{'s' if file_count != 1 else ''}"

        if self.use_color:
            color = "yellow" if results.is_dry_run else "green"
            lines.append(self._styled(summary_line, f"{color} bold"))
        else:
            lines.append(summary_line)

        # Efficiency metrics with improved formatting
        stats = results.optimization_stats
        efficiency_pct = f"{stats.efficiency_percentage:.1f}%"

        # Build metrics with baselines and improvements when available
        metrics_lines = [f"+- Context efficiency:    {efficiency_pct}"]

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
            accuracy_pct = f"{stats.placement_accuracy * 100:.1f}%"
            metrics_lines.append(f"|- Placement accuracy:    {accuracy_pct} (mathematical optimum)")

        if stats.generation_time_ms is not None:
            metrics_lines.append(f"+- Generation time:       {stats.generation_time_ms}ms")
        else:  # noqa: PLR5501
            # Change last |- to +-
            if len(metrics_lines) > 1:
                metrics_lines[-1] = metrics_lines[-1].replace("|-", "+-")

        for line in metrics_lines:
            if self.use_color:
                lines.append(self._styled(line, "dim"))
            else:
                lines.append(line)

        # Add placement distribution summary
        lines.append("")
        if self.use_color:
            lines.append(self._styled("Placement Distribution", "cyan bold"))
        else:
            lines.append("Placement Distribution")

        # Show distribution of files
        for summary in results.placement_summaries:
            rel_path = str(summary.get_relative_path(Path.cwd()))
            content_text = self._get_placement_description(summary)
            source_text = f"{summary.source_count} source{'s' if summary.source_count != 1 else ''}"

            # Use proper tree formatting
            prefix = "|-" if summary != results.placement_summaries[-1] else "+-"
            line = f"{prefix} {rel_path:<30} {content_text} from {source_text}"

            if self.use_color:
                lines.append(self._styled(line, "dim"))
            else:
                lines.append(line)

        return lines

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

    def _get_strategy_symbol(self, strategy: PlacementStrategy) -> str:
        """Get symbol for placement strategy."""
        symbols = {
            PlacementStrategy.SINGLE_POINT: "*",
            PlacementStrategy.SELECTIVE_MULTI: "*",
            PlacementStrategy.DISTRIBUTED: "*",
        }
        return symbols.get(strategy, "*")

    def _get_strategy_color(self, strategy: PlacementStrategy) -> str:
        """Get color for placement strategy."""
        colors = {
            PlacementStrategy.SINGLE_POINT: "green",
            PlacementStrategy.SELECTIVE_MULTI: "yellow",
            PlacementStrategy.DISTRIBUTED: "blue",
        }
        return colors.get(strategy, "white")

    def _get_relative_display_path(self, path: Path) -> str:
        """Get display-friendly relative path."""
        try:
            rel_path = path.relative_to(Path.cwd())
            if rel_path == Path("."):
                return f"./{self._target_name}"
            return str(rel_path / self._target_name)
        except ValueError:
            return str(path / self._target_name)

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

    def _styled(self, text: str, style: str) -> str:
        """Apply styling to text with rich fallback."""
        if self.use_color and RICH_AVAILABLE:
            styled_text = Text(text)
            styled_text.style = style
            with self.console.capture() as capture:
                self.console.print(styled_text, end="")
            return capture.get()
        else:
            return text
