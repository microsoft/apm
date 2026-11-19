"""MCP and LSP servers a harness loads natively from their declaring plugin.

A server whose plugin a runtime loads natively (an admitted Claude plugin)
gets no APM-written copy on that runtime; any loose entry APM wrote there
earlier is retired.
"""

from __future__ import annotations

from typing import Any


def native_runtimes_of(dep: Any) -> frozenset[str]:
    """Runtimes that load *dep* from its plugin; empty for any other value."""
    runtimes = getattr(dep, "native_runtimes", None)
    return frozenset(runtimes) if isinstance(runtimes, (set, frozenset)) else frozenset()


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

    from .ownership import retire_runtime_claim

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
