"""Canonical install outcome classification."""

from __future__ import annotations

from apm_cli.models.results import InstallDisposition, InstallResult


def diagnostic_error_count(diagnostics: object | None) -> int:
    """Return a defensive integer error count."""
    if diagnostics is None:
        return 0
    try:
        return int(getattr(diagnostics, "error_count", 0))
    except (TypeError, ValueError):
        return 0


def finalize_install_result(
    result: InstallResult,
    *,
    force: bool,
) -> InstallResult:
    """Classify diagnostics before hooks, transaction completion, or return."""
    if result.disposition in {
        InstallDisposition.CANCELLED,
        InstallDisposition.DRY_RUN,
        InstallDisposition.VALIDATION_FAILED,
    }:
        result.exit_code = 1 if result.disposition is InstallDisposition.VALIDATION_FAILED else 0
        return result
    diagnostics = result.diagnostics
    has_critical = bool(
        diagnostics is not None and getattr(diagnostics, "has_critical_security", False)
    )
    if (
        result.disposition is InstallDisposition.FAILED
        or diagnostic_error_count(diagnostics) > 0
        or (has_critical and not force)
    ):
        result.disposition = InstallDisposition.FAILED
        result.exit_code = 1
    else:
        result.exit_code = 0
    return result
