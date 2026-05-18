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
from dataclasses import dataclass
from typing import Any

from apm_cli.commands._helpers import _rich_blank_line


@dataclass(frozen=True, slots=True)
class PostInstallSummaryParams:
    """Parameter bundle for :func:`render_post_install_summary`."""

    apm_count: int
    mcp_count: int
    apm_diagnostics: Any
    force: bool
    elapsed_seconds: float | None = None


def render_post_install_summary(logger: Any, params: PostInstallSummaryParams) -> None:
    """Render diagnostics, the final summary line, and (optionally)
    hard-fail on critical security findings.

    Args:
        logger: An ``InstallLogger`` instance.
        params: Bundled summary parameters.

    Side effects:
        Writes to stdout via the logger and may call ``sys.exit(1)`` to
        propagate a critical-security hard-fail.
    """
    if params.apm_diagnostics and params.apm_diagnostics.has_diagnostics:
        params.apm_diagnostics.render_summary()
    else:
        _rich_blank_line()

    error_count = 0
    if params.apm_diagnostics:
        try:
            error_count = int(params.apm_diagnostics.error_count)
        except (TypeError, ValueError):
            error_count = 0
    logger.install_summary(
        apm_count=params.apm_count,
        mcp_count=params.mcp_count,
        errors=error_count,
        stale_cleaned=logger.stale_cleaned_total,
        elapsed_seconds=params.elapsed_seconds,
    )

    # Hard-fail when critical security findings blocked any package
    # (consistent with ``apm unpack``). ``--force`` overrides.
    if not params.force and params.apm_diagnostics and params.apm_diagnostics.has_critical_security:
        sys.exit(1)
