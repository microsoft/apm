"""Audit command heavy-lifting extracted to keep audit.py under 800 lines.

All patched globals (ContentScanner, get_lockfile_path, scan_project_files,
_has_actionable_findings, _render_summary, _render_findings_table, _preview_strip,
_apply_strip, _resolve_external_options, _run_external_scanners, _scan_single_file)
are accessed through the original ``audit`` module at call-time so that test
monkey-patches on ``apm_cli.commands.audit.*`` take effect normally.

No module-level import of ``audit`` here to avoid circular imports; each
function does a function-level ``from apm_cli.commands import audit as _a``.
"""

import os
import sys
from pathlib import Path

import click

from ..core.deployment_ledger import (
    DEPLOYMENT_OWNER_REMEDIATION,
    DeploymentLedgerCodec,
    DeploymentOwnerViolation,
)
from ..deps.lockfile import LockFile
from ..utils.console import STATUS_SYMBOLS

# ---------------------------------------------------------------------------
# _audit_ci_gate
# ---------------------------------------------------------------------------


def _audit_ci_gate(
    cfg,
    policy_source,
    no_cache,
    no_policy,
    no_fail_fast,
    no_drift=False,
):
    """Handle ``apm audit --ci`` -- lockfile consistency gate."""
    from apm_cli.commands import audit as _a  # route patched globals through original module

    logger = cfg.logger

    from ..policy.ci_checks import _check_drift, run_baseline_checks
    from ..policy.policy_checks import run_policy_checks

    fail_fast = not no_fail_fast

    prepared_replay = None
    prepared_replay_error = None
    if (cfg.project_root / "apm.yml").exists() and not (cfg.project_root / "apm_modules").exists():
        from ..deps.lockfile import get_lockfile_path
        from ..install.audit_replay import CiAuditReplayError, prepare_ci_audit_replay

        if get_lockfile_path(cfg.project_root).exists():
            try:
                prepared_replay = prepare_ci_audit_replay(
                    cfg.project_root,
                    verbose=cfg.verbose,
                )
            except CiAuditReplayError as exc:
                prepared_replay_error = str(exc)

    ci_result = run_baseline_checks(
        cfg.project_root,
        fail_fast=fail_fast,
        ci_mode=True,
        prepared_replay=prepared_replay,
        prepared_replay_error=prepared_replay_error,
    )

    from ..policy.discovery import discover_policy_with_chain
    from ..policy.project_config import read_project_fetch_failure_default

    fetch_result = None
    auto_discovered = False
    if policy_source and (not fail_fast or ci_result.passed):
        fetch_result = discover_policy_with_chain(
            cfg.project_root,
            policy_override=policy_source,
            no_cache=no_cache,
        )
    elif not policy_source and not no_policy and (not fail_fast or ci_result.passed):
        fetch_result = discover_policy_with_chain(cfg.project_root)
        auto_discovered = True

    if fetch_result is not None:
        fetch_failure_outcomes = (
            "malformed",
            "cache_miss_fetch_fail",
            "garbage_response",
            "incomplete_chain",
        )
        no_policy_outcomes = ("absent", "no_git_remote", "empty")

        if auto_discovered and fetch_result.outcome == "disabled":
            click.echo(
                "[i] Org-policy auto-discovery disabled by project apm.yml "
                "(policy.discovery_enabled=false); no enforcement applied",
                err=True,
            )
            fetch_result = None
        elif (
            fetch_result.outcome in fetch_failure_outcomes
            or fetch_result.error
            or (auto_discovered and fetch_result.outcome in no_policy_outcomes)
        ):
            project_default = read_project_fetch_failure_default(cfg.project_root)
            source = fetch_result.source
            err_text = fetch_result.error or fetch_result.fetch_error or fetch_result.outcome
            cause = _a._audit_outcome_cause(fetch_result.outcome, source, err_text)
            if fetch_result.outcome == "incomplete_chain" or project_default == "block":
                click.echo(
                    f"[x] {cause} (policy.fetch_failure_default=block)",
                    err=True,
                )
                sys.exit(1)
            else:
                click.echo(
                    f"[!] {cause}; enforcement skipped "
                    "(set policy.fetch_failure_default=block in apm.yml to fail closed)",
                    err=True,
                )
                fetch_result = None

    if fetch_result is not None and fetch_result.found:
        policy_obj = fetch_result.policy

        if policy_obj.enforcement == "off":
            pass  # Policy checks disabled
        else:
            from ..policy.models import CheckResult
            from ..policy.policy_checks import FAIL_CLOSED_POLICY_CHECKS

            policy_result = run_policy_checks(cfg.project_root, policy_obj, fail_fast=fail_fast)
            if policy_obj.enforcement == "block":
                ci_result.checks.extend(policy_result.checks)
            else:
                for check in policy_result.checks:
                    if check.name in FAIL_CLOSED_POLICY_CHECKS and not check.passed:
                        ci_result.checks.append(check)
                        continue
                    ci_result.checks.append(
                        CheckResult(
                            name=check.name,
                            passed=True,
                            message=check.message
                            + (" (enforcement: warn)" if not check.passed else ""),
                            details=check.details,
                        )
                    )

    drift_findings: list = []
    if not no_drift and (cfg.project_root / "apm.yml").exists():
        lockfile_path = _a.get_lockfile_path(cfg.project_root)
        if lockfile_path.exists():
            lockfile = LockFile.read(lockfile_path)
            if lockfile is not None:
                if prepared_replay_error is not None:
                    from ..policy.models import CheckResult

                    drift_check = CheckResult(
                        name="drift",
                        passed=False,
                        message=f"drift replay failed: {prepared_replay_error}",
                        details=[prepared_replay_error],
                    )
                    drift_findings = []
                else:
                    drift_check, drift_findings = _check_drift(
                        cfg.project_root,
                        lockfile,
                        cache_only=prepared_replay is None,
                        verbose=cfg.verbose,
                        prepared_replay=prepared_replay,
                    )
                ci_result.checks.append(drift_check)
    elif no_drift and cfg.output_format == "text":
        click.echo(
            f"{STATUS_SYMBOLS['warning']} drift detection skipped (--no-drift); "
            "coverage reduced -- hand-edits and missing integrations will not be caught",
            err=True,
        )

    effective_format = cfg.output_format
    if cfg.output_path and effective_format == "text":
        from ..security.audit_report import detect_format_from_extension

        effective_format = detect_format_from_extension(Path(cfg.output_path))

    if effective_format in ("json", "sarif"):
        import json as _json

        from ..install.drift import render_drift_json, render_drift_sarif

        if effective_format == "sarif":
            payload = ci_result.to_sarif()
            if drift_findings:
                payload["runs"][0]["results"].extend(render_drift_sarif(drift_findings))
        else:
            payload = ci_result.to_json()
            if drift_findings or not no_drift:
                payload["drift"] = render_drift_json(drift_findings)

        output = _json.dumps(payload, indent=2)
        if cfg.output_path:
            Path(cfg.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(cfg.output_path).write_text(output, encoding="utf-8")
            logger.success(f"CI audit report written to {cfg.output_path}")
        else:
            click.echo(output)
    else:
        _a._render_ci_results(ci_result)
        if drift_findings:
            from ..install.drift import render_drift_text

            click.echo("")
            click.echo(render_drift_text(drift_findings, verbose=cfg.verbose))

    sys.exit(0 if ci_result.passed else 1)


# ---------------------------------------------------------------------------
# _audit_content_scan
# ---------------------------------------------------------------------------


def _render_owner_violations(
    violations: tuple[DeploymentOwnerViolation, ...],
    logger,
) -> None:
    """Render invalid canonical owner references with one remediation."""
    if not violations:
        return
    logger.error(f"{len(violations)} invalid deployment ownership record(s) in apm.lock.yaml")
    for violation in violations:
        invalid = ", ".join(violation.invalid_owners)
        active = (
            f"; active owner={violation.invalid_active_owner}"
            if violation.invalid_active_owner is not None
            else ""
        )
        logger.error_detail(f"  {violation.locator.key}: invalid owner(s)={invalid}{active}")
    logger.error_detail(DEPLOYMENT_OWNER_REMEDIATION)


def _resolve_fail_on_drift(project_root: Path) -> bool:
    """Return True when ``security.audit.fail_on_drift`` is enabled.

    Respects ``APM_POLICY_DISABLE`` and fails open on any discovery error so a
    transient policy-resolution failure never converts advisory drift into a
    hard failure. Discovery is invoked by the caller only when drift was
    actually detected, keeping the no-drift common path free of extra work.
    """
    if os.environ.get("APM_POLICY_DISABLE"):
        return False
    try:
        from ..policy.discovery import discover_policy_with_chain

        fetch_result = discover_policy_with_chain(project_root)
    except Exception:
        return False
    policy = getattr(fetch_result, "policy", None)
    if policy is None:
        return False
    return bool(policy.security.audit.fail_on_drift)


def _run_drift_detection(
    cfg,
    project_root: Path,
    *,
    no_drift: bool,
    strip: bool,
    file_path,
    package,
) -> tuple[list, bool]:
    """Run advisory drift detection for a bare ``apm audit`` run.

    Returns ``(drift_findings, drift_failed)``. Drift detection is skipped for
    ``--strip``, ``--file``, package-scoped, and ``--no-drift`` runs (the last
    emits an advisory coverage-reduced note in text mode). Renders the
    could-not-run / advisory-skip warnings as a side effect.
    """
    if no_drift:
        if cfg.output_format == "text":
            click.echo(
                f"{STATUS_SYMBOLS['warning']} drift detection skipped (--no-drift); "
                "coverage reduced -- hand-edits and missing integrations will not be caught",
                err=True,
            )
        return [], False

    if strip or file_path or package or not (project_root / "apm.yml").exists():
        return [], False

    from apm_cli.commands import audit as _a

    from ..policy.ci_checks import DRIFT_SKIP_PREFIX, _check_drift

    lockfile_path = _a.get_lockfile_path(project_root)
    if not lockfile_path.exists():
        return [], False
    lockfile = LockFile.read(lockfile_path)
    if lockfile is None:
        return [], False

    drift_check, drift_findings = _check_drift(
        project_root,
        lockfile,
        cache_only=True,
        verbose=cfg.verbose,
    )
    drift_failed = not drift_check.passed
    if drift_failed and not drift_findings:
        click.echo(
            f"{STATUS_SYMBOLS['warning']} drift check could not run: {drift_check.message}",
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
    return drift_findings, drift_failed


def _scan_lockfile_packages_or_exit(
    _a,
    lockfile_path,
    project_root,
    package,
    effective_format,
    external,
    logger,
):
    """Load the lockfile and scan its deployed files.

    Returns ``(findings_by_file, files_scanned, owner_violations)``.
    Calls ``sys.exit`` on fatal conditions (missing lockfile with no external
    scanners, invalid lockfile format, no deployed files).
    """
    if not lockfile_path.exists():
        if not external:
            logger.progress(
                "No apm.lock.yaml found -- nothing to scan. Use --file to scan a specific file."
            )
            sys.exit(0)
        return {}, 0, ()

    if effective_format == "text":
        if package:
            logger.progress(f"Scanning package: {package}")
        else:
            logger.start("Scanning installed packages and deployed files...")

    from ..deps.lockfile import LockfileFormatError

    try:
        lockfile = LockFile.read(lockfile_path)
        owner_violations = (
            DeploymentLedgerCodec.owner_reference_violations(lockfile)
            if lockfile is not None
            else ()
        )
        findings_by_file, files_scanned = _a.scan_project_files(
            project_root,
            package_filter=package,
            lockfile=lockfile,
            include_deployed_trees=package is None,
        )
    except LockfileFormatError as exc:
        logger.error(f"Cannot audit invalid apm.lock.yaml: {exc}")
        sys.exit(1)

    if files_scanned == 0 and not external and not owner_violations:
        if package:
            logger.warning(
                f"Package '{package}' not found in apm.lock.yaml or has no deployed files"
            )
        else:
            logger.progress("No deployed files found")
        sys.exit(0)
    return findings_by_file, files_scanned, owner_violations


def _audit_content_scan(
    cfg,
    package,
    file_path,
    strip,
    dry_run,
    no_drift=False,
    external=(),
    external_sarif=None,
    external_llm=None,
    external_args=None,
):
    """Handle default ``apm audit`` -- content integrity scanning."""
    from apm_cli.commands import audit as _a  # route patched globals through original module

    logger = cfg.logger
    project_root = cfg.project_root

    effective_format = cfg.output_format
    if cfg.output_path and effective_format == "text":
        from ..security.audit_report import detect_format_from_extension

        effective_format = detect_format_from_extension(Path(cfg.output_path))

    if effective_format != "text" and (strip or dry_run):
        raise click.UsageError(
            f"--format {effective_format} cannot be combined with --strip or --dry-run"
        )

    if file_path:
        findings_by_file, files_scanned = _a._scan_single_file(Path(file_path), logger)
        scan_paths = [Path(file_path)]
        owner_violations: tuple[DeploymentOwnerViolation, ...] = ()
    else:
        scan_paths = [project_root]
        lockfile_path = _a.get_lockfile_path(project_root)
        findings_by_file, files_scanned, owner_violations = _scan_lockfile_packages_or_exit(
            _a,
            lockfile_path,
            project_root,
            package,
            effective_format,
            external,
            logger,
        )

    if external:
        options_by_name = _a._resolve_external_options(external, external_llm, external_args)
        external_findings = _a._run_external_scanners(
            cfg, external, external_sarif, scan_paths, options_by_name
        )
        from ..security.external.runner import merge_findings

        merge_findings(findings_by_file, external_findings)

    if dry_run and not strip:
        logger.progress("--dry-run only works with --strip (e.g. apm audit --strip --dry-run)")

    if strip:
        if owner_violations:
            _render_owner_violations(owner_violations, logger)
            logger.error_detail("Content was not modified while lockfile ownership is invalid.")
            sys.exit(1)
        if not findings_by_file:
            logger.progress("Nothing to clean -- no hidden characters found")
            sys.exit(0)
        if dry_run:
            _a._preview_strip(findings_by_file, logger)
            sys.exit(0)
        modified = _a._apply_strip(findings_by_file, project_root, logger)
        if modified > 0:
            logger.success(f"Cleaned {modified} file(s)")
        else:
            logger.progress("Nothing to clean -- no strippable characters found")
        sys.exit(0)

    drift_findings, drift_failed = _run_drift_detection(
        cfg, project_root, no_drift=no_drift, strip=strip, file_path=file_path, package=package
    )

    if not findings_by_file or not _a._has_actionable_findings(findings_by_file):
        exit_code = 0
    else:
        all_findings = [f for ff in findings_by_file.values() for f in ff]
        exit_code = 1 if _a.ContentScanner.has_critical(all_findings) else 2
    if owner_violations:
        exit_code = 1

    # Bare `apm audit` is advisory for drift by default: drift findings are
    # rendered (text/json/sarif) but DO NOT escalate the exit code. When
    # `security.audit.fail_on_drift` is enabled, any drift-check FAILURE
    # escalates a clean run to exit 1 -- matching the `apm audit --ci` gate,
    # which fails on the same `drift_check.passed is False` signal. That covers
    # both detected drift AND a drift check that could not run (corrupt local
    # graph, unsupported replay); an advisory cache-miss SKIP stays passed=True
    # and does NOT gate. Policy is discovered only when a drift failure
    # occurred, so the clean common case is unchanged.
    if drift_failed and exit_code == 0 and _a._resolve_fail_on_drift(project_root):
        exit_code = 1

    if effective_format == "text":
        if cfg.output_path:
            logger.error(
                "Text format does not support --output. "
                "Use --format json, sarif, or markdown to write to a file."
            )
            sys.exit(1)
        if findings_by_file:
            _a._render_findings_table(findings_by_file, verbose=cfg.verbose)
            _a._render_summary(findings_by_file, files_scanned, logger)
        elif not owner_violations:
            _a._render_summary(findings_by_file, files_scanned, logger)
        _render_owner_violations(owner_violations, logger)
        if not file_path:
            _a._render_canvas_note(cfg.project_root, package, logger)
        if drift_findings:
            from ..install.drift import render_drift_text

            click.echo("")
            click.echo(render_drift_text(drift_findings, verbose=cfg.verbose))
    elif effective_format == "markdown":
        from ..security.audit_report import findings_to_markdown

        md_report = findings_to_markdown(
            findings_by_file,
            files_scanned=files_scanned,
            owner_violations=owner_violations,
        )
        if cfg.output_path:
            Path(cfg.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(cfg.output_path).write_text(md_report, encoding="utf-8")
            logger.success(f"Audit report written to {cfg.output_path}")
        else:
            click.echo(md_report)
    else:
        from ..security.audit_report import (
            findings_to_json,
            findings_to_sarif,
            serialize_report,
            write_report,
        )

        if effective_format == "sarif":
            report = findings_to_sarif(
                findings_by_file,
                files_scanned=files_scanned,
                owner_violations=owner_violations,
            )
        else:
            report = findings_to_json(
                findings_by_file,
                files_scanned=files_scanned,
                exit_code=exit_code,
                owner_violations=owner_violations,
            )

        if cfg.output_path:
            write_report(report, Path(cfg.output_path))
            logger.success(f"Audit report written to {cfg.output_path}")
        else:
            click.echo(serialize_report(report))

    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# _resolve_external_options / _run_external_scanners
# ---------------------------------------------------------------------------
# Extracted from audit.py to keep it under 800 lines.  Both are re-exported
# from audit.py so ``apm_cli.commands.audit._resolve_external_options`` and
# ``apm_cli.commands.audit._run_external_scanners`` remain patchable by tests.
# _audit_content_scan calls them via ``_a.<name>`` (routing through the
# original audit module), so test monkey-patches still take effect.


def _resolve_external_options(
    external: tuple[str, ...],
    external_llm: bool | None,
    external_args: str | None,
) -> "dict[str, object]":
    """Resolve per-scanner ScannerOptions from CLI + config layers.

    Policy ``allow_args`` governance is applied at the install-time audit
    phase (where org policy is already loaded), not in the interactive
    ``apm audit`` path; the per-adapter allowlist still validates every token.
    """
    import shlex

    from ..config import get_scanner_options
    from ..security.external.options import resolve_scanner_options

    if external_args is not None:
        try:
            cli_args: tuple[str, ...] | None = tuple(
                shlex.split(external_args, posix=(os.name != "nt"))
            )
        except ValueError as exc:
            raise click.UsageError(f"--external-args could not be parsed: {exc}") from exc
    else:
        cli_args = None
    options_by_name: dict[str, object] = {}
    for name in external:
        config_llm, config_args = get_scanner_options(name)
        options_by_name[name] = resolve_scanner_options(
            cli_llm=external_llm,
            cli_args=cli_args,
            config_llm=config_llm,
            config_args=config_args,
            policy_allow_args=None,
        )
    return options_by_name


def _run_external_scanners(
    cfg,
    external: tuple[str, ...],
    external_sarif: str | None,
    scan_paths: list[Path],
    options_by_name: "dict[str, object] | None" = None,
) -> "dict[str, list]":
    """Run opted-in external SARIF-native scanners and return merged findings.

    Fail-closed: the ``external_scanners`` experimental flag must be enabled
    (exit 2 otherwise) and each adapter must be available (exit 2 otherwise).
    APM's own content scan is never weakened -- external findings are purely
    additive.  The resolve/validate/run/merge loop is shared with the
    install-time audit phase via
    :func:`apm_cli.security.external.runner.run_external_scanners`.
    """
    from ..security.external.base import ExternalScanError
    from ..security.external.gate import (
        ExternalScannersFeatureDisabledError,
        require_external_scanners_enabled,
    )
    from ..security.external.runner import run_external_scanners

    logger = cfg.logger

    try:
        require_external_scanners_enabled("Ingesting external scanners with --external")
    except ExternalScannersFeatureDisabledError as exc:
        logger.error(str(exc))
        sys.exit(3)

    try:
        return run_external_scanners(
            external,
            external_sarif,
            scan_paths,
            options_by_name=options_by_name,
            logger=logger,
        )
    except ExternalScanError as exc:
        logger.error(str(exc))
        sys.exit(3)
