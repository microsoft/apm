"""Text constants and threshold-based assessment helpers for format_metrics.

Extracted from format_metrics to keep that module under 400 lines.
"""

from __future__ import annotations

FOUNDATION_TEXT = """Objective: minimize sum(context_pollution x directory_weight)
Constraints: for_allfile_matching_pattern -> can_inherit_instruction
Variables: placement_matrix in {0,1}
Algorithm: Three-tier strategy with hierarchical coverage verification

Coverage Guarantee: Every file can access applicable instructions through
hierarchical inheritance. Coverage takes priority over efficiency."""

METRICS_GUIDE_TEXT = """How These Metrics Are Calculated

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


def _build_assessment(
    value: float, thresholds: list[tuple[float, str, str]], fallback: tuple[str, str]
) -> tuple[str, str]:
    """Return assessment label and colour for a numeric threshold table."""
    for minimum, label, colour in thresholds:
        if value >= minimum:
            return label, colour
    return fallback


def _build_efficiency_assessment(efficiency: float) -> tuple[str, str]:
    return _build_assessment(
        efficiency,
        [
            (80, "Excellent - perfect pattern locality", "bright_green"),
            (60, "Good - well-optimized with minimal coverage conflicts", "green"),
            (40, "Fair - moderate coverage-driven pollution", "yellow"),
            (20, "Poor - significant coverage constraints", "orange1"),
        ],
        ("Very Poor - may be mathematically optimal given coverage", "red"),
    )


def _build_pollution_assessment(pollution_level: float) -> tuple[str, str]:
    return _build_assessment(
        -pollution_level,
        [
            (-20, "Excellent - perfect pattern locality", "bright_green"),
            (-40, "Good - minimal coverage conflicts", "green"),
            (-60, "Fair - acceptable coverage-driven pollution", "yellow"),
            (-80, "Poor - high coverage constraints", "orange1"),
        ],
        ("Very Poor - but may guarantee coverage", "red"),
    )


def _build_accuracy_assessment(accuracy: float) -> tuple[str, str]:
    return _build_assessment(
        accuracy,
        [
            (95, "Excellent - mathematically optimal", "bright_green"),
            (85, "Good - near optimal", "green"),
            (70, "Fair - reasonably placed", "yellow"),
        ],
        ("Poor - suboptimal placement", "orange1"),
    )
