"""Frozen-install MCP drift reasons.

Turns a selected :class:`~apm_cli.integration.mcp_config_view.CurrentMcpConfigView`
and the committed lockfile into the reasons a ``--frozen`` install reports.
Consumed by :class:`~apm_cli.install.service.InstallService`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.mcp_config_view import CurrentMcpConfigView, McpConfigDiff


def frozen_mcp_drift_reasons(
    current: CurrentMcpConfigView,
    lockfile: LockFile,
    diff: McpConfigDiff,
    pending: frozenset[str],
) -> list[str]:
    """Name every way *current* (whose config *diff* InstallService computed) differs from *lockfile*."""
    reasons = [f"  - {problem.package_key}: {problem.message}" for problem in current.problems]
    consent = current.consent_problems(lockfile)
    for name, message in consent.items():
        reasons.append(f"  - MCP server {name!r} is {message}")
    for name in sorted(diff.changed):
        reasons.append(f"  - MCP server {name!r} config differs from apm.lock.yaml")
    for name in sorted(diff.source_only):
        reasons.append(f"  - MCP server {name!r} is missing from apm.lock.yaml")
    for name in sorted(diff.lock_only - pending - set(consent)):
        evidence = current.omissions.get(name)
        reason = evidence.get("reason") if isinstance(evidence, Mapping) else None
        if reason is not None:
            # Still declared: the gate left it out this run (#3957).
            reasons.append(f"  - MCP server {name!r} is now left out ({reason})")
        else:
            reasons.append(f"  - MCP server {name!r} is no longer declared in apm.yml")
    if set(current.configs) | pending != set(lockfile.mcp_servers) and not reasons:
        reasons.append("  - MCP server names in apm.lock.yaml are out of sync with apm.yml")
    return reasons
