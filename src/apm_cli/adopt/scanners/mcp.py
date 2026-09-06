"""Scanner for MCP server definitions in each client's native config.

Paths and document shapes come from the MCP client adapters so discovery
never hardcodes a client config location.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from apm_cli.factory import ClientFactory
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

from ..model import HarnessKind, RawFinding, Risk, ScanError, Scope
from ..registry import ScanContext

# Runtimes whose adapters only ever write machine-global configuration.
_USER_ONLY_RUNTIMES = frozenset({"intellij", "windsurf", "hermes"})


def server_configs_from_document(document: Any, key: str) -> dict[str, Mapping[str, Any]]:
    """Return ``{name: config}`` from a parsed client document.

    Codex TOML may surface ``mcp_servers."name"`` as flat dotted keys; both the
    nested and the flat spelling are normalised here.
    """
    if not isinstance(document, Mapping):
        return {}
    servers: dict[str, Mapping[str, Any]] = {}
    nested = document.get(key)
    if isinstance(nested, Mapping):
        for name, config in nested.items():
            if isinstance(config, Mapping):
                servers[str(name)] = config
    prefix = f"{key}."
    for raw_key, config in document.items():
        if isinstance(raw_key, str) and raw_key.startswith(prefix) and isinstance(config, Mapping):
            servers.setdefault(raw_key[len(prefix) :].strip('"'), config)
    return servers


def _risk_for(config: Mapping[str, Any]) -> frozenset[Risk]:
    risks: set[Risk] = set()
    if any(config.get(k) for k in ("url", "httpUrl", "serverUrl")):
        risks.add(Risk.NETWORK)
    if config.get("command"):
        risks.add(Risk.EXECUTES_CODE)
    if not risks:
        risks.add(Risk.NETWORK)
    return frozenset(risks)


class McpScanner:
    """Enumerate MCP servers per client via the adapter registry."""

    name = "mcp"

    def scan(self, ctx: ScanContext) -> Iterable[RawFinding]:
        user_scope = ctx.scope is Scope.USER
        for runtime in sorted(ClientFactory.supported_clients()):
            if not user_scope and runtime in _USER_ONLY_RUNTIMES:
                continue
            try:
                adapter = ClientFactory.create_client(
                    runtime, project_root=ctx.root, user_scope=user_scope
                )
                config_path = Path(str(adapter.get_config_path()))
            except Exception as exc:  # adapter construction depends on env/tooling
                ctx.errors.append(
                    ScanError(f"<{runtime} mcp config>", f"adapter error: {type(exc).__name__}")
                )
                continue
            if not user_scope:
                try:
                    ensure_path_within(config_path.resolve(strict=False), ctx.root)
                except PathTraversalError:
                    continue
            if not config_path.is_file():
                continue
            try:
                document = adapter.get_current_config()
            except Exception as exc:  # malformed on-disk config must not abort discovery
                ctx.error(config_path, f"unreadable MCP config: {type(exc).__name__}")
                continue
            servers = server_configs_from_document(document, adapter.mcp_servers_key or "")
            if not servers:
                continue
            display = ctx.display(config_path)
            for name in sorted(servers):
                config = servers[name]
                yield RawFinding(
                    tool=runtime,
                    scope=ctx.scope,
                    kind=HarnessKind.MCP_SERVER,
                    display_path=f"{display}#{name}",
                    abs_path=None,
                    format_id=f"{runtime}_mcp",
                    primitive="mcp",
                    risk=_risk_for(config),
                    evidence=(f"mcp-config:{display}",),
                    payload={
                        "tool": runtime,
                        "name": name,
                        "config": dict(config),
                        "config_path": display,
                    },
                )
