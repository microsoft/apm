"""APM audit command -- content integrity scanning for prompt files.

Scans installed APM packages (or arbitrary files) for hidden Unicode
characters that could embed invisible instructions.  This is the first
pillar of ``apm audit``; lock-file consistency (``--ci``) and drift
detection (``--drift``) are planned as future modes.

Exit codes:
    0 -- clean (no findings, or info-only)
    1 -- critical findings detected
    2 -- warnings only (no critical)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..security.content_scanner import ContentScanner, ScanFinding
from ..utils.console import (
    STATUS_SYMBOLS,
    _get_console,
    _rich_echo,
    _rich_error,
    _rich_success,
)

if TYPE_CHECKING:
    from ..policy.models import CIAuditResult
    from .audit import _AuditConfig


def _render_findings_table(
    findings_by_file: dict[str, list[ScanFinding]],
    verbose: bool = False,
) -> None:
    """Render a Rich table of scan findings."""
    console = _get_console()

    severity_order = {"critical": 0, "warning": 1, "info": 2}
    rows: list[ScanFinding] = []
    for findings in findings_by_file.values():
        rows.extend(findings)
    rows.sort(key=lambda f: (severity_order.get(f.severity, 3), f.file, f.line))

    if not verbose:
        rows = [r for r in rows if r.severity != "info"]

    if not rows:
        return

    if console:
        try:
            from rich.table import Table

            from ..security.audit_report import relative_path_for_report

            table = Table(
                title=f"{STATUS_SYMBOLS['search']} Content Scan Findings",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Severity", style="bold", width=10)
            table.add_column("File", style="white")
            table.add_column("Location", style="dim", width=10)
            table.add_column("Codepoint", style="bold white", width=10)
            table.add_column("Description", style="white")

            sev_styles = {
                "critical": "bold red",
                "warning": "yellow",
                "info": "dim",
            }
            for f in rows:
                table.add_row(
                    f.severity.upper(),
                    relative_path_for_report(f.file),
                    f"{f.line}:{f.column}",
                    f.codepoint,
                    f.description,
                    style=sev_styles.get(f.severity, "white"),
                )
            console.print()
            console.print(table)
            return
        except (ImportError, Exception):
            pass

    _rich_echo("")
    _rich_echo(
        f"{STATUS_SYMBOLS['search']} Content Scan Findings",
        color="cyan",
        bold=True,
    )
    for f in rows:
        sev_label = f.severity.upper()
        color = (
            "red" if f.severity == "critical" else ("yellow" if f.severity == "warning" else "dim")
        )
        _rich_echo(
            f"  {sev_label:<10} {f.file} {f.line}:{f.column}  {f.codepoint}  {f.description}",
            color=color,
        )


def _render_summary(
    findings_by_file: dict[str, list[ScanFinding]],
    files_scanned: int,
    logger,
) -> None:
    """Render a summary panel with counts."""
    all_findings: list[ScanFinding] = []
    for findings in findings_by_file.values():
        all_findings.extend(findings)

    counts = ContentScanner.summarize(all_findings)
    critical = counts.get("critical", 0)
    warning = counts.get("warning", 0)
    info = counts.get("info", 0)
    affected = len(findings_by_file)

    _rich_echo("")
    if critical > 0:
        logger.error(
            f"{critical} critical finding(s) in {affected} file(s) -- hidden characters detected"
        )
        logger.progress("  These characters may embed invisible instructions")
        logger.progress("  Review file contents, then run 'apm audit --strip' to remove")
    elif warning > 0:
        logger.warning(f"{warning} warning(s) in {affected} file(s) -- hidden characters detected")
        logger.progress("  Run 'apm audit --strip' to remove hidden characters")
    elif info > 0:
        logger.progress(
            f"{info} info-level finding(s) in "
            f"{affected} file(s) -- unusual characters (use --verbose to see)"
        )
    else:
        logger.success(f"{files_scanned} file(s) scanned -- no issues found")

    if info > 0 and (critical > 0 or warning > 0):
        logger.progress(f"  Plus {info} info-level finding(s) (use --verbose to see)")


def _apply_strip(
    findings_by_file: dict[str, list[ScanFinding]],
    project_root: Path,
    logger,
) -> int:
    """Strip dangerous and suspicious characters from affected files.

    Only modifies files that resolve within *project_root* (for lockfile
    paths) or that are given as absolute paths (for ``--file`` mode).
    Returns number of files modified.
    """
    modified = 0
    for rel_path, _findings in findings_by_file.items():
        abs_path = Path(rel_path)
        if not abs_path.is_absolute():
            abs_path = project_root / rel_path
            try:
                abs_path.resolve().relative_to(project_root.resolve())
            except ValueError:
                logger.warning(f"  Skipping {rel_path}: outside project root")
                continue

        if not abs_path.exists():
            continue

        try:
            original = abs_path.read_text(encoding="utf-8")
            cleaned = ContentScanner.strip_dangerous(original)
            if cleaned != original:
                abs_path.write_text(cleaned, encoding="utf-8")
                modified += 1
                logger.progress(f"  Cleaned: {rel_path}", symbol="check")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(f"  Could not clean {rel_path}: {exc}")

    return modified


def _iter_strippable_file_counts(findings_by_file: dict[str, list[ScanFinding]]):
    """Yield ``(path, critical_count, warning_count, total)`` rows for strip preview."""
    for rel_path, findings in findings_by_file.items():
        strippable = [f for f in findings if f.severity in ("critical", "warning")]
        if not strippable:
            continue
        crit = sum(1 for finding in strippable if finding.severity == "critical")
        warn = sum(1 for finding in strippable if finding.severity == "warning")
        yield rel_path, crit, warn, len(strippable)


def _render_strip_preview_rich(console, rows) -> None:
    """Render ``--strip --dry-run`` preview using Rich when available."""
    from rich.table import Table

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("File", style="white")
    table.add_column("Critical", style="bold red", justify="right", width=10)
    table.add_column("Warning", style="yellow", justify="right", width=10)
    table.add_column("Total", style="bold white", justify="right", width=10)
    for rel_path, crit, warn, total in rows:
        table.add_row(
            rel_path,
            str(crit) if crit else "-",
            str(warn) if warn else "-",
            str(total),
        )
    console.print(table)


def _render_strip_preview_plain(rows) -> None:
    """Render ``--strip --dry-run`` preview without Rich."""
    for rel_path, _, _, total in rows:
        _rich_echo(f"  {rel_path}: {total} character(s)", color="white")


def _preview_strip(
    findings_by_file: dict[str, list[ScanFinding]],
    logger,
) -> int:
    """Preview what --strip would remove; return count of files that would be modified."""
    console = _get_console()
    rows = list(_iter_strippable_file_counts(findings_by_file))
    affected = len(rows)
    if affected == 0:
        logger.progress("Nothing to clean -- no strippable characters found")
        return 0

    _rich_echo("")
    logger.progress("Dry run -- the following would be removed by --strip:", symbol="search")
    _rich_echo("")

    if console:
        try:
            _render_strip_preview_rich(console, rows)
        except (ImportError, Exception):
            _render_strip_preview_plain(rows)
    else:
        _render_strip_preview_plain(rows)

    _rich_echo("")
    logger.progress(f"{affected} file(s) would be modified")
    logger.progress("Run 'apm audit --strip' to apply")
    return affected


def _render_ci_summary(ci_result: CIAuditResult) -> None:
    """Render the final CI summary line."""
    summary = ci_result.to_json()["summary"]
    if ci_result.passed:
        _rich_success(f"{STATUS_SYMBOLS['success']} All {summary['total']} check(s) passed")
        return
    _rich_error(
        f"{STATUS_SYMBOLS['error']} {summary['failed']} of {summary['total']} check(s) failed"
    )


def _render_ci_results_rich(console, ci_result: CIAuditResult) -> None:
    """Render CI results with Rich output."""
    from rich.table import Table

    table = Table(
        title=f"{STATUS_SYMBOLS['search']} APM Policy Compliance",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Status", style="bold", width=8)
    table.add_column("Check", style="white")
    table.add_column("Message", style="white")
    for check in ci_result.checks:
        status = (
            f"[green]{STATUS_SYMBOLS['check']}[/green]"
            if check.passed
            else f"[red]{STATUS_SYMBOLS['cross']}[/red]"
        )
        table.add_row(status, check.name, check.message)

    console.print()
    console.print(table)
    for check in ci_result.failed_checks:
        if not check.details:
            continue
        console.print()
        _rich_echo(f"  {check.name} details:", color="red", bold=True)
        for detail in check.details:
            _rich_echo(f"    - {detail}", color="dim")
    console.print()
    _render_ci_summary(ci_result)


def _render_ci_results_plain(ci_result: CIAuditResult) -> None:
    """Render CI results without Rich."""
    _rich_echo("")
    _rich_echo(
        f"{STATUS_SYMBOLS['search']} APM Policy Compliance",
        color="cyan",
        bold=True,
    )
    for check in ci_result.checks:
        symbol = STATUS_SYMBOLS["check"] if check.passed else STATUS_SYMBOLS["cross"]
        color = "green" if check.passed else "red"
        _rich_echo(f"  {symbol} {check.name}: {check.message}", color=color)
        if not check.passed and check.details:
            for detail in check.details:
                _rich_echo(f"      - {detail}", color="dim")
    _rich_echo("")
    _render_ci_summary(ci_result)


def _render_ci_results(ci_result: CIAuditResult) -> None:
    """Render CI check results as a Rich table (text format)."""

    console = _get_console()
    if console:
        try:
            _render_ci_results_rich(console, ci_result)
            return
        except (ImportError, Exception):
            pass

    _render_ci_results_plain(ci_result)


def _audit_ci_gate(
    cfg: _AuditConfig,
    policy_source: str | None,
    no_cache: bool,
    no_policy: bool,
    no_fail_fast: bool,
    no_drift: bool = False,
) -> None:
    return _audit_content._audit_ci_gate(
        cfg,
        _audit_content._CiGateRequest(
            policy_source=policy_source,
            no_cache=no_cache,
            no_policy=no_policy,
            no_fail_fast=no_fail_fast,
            no_drift=no_drift,
        ),
    )


def _audit_content_scan(
    cfg: _AuditConfig,
    package: str | None,
    file_path: str | None,
    strip: bool,
    dry_run: bool,
    no_drift: bool = False,
) -> None:
    return _audit_content._audit_content_scan(
        cfg,
        _audit_content._ContentScanRequest(
            package=package,
            file_path=file_path,
            strip=strip,
            dry_run=dry_run,
            no_drift=no_drift,
        ),
    )


def audit(
    ctx,
    package,
    file_path,
    strip,
    verbose,
    dry_run,
    output_format,
    output_path,
    ci,
    policy_source,
    no_cache,
    no_policy,
    no_fail_fast,
    no_drift,
):
    return _audit_content.audit(
        ctx,
        package,
        file_path,
        strip,
        verbose,
        dry_run,
        output_format,
        output_path,
        ci,
        policy_source,
        no_cache,
        no_policy,
        no_fail_fast,
        no_drift,
    )


from . import audit_content as _audit_content
