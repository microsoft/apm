"""Retire stale MCP claims, recording what reached disk if a retirement is refused (#4792).

``run_mcp_integration`` (install/mcp/integration.py) hands over the deselected
and stale claims; :func:`retire_mcp_claims` plans each retirement as
``(runtime, names, exclude)`` and runs them in order.
When the owner refuses one because the user edited a managed entry
(``require_removable_mcp_connections`` raises ``RequiredIntegrationError``),
:func:`record_refused_retirement` persists the claims this run wrote and every
earlier claim the run did not actually remove, then the refusal propagates.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable
from pathlib import Path
from typing import Any

from apm_cli.install.mcp.ownership import declarations_share_native_key

Retirement = tuple[str | None, builtins.set, str | None]


def retire_mcp_claims(
    deselected: dict[str, builtins.set],
    stale: tuple[builtins.set, dict[str | None, builtins.set], str | None],
    remove: Callable[[builtins.set, str | None, str | None], None],
    on_refusal: Callable[[dict[str | None, builtins.set], list[Retirement]], None],
) -> None:
    """Run each planned retirement; on a refusal, report what was and was not removed.

    *deselected* maps a runtime to the claims it loses; *stale* is
    ``(stale_servers, cleanup_owners, exclude)`` for servers no longer declared.
    """
    from apm_cli.install.errors import RequiredIntegrationError

    stale_servers, cleanup_owners, exclude = stale
    retirements: list[Retirement] = [(target, names, None) for target, names in deselected.items()]
    retirements += [
        (owner, stale_servers & owned, exclude)
        for owner, owned in sorted(cleanup_owners.items(), key=lambda item: item[0] or "")
    ]
    planned = [(runtime, set(names), exclude) for runtime, names, exclude in retirements if names]
    removed: dict[str | None, builtins.set] = {}
    try:
        for runtime, names, exclude in planned:
            remove(names, runtime, exclude)
            removed.setdefault(runtime, set()).update(names)
    except RequiredIntegrationError:
        on_refusal(removed, planned)
        raise


def stale_claim_remover(
    logger: Any,
    destination: tuple[Path | None, bool, Any],
    baselines: tuple[dict, dict],
) -> Callable[[builtins.set, str | None, str | None], None]:
    """Return the ``remove`` step: retire stale claims against their recorded baselines."""
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    project_root, user_scope, scope = destination
    stored_configs, native_hashes = baselines

    def remove(names: builtins.set, runtime: str | None, exclude: str | None) -> None:
        MCPIntegrator.remove_stale(
            names,
            runtime,
            exclude,
            project_root=project_root,
            user_scope=user_scope,
            scope=scope,
            fail_on_write_error=True,
            stored_configs=stored_configs,
            native_hashes=native_hashes,
        )
        logger.record_mcp_removed(names)

    return remove


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

    planned, old_target_servers, removed = _concrete_retirements(
        planned, old_target_servers, removed
    )
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
        if _baseline_can_stand(name, pending, managed_target_servers, old_target_servers, configs)
    }
    for runtime, names in pending.items():
        # The lockfile holds one declaration per server: the refused baseline
        # stays only where APM itself wrote it on every runtime claiming the
        # server; otherwise the edited entry is left unowned, never deleted.
        for name in names & keep_baseline:
            managed_target_servers.setdefault(runtime, set()).add(name)
            if name in old_hashes.get(runtime, {}):
                new_hashes.setdefault(runtime, {})[name] = old_hashes[runtime][name]
        # Also undo a claim retain_deferred_mcp_claims restored for an inactive runtime.
        for name in names - keep_baseline:
            managed_target_servers.get(runtime, set()).discard(name)
            new_hashes.get(runtime, {}).pop(name, None)
        if runtime in managed_target_servers and not managed_target_servers[runtime]:
            del managed_target_servers[runtime]
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


def reconcile_skipped_baseline(
    name: str,
    configs: tuple[dict, dict],
    managed_target_servers: dict[str, builtins.set],
    native_hashes: dict[str, dict[str, str]],
    *,
    skipped: builtins.set,
) -> None:
    """Keep the old declaration of *name* for the runtimes this run skipped (#4949).

    A skipped runtime whose native key differs between the old and new
    declaration drops its claim (its entry stays, unowned) and the new
    declaration stays in the lock: the old baseline would retire the wrong key
    and mask what the active runtimes wrote.
    """
    new_configs, old_configs = configs
    stale = {
        runtime
        for runtime in skipped
        if name in managed_target_servers.get(runtime, ())
        and not declarations_share_native_key(
            [runtime], name, old_configs.get(name), new_configs.get(name)
        )
    }
    if not stale:
        new_configs[name] = old_configs[name]
        return
    for runtime in stale:
        managed_target_servers[runtime].discard(name)
        native_hashes.get(runtime, {}).pop(name, None)


def _concrete_retirements(
    planned: list[Retirement],
    owners: dict,
    removed: dict,
) -> tuple[list[Retirement], dict[str, builtins.set], dict[str, builtins.set]]:
    """Expand an untargeted legacy cleanup (runtime ``None``) into each runtime it reaches.

    ``remove_stale`` with no runtime cleans every supported client but the
    excluded one; claims are recorded per concrete runtime, never under ``None``.
    """
    from apm_cli.factory import ClientFactory

    exclude = next((ex for runtime, _names, ex in planned if runtime is None), None)
    every = sorted(set(ClientFactory.supported_clients()) - {exclude})

    def expand(mapping: dict) -> dict[str, builtins.set]:
        out = {k: set(v) for k, v in mapping.items() if k is not None}
        for runtime in every if None in mapping else ():
            out.setdefault(runtime, set()).update(mapping[None])
        return out

    concrete = [
        (target, names, ex)
        for runtime, names, ex in planned
        for target in ([runtime] if runtime is not None else every)
    ]
    return concrete, expand(owners), expand(removed)


def removed_names(removed: dict[str | None, builtins.set]) -> builtins.set:
    """Every server name a retirement removed from at least one runtime."""
    return set().union(*removed.values()) if removed else set()


def _baseline_can_stand(
    name: str,
    pending: dict[str, builtins.set],
    managed_target_servers: dict[str, builtins.set],
    old_target_servers: dict[str, builtins.set],
    configs: tuple[dict, dict],
) -> bool:
    """Whether the refused (old) declaration of *name* may stay its recorded baseline.

    A claim with no recorded declaration can never be retired, and a baseline
    that keys *name* to another native entry than the new declaration would
    retire the wrong key (#4899): either way the new declaration stands.
    """
    new_configs, old_configs = configs
    if name in new_configs:
        if name not in old_configs:
            return False
        runtimes = {r for r, names in managed_target_servers.items() if name in names}
        runtimes |= {r for r, names in pending.items() if name in names}
        if not declarations_share_native_key(runtimes, name, old_configs[name], new_configs[name]):
            return False
    return _refused_baseline_is_ours(
        name, managed_target_servers, old_target_servers
    ) or old_configs.get(name) == new_configs.get(name)


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
