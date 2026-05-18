# pylint: disable=duplicate-code
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
from ..policy._help_text import POLICY_SOURCE_FORMS_HELP
from ..security.content_scanner import ContentScanner, ScanFinding

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


def _render_findings_table(
    findings_by_file: dict[str, list[ScanFinding]], verbose: bool = False
) -> None:
    return _audit_sections._render_findings_table(findings_by_file, verbose)


def _render_summary(
    findings_by_file: dict[str, list[ScanFinding]], files_scanned: int, logger
) -> None:
    return _audit_sections._render_summary(findings_by_file, files_scanned, logger)


def _apply_strip(findings_by_file: dict[str, list[ScanFinding]], project_root: Path, logger) -> int:
    return _audit_sections._apply_strip(findings_by_file, project_root, logger)


def _preview_strip(findings_by_file: dict[str, list[ScanFinding]], logger) -> int:
    return _audit_sections._preview_strip(findings_by_file, logger)


def _render_ci_results(ci_result: "CIAuditResult") -> None:
    return _audit_sections._render_ci_results(ci_result)


# -- Mode handlers --------------------------------------------------


def _audit_ci_gate(
    cfg: _AuditConfig,
    policy_source: str | None,
    no_cache: bool,
    no_policy: bool,
    no_fail_fast: bool,
    no_drift: bool = False,
) -> None:
    return _audit_sections._audit_ci_gate(
        cfg, policy_source, no_cache, no_policy, no_fail_fast, no_drift
    )


def _audit_content_scan(
    cfg: _AuditConfig,
    package: str | None,
    file_path: str | None,
    strip: bool,
    dry_run: bool,
    no_drift: bool = False,
) -> None:
    return _audit_sections._audit_content_scan(cfg, package, file_path, strip, dry_run, no_drift)


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
@click.pass_context
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
    return _audit_sections.audit(
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


from . import audit_sections as _audit_sections
