"""Renderers for the adoption report: text table, JSON and YAML."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

import click

from apm_cli.core.command_logger import CommandLogger
from apm_cli.utils.yaml_io import yaml_to_str

from .model import AdoptionReport, Finding, Importability

_COLUMNS = ("TOOL", "SCOPE", "KIND", "PATH", "IMPORT", "OWNER", "RISK", "-> DEST")
_RISK_SHORT = {"executes-code": "exec", "network": "net", "writes-files": "write"}


def report_to_json(report: AdoptionReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)


def report_to_yaml(report: AdoptionReport) -> str:
    return yaml_to_str(report.to_dict(), sort_keys=True)


def _row(finding: Finding) -> tuple[str, ...]:
    return (
        finding.tool,
        finding.scope.value,
        finding.kind.value,
        finding.display_path,
        finding.importability.value,
        finding.ownership.value,
        ",".join(sorted(_RISK_SHORT.get(r.value, r.value) for r in finding.risk)) or "-",
        finding.proposed_target or "-",
    )


def _print_table(rows: Sequence[tuple[str, ...]]) -> None:
    """Print an ASCII table; Rich when available, aligned text otherwise."""
    try:
        from rich import box
        from rich.table import Table

        from apm_cli.utils.console import _get_console

        console = _get_console()
    except Exception:  # Rich missing or console unavailable
        console = None
    if console is not None and getattr(console, "is_terminal", False) and console.width >= 120:
        table = Table(
            title="Discovered agent context",
            show_header=True,
            header_style="bold cyan",
            box=box.ASCII,
        )
        for column in _COLUMNS:
            table.add_column(
                column, style="bold white" if column == "PATH" else "white", overflow="fold"
            )
        for row in rows:
            table.add_row(*row)
        console.print(table)
        return
    widths = [len(c) for c in _COLUMNS]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    click.echo("  " + fmt.format(*_COLUMNS))
    click.echo("  " + "  ".join("-" * w for w in widths))
    for row in rows:
        click.echo("  " + fmt.format(*row))


def render_text(
    report: AdoptionReport, logger: CommandLogger, next_steps: Iterable[str] = ()
) -> None:
    """Human-readable report: status lines, table, counts, next steps."""
    if not report.findings:
        logger.info("No agent-harness context files found.")
    else:
        _print_table([_row(f) for f in report.findings])
    counts = report.counts()
    click.echo()
    importable = sum(
        counts["importability"].get(k, 0)
        for k in (Importability.APM_NATIVE.value, Importability.CONVERTIBLE.value)
    )
    logger.info(
        f"{len(report.findings)} findings: {importable} importable, "
        f"{counts['importability'].get(Importability.REFERENCE_ONLY.value, 0)} reference-only, "
        f"{counts['importability'].get(Importability.IGNORED.value, 0)} ignored"
    )
    ambiguous = counts["ownership"].get("ambiguous", 0)
    if ambiguous:
        logger.warning(f"{ambiguous} ambiguous (not written); review notes before --apply")
    if report.errors:
        logger.warning(f"{len(report.errors)} paths could not be evaluated:")
        for error in report.errors[:20]:
            logger.tree_item(f"{error.display_path}: {error.reason}")
    if report.detected_tools:
        logger.info(f"Detected tools: {', '.join(report.detected_tools)}")
    if report.proposed_targets or report.proposed_mcp:
        click.echo()
        logger.info("Proposed apm.yml changes:")
        if report.proposed_targets:
            logger.tree_item(f"targets: {', '.join(report.proposed_targets)}")
        for entry in report.proposed_mcp:
            logger.tree_item(
                f"dependencies.mcp: {entry.get('name')} (from {entry.get('source', '?')})"
            )
    steps = list(next_steps)
    if steps:
        click.echo()
        logger.info("Next steps:")
        for step in steps:
            logger.tree_item(step)


def render(
    report: AdoptionReport, fmt: str, logger: CommandLogger, next_steps: Iterable[str] = ()
) -> None:
    if fmt == "json":
        click.echo(report_to_json(report))
    elif fmt == "yaml":
        click.echo(report_to_yaml(report))
    else:
        render_text(report, logger, next_steps)


__all__ = ["render", "render_text", "report_to_json", "report_to_yaml"]

_ = Any
