"""Retire APM's MCP claims, including servers a harness loads from their plugin.

A server whose plugin a runtime loads natively (an admitted Claude plugin)
gets no APM-written copy on that runtime; any loose entry APM wrote there
earlier is retired through :func:`retire_runtime_claim`.
"""

from __future__ import annotations

from typing import Any


def native_runtimes_of(dep: Any) -> frozenset[str]:
    """Runtimes that load *dep* from its plugin; empty for any other value."""
    runtimes = getattr(dep, "native_runtimes", None)
    return frozenset(runtimes) if isinstance(runtimes, (set, frozenset)) else frozenset()


def retire_runtime_claim(
    client: Any,
    runtime: str,
    name: str,
    requested_info: dict | None,
    logger: Any,
    *,
    stored_config: dict | None,
    managed_target_servers: dict[str, set[str]] | None,
    native_hashes: dict[str, dict[str, str]] | None,
) -> None:
    """Drop APM's claim on *name* for *runtime*, removing only an entry APM wrote."""
    from apm_cli.integration.mcp_integrator_install import _remove_native_entry

    from .ownership import managed_unsupported_connection

    claims = (managed_target_servers or {}).get(runtime, set())
    if name not in claims:
        return
    key = managed_unsupported_connection(
        client,
        name,
        requested_info=requested_info,
        stored_config=stored_config,
        native_hash=(native_hashes or {}).get(runtime, {}).get(name),
    )
    if key is not None:
        _remove_native_entry(client, key, logger)
    # Reached only after a successful removal (cleanup failures raise).
    claims.discard(name)
    if native_hashes is not None:
        native_hashes.get(runtime, {}).pop(name, None)


def retire_native_runtime_claims(
    deps: list,
    target_runtimes: list[str],
    logger: Any,
    *,
    project_root: Any,
    user_scope: bool,
    stored_mcp_configs: dict,
    managed_target_servers: dict[str, set[str]] | None,
    native_hashes: dict[str, dict[str, str]] | None,
) -> None:
    """Retire APM's loose entries on runtimes that load a server from its plugin."""
    from apm_cli.factory import ClientFactory

    for dep in deps:
        for runtime in sorted(set(target_runtimes) & native_runtimes_of(dep)):
            if dep.name not in (managed_target_servers or {}).get(runtime, set()):
                continue
            retire_runtime_claim(
                ClientFactory.create_client(
                    runtime, project_root=project_root, user_scope=user_scope
                ),
                runtime,
                dep.name,
                None,
                logger,
                stored_config=stored_mcp_configs.get(dep.name),
                managed_target_servers=managed_target_servers,
                native_hashes=native_hashes,
            )
