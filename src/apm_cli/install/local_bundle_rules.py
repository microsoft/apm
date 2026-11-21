"""Rule rendering for APM instructions packed into a local bundle (#3686)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.command_logger import InstallLogger
    from ..utils.diagnostics import DiagnosticCollector


def render_bundle_rule(
    text: str | None,
    rule_format: str | None,
    *,
    record: str,
    diagnostics: DiagnosticCollector | None,
    logger: InstallLogger | None,
) -> tuple[str | None, bool]:
    """Convert a packed APM instruction to *rule_format* (#3686).

    Returns ``(text, unrenderable)``; *text* passes through unchanged when no
    rule format applies.
    """
    if text is None or rule_format is None:
        return text, False
    import yaml

    from ..integration.instruction_integrator import InstructionIntegrator

    try:
        return InstructionIntegrator.convert_rule_content(text, rule_format), False
    except yaml.YAMLError:
        message = (
            f"Skipped {record}: its instruction frontmatter is not valid YAML. "
            "Fix the frontmatter in the source package and pack again."
        )
        if diagnostics is not None:
            diagnostics.warn(message)
        elif logger is not None:
            logger.warning(message)
        return None, True
