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

import sys
from pathlib import Path

import click

from ...core.command_logger import CommandLogger
from ..audit import _AuditConfig
from .ci_gate import _audit_ci_gate as _audit_ci_gate

# Re-export the complex functions from their new homes
from .ci_gate import _CiGateRequest
from .content_scan import _audit_content_scan as _audit_content_scan
from .content_scan import _ContentScanRequest

__all__ = ["_audit_ci_gate", "_audit_content_scan", "audit"]


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
            logger.error("--ci cannot be combined with --strip, --dry-run, --file, or PACKAGE")
            sys.exit(1)
        if output_format == "markdown":
            logger.error("--ci does not support --format markdown. Use json or sarif.")
            sys.exit(1)

        _audit_ci_gate(
            cfg,
            _CiGateRequest(
                policy_source=policy_source,
                no_cache=no_cache,
                no_policy=no_policy,
                no_fail_fast=no_fail_fast,
                no_drift=no_drift,
            ),
        )
        return  # _audit_ci_gate calls sys.exit; return guards against fall-through

    # -- Content scan mode ------------------------------------------
    if policy_source:
        logger.warning(
            "--policy requires --ci mode. "
            "Use 'apm audit --ci --policy <source>' to run policy checks."
        )

    _audit_content_scan(
        cfg,
        _ContentScanRequest(
            package=package,
            file_path=file_path,
            strip=strip,
            dry_run=dry_run,
            no_drift=no_drift,
        ),
    )
