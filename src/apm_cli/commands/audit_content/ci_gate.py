"""CI gate logic for audit command -- policy resolution and enforcement."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import click

from ...utils.console import STATUS_SYMBOLS
from ..audit import _audit_outcome_cause


@dataclass(frozen=True, slots=True)
class _PolicyRequest:
    """Options controlling policy resolution in CI mode."""

    policy_source: str | None
    no_cache: bool
    no_policy: bool
    fail_fast: bool


@dataclass(frozen=True, slots=True)
class _CiGateRequest:
    """Options controlling the audit CI gate."""

    policy_source: str | None
    no_cache: bool
    no_policy: bool
    no_fail_fast: bool
    no_drift: bool = False


def _resolve_policy(cfg, ci_result, request: _PolicyRequest):
    """Resolve policy source and handle fetch failures.

    Returns (fetch_result, should_run_policy) tuple.
    """
    from ...policy import discovery as policy_discovery
    from ...policy.project_config import read_project_fetch_failure_default

    fetch_result = None
    auto_discovered = False

    if request.policy_source and (not request.fail_fast or ci_result.passed):
        fetch_result = policy_discovery.discover_policy(
            cfg.project_root,
            policy_override=request.policy_source,
            no_cache=request.no_cache,
        )
    elif (
        not request.policy_source
        and not request.no_policy
        and (not request.fail_fast or ci_result.passed)
    ):
        # Auto-discovery (mirror install path)
        fetch_result = policy_discovery.discover_policy_with_chain(cfg.project_root)
        auto_discovered = True

    if fetch_result is None:
        return None, False

    # Honour project-side fetch_failure_default for outcomes that
    # mean "no enforcement applied".  Pre-#1159, auto-discovery
    # silently swallowed `absent` / `no_git_remote` / `empty` /
    # `disabled` -- a fail-open governance bypass.  Now those
    # outcomes are surfaced explicitly:
    #
    #   * malformed / cache_miss_fetch_fail / garbage_response
    #     -> existing fetch-failure handling (warn unless block);
    #     applies to BOTH explicit --policy and auto-discovery.
    #   * absent / no_git_remote / empty   (auto-discovery only)
    #     -> were silently dropped pre-#1159; now surfaced as
    #        explicit warnings, and honour `block` for parity with
    #        install.  Explicit --policy keeps the legacy fall-
    #        through so an opt-in pointer at a baseline file does
    #        not regress.
    #   * disabled   (auto-discovery only)
    #     -> emit a forensic `[i]` breadcrumb in --ci mode so
    #        audit logs explain WHY no policy ran.
    fetch_failure_outcomes = (
        "malformed",
        "cache_miss_fetch_fail",
        "garbage_response",
    )
    no_policy_outcomes = ("absent", "no_git_remote", "empty")

    if auto_discovered and fetch_result.outcome == "disabled":
        click.echo(
            "[i] Org-policy auto-discovery disabled by project apm.yml "
            "(policy.discovery_enabled=false); no enforcement applied",
            err=True,
        )
        return None, False

    if (
        fetch_result.outcome in fetch_failure_outcomes
        or fetch_result.error
        or (auto_discovered and fetch_result.outcome in no_policy_outcomes)
    ):
        project_default = read_project_fetch_failure_default(cfg.project_root)
        source = fetch_result.source
        err_text = fetch_result.error or fetch_result.fetch_error or fetch_result.outcome
        cause = _audit_outcome_cause(fetch_result.outcome, source, err_text)
        if project_default == "block":
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
            return None, False

    return fetch_result, True


def _run_policy_checks(cfg, fetch_result, fail_fast, ci_result):
    """Run policy checks and merge results into ci_result."""
    from ...policy.models import CheckResult
    from ...policy.policy_checks import run_policy_checks

    if not fetch_result.found:
        return

    policy_obj = fetch_result.policy

    # Respect enforcement level
    if policy_obj.enforcement == "off":
        return  # Policy checks disabled

    policy_result = run_policy_checks(cfg.project_root, policy_obj, fail_fast=fail_fast)
    if policy_obj.enforcement == "block":
        ci_result.checks.extend(policy_result.checks)
    else:
        # enforcement == "warn": include results but don't fail
        for check in policy_result.checks:
            ci_result.checks.append(
                CheckResult(
                    name=check.name,
                    passed=True,  # downgrade to pass
                    message=check.message + (" (enforcement: warn)" if not check.passed else ""),
                    details=check.details,
                )
            )


def _run_drift_detection(cfg, no_drift, ci_result):
    """Run drift detection and return findings list."""
    from ...deps.lockfile import LockFile, get_lockfile_path
    from ...policy.ci_checks import _check_drift

    drift_findings: list = []
    if not no_drift and (cfg.project_root / "apm.yml").exists():
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
                ci_result.checks.append(drift_check)
    elif no_drift and cfg.output_format == "text":
        # In structured output (json/sarif), --no-drift is implicit from
        # the absence of the drift check entry; no need to pollute output.
        click.echo(
            f"{STATUS_SYMBOLS['warning']} drift detection skipped (--no-drift); "
            "coverage reduced -- hand-edits and missing integrations will not be caught",
            err=True,
        )
    return drift_findings


def _emit_ci_report(cfg, ci_result, drift_findings, no_drift):
    """Emit CI report in the appropriate format and exit."""
    from ...security.audit_report import detect_format_from_extension

    # Resolve effective format
    effective_format = cfg.output_format
    if cfg.output_path and effective_format == "text":
        effective_format = detect_format_from_extension(Path(cfg.output_path))

    if effective_format in ("json", "sarif"):
        import json as _json

        from ...install.drift import render_drift_json, render_drift_sarif

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
            cfg.logger.success(f"CI audit report written to {cfg.output_path}")
        else:
            click.echo(output)
    else:
        from ..audit_sections import _render_ci_results

        _render_ci_results(ci_result)
        if drift_findings:
            from ...install.drift import render_drift_text

            click.echo("")
            click.echo(render_drift_text(drift_findings, verbose=cfg.verbose))

    sys.exit(0 if ci_result.passed else 1)


def _audit_ci_gate(cfg, request: _CiGateRequest) -> None:
    """Handle ``apm audit --ci`` -- lockfile consistency gate.

    Runs baseline lockfile checks, drift detection (unless ``--no-drift``),
    and (optionally) org-policy checks, then emits a structured report
    and exits with 0 (clean) or 1 (violations).
    """
    from ...policy.ci_checks import run_baseline_checks

    fail_fast = not request.no_fail_fast

    # Always run baseline checks
    ci_result = run_baseline_checks(cfg.project_root, fail_fast=fail_fast, ci_mode=True)

    # Resolve policy source and run policy checks
    fetch_result, should_run = _resolve_policy(
        cfg,
        ci_result,
        _PolicyRequest(
            policy_source=request.policy_source,
            no_cache=request.no_cache,
            no_policy=request.no_policy,
            fail_fast=fail_fast,
        ),
    )
    if should_run and fetch_result is not None:
        _run_policy_checks(cfg, fetch_result, fail_fast, ci_result)

    # Run drift detection
    drift_findings = _run_drift_detection(cfg, request.no_drift, ci_result)

    # Emit report and exit
    _emit_ci_report(cfg, ci_result, drift_findings, request.no_drift)
