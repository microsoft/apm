"""Retire stale MCP claims, recording what reached disk if a retirement is refused (#4792).

``run_mcp_integration`` (install/mcp/integration.py) plans each retirement as
``(runtime, names, exclude)``; :func:`retire_mcp_claims` runs them in order.
When the owner refuses one because the user edited a managed entry
(``require_removable_mcp_connections`` raises ``RequiredIntegrationError``),
:func:`record_refused_retirement` persists the claims this run wrote and every
earlier claim the run did not actually remove, then the refusal propagates.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

Retirement = tuple[str | None, builtins.set, str | None]


def retire_mcp_claims(
    retirements: Iterable[Retirement],
    remove: Callable[[builtins.set, str | None, str | None], None],
    on_refusal: Callable[[dict[str | None, builtins.set], list[Retirement]], None],
) -> None:
    """Run each planned retirement; on a refusal, report what was and was not removed."""
    from apm_cli.install.errors import RequiredIntegrationError

    planned = [(runtime, set(names), exclude) for runtime, names, exclude in retirements if names]
    removed: dict[str | None, builtins.set] = {}
    try:
        for runtime, names, exclude in planned:
            remove(names, runtime, exclude)
            removed.setdefault(runtime, set()).update(names)
    except RequiredIntegrationError:
        on_refusal(removed, planned)
        raise


def record_refused_retirement(
    lock_path: Path,
    logger: Any,
    *,
    managed_target_servers: dict[str, builtins.set],
    old_target_servers: dict[str, builtins.set],
    removed: dict[str | None, builtins.set],
    planned: list[Retirement],
    servers: tuple[builtins.set, builtins.set],
    configs: tuple[dict, dict],
    provenance: tuple[dict, dict],
    native_hashes: dict[str, dict[str, str]],
) -> None:
    """Persist the claims a run wrote before a refused retirement stopped it (#4792).

    The entries this run wrote stay claimed, as the write-path partial-failure
    branch records them (req-lk-006), so the lockfile equals disk. Earlier
    claims the run did not remove are kept for the retry: untouched ones, and
    every planned retirement that did not run, including a runtime that a
    still-declared server's ``targets:`` deselected. A claim the run revoked
    for a requested server (a preserved collision) is not restored.
    """
    from apm_cli.install.mcp.ownership import retain_deferred_mcp_claims
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    new_servers, old_servers = servers
    retained = {
        target: set(names) - removed.get(target, set())
        for target, names in old_target_servers.items()
    }
    retain_deferred_mcp_claims(managed_target_servers, retained, set(new_servers))
    for runtime, names, _exclude in planned:
        if runtime is not None and runtime in old_target_servers:
            pending = names & retained[runtime]
            if pending:
                managed_target_servers.setdefault(runtime, set()).update(pending)
    claimed = set().union(*managed_target_servers.values())
    gone = removed_names(removed) - claimed
    kept_old = {name for name in old_servers if name not in gone}
    new_configs, old_configs = configs
    new_provenance, old_provenance = provenance
    MCPIntegrator.update_lockfile(
        set(new_servers) | kept_old,
        lock_path,
        mcp_configs={**{n: c for n, c in old_configs.items() if n in kept_old}, **new_configs},
        mcp_target_servers=managed_target_servers,
        mcp_config_provenance={
            **{n: p for n, p in old_provenance.items() if n in kept_old},
            **new_provenance,
        },
        native_hashes=native_hashes,
        logger=logger,
        fail_on_write_error=True,
    )


def removed_names(removed: dict[str | None, builtins.set]) -> builtins.set:
    """Every server name a retirement removed from at least one runtime."""
    return set().union(*removed.values()) if removed else set()
