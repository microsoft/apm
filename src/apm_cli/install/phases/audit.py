"""Install-time content audit phase (optional, opt-in).

Runs APM's content scan -- and any policy-required external SARIF scanners --
over the files just deployed by the install, so hidden-Unicode attacks and
external-scanner findings surface (``warn``) or block (``block``) the install
*before* the user starts trusting the freshly integrated context.

Wholly gated by the ``external_scanners`` experimental flag and the
config/policy precedence resolved in
:mod:`apm_cli.core.install_audit`.  When the effective mode is ``off`` (the
default), this phase is a hard no-op -- zero added cost.

Runs AFTER the lockfile is written (so deployed files are enumerable via
``scan_lockfile_packages``) and BEFORE ``finalize``.  A ``block`` decision
raises :class:`~apm_cli.install.errors.PolicyViolationError`, reusing the
pipeline's existing clean-halt path; ``--force`` downgrades a block to a warn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import for type hints only
    from apm_cli.install.context import InstallContext


def run(ctx: InstallContext) -> None:
    """Execute the optional install-time audit phase."""
    from apm_cli.core.install_audit import decide_for_install

    decision = decide_for_install(ctx)
    if decision.mode == "off":
        return

    logger = ctx.logger

    # Warn when policy silently overrides --no-audit.
    cli_override = getattr(ctx, "audit_override", None)
    if cli_override == "off" and decision.mode != "off" and logger is not None:
        logger.warning(
            f"Policy overrides --no-audit to '{decision.mode}' "
            f"(set by {decision.source}). "
            f"Use '--no-policy' to skip the policy floor."
        )
    project_root = ctx.project_root

    # ------------------------------------------------------------------
    # 1. Native content scan over deployed files (always runs in warn/block).
    # ------------------------------------------------------------------
    from apm_cli.security.content_scanner import ScanFinding
    from apm_cli.security.file_scanner import scan_lockfile_packages

    findings_by_file: dict[str, list[ScanFinding]] = {}
    scanned, _ = scan_lockfile_packages(project_root)
    for file_key, file_findings in scanned.items():
        findings_by_file.setdefault(file_key, []).extend(file_findings)

    # ------------------------------------------------------------------
    # 2. Policy-required external SARIF scanners (additive). Fail-closed:
    #    an unavailable scanner aborts the install with a clear reason.
    # ------------------------------------------------------------------
    if decision.external:
        from apm_cli.install.errors import PolicyViolationError
        from apm_cli.security.external.base import ExternalScanError
        from apm_cli.security.external.runner import run_external_scanners

        try:
            external_findings = run_external_scanners(
                decision.external,
                None,  # SARIF file is an audit-only CLI affordance
                [project_root],
                options_by_name=decision.options_by_name,
                logger=logger,
            )
        except ExternalScanError as exc:
            raise PolicyViolationError(
                f"Install-time audit could not run a required external scanner: {exc} "
                f"(required by {decision.source})."
            ) from exc
        for file_key, file_findings in external_findings.items():
            findings_by_file.setdefault(file_key, []).extend(file_findings)

    # ------------------------------------------------------------------
    # 3. Classify and route (warn -> diagnostics; block -> halt).
    # ------------------------------------------------------------------
    from apm_cli.security.content_scanner import ContentScanner

    flat = [f for file_findings in findings_by_file.values() for f in file_findings]
    has_critical, counts = ContentScanner.classify(flat)

    if not flat:
        if logger is not None:
            logger.verbose_detail(f"Install-time audit: no findings ({decision.source})")
        return

    summary = (
        f"{counts['critical']} critical, {counts['warning']} warning, "
        f"{counts['info']} info finding(s) across {len(findings_by_file)} file(s)"
    )

    # ``--force`` downgrades block to warn, mirroring SecurityGate's
    # ``force_overrides`` semantics for the content gate.
    blocking = decision.mode == "block" and has_critical and not ctx.force

    if blocking:
        from apm_cli.install.errors import PolicyViolationError

        raise PolicyViolationError(
            f"Install-time audit blocked: {summary}. "
            f"Run 'apm audit --strip' to clean hidden characters, or "
            f"'apm install --force' to override"
            + (
                ", or '--no-policy' to skip the policy floor"
                if "policy" in decision.source.lower()
                else ""
            )
            + f" (mode set by {decision.source})."
        )

    # warn mode, or block downgraded by --force: record without halting.
    if ctx.diagnostics is not None:
        severity = "critical" if has_critical else "warning"
        ctx.diagnostics.security(
            f"Install-time audit found hidden-character / scanner findings: {summary}",
            package="audit",
            detail=(
                "Review with 'apm audit' or clean with 'apm audit --strip'."
                + (
                    " Deployed despite critical findings (--force)."
                    if has_critical and ctx.force
                    else ""
                )
            ),
            severity=severity,
        )
