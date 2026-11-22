"""Rendering choices for files packed into a local bundle (#3686)."""

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
    force: bool = False,
    package_name: str = "",
    diagnostics: DiagnosticCollector | None,
    logger: InstallLogger | None,
) -> tuple[str | None, bool]:
    """Convert a packed APM instruction to *rule_format* (#3686).

    Returns ``(text, unrenderable)``; *text* passes through unchanged when no
    rule format applies or it is already a native rule (a Claude rule named
    ``py.instructions.md`` keeps its ``paths:``). Decoded frontmatter passes
    the instruction owner's hidden-character preflight first, so a rule it
    rejects is skipped (``force`` allows it, as for source installs).
    """
    if text is None or rule_format is None:
        return text, False
    import yaml

    from ..integration.instruction_integrator import InstructionIntegrator

    try:
        metadata, _body = InstructionIntegrator._parse_frontmatter(text)
        if InstructionIntegrator.decoded_frontmatter_blocked(
            metadata,
            record,
            record.rsplit("/", 1)[-1],
            force=force,
            diagnostics=diagnostics,
            package_name=package_name,
        ):
            reason = "its decoded frontmatter contains critical hidden Unicode characters"
        elif InstructionIntegrator.is_native_rule(text):
            return text, False
        else:
            return InstructionIntegrator.convert_rule_content(text, rule_format), False
    except yaml.YAMLError:
        reason = "its instruction frontmatter is not valid YAML"
    message = (
        f"Skipped {record}: {reason}. Fix the frontmatter in the source package and pack again."
    )
    if diagnostics is not None:
        diagnostics.warn(message)
    elif logger is not None:
        logger.warning(message)
    return None, True


def warn_superseded_renderings(
    target_name: str,
    superseded: dict[str, str],
    *,
    diagnostics: DiagnosticCollector | None,
    logger: InstallLogger | None,
) -> None:
    """Name every bundle path skipped for another rendering of the same file."""
    if not superseded:
        return
    skipped = ", ".join(f"{rel} (kept {kept})" for rel, kept in sorted(superseded.items()))
    message = (
        f"Skipped bundle files for target {target_name} that deploy to the same "
        f"file as another rendering: {skipped}"
    )
    if diagnostics is not None:
        diagnostics.warn(message)
    elif logger is not None:
        logger.warning(message)
