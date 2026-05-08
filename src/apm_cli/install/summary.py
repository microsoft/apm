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

import sys

from apm_cli.commands._helpers import _rich_blank_line


def render_post_install_summary(
    *,
    logger,
    apm_count: int,
    mcp_count: int,
    apm_diagnostics,
    force: bool,
    elapsed_seconds: float | None = None,
) -> None:
    """Render diagnostics, the final summary line, and (optionally)
    hard-fail on critical security findings.

    Args:
        logger: An ``InstallLogger`` instance.
        apm_count: Number of APM dependencies installed.
        mcp_count: Number of MCP servers installed.
        apm_diagnostics: ``DiagnosticCollector`` for the install run, or
            ``None`` when no diagnostics were captured.
        force: When ``True``, suppresses the hard-fail on critical
            security findings (mirrors ``apm unpack --force``).
        elapsed_seconds: Wall-clock duration of the whole install
            command, captured by the caller immediately after logger
            construction. ``None`` keeps the legacy "... ." suffix; a
            float appends `` in {x:.1f}s`` before the period (F5).

    Side effects:
        Writes to stdout via the logger and may call ``sys.exit(1)`` to
        propagate a critical-security hard-fail.
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
        errors=error_count,
        stale_cleaned=logger.stale_cleaned_total,
        elapsed_seconds=elapsed_seconds,
    )

    # Hard-fail when critical security findings blocked any package
    # (consistent with ``apm unpack``). ``--force`` overrides.
    if not force and apm_diagnostics and apm_diagnostics.has_critical_security:
        sys.exit(1)
