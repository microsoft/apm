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
    native_hashes: tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]],
    stamp_version: bool = False,
) -> None:
    """Persist the claims a run wrote before a refused retirement stopped it (#4792).

    The entries this run wrote stay claimed, as the write-path partial-failure
    branch records them (req-lk-006), so the lockfile equals disk. Earlier
    claims the run did not remove are kept for the retry: untouched ones, and
    every planned retirement that did not run, including a runtime that a
    still-declared server's ``targets:`` deselected. A claim the run revoked
    for a requested server (a preserved collision) is not restored. When the
    declaration changed and a runtime claims the server for the first time,
    the refused baseline would vouch for bytes APM never wrote there, so the
    pending claim is dropped instead: the edited entry stays, unowned.
    """
    from apm_cli.install.mcp.ownership import retain_deferred_mcp_claims
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    new_servers, old_servers = servers
    retained = {
        target: set(names) - removed.get(target, set())
        for target, names in old_target_servers.items()
    }
    retain_deferred_mcp_claims(managed_target_servers, retained, set(new_servers))
    new_hashes, old_hashes = native_hashes
    new_configs, old_configs = configs
    pending: dict[str, builtins.set] = {}
    for runtime, names, _exclude in planned:
        if runtime is not None and runtime in old_target_servers:
            pending.setdefault(runtime, set()).update(names & retained[runtime])
    keep_baseline = {
        name
        for name in set().union(*pending.values(), set())
        if _refused_baseline_is_ours(name, managed_target_servers, old_target_servers)
        or old_configs.get(name) == new_configs.get(name)
    }
    for runtime, names in pending.items():
        # The lockfile holds one declaration per server: the refused baseline
        # stays only where APM itself wrote it on every runtime claiming the
        # server; otherwise the edited entry is left unowned, never deleted.
        for name in names & keep_baseline:
            managed_target_servers.setdefault(runtime, set()).add(name)
            if name in old_hashes.get(runtime, {}):
                new_hashes.setdefault(runtime, {})[name] = old_hashes[runtime][name]
    claimed = set().union(*managed_target_servers.values())
    gone = removed_names(removed) - claimed
    kept_old = {name for name in old_servers if name not in gone}
    # A kept pending retirement keeps its recorded declaration as its baseline,
    # so an identical retry still refuses an edit matching only the new one.
    baselines = {n: c for n, c in old_configs.items() if n in kept_old}
    baselines.update({n: c for n, c in new_configs.items() if n not in keep_baseline})
    new_provenance, old_provenance = provenance
    MCPIntegrator.update_lockfile(
        set(new_servers) | kept_old,
        lock_path,
        mcp_configs=baselines,
        mcp_target_servers=managed_target_servers,
        mcp_config_provenance={
            **{n: p for n, p in old_provenance.items() if n in kept_old},
            **new_provenance,
        },
        native_hashes=new_hashes,
        logger=logger,
        fail_on_write_error=True,
        stamp_version=stamp_version,
    )


def removed_names(removed: dict[str | None, builtins.set]) -> builtins.set:
    """Every server name a retirement removed from at least one runtime."""
    return set().union(*removed.values()) if removed else set()


def _refused_baseline_is_ours(
    name: str,
    managed_target_servers: dict[str, builtins.set],
    old_target_servers: dict[str, builtins.set],
) -> bool:
    """Whether every runtime claiming *name* now also claimed it before this run."""
    return all(
        name in old_target_servers.get(runtime, set())
        for runtime, names in managed_target_servers.items()
        if name in names
    )
