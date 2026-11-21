"""Resolve the effective canonical transport of MCP dependencies for policy.

Org policy ``mcp.transport.allow`` must judge what APM will write, not only
what the manifest declares. A registry server usually declares no
``transport:``; its transport comes from the registry remote. This module
builds the same server info the install path hands to the client adapters
(registry lookup plus manifest overlay, or the self-defined builder) and
projects it through the transport owner
(:func:`apm_cli.mcp_transport.server_write_transports`).
"""

from __future__ import annotations

import copy
import threading
import warnings
from collections.abc import Iterable
from typing import Any

from ..mcp_transport import (
    UnresolvedTransport,
    canonical_transport_name,
    server_write_transports,
)

EffectiveTransport = frozenset[str] | UnresolvedTransport

# Process-local memo of registry lookups keyed by (registry URL, name), so the
# install gate and the pre-write preflight do not query the registry twice.
_REGISTRY_INFO_MEMO: dict[tuple[str, str], dict[str, Any] | UnresolvedTransport] = {}
_MEMO_LOCK = threading.Lock()


def policy_checked_registry_info(registry_url: str, name: str) -> dict[str, Any] | None:
    """Return a copy of the registry info policy judged for *name*, if any.

    Install writes from this snapshot so the transport policy checked is the
    transport written, even when the registry answers differently later.
    """
    with _MEMO_LOCK:
        info = _REGISTRY_INFO_MEMO.get((registry_url, name))
    return copy.deepcopy(info) if isinstance(info, dict) else None


def write_transports(server_info: dict[str, Any]) -> frozenset[str]:
    """Canonical transports the client adapters would write for *server_info*."""
    from ..adapters.client.base import MCPClientAdapter

    return server_write_transports(server_info, selected_remote=MCPClientAdapter.selected_remote)


def clear_registry_info_memo() -> None:
    """Forget memoized registry lookups (tests and long-lived callers)."""
    with _MEMO_LOCK:
        _REGISTRY_INFO_MEMO.clear()


def _dep_name(dep: Any) -> str:
    return dep if isinstance(dep, str) else dep.name


def _declared_transport(dep: Any) -> str | None:
    if isinstance(dep, str):
        return None
    declared = getattr(dep, "transport", None)
    if isinstance(declared, str) and declared.strip():
        return declared
    return None


def _is_registry_dep(dep: Any) -> bool:
    return isinstance(dep, str) or bool(getattr(dep, "is_registry_resolved", False))


def _project(info: dict[str, Any]) -> EffectiveTransport:
    try:
        return write_transports(info)
    except ValueError as exc:
        return UnresolvedTransport(f"registry data is not usable: {exc}")


def _lookup(client: Any, safe_url: str, name: str) -> dict[str, Any] | UnresolvedTransport:
    try:
        info = client.find_server_by_reference(name)
    except Exception:
        return UnresolvedTransport(f"MCP registry {safe_url} is unreachable")
    if info is None:
        return UnresolvedTransport(f"server not found in MCP registry {safe_url}")
    return info


def _registry_client(registry_url: str | None) -> Any:
    """Return the registry client install uses for *registry_url*."""
    from ..registry.operations import MCPServerOperations

    return MCPServerOperations(registry_url=registry_url).registry_client


def _fetch_registry_infos(
    registry_url: str | None, names: list[str]
) -> tuple[str | None, dict[str, dict[str, Any] | UnresolvedTransport]]:
    """Look up *names* in one registry, memoized per process.

    Returns the resolved registry URL (``None`` when the registry
    configuration is unusable) and the per-name result.
    """
    from concurrent.futures import ThreadPoolExecutor

    from ..registry.client import redact_mcp_registry_url

    try:
        client = _registry_client(registry_url)
    except ValueError as exc:
        return None, dict.fromkeys(names, UnresolvedTransport(f"MCP registry is not usable: {exc}"))
    resolved_url = client.registry_url
    safe_url = redact_mcp_registry_url(resolved_url)
    found: dict[str, dict[str, Any] | UnresolvedTransport] = {}
    with _MEMO_LOCK:
        for name in names:
            memo = _REGISTRY_INFO_MEMO.get((resolved_url, name))
            if memo is not None:
                found[name] = memo
    missing = [name for name in names if name not in found]
    if missing:
        workers = min(4, len(missing))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mcp-policy") as pool:
            looked_up = list(pool.map(lambda n: _lookup(client, safe_url, n), missing))
        with _MEMO_LOCK:
            for name, info in zip(missing, looked_up, strict=True):
                _REGISTRY_INFO_MEMO[(resolved_url, name)] = info
                found[name] = info
    return resolved_url, found


def _registry_transports(deps: list[Any]) -> dict[str, EffectiveTransport]:
    from .mcp_integrator_install import _overlaid_server_infos, _registry_group_key

    groups: dict[str | None, list[Any]] = {}
    for dep in deps:
        groups.setdefault(_registry_group_key(dep), []).append(dep)

    result: dict[str, EffectiveTransport] = {}
    for registry_url, group in groups.items():
        _resolved_url, infos = _fetch_registry_infos(
            registry_url, [_dep_name(dep) for dep in group]
        )
        known = {name: info for name, info in infos.items() if isinstance(info, dict)}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # The same fetch-then-overlay step install uses; ``known`` is
            # complete, so it never queries the registry again.
            overlaid = _overlaid_server_infos(
                None,
                list(known),
                {dep.name: dep for dep in group if not isinstance(dep, str)},
                known=known,
            )
        for dep in group:
            name = _dep_name(dep)
            raw = infos[name]
            result[name] = raw if isinstance(raw, UnresolvedTransport) else _project(overlaid[name])
    return result


def resolve_effective_mcp_transports(
    mcp_deps: Iterable[Any],
) -> dict[str, EffectiveTransport]:
    """Map each MCP dependency name to the canonical transports APM may write.

    Registry servers are looked up in the same registry install uses
    (per-dependency ``registry:`` URL, else ``MCP_REGISTRY_URL`` or the
    configured default) and the manifest overlay is applied, so a declared
    ``transport:`` narrows the result only when the server offers it. A
    server offering both a package and a remote reports both. A registry
    server the registry cannot resolve is an :class:`UnresolvedTransport`,
    even when it declares a transport. Self-defined servers resolve
    offline: their declaration is exactly what install writes.
    """
    from ._shared import deduplicate_deps
    from .mcp_integrator import MCPIntegrator

    # First declaration wins, as in install.
    deps = deduplicate_deps(list(mcp_deps))
    registry_deps = [dep for dep in deps if _is_registry_dep(dep)]
    looked_up = _registry_transports(registry_deps) if registry_deps else {}

    result: dict[str, EffectiveTransport] = {}
    for dep in deps:
        name = _dep_name(dep)
        if name in result:
            continue
        declared = _declared_transport(dep)
        if _is_registry_dep(dep):
            # Install writes what the registry serves (a declaration only
            # narrows it), so an unresolvable server stays unknown.
            result[name] = looked_up[name]
        elif declared is not None:
            result[name] = frozenset({canonical_transport_name(declared)})
        else:
            result[name] = _project(MCPIntegrator._build_self_defined_info(dep))
    return result
