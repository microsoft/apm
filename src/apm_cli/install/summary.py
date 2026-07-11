"""Final-summary rendering for ``apm install``.

Extracted from ``apm_cli.commands.install`` to keep the command file
under its architectural LOC budget while we layer on the perf+UX
findings F1-F7 (microsoft/apm#1116). This module is a *pure* renderer:
it takes already-collected diagnostics, formats them through the
``InstallLogger``, and decides whether the command should hard-fail on
critical security findings.

Keeping it free of the install pipeline state (no ``InstallContext``)
lets the unit tests exercise summary behaviour without spinning up
sources, locks, or filesystem fixtures.
"""

from __future__ import annotations

from apm_cli.commands._helpers import _rich_blank_line
from apm_cli.models.results import InstallDisposition, InstallResult


def render_post_install_summary(
    *,
    logger,
    apm_count: int,
    mcp_count: int,
    lsp_count: int = 0,
    apm_diagnostics=None,
    force: bool,
    elapsed_seconds: float | None = None,
) -> InstallResult:
    """Render diagnostics, the final summary line, and (optionally)
    hard-fail on critical security findings.

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

    Returns:
        Structured completion state for the command adapter.
    """
    if apm_diagnostics and apm_diagnostics.has_diagnostics:
        apm_diagnostics.render_summary()
    else:
        _rich_blank_line()

    error_count = 0
    if apm_diagnostics:
        try:
            error_count = int(apm_diagnostics.error_count)
        except (TypeError, ValueError):
            error_count = 0
    logger.install_summary(
        apm_count=apm_count,
        mcp_count=mcp_count,
        lsp_count=lsp_count,
        errors=error_count,
        stale_cleaned=logger.stale_cleaned_total,
        elapsed_seconds=elapsed_seconds,
    )

    # Hard-fail when critical security findings blocked any package
    # (consistent with ``apm unpack``). ``--force`` overrides.
    if not force and apm_diagnostics and apm_diagnostics.has_critical_security:
        return InstallResult(
            installed_count=apm_count,
            diagnostics=apm_diagnostics,
            disposition=InstallDisposition.FAILED,
            exit_code=1,
        )

    # Hard-fail when ANY per-dep install error was reported. Matches
    # the npm / pip / cargo convention: any install failure -> non-zero
    # exit so CI scripts can detect failure without parsing stderr.
    # ``--force`` covers critical-security overrides only; it does NOT
    # suppress this hard-fail (Bug 2 fix on #1496, where the CLI used
    # to exit 0 even after printing "Installation failed with N error(s)").
    if error_count > 0:
        return InstallResult(
            installed_count=apm_count,
            diagnostics=apm_diagnostics,
            disposition=InstallDisposition.FAILED,
            exit_code=1,
        )
    return InstallResult(
        installed_count=apm_count,
        diagnostics=apm_diagnostics,
    )
