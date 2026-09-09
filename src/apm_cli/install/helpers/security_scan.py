"""Pre-deploy security scan that runs before any file is written to the project tree.

Wraps the :class:`~apm_cli.security.gate.SecurityGate` scanner used by the
install pipeline. The scan detects hidden characters (zero-width joiners,
bidirectional overrides, etc.) that could be used to smuggle malicious payloads
into prompts, skills, or agent definitions.
"""

from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.utils.diagnostics import DiagnosticCollector, printable_ascii_text


def _pre_deploy_security_scan(
    source_plan: DeployableSourcePlan,
    diagnostics: DiagnosticCollector,
    package_name: str = "",
    force: bool = False,
    logger=None,
) -> bool:
    """Scan authorized deployable source files for hidden characters before deployment.

    Delegates to :class:`SecurityGate` for the scan->classify->decide pipeline.
    Inline CLI feedback (error/info lines) is kept here because it is
    install-specific formatting.

    Returns:
        True if deployment should proceed, False to block.
    """
    from apm_cli.security.gate import BLOCK_POLICY, SecurityGate

    verdict = source_plan.scan_security(policy=BLOCK_POLICY, force=force)
    if not verdict.has_findings:
        return True

    # Record into diagnostics (consistent messaging via gate)
    SecurityGate.report(verdict, diagnostics, package=package_name, force=force)

    if verdict.should_block:
        if logger:
            logger.error(
                "  Blocked: "
                f"{printable_ascii_text(package_name or 'package')} "
                "contains critical hidden character(s)"
            )
            affected_paths = sorted(verdict.findings_by_file)
            for path in affected_paths[:5]:
                logger.error_detail(f"  |-- {printable_ascii_text(path)}")
            if len(affected_paths) > 5:
                logger.error_detail(f"  |-- ... and {len(affected_paths) - 5} more file(s)")
            logger.error_detail(
                "  |-- Fix the reported file(s) in the package source, then reinstall"
            )
            logger.error_detail(
                "  |-- Use --force only after reviewing the reported findings "
                f"from {printable_ascii_text(str(source_plan.source_root))}"
            )
        return False

    return True
