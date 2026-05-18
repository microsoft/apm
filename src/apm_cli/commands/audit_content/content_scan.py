"""Content scanning logic for audit command -- file scanning and reporting."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import click

from ...deps.lockfile import LockFile, get_lockfile_path
from ...security.content_scanner import ContentScanner
from ...security.file_scanner import scan_lockfile_packages
from ...utils.console import STATUS_SYMBOLS
from ..audit import _has_actionable_findings, _scan_single_file
from ..audit_sections import _apply_strip, _preview_strip, _render_findings_table, _render_summary


@dataclass(frozen=True, slots=True)
class _ContentReportRequest:
    """Inputs required to render a content-audit report."""

    files_scanned: int
    drift_findings: list
    exit_code: int
    effective_format: str


@dataclass(frozen=True, slots=True)
class _ContentScanRequest:
    """Options controlling the audit content scan path."""

    package: str | None
    file_path: str | None
    strip: bool
    dry_run: bool
    no_drift: bool = False


def _scan_files(cfg, package: str | None, file_path: str | None):
    """Scan files and return findings_by_file and files_scanned."""
    if file_path:
        # -- File mode: scan a single arbitrary file --
        findings_by_file, files_scanned = _scan_single_file(Path(file_path), cfg.logger)
    else:
        # -- Package mode: scan from lockfile --
        lockfile_path = get_lockfile_path(cfg.project_root)
        if not lockfile_path.exists():
            cfg.logger.progress(
                "No apm.lock.yaml found -- nothing to scan. Use --file to scan a specific file."
            )
            sys.exit(0)

        if package:
            cfg.logger.progress(f"Scanning package: {package}")
        else:
            cfg.logger.start("Scanning all installed packages...")

        findings_by_file, files_scanned = scan_lockfile_packages(
            cfg.project_root,
            package_filter=package,
        )

        if files_scanned == 0:
            if package:
                cfg.logger.warning(
                    f"Package '{package}' not found in apm.lock.yaml or has no deployed files"
                )
            else:
                cfg.logger.progress("No deployed files found in apm.lock.yaml")
            sys.exit(0)

    return findings_by_file, files_scanned


def _handle_strip_mode(findings_by_file, cfg, dry_run: bool):
    """Handle --strip mode and exit."""
    if not findings_by_file:
        cfg.logger.progress("Nothing to clean -- no hidden characters found")
        sys.exit(0)
    if dry_run:
        _preview_strip(findings_by_file, cfg.logger)
        sys.exit(0)
    modified = _apply_strip(findings_by_file, cfg.project_root, cfg.logger)
    if modified > 0:
        cfg.logger.success(f"Cleaned {modified} file(s)")
    else:
        cfg.logger.progress("Nothing to clean -- no strippable characters found")
    sys.exit(0)


def _run_content_drift_detection(
    cfg, no_drift: bool, strip: bool, file_path: str | None, package: str | None
):
    """Run drift detection for content scan mode and return (drift_findings, drift_failed)."""
    from ...policy.ci_checks import DRIFT_SKIP_PREFIX, _check_drift

    drift_findings: list = []
    drift_failed = False

    if (
        not no_drift
        and not strip
        and not file_path
        and not package
        and (cfg.project_root / "apm.yml").exists()
    ):
        lockfile_path = get_lockfile_path(cfg.project_root)
        if lockfile_path.exists():
            lockfile = LockFile.read(lockfile_path)
            if lockfile is not None:
                drift_check, drift_findings = _check_drift(
                    cfg.project_root,
                    lockfile,
                    cache_only=True,
                    verbose=cfg.verbose,
                )
                drift_failed = not drift_check.passed
                # Bare `apm audit` is advisory: drift_failed does not gate
                # the exit code (that lives in --ci). But silence on a
                # cache-pin / cache-miss skip or failure is a UX trap: the
                # user cannot tell whether drift was clean or whether it was
                # never attempted. Surface the reason on stderr whenever the
                # drift check produced no findings.
                if drift_failed and not drift_findings:
                    click.echo(
                        f"{STATUS_SYMBOLS['warning']} drift check could not run: "
                        f"{drift_check.message}",
                        err=True,
                    )
                elif (
                    drift_check.passed
                    and not drift_findings
                    and drift_check.message.startswith(DRIFT_SKIP_PREFIX)
                ):
                    click.echo(
                        f"{STATUS_SYMBOLS['warning']} {drift_check.message}",
                        err=True,
                    )
    elif no_drift and cfg.output_format == "text":
        # In structured output (json/sarif), --no-drift is implicit from
        # the absence of the drift check entry; no need to pollute output.
        click.echo(
            f"{STATUS_SYMBOLS['warning']} drift detection skipped (--no-drift); "
            "coverage reduced -- hand-edits and missing integrations will not be caught",
            err=True,
        )

    return drift_findings, drift_failed


def _emit_content_report(cfg, findings_by_file, request: _ContentReportRequest):
    """Emit content scan report in the appropriate format."""
    if request.effective_format == "text":
        if cfg.output_path:
            cfg.logger.error(
                "Text format does not support --output. "
                "Use --format json, sarif, or markdown to write to a file."
            )
            sys.exit(1)
        if findings_by_file:
            _render_findings_table(findings_by_file, verbose=cfg.verbose)
        _render_summary(findings_by_file, request.files_scanned, cfg.logger)
        if request.drift_findings:
            from ...install.drift import render_drift_text

            click.echo("")
            click.echo(render_drift_text(request.drift_findings, verbose=cfg.verbose))
    elif request.effective_format == "markdown":
        from ...security.audit_report import findings_to_markdown

        md_report = findings_to_markdown(findings_by_file, files_scanned=request.files_scanned)
        if cfg.output_path:
            Path(cfg.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(cfg.output_path).write_text(md_report, encoding="utf-8")
            cfg.logger.success(f"Audit report written to {cfg.output_path}")
        else:
            click.echo(md_report)
    else:
        from ...security.audit_report import (
            findings_to_json,
            findings_to_sarif,
            serialize_report,
            write_report,
        )

        if request.effective_format == "sarif":
            report = findings_to_sarif(findings_by_file, files_scanned=request.files_scanned)
        else:
            report = findings_to_json(
                findings_by_file,
                files_scanned=request.files_scanned,
                exit_code=request.exit_code,
            )

        if cfg.output_path:
            write_report(report, Path(cfg.output_path))
            cfg.logger.success(f"Audit report written to {cfg.output_path}")
        else:
            click.echo(serialize_report(report))


def _audit_content_scan(cfg, request: _ContentScanRequest) -> None:
    """Handle default ``apm audit`` -- content integrity scanning.

    Scans deployed prompt files (or a single file via ``--file``) for
    hidden Unicode characters, optionally stripping them.
    """
    from ...security.audit_report import detect_format_from_extension

    # Resolve effective format (auto-detect from extension when needed)
    effective_format = cfg.output_format
    if cfg.output_path and effective_format == "text":
        effective_format = detect_format_from_extension(Path(cfg.output_path))

    # --format json/sarif/markdown is incompatible with --strip / --dry-run
    if effective_format != "text" and (request.strip or request.dry_run):
        cfg.logger.error(
            f"--format {effective_format} cannot be combined with --strip or --dry-run"
        )
        sys.exit(1)

    # Scan files
    findings_by_file, files_scanned = _scan_files(cfg, request.package, request.file_path)

    # -- Warn if --dry-run used without --strip --
    if request.dry_run and not request.strip:
        cfg.logger.progress("--dry-run only works with --strip (e.g. apm audit --strip --dry-run)")

    # -- Strip mode --
    if request.strip:
        _handle_strip_mode(findings_by_file, cfg, request.dry_run)

    # -- Drift detection (default-on per ADR-02) --------------------
    # Drift only applies to whole-project audit (not --file or --strip
    # modes; not single-package scoped).  Mutex on no_drift+strip/file
    # is enforced earlier via UsageError.
    drift_findings, drift_failed = _run_content_drift_detection(
        cfg,
        request.no_drift,
        request.strip,
        request.file_path,
        request.package,
    )

    # -- Display findings --
    # Determine exit code first (shared by all formats)
    if not findings_by_file or not _has_actionable_findings(findings_by_file):
        exit_code = 0
    else:
        all_findings = [f for ff in findings_by_file.values() for f in ff]
        exit_code = 1 if ContentScanner.has_critical(all_findings) else 2

    # Note: bare `apm audit` is advisory for drift; drift findings are
    # rendered (text/json/sarif) but DO NOT escalate the exit code. Use
    # `apm audit --ci` (handled in _audit_ci_gate) to gate on drift.
    _ = drift_failed  # retained for symmetry; gate path lives in --ci.

    # Emit report
    _emit_content_report(
        cfg,
        findings_by_file,
        _ContentReportRequest(
            files_scanned=files_scanned,
            drift_findings=drift_findings,
            exit_code=exit_code,
            effective_format=effective_format,
        ),
    )

    # -- Exit code --
    sys.exit(exit_code)
