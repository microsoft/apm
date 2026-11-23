"""MCP outputs whose no-op form the verified-state record cannot know (#4977).

The record is written only after a verified no-op; any of these in that run
means nothing is recorded (install/verdict_record.unknown).
"""

from __future__ import annotations

from typing import Any

from apm_cli.install import verdict_record


def mark_unknown_noop_forms(
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
