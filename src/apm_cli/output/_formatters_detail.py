"""Heavy detail-rendering mixin for CompilationFormatter.

Extracted from formatters.py to keep that module under 800 lines.
``CompilationFormatter`` composes this mixin in so all method names
remain importable/patchable at their original paths.

Rule B: moved methods that check ``RICH_AVAILABLE`` fetch it via a
function-level late import from the parent module so tests patching
``apm_cli.output.formatters.RICH_AVAILABLE`` are correctly intercepted.
``Path`` is not used in any of the moved methods, so no Rule B routing
is needed for that name.
"""

try:
    from rich import box
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    box = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
    Text = None  # type: ignore[assignment]


class _FormattersDetailMixin:
    """Heavy detail renderers composed into CompilationFormatter.

    Accesses ``self.use_color``, ``self.console``, and ``self._styled``
    which are defined on ``CompilationFormatter``.
    """

    def _format_mathematical_analysis(self, decisions) -> list:
        """Format mathematical analysis for verbose mode with coverage-first principles."""
        # Rule B: fetch RICH_AVAILABLE from the parent module at call time
        # so tests patching apm_cli.output.formatters.RICH_AVAILABLE work.
        from apm_cli.output import formatters as _f

        RICH_AVAILABLE = _f.RICH_AVAILABLE
        lines = []

        if self.use_color:
            lines.append(self._styled("Mathematical Optimization Analysis", "cyan bold"))
        else:
            lines.append("Mathematical Optimization Analysis")

        lines.append("")

        if self.use_color and RICH_AVAILABLE:
            # Coverage-First Strategy Table
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
                pattern = decision.pattern if decision.pattern else "(global)"

                # Extract source information
                source_display = "unknown"
                if decision.instruction and hasattr(decision.instruction, "file_path"):
                    try:
                        source_display = decision.instruction.file_path.name
                    except Exception:
                        source_display = "unknown"

                # Distribution score with threshold classification
                score = decision.distribution_score
                if score < 0.3:
                    dist_display = f"{score:.3f} (Low)"
                    strategy_name = "Single Point"
                    coverage_status = "[+] Perfect"
                elif score > 0.7:
                    dist_display = f"{score:.3f} (High)"
                    strategy_name = "Distributed"
                    coverage_status = "[+] Universal"
                else:
                    dist_display = f"{score:.3f} (Medium)"
                    strategy_name = "Selective Multi"
                    # Check if root placement was used (indicates coverage fallback)
                    if any(str(p) == "." or p.name == "" for p in decision.placement_directories):
                        coverage_status = "[!] Root Fallback"
                    else:
                        coverage_status = "[+] Verified"

                strategy_table.add_row(
                    pattern, source_display, dist_display, strategy_name, coverage_status
                )

            # Render strategy table
            if self.console:
                with self.console.capture() as capture:
                    self.console.print(strategy_table)
                table_output = capture.get()
                if table_output.strip():
                    lines.extend(table_output.split("\n"))

            lines.append("")

            # Hierarchical Coverage Analysis Table
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
                pattern = decision.pattern if decision.pattern else "(global)"
                matching_files = f"{decision.matching_directories} dirs"

                if len(decision.placement_directories) == 1:
                    placement = self._get_relative_display_path(decision.placement_directories[0])

                    # Analyze coverage outcome
                    if str(decision.placement_directories[0]).endswith("."):
                        coverage_result = "Root -> All files inherit"
                    elif decision.distribution_score < 0.3:
                        coverage_result = "Local -> Perfect efficiency"
                    else:
                        coverage_result = "Selective -> Coverage verified"
                else:
                    placement = f"{len(decision.placement_directories)} locations"
                    coverage_result = "Multi-point -> Full coverage"

                coverage_table.add_row(pattern, matching_files, placement, coverage_result)

            # Render coverage table
            if self.console:
                with self.console.capture() as capture:
                    self.console.print(coverage_table)
                table_output = capture.get()
                if table_output.strip():
                    lines.extend(table_output.split("\n"))

            lines.append("")

            # Updated Mathematical Foundation Panel
            foundation_text = """Objective: minimize sum(context_pollution x directory_weight)
Constraints: for_allfile_matching_pattern -> can_inherit_instruction
Variables: placement_matrix in {0,1}
Algorithm: Three-tier strategy with hierarchical coverage verification

Coverage Guarantee: Every file can access applicable instructions through
hierarchical inheritance. Coverage takes priority over efficiency."""

            if self.console:
                from rich.panel import Panel as _Panel

                try:
                    panel = _Panel(
                        foundation_text,
                        title="Coverage-Constrained Optimization",
                        border_style="cyan",
                    )
                    with self.console.capture() as capture:
                        self.console.print(panel)
                    panel_output = capture.get()
                    if panel_output.strip():
                        lines.extend(panel_output.split("\n"))
                except Exception:
                    # Fallback to simple text
                    lines.append("Coverage-Constrained Optimization:")
                    for line in foundation_text.split("\n"):
                        lines.append(f"  {line}")

        else:
            # Fallback for non-Rich environments
            lines.append("Coverage-First Strategy Analysis:")
            for decision in decisions:
                pattern = decision.pattern if decision.pattern else "(global)"
                score = f"{decision.distribution_score:.3f}"
                strategy = decision.strategy.value
                coverage = (
                    "[+] Verified" if decision.distribution_score < 0.7 else "[!] Root Fallback"
                )
                lines.append(f"  {pattern:<30} {score:<8} {strategy:<15} {coverage}")

            lines.append("")
            lines.append("Mathematical Foundation:")
            lines.append("  Objective: minimize sum(context_pollution x directory_weight)")
            lines.append("  Constraints: for_allfile_matching_pattern -> can_inherit_instruction")
            lines.append("  Algorithm: Three-tier strategy with coverage verification")
            lines.append("  Principle: Coverage guarantee takes priority over efficiency")

        return lines

    def _format_detailed_metrics(self, stats) -> list:
        """Format detailed performance metrics table with interpretations."""
        # Rule B: fetch RICH_AVAILABLE from the parent module at call time.
        from apm_cli.output import formatters as _f

        RICH_AVAILABLE = _f.RICH_AVAILABLE
        lines = []

        if self.use_color:
            lines.append(self._styled("Performance Metrics", "cyan bold"))
        else:
            lines.append("Performance Metrics")

        # Create metrics table
        if self.use_color and RICH_AVAILABLE:
            table = Table(box=box.SIMPLE)
            table.add_column("Metric", style="white", width=20)
            table.add_column("Value", style="white", width=12)
            table.add_column("Assessment", style="blue", width=35)

            # Context Efficiency with coverage-first interpretation
            efficiency = stats.efficiency_percentage
            if efficiency >= 80:
                assessment = "Excellent - perfect pattern locality"
                assessment_color = "bright_green"
                value_color = "bright_green"
            elif efficiency >= 60:
                assessment = "Good - well-optimized with minimal coverage conflicts"
                assessment_color = "green"
                value_color = "green"
            elif efficiency >= 40:
                assessment = "Fair - moderate coverage-driven pollution"
                assessment_color = "yellow"
                value_color = "yellow"
            elif efficiency >= 20:
                assessment = "Poor - significant coverage constraints"
                assessment_color = "orange1"
                value_color = "orange1"
            else:
                assessment = "Very Poor - may be mathematically optimal given coverage"
                assessment_color = "red"
                value_color = "red"

            table.add_row(
                "Context Efficiency",
                Text(f"{efficiency:.1f}%", style=value_color),
                Text(assessment, style=assessment_color),
            )

            # Calculate pollution level with coverage-aware interpretation
            pollution_level = 100 - efficiency
            if pollution_level <= 20:
                pollution_assessment = "Excellent - perfect pattern locality"
                pollution_color = "bright_green"
            elif pollution_level <= 40:
                pollution_assessment = "Good - minimal coverage conflicts"
                pollution_color = "green"
            elif pollution_level <= 60:
                pollution_assessment = "Fair - acceptable coverage-driven pollution"
                pollution_color = "yellow"
            elif pollution_level <= 80:
                pollution_assessment = "Poor - high coverage constraints"
                pollution_color = "orange1"
            else:
                pollution_assessment = "Very Poor - but may guarantee coverage"
                pollution_color = "red"

            table.add_row(
                "Pollution Level",
                Text(f"{pollution_level:.1f}%", style=pollution_color),
                Text(pollution_assessment, style=pollution_color),
            )

            if stats.placement_accuracy:
                accuracy = stats.placement_accuracy * 100
                if accuracy >= 95:
                    accuracy_assessment = "Excellent - mathematically optimal"
                    accuracy_color = "bright_green"
                elif accuracy >= 85:
                    accuracy_assessment = "Good - near optimal"
                    accuracy_color = "green"
                elif accuracy >= 70:
                    accuracy_assessment = "Fair - reasonably placed"
                    accuracy_color = "yellow"
                else:
                    accuracy_assessment = "Poor - suboptimal placement"
                    accuracy_color = "orange1"

                table.add_row(
                    "Placement Accuracy",
                    Text(f"{accuracy:.1f}%", style=accuracy_color),
                    Text(accuracy_assessment, style=accuracy_color),
                )

            # Render table
            if self.console:
                with self.console.capture() as capture:
                    self.console.print(table)
                table_output = capture.get()
                if table_output.strip():
                    lines.extend(table_output.split("\n"))

            lines.append("")

            # Add interpretation guide
            if self.console:
                try:
                    interpretation_text = """How These Metrics Are Calculated

Context Efficiency = Average across all directories of (Relevant Instructions / Total Instructions)
* For each directory, APM analyzes what instructions agents would inherit from AGENTS.md files
* Calculates ratio of instructions that apply to files in that directory vs total instructions loaded
* Takes weighted average across all project directories with files

Pollution Level = 100% - Context Efficiency (inverse relationship)
* High pollution = agents load many irrelevant instructions when working in specific directories
* Low pollution = agents see mostly relevant instructions for their current context

Interpretation Benchmarks

Context Efficiency:
* 80-100%: Excellent - Instructions perfectly targeted to usage context
* 60-80%: Good - Well-optimized with minimal wasted context  
* 40-60%: Fair - Some optimization opportunities exist
* 20-40%: Poor - Significant context pollution, consider restructuring
* 0-20%: Very Poor - High pollution, instructions poorly distributed

Pollution Level:
* 0-10%: Excellent - Agents see highly relevant instructions only
* 10-25%: Good - Low noise, mostly relevant context
* 25-50%: Fair - Moderate noise, some irrelevant instructions  
* 50%+: Poor - High noise, agents see many irrelevant instructions

Example: 36.7% efficiency means agents working in specific directories see only 36.7% relevant instructions and 63.3% irrelevant context pollution."""

                    panel = Panel(
                        interpretation_text,
                        title="Metrics Guide",
                        border_style="dim",
                        title_align="left",
                    )
                    with self.console.capture() as capture:
                        self.console.print(panel)
                    panel_output = capture.get()
                    if panel_output.strip():
                        lines.extend(panel_output.split("\n"))
                except Exception:
                    # Fallback to simple text
                    lines.extend(
                        [
                            "Metrics Guide:",
                            "* Context Efficiency 80-100%: Excellent | 60-80%: Good | 40-60%: Fair | <40%: Poor",
                            "* Pollution 0-10%: Excellent | 10-25%: Good | 25-50%: Fair | >50%: Poor",
                        ]
                    )
        else:
            # Fallback for non-Rich environments
            efficiency = stats.efficiency_percentage
            pollution = 100 - efficiency

            if efficiency >= 80:
                efficiency_assessment = "Excellent"
            elif efficiency >= 60:
                efficiency_assessment = "Good"
            elif efficiency >= 40:
                efficiency_assessment = "Fair"
            elif efficiency >= 20:
                efficiency_assessment = "Poor"
            else:
                efficiency_assessment = "Very Poor"

            if pollution <= 10:
                pollution_assessment = "Excellent"
            elif pollution <= 25:
                pollution_assessment = "Good"
            elif pollution <= 50:
                pollution_assessment = "Fair"
            else:
                pollution_assessment = "Poor"

            lines.extend(
                [
                    f"Context Efficiency: {efficiency:.1f}% ({efficiency_assessment})",
                    f"Pollution Level: {pollution:.1f}% ({pollution_assessment})",
                    "Guide: 80-100% Excellent | 60-80% Good | 40-60% Fair | 20-40% Poor | <20% Very Poor",
                ]
            )

        return lines

    def _format_coverage_explanation(self, stats) -> list:
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

    def _format_issues(self, warnings: list, errors: list) -> list:
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
            else:  # noqa: PLR5501
                # Single-line warning - standard format
                if self.use_color:
                    lines.append(self._styled(f"[!] Warning: {warning}", "yellow"))
                else:
                    lines.append(f"[!] Warning: {warning}")

        return lines
