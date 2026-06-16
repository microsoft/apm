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

import dataclasses
import sys
from pathlib import Path

import click

from ..core.command_logger import CommandLogger
from ..deps.lockfile import get_lockfile_path
from ..policy._help_text import POLICY_SOURCE_FORMS_HELP
from ..security.content_scanner import ContentScanner, ScanFinding
from ..security.file_scanner import (
    scan_lockfile_packages,  # noqa: F401 -- re-exported for test patching
)
from ..utils.console import (
    STATUS_SYMBOLS,
    _get_console,
    _rich_echo,
    _rich_error,
    _rich_success,
)
from ._audit_ops import _audit_ci_gate as _audit_ci_gate
from ._audit_ops import _audit_content_scan as _audit_content_scan
from ._audit_ops import _resolve_external_options as _resolve_external_options
from ._audit_ops import _run_external_scanners as _run_external_scanners

# -- Shared config --------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _AuditConfig:
    """Bundled configuration shared by both audit modes.

    Reduces parameter counts on extracted handler functions so each
    receives a single config object plus its mode-specific arguments.
    """

    project_root: Path
    logger: "CommandLogger"
    verbose: bool
    output_format: str
    output_path: str | None


# -- Helpers --------------------------------------------------------


def _audit_outcome_cause(outcome: str, source: str | None, err_text: str | None) -> str:
    """Render a per-outcome `cause` line for audit --ci policy-discovery messages.

    Used by both the ``warn`` (`[!]`) and ``block`` (`[x]`) branches so the
    wording is identical; only the prefix and suffix change. Closes #1159
    by replacing the prior silent-skip with explicit, actionable causes
    for ``no_git_remote`` / ``absent`` / ``empty`` outcomes (and matching
    the existing wording for fetch failures).
    """
    src = source or "unknown"
    if outcome == "no_git_remote":
        return "Could not determine org from git remote"
    if outcome == "absent":
        return f"No org policy found at {src}"
    if outcome == "empty":
        return f"Org policy at {src} is present but empty"
    # malformed / cache_miss_fetch_fail / garbage_response (and any
    # `error` set on the result): preserve the legacy wording so existing
    # consumers parsing the line keep working.
    return f"Policy fetch failed: {err_text or outcome}"


def _scan_single_file(file_path: Path, logger) -> tuple[dict[str, list[ScanFinding]], int]:
    """Scan a single arbitrary file.

    Returns (findings_by_file, files_scanned).
    """
    if not file_path.exists():
        logger.error(f"File not found: {file_path}")
        sys.exit(1)
    if file_path.is_dir():
        logger.error(f"Path is a directory, not a file: {file_path}")
        sys.exit(1)

    findings = ContentScanner.scan_file(file_path)
    files_scanned = 1
    if findings:
        # Resolve to absolute so --strip can locate the file reliably
        return {str(file_path.resolve()): findings}, files_scanned
    return {}, files_scanned


def _has_actionable_findings(
    findings_by_file: dict[str, list[ScanFinding]],
) -> bool:
    """Return True if any finding is critical or warning (not just info)."""
    return any(
        f.severity in ("critical", "warning") for ff in findings_by_file.values() for f in ff
    )


def _finding_source(finding: ScanFinding) -> str:
    """Derive the scanner source from a finding's category prefix."""
    if "/" in finding.category:
        prefix = finding.category.split("/", 1)[0]
        if prefix != "apm":
            return prefix
    return "apm"


def _has_external_findings(rows: list[ScanFinding]) -> bool:
    """Return True if any finding originates from an external scanner."""
    return any(_finding_source(f) != "apm" for f in rows)


def _source_counts(rows: list[ScanFinding]) -> dict[str, int]:
    """Count findings by source for the table title."""
    counts: dict[str, int] = {}
    for f in rows:
        src = _finding_source(f)
        counts[src] = counts.get(src, 0) + 1
    return counts


