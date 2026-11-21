"""Retire parked MCP entries the lock does not record but APM provably wrote.

When the executables gate (or transitive trust) parks a dependency's server,
install removes the entry it deployed earlier through the lock-driven stale
cleanup. A project whose lockfile was lost has no such record, so this owner
removes a parked self-defined server's entry only where the runtime config
holds exactly what its declaration renders (the same proof adoption uses).
A differing entry is the user's and stays.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def retire_unrecorded_parked_servers(
    parked: Mapping[str, Any],
    *,
    recorded: Iterable[str],
    runtimes: Iterable[str | None],
    project_root: Path,
    user_scope: bool,
    scope: Any,
    logger: Any,
) -> builtins.set[str]:
    """Remove each parked server's matching entry no lock row records; return their names."""
    from apm_cli.install.mcp.native_entry import native_mcp_entry_matches_declaration
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    known = builtins.set(recorded)
    removed: builtins.set[str] = builtins.set()
    for runtime in sorted({runtime for runtime in runtimes if runtime}):
        for name, declaration in sorted(parked.items()):
            if name in known or not getattr(declaration, "is_self_defined", False):
                continue
            stored = declaration.to_dict()
            if not native_mcp_entry_matches_declaration(
                runtime,
                name,
                stored_config=stored,
                project_root=project_root,
                user_scope=user_scope,
            ):
                continue
            MCPIntegrator.remove_stale(
                {name},
                runtime=runtime,
                project_root=project_root,
                user_scope=user_scope,
                scope=scope,
                fail_on_write_error=True,
                stored_configs={name: stored},
            )
            removed.add(name)
    if removed:
        logger.record_mcp_removed(removed)
    return removed
