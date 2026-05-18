"""Local bundle MCP helpers extracted from local_bundle_handler.py."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class _WireOpts:
    """Bundled MCP-wiring options for :func:`_wire_bundle_mcp_servers`."""

    user_scope: bool
    verbose: bool
    logger: Any


def _parse_bundle_mcp_servers(bundle_dir: Path) -> list:
    """Parse ``<bundle>/.mcp.json`` (case-insensitive) into a list of
    self-defined :class:`MCPDependency` entries.

    Returns an empty list when the file is missing, malformed, or has no
    ``mcpServers`` map.  Per-server parsing errors are logged at debug
    level and the offending entry is dropped so a single bad entry does
    not block the rest of the bundle's MCP wiring.
    """
    from apm_cli.models.dependency.mcp import MCPDependency

    # Case-insensitive lookup mirrors the rest of the bundle metadata
    # filtering (HFS+/NTFS case folding).
    mcp_path: Path | None = None
    for entry in bundle_dir.iterdir() if bundle_dir.is_dir() else []:
        if entry.is_file() and not entry.is_symlink() and entry.name.lower() == ".mcp.json":
            mcp_path = entry
            break
    if mcp_path is None:
        return []

    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return []

    out: list[MCPDependency] = []
    for name, cfg in servers.items():
        if not isinstance(name, str) or not isinstance(cfg, dict):
            continue
        # Anthropic plugin .mcp.json schema -> MCPDependency self-defined:
        # ``type`` aliases ``transport`` (handled by MCPDependency.from_dict).
        spec = dict(cfg)
        spec["name"] = name
        spec["registry"] = False
        try:
            out.append(MCPDependency.from_dict(spec))
        except (ValueError, TypeError):
            # Per-server parse failure: skip and continue.
            continue
    return out


def _wire_bundle_mcp_servers(
    *,
    bundle_dir: Path,
    targets,
    project_root: Path,
    wire_opts: _WireOpts,
) -> int:
    """Wire bundle ``.mcp.json`` servers through ``MCPIntegrator.install``.

    Returns the count of newly configured/updated MCP servers across all
    resolved targets.  The function is best-effort: any per-target failure
    is logged and the remaining targets continue to be processed.
    """
    user_scope = wire_opts.user_scope
    verbose = wire_opts.verbose
    logger = wire_opts.logger
    deps = _parse_bundle_mcp_servers(bundle_dir)
    if not deps:
        return 0

    from apm_cli.integration.mcp_integrator import MCPIntegrator

    target_names = [t.name for t in targets]
    apm_config = {"targets": target_names, "scripts": {}}
    try:
        count = MCPIntegrator.install(
            deps,
            verbose=verbose,
            apm_config=apm_config,
            project_root=project_root,
            user_scope=user_scope,
            explicit_target=target_names,
            logger=logger,
        )
    except Exception as exc:
        logger.warning(
            f"Bundle .mcp.json present but MCP wiring failed: {exc}. "
            "Copy the entries into your project's apm.yml mcp_dependencies "
            "and re-run 'apm install' to register them."
        )
        return 0

    if count:
        joined = ", ".join(target_names)
        logger.success(f"Wired {count} MCP server(s) from bundle .mcp.json (target(s): {joined})")
    elif deps:
        # Bundle declared servers but none applied (e.g. resolved targets
        # all gated out, or all servers already configured).  Emit an info
        # line so users have a paper-trail.
        joined = ", ".join(target_names)
        logger.info(
            f"Bundle .mcp.json declared {len(deps)} server(s); "
            f"no new MCP config changes for target(s): {joined}"
        )
    return count