def _findings_title(rows: list[ScanFinding], has_external: bool) -> str:
    """Build the findings table title, with per-source counts when mixed."""
    base = f"{STATUS_SYMBOLS['search']} Audit Findings"
    if not has_external:
        return f"{STATUS_SYMBOLS['search']} Content Scan Findings"
    counts = _source_counts(rows)
    parts = [f"{src}: {n}" for src, n in sorted(counts.items())]
    return f"{base}  ({', '.join(parts)})"


def _render_findings_table(
    findings_by_file: dict[str, list[ScanFinding]],
    verbose: bool = False,
) -> None:
    """Render a Rich table of scan findings."""
    console = _get_console()

    # Flatten into rows, sorted by severity (critical first)
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    rows: list[ScanFinding] = []
    for findings in findings_by_file.values():
        rows.extend(findings)
    rows.sort(key=lambda f: (severity_order.get(f.severity, 3), f.file, f.line))

    # Filter out info-level in non-verbose mode
    if not verbose:
        rows = [r for r in rows if r.severity != "info"]

    if not rows:
        return

    has_external = _has_external_findings(rows)
    title = _findings_title(rows, has_external)

    if console:
        try:
            from rich.table import Table

            from ..security.audit_report import relative_path_for_report

            table = Table(
                title=title,
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Severity", style="bold", width=10)
            if has_external:
                table.add_column("Source", style="cyan", width=14)
            table.add_column("File", style="white")
            table.add_column("Location", style="dim", width=10)
            if has_external:
                table.add_column("Category", style="bold white")
            else:
                table.add_column("Codepoint", style="bold white", width=10)
            table.add_column("Description", style="white")

            sev_styles = {
                "critical": "bold red",
                "warning": "yellow",
                "info": "dim",
            }
            for f in rows:
                category_or_codepoint = (
                    f.category.split("/", 1)[1]
                    if has_external and "/" in f.category
                    else f.category
                    if has_external
                    else f.codepoint
                )
                row_cells = [f.severity.upper()]
                if has_external:
                    row_cells.append(_finding_source(f))
                row_cells.extend(
                    [
                        relative_path_for_report(f.file),
                        f"{f.line}:{f.column}",
                        category_or_codepoint,
                        f.description,
                    ]
                )
                table.add_row(
                    *row_cells,
                    style=sev_styles.get(f.severity, "white"),
                )
            console.print()
            console.print(table)
            return
        except (ImportError, Exception):
            pass

    # Fallback: plain text
    _rich_echo("")
    _rich_echo(title, color="cyan", bold=True)
    for f in rows:
        sev_label = f.severity.upper()
        color = (
            "red" if f.severity == "critical" else ("yellow" if f.severity == "warning" else "dim")
        )
        source_part = f" [{_finding_source(f)}]" if has_external else ""
        detail = f.category if has_external else f.codepoint
        _rich_echo(
            f"  {sev_label:<10}{source_part} {f.file} {f.line}:{f.column}  {detail}  "
            f"{f.description}",
            color=color,
        )


def _deployed_canvas_bundles(project_root: Path, package_filter: str | None) -> list[str]:
    """Return sorted canvas bundle roots deployed per apm.lock.yaml.

    A canvas bundle is an executable Copilot extension (``extension.mjs``)
    deployed under a client ``extensions/`` directory (``.github/extensions/``
    project scope, ``.copilot/extensions/`` user scope). Surfacing them lets an
    audit reader see at a glance that executable extension code is installed,
    even when the content scan finds no hidden characters. Returns bundle roots
    such as ``.copilot/extensions/widget`` (one entry per bundle).
    """
    from ..deps.lockfile import LockFile
    from ..integration.canvas_integrator import is_canvas_bundle_path

    lock = LockFile.read(get_lockfile_path(project_root))
    if lock is None:
        return []

    roots: set[str] = set()
    for dep_key, dep in lock.dependencies.items():
        if package_filter and dep_key != package_filter:
            continue
        for rel in dep.deployed_files:
            norm = rel.replace("\\", "/").strip("/")
            if not norm or not is_canvas_bundle_path(norm):
                continue
            parts = norm.split("/")
            for idx, seg in enumerate(parts):
                if seg == "extensions" and idx + 1 < len(parts):
                    roots.add("/".join(parts[: idx + 2]))
                    break
    return sorted(roots)


def _render_canvas_note(project_root: Path, package_filter: str | None, logger) -> None:
    """Emit an informational note listing deployed canvas extensions."""
    bundles = _deployed_canvas_bundles(project_root, package_filter)
    if not bundles:
        return
    logger.info(
        f"{len(bundles)} executable canvas extension(s) deployed (experimental, trust-gated):"
    )
    for root in bundles:
        logger.info(f"  {root}", symbol="info")


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
    for rel_path, findings in findings_by_file.items():  # noqa: B007
        abs_path = Path(rel_path)
        if not abs_path.is_absolute():
            # Relative path from lockfile: validate within project_root
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


def _preview_strip(
    findings_by_file: dict[str, list[ScanFinding]],
    logger,
) -> int:
    """Preview what --strip would remove without modifying files.

    Shows a summary of strippable characters per file.
    Returns the number of files that would be modified.
    """
    console = _get_console()
    affected = 0

    for rel_path, findings in findings_by_file.items():  # noqa: B007
        # Only critical+warning chars are stripped
        strippable = [f for f in findings if f.severity in ("critical", "warning")]
        if not strippable:
            continue
        affected += 1

    if affected == 0:
        logger.progress("Nothing to clean -- no strippable characters found")
        return 0

    _rich_echo("")
    logger.progress("Dry run -- the following would be removed by --strip:", symbol="search")
    _rich_echo("")

    if console:
        try:
            from rich.table import Table

            table = Table(
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("File", style="white")
            table.add_column("Critical", style="bold red", justify="right", width=10)
            table.add_column("Warning", style="yellow", justify="right", width=10)
            table.add_column("Total", style="bold white", justify="right", width=10)

            for rel_path, findings in findings_by_file.items():
                strippable = [f for f in findings if f.severity in ("critical", "warning")]
                if not strippable:
                    continue
                crit = sum(1 for f in strippable if f.severity == "critical")
                warn = sum(1 for f in strippable if f.severity == "warning")
                table.add_row(
                    rel_path,
                    str(crit) if crit else "-",
                    str(warn) if warn else "-",
                    str(len(strippable)),
                )

            console.print(table)
        except (ImportError, Exception):
            # Fallback: plain text
            for rel_path, findings in findings_by_file.items():
                strippable = [f for f in findings if f.severity in ("critical", "warning")]
                if not strippable:
                    continue
                _rich_echo(f"  {rel_path}: {len(strippable)} character(s)", color="white")
    else:
        for rel_path, findings in findings_by_file.items():
            strippable = [f for f in findings if f.severity in ("critical", "warning")]
            if not strippable:
                continue
            _rich_echo(f"  {rel_path}: {len(strippable)} character(s)", color="white")

    _rich_echo("")
    logger.progress(f"{affected} file(s) would be modified")
    logger.progress("Run 'apm audit --strip' to apply")
    return affected


def _render_ci_results(ci_result: "CIAuditResult") -> None:
    """Render CI check results as a Rich table (text format)."""

    console = _get_console()

    if console:
        try:
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

            # Show details for failed checks
            for check in ci_result.failed_checks:
                if check.details:
                    console.print()
                    _rich_echo(
                        f"  {check.name} details:",
                        color="red",
                        bold=True,
                    )
                    for detail in check.details:
                        _rich_echo(f"    - {detail}", color="dim")

            console.print()
            summary = ci_result.to_json()["summary"]
            if ci_result.passed:
                _rich_success(f"{STATUS_SYMBOLS['success']} All {summary['total']} check(s) passed")
            else:
                _rich_error(
                    f"{STATUS_SYMBOLS['error']} {summary['failed']} of "
                    f"{summary['total']} check(s) failed"
                )
            return
        except (ImportError, Exception):
            pass

    # Fallback: plain text
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
    summary = ci_result.to_json()["summary"]
    if ci_result.passed:
        _rich_success(f"{STATUS_SYMBOLS['success']} All {summary['total']} check(s) passed")
    else:
        _rich_error(
            f"{STATUS_SYMBOLS['error']} {summary['failed']} of {summary['total']} check(s) failed"
        )


# -- Mode handlers (implementations live in _audit_ops.py) ----------
# Re-exported at import time so ``apm_cli.commands.audit._audit_ci_gate``,
# ``apm_cli.commands.audit._audit_content_scan``,
# ``apm_cli.commands.audit._resolve_external_options``, and
# ``apm_cli.commands.audit._run_external_scanners`` remain patchable.


# -- Command --------------------------------------------------------


@click.command(help="Scan installed packages for hidden Unicode characters")
@click.argument("package", required=False)
@click.option(
    "--file",
    "file_path",
    type=click.Path(exists=False),
    help="Scan an arbitrary file (not just APM-managed files)",
)
@click.option(
    "--strip",
    is_flag=True,
    help="Remove hidden characters from scanned files (preserves emoji and whitespace)",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show all findings including harmless ones",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview what --strip would remove without modifying files",
)
@click.option(
    "--format",
    "-f",
    "output_format",
    type=click.Choice(["text", "json", "sarif", "markdown"], case_sensitive=False),
    default="text",
    help="Output format: text (default), json, sarif (GitHub Code Scanning), markdown (step summaries).",
)
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(),
    default=None,
    help="Write output to file (auto-detects format from extension: .sarif, .json, .md).",
)
@click.option(
    "--ci",
    is_flag=True,
    help="Run lockfile consistency checks for CI/CD gates. Exit 0 if clean, 1 if violations found.",
)
@click.option(
    "--policy",
    "policy_source",
    default=None,
    help=(
        f"Policy source. {POLICY_SOURCE_FORMS_HELP} "
        "Used with --ci for policy checks. [experimental]"
    ),
)
@click.option(
    "--no-cache",
    "no_cache",
    is_flag=True,
    help="Force fresh policy fetch (skip cache).",
)
@click.option(
    "--no-policy",
    "no_policy",
    is_flag=True,
    help=(
        "Skip org policy discovery and enforcement. Overridden when --policy is passed explicitly."
    ),
)
@click.option(
    "--no-fail-fast",
    "no_fail_fast",
    is_flag=True,
    help="Run all checks even after a failure (default: stop at first failure).",
)
@click.option(
    "--no-drift",
    "no_drift",
    is_flag=True,
    help=(
        "Skip the install-replay drift check. Reduces coverage; "
        "use only for performance-constrained CI loops."
    ),
)
@click.option(
    "--external",
    "external",
    multiple=True,
    metavar="NAME",
    help=(
        "Ingest findings from an external SARIF-native scanner "
        "(repeatable). Names: skillspector, sarif. "
        "Not supported with --ci. "
        "Requires 'apm experimental enable external-scanners'. [experimental]"
    ),
)
@click.option(
    "--external-sarif",
    "external_sarif",
    type=click.Path(exists=False),
    default=None,
    help="SARIF file to ingest for '--external sarif'. [experimental]",
)
@click.option(
    "--external-llm/--no-external-llm",
    "external_llm",
    default=None,
    help=(
        "Force LLM-powered analysis on/off for external scanners this run "
        "(overrides config). LLM mode makes outbound API calls and needs an "
        "API key. Requires --external. [experimental]"
    ),
)
@click.option(
    "--external-args",
    "external_args",
    default=None,
    metavar="TEXT",
    help=(
        "Extra argv tokens for external scanners this run (shlex-split, "
        "allowlist-validated per scanner). Overrides config args. "
        "Requires --external. [experimental]"
    ),
)
@click.pass_context
def audit(  # noqa: PLR0913 -- Click handler
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
    external,
    external_sarif,
    external_llm,
    external_args,
):
    """Scan deployed prompt files for hidden Unicode characters.

    Detects invisible characters that could embed hidden instructions in
    prompt, instruction, and rules files. Dangerous and suspicious
    characters can be removed with --strip.

    By default, also runs install-replay drift detection: catches
    hand-edits to deployed files, missing integrations, and orphaned
    files vs the lockfile.  Use --no-drift to skip (reduces coverage).

    With --ci, runs lockfile consistency checks AND drift in machine-
    readable format, suitable for CI/CD pipeline gates.

    \b
    Exit codes:
        0  Clean, info-only findings, or drift-only (advisory) in bare
           audit, or successful strip
        1  Critical findings detected, or --ci with violations
           (including drift in --ci mode)
        2  Warning-only findings (suspicious but not critical), or
           usage error (mutually exclusive flags)
        3  Configuration or infrastructure error (experimental feature
           disabled, external scanner not installed or unavailable)

    \b
    Examples:
        apm audit                      # Scan + drift (all checks)
        apm audit my-package           # Scan a specific package
        apm audit --file .cursorrules  # Scan any file (no drift)
        apm audit --strip              # Remove dangerous/suspicious chars
        apm audit --no-drift           # Skip drift only (escape hatch)
        apm audit --ci                 # CI gate (lockfile + drift)
        apm audit --ci --no-drift      # CI gate without drift (rare)
        apm audit --ci --policy org    # CI gate with org policy checks
        apm audit --ci -f json         # JSON CI report
        apm audit --ci -f sarif        # SARIF for GitHub Code Scanning
        apm audit -o report.sarif      # Write SARIF to file
        apm audit --external skillspector                    # SkillSpector
        apm audit --external sarif --external-sarif r.sarif  # Any SARIF
    """
    project_root = Path.cwd()
    logger = CommandLogger("audit", verbose=verbose)

    cfg = _AuditConfig(
        project_root=project_root,
        logger=logger,
        verbose=verbose,
        output_format=output_format,
        output_path=output_path,
    )

    # --no-drift is a different audit mode from --strip / --file (those
    # are content-scanning operations unrelated to integration drift).
    # Click-native UsageError gives exit code 2 with "Usage:" prefix.
    if no_drift and (strip or file_path):
        raise click.UsageError(
            "--no-drift cannot be combined with --strip or --file "
            "(those modes do not run drift detection)"
        )

    # -- CI mode: lockfile consistency gate -------------------------
    if ci:
        if verbose:
            logger.warning("--verbose has no effect in --ci mode (output is structured)")
        if strip or dry_run or file_path or package:
            raise click.UsageError(
                "--ci cannot be combined with --strip, --dry-run, --file, or PACKAGE"
            )
        if output_format == "markdown":
            logger.error("--ci does not support --format markdown. Use json or sarif.")
            sys.exit(1)
        if external:
            raise click.UsageError(
                "--ci does not support --external scanners yet. "
                "Run external scanners in bare 'apm audit' mode."
            )

        _audit_ci_gate(cfg, policy_source, no_cache, no_policy, no_fail_fast, no_drift)
        return  # _audit_ci_gate calls sys.exit; return guards against fall-through

    # -- External scanners are an additive, opt-in content-scan source.
    # They cannot be combined with --strip/--dry-run (APM only knows how to
    # strip the Unicode characters its own scanner detects).
    if external and (strip or dry_run):
        raise click.UsageError("--external cannot be combined with --strip or --dry-run")
    if external_sarif and not external:
        raise click.UsageError("--external-sarif requires '--external sarif'")
    # Orphan-flag guards: scanner-config flags are meaningless without a
    # scanner. UsageError yields exit 2 (usage error), matching --no-drift.
    if external_llm is not None and not external:
        raise click.UsageError("--external-llm/--no-external-llm requires '--external <name>'")
    if external_args is not None and not external:
        raise click.UsageError("--external-args requires '--external <name>'")

    # -- Content scan mode ------------------------------------------
    if policy_source:
        logger.warning(
            "--policy requires --ci mode. "
            "Use 'apm audit --ci --policy <source>' to run policy checks."
        )

    _audit_content_scan(
        cfg,
        package,
        file_path,
        strip,
        dry_run,
        no_drift,
        external,
        external_sarif,
        external_llm,
        external_args,
    )
