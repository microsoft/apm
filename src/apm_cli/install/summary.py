"""Final-summary rendering for ``apm install``.

Extracted from ``apm_cli.commands.install`` to keep the command file
under its architectural LOC budget while we layer on the perf+UX
findings F1-F7 (microsoft/apm#1116). This module classifies the result
without output, then renders already-collected diagnostics through the
``InstallLogger`` after transaction completion.

Keeping it free of the install pipeline state (no ``InstallContext``)
lets the unit tests exercise summary behaviour without spinning up
sources, locks, or filesystem fixtures.
"""

from __future__ import annotations

from apm_cli.commands._helpers import _rich_blank_line
from apm_cli.install.outcome import (
    InstallCommandOutcome,
    apply_install_command_outcome,
    diagnostic_error_count,
    finalize_install_result,
)
from apm_cli.models.results import InstallResult


def _error_count(apm_diagnostics) -> int:
    """Return a defensive integer count from optional diagnostics."""
    return diagnostic_error_count(apm_diagnostics)


def classify_post_install_result(
    *,
    apm_count: int,
    apm_diagnostics=None,
    force: bool,
) -> InstallResult:
    """Classify completion without rendering user-facing output."""
    return finalize_install_result(
        InstallResult(
            installed_count=apm_count,
            diagnostics=apm_diagnostics,
        ),
        force=force,
    )


def render_install_result_failure_summary(
    *,
    logger,
    result: InstallResult,
) -> InstallCommandOutcome:
    """Render diagnostics and return the canonical outcome for failed adapters."""
    outcome = apply_install_command_outcome(result)
    if outcome.success_summary_allowed:
        return outcome

    if outcome.exit_code != 0:
        diagnostics = result.diagnostics
        if diagnostics is not None and getattr(diagnostics, "has_diagnostics", False):
            diagnostics.render_summary()
        logger.install_summary(
            apm_count=result.installed_count,
            mcp_count=0,
            errors=diagnostic_error_count(diagnostics),
            disposition=result.disposition,
        )
    return outcome


def exit_unless_install_result_allows_success(
    *,
    logger,
    result: InstallResult,
    allow_neutral_outcome: bool = False,
) -> InstallCommandOutcome:
    """Exit before adapter-specific success output when install disposition failed."""
    outcome = render_install_result_failure_summary(logger=logger, result=result)
    if outcome.success_summary_allowed or (allow_neutral_outcome and outcome.exit_code == 0):
        return outcome
    raise SystemExit(outcome.exit_code)


def render_post_install_summary(
    *,
    logger,
    apm_count: int,
    mcp_count: int,
    lsp_count: int = 0,
    apm_diagnostics=None,
    force: bool,
    elapsed_seconds: float | None = None,
    result: InstallResult | None = None,
) -> InstallResult:
    """Render diagnostics and the final disposition-aware summary line.

    Args:
        logger: An ``InstallLogger`` instance.
        apm_count: Number of APM dependencies installed.
        mcp_count: Number of MCP servers installed.
        lsp_count: Number of LSP servers installed.
        apm_diagnostics: ``DiagnosticCollector`` for the install run, or
            ``None`` when no diagnostics were captured.
        force: When ``True``, suppresses the hard-fail on critical
            security findings (mirrors ``apm unpack --force``).
        elapsed_seconds: Wall-clock duration of the whole install
            command, captured by the caller immediately after logger
            construction. ``None`` keeps the legacy "... ." suffix; a
            float appends `` in {x:.1f}s`` before the period (F5).
        result: Final result after commit or rollback. When omitted,
            classify from diagnostics for backward-compatible callers.

    Returns:
        Structured completion state for the command adapter.
    """
    if apm_diagnostics and apm_diagnostics.has_diagnostics:
        apm_diagnostics.render_summary()
    else:
        _rich_blank_line()

    error_count = _error_count(apm_diagnostics)
    if result is None:
        result = classify_post_install_result(
            apm_count=apm_count,
            apm_diagnostics=apm_diagnostics,
            force=force,
        )
    apply_install_command_outcome(result)
    logger.install_summary(
        apm_count=apm_count,
        mcp_count=mcp_count,
        lsp_count=lsp_count,
        errors=error_count,
        stale_cleaned=logger.stale_cleaned_total,
        elapsed_seconds=elapsed_seconds,
        disposition=result.disposition,
    )
    return result
