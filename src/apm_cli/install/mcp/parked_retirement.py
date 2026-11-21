"""Retire parked MCP entries a lost lockfile no longer records.

When the executables gate (or transitive trust) parks a dependency's server,
install removes the entry it deployed earlier through the lock-driven stale
cleanup. A project whose lockfile was lost has no such record. Equality with
a declaration alone never proves ownership (a user may hold the same entry),
so this owner acts only when no prior lock exists at all and this
installation's deployment receipts prove APM integrated the declaring
package here; it then removes a parked self-defined server's entry only
where the runtime config holds exactly what its declaration renders.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def retire_unrecorded_parked_servers(
    parked: Mapping[str, tuple[str, Any]],
    *,
    runtimes: Iterable[str | None],
    receipted_owners: frozenset[str],
    project_root: Path,
    user_scope: bool,
    scope: Any,
    logger: Any,
) -> builtins.set[str]:
    """Remove each proven parked server's matching entry; return the names removed.

    Callers invoke this only when the install started without a lockfile.
    *receipted_owners* are the lock keys whose files this installation's
    receipts recorded before this run's cleanup.
    """
    from apm_cli.install.mcp.native_entry import native_mcp_entry_matches_declaration
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    proven = {
        name: declaration
        for name, (owner, declaration) in parked.items()
        if getattr(declaration, "is_self_defined", False) and owner in receipted_owners
    }
    removed: builtins.set[str] = builtins.set()
    for runtime in sorted({runtime for runtime in runtimes if runtime}):
        for name, declaration in sorted(proven.items()):
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
