"""Professional CLI output formatters for APM compilation."""

from pathlib import Path

try:
    from rich.console import Console
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from apm_cli.utils.console import _get_console

from .models import CompilationResults, OptimizationDecision, PlacementStrategy


class CompilationFormatter:
    """Professional formatter for compilation output with fallback for no-rich environments."""

    def __init__(self, use_color: bool = True):
        """Initialize formatter.

        Args:
            use_color: Whether to use colors and rich formatting.
        """
        self.use_color = use_color and RICH_AVAILABLE
        self.console = _get_console() if self.use_color else None
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
        lines.extend(self._format_final_summary(results))

        # Issues (warnings/errors)
        if results.has_issues:
            lines.append("")
            lines.extend(self._format_issues(results.warnings, results.errors))

        return "\n".join(lines)

    def _format_final_summary(self, results: CompilationResults) -> list[str]:
        return _format_sections._format_final_summary(self, results)

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
        return _format_sections._format_project_discovery(self, analysis)

    def _format_optimization_progress(
        self, decisions: list[OptimizationDecision], analysis=None
    ) -> list[str]:
        return _format_sections._format_optimization_progress(self, decisions, analysis)

    def _format_results_summary(self, results: CompilationResults) -> list[str]:
        return _format_sections._format_results_summary(self, results)

    def _format_dry_run_summary(self, results: CompilationResults) -> list[str]:
        return _format_sections._format_dry_run_summary(self, results)

    def _format_mathematical_analysis(self, decisions: list[OptimizationDecision]) -> list[str]:
        return _format_metrics._format_mathematical_analysis(self, decisions)

    def _format_detailed_metrics(self, stats) -> list[str]:
        return _format_metrics._format_detailed_metrics(self, stats)

    def _format_issues(self, warnings: list[str], errors: list[str]) -> list[str]:
        return _format_metrics._format_issues(self, warnings, errors)

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

    def _format_coverage_explanation(self, stats) -> list[str]:
        return _format_metrics._format_coverage_explanation(self, stats)

    def _get_placement_description(self, summary) -> str:
        return _format_metrics._get_placement_description(self, summary)

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


from . import format_metrics as _format_metrics
from . import format_sections as _format_sections
