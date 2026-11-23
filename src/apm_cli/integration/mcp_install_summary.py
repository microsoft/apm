"""The MCP install summary footer and its no-op forms (verified-state record, #4977).

Split out of ``mcp_integrator_install.py`` (file-length ratchet): the footer an
install prints, and the no-op form of each MCP output it records.
"""

from __future__ import annotations

import builtins
from typing import Any

from apm_cli.install import verdict_record
from apm_cli.utils.console import STATUS_SYMBOLS


def _print_mcp_summary(
    console,
    configured_count: int,
    successful_updates: builtins.set,
) -> None:
    """Print the MCP install summary footer panel."""
    if not console:
        return
    if configured_count == 0:
        _print_servers_up_to_date(console)
        return
    if verdict_record.recording_active():
        # The no-op form of this footer, for the verified-state record (#4977).
        with verdict_record.noop_only():
            _print_servers_up_to_date(console)
    # Use successful_updates (not servers_to_update) for accurate counts.
    # servers_to_update = all drift-detected servers (some may have failed).
    # successful_updates = servers that were re-applied AND succeeded.
    update_count = builtins.len(successful_updates)
    new_count = configured_count - update_count
    parts = []
    if new_count > 0:
        parts.append(f"configured {new_count} server{'s' if new_count != 1 else ''}")
    if update_count > 0:
        parts.append(f"updated {update_count} server{'s' if update_count != 1 else ''}")
    with verdict_record.change_only():
        console.print(f"[green]{STATUS_SYMBOLS['success']} {', '.join(parts).capitalize()}[/green]")


def _mark_unknown_noop_forms(
    updates: Any, already_configured: Any, *, configuring: Any = (), uncompared: Any = ()
) -> None:
    """Outputs whose no-op form is not known: the verified-state record records nothing."""
    if updates:
        verdict_record.unknown("mcp update")
    if already_configured:
        verdict_record.unknown("mcp already-configured listing")
    if configuring and uncompared:
        # A client without native comparison lists the server as already configured
        # on the next run, a form this install cannot render (IntelliJ).
        verdict_record.unknown("mcp client without native comparison")


def _print_servers_up_to_date(console: Any) -> None:
    console.print(f"[green]{STATUS_SYMBOLS['success']} All servers up to date[/green]")
