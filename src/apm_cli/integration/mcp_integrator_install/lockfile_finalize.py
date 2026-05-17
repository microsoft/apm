"""MCP install summary rendering."""

from __future__ import annotations

import builtins

from apm_cli.utils.console import STATUS_SYMBOLS


def _finalize_lockfile(configured_count: int, successful_updates: set, console) -> None:
    """Render the final MCP install summary."""
    if not console:
        return
    if configured_count <= 0:
        console.print(f"[green]{STATUS_SYMBOLS['success']} All servers up to date[/green]")
        return
    update_count = builtins.len(successful_updates)
    new_count = configured_count - update_count
    parts = []
    if new_count > 0:
        parts.append(f"configured {new_count} server{'s' if new_count != 1 else ''}")
    if update_count > 0:
        parts.append(f"updated {update_count} server{'s' if update_count != 1 else ''}")
    console.print(f"[green]{STATUS_SYMBOLS['success']} {', '.join(parts).capitalize()}[/green]")
