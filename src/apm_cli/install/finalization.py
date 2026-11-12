"""Install command and pipeline context finalization."""

from __future__ import annotations

import sys
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .context import InstallContext


def close_install_contexts(
    root_redirect: AbstractContextManager[Any],
    transaction: AbstractContextManager[Any] | None,
) -> None:
    """Restore the working directory and always close the transaction."""
    try:
        root_redirect.__exit__(None, None, None)
    finally:
        if transaction is not None:
            transaction.__exit__(*sys.exc_info())


def close_pipeline_attempt(ctx: InstallContext) -> None:
    """Run ``run_install_pipeline``'s exit teardown on every exit.

    Native Copilot plugin registration is deactivated and the TUI closes
    before a failed attempt names the native target files it left (#3525).
    """
    from .failure_residue import report_failed_install_residue
    from .phases import copilot_plugins

    copilot_plugins.deactivate(ctx)
    ctx.tui.__exit__()
    report_failed_install_residue(ctx)
