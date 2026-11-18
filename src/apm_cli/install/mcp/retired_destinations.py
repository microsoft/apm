"""Heal APM-managed MCP entries out of retired project destinations (#3866).

APM <= 0.36 wrote ``copilot`` project MCP servers to ``.github/mcp.json`` and
the removed ``vscode`` target's servers to ``.vscode/mcp.json``. ``copilot``
now writes workspace-root ``.mcp.json``
(:mod:`apm_cli.adapters.client.copilot_project`), so the next project install
removes the entries APM wrote to those files:

* Only names the lockfile records for that runtime are candidates.
* A candidate is removed only with proof APM wrote it unchanged: its recorded
  write fingerprint matches, or it equals the render that release wrote for
  the same self-defined declaration. Anything else (a user-authored or
  user-edited entry) stays where it is and is reported once.
* Other keys and the file itself are kept. A file is deleted only when APM
  created it: every server it held was removed by this heal and nothing but
  empty scaffolding (VS Code's ``inputs: []``) remains in it.

The healed names leave that runtime's recorded ownership so the ``.mcp.json``
write claims them afresh (``vscode`` ownership otherwise maps to ``copilot``
through :func:`~apm_cli.install.mcp.ownership.migrate_legacy_project_target_servers`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetiredMcpDestination:
    """One project MCP file APM no longer writes."""

    runtime: str
    relative_path: str
    servers_key: str
    adapter: Callable[[Path], Any]


def _legacy_copilot_adapter(project_root: Path) -> Any:
    from apm_cli.adapters.client.copilot import CopilotClientAdapter

    return CopilotClientAdapter(project_root=project_root, user_scope=False)


def _legacy_vscode_adapter(project_root: Path) -> Any:
    from apm_cli.adapters.client.vscode import VSCodeClientAdapter

    return VSCodeClientAdapter(project_root=project_root, user_scope=False)


RETIRED_PROJECT_MCP_DESTINATIONS: tuple[RetiredMcpDestination, ...] = (
    RetiredMcpDestination(
        "copilot",
        ".github/mcp.json",  # architecture-authority-exempt: retired MCP destination
        "mcpServers",
        _legacy_copilot_adapter,
    ),
    RetiredMcpDestination(
        "vscode",
        ".vscode/mcp.json",  # architecture-authority-exempt: retired MCP destination
        "servers",
        _legacy_vscode_adapter,
    ),
)


# The first release whose lockfile ownership for ``copilot`` means ``.mcp.json``.
_FIRST_ROOT_MCP_JSON_RELEASE = (0, 37)


def lock_predates_root_mcp_json(apm_version: str | None) -> bool:
    """Whether a lockfile was written when ``copilot`` meant ``.github/mcp.json``.

    Only such lockfiles name entries in the retired files; a newer lock's
    ``copilot`` ownership refers to ``.mcp.json`` and never heals anything.
    """
    if not apm_version:
        return True
    from packaging.version import InvalidVersion, Version

    try:
        release = Version(apm_version).release
    except InvalidVersion:
        return False
    return tuple(release[:2]) < _FIRST_ROOT_MCP_JSON_RELEASE


@dataclass
class RetiredMcpHeal:
    """What one heal pass removed and kept, per runtime."""

    removed: dict[str, set[str]]
    kept: dict[str, set[str]]
    deleted_files: list[str]


def _written_by_apm(
    client: Any,
    name: str,
    native: object,
    *,
    stored_config: dict | None,
    native_hash: str | None,
) -> bool:
    from apm_cli.install.mcp.ownership import mcp_native_hash

    if not isinstance(native, dict):
        return False
    if native_hash is not None:
        return mcp_native_hash(client.managed_server_config(native)) == native_hash
    if not isinstance(stored_config, dict) or stored_config.get("registry") is not False:
        return False  # a registry entry without a fingerprint cannot be proven
    try:
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        from apm_cli.models.dependency.mcp import MCPDependency

        info = MCPIntegrator._build_self_defined_info(MCPDependency.from_dict(stored_config))
        renders = (client.render_server_config(info), *client.legacy_managed_renders(info))
    except (TypeError, ValueError):
        return False
    return any(client.matches_managed_server_config(native, render) for render in renders)


def heal_retired_project_mcp_destinations(
    owned: Mapping[str, set[str]],
    *,
    stored_configs: Mapping[str, dict],
    native_hashes: Mapping[str, Mapping[str, str]],
    project_root: Path,
) -> RetiredMcpHeal:
    """Remove APM-written entries from retired project MCP files (never user entries)."""
    from apm_cli.adapters.client.base import MCPClientAdapter
    from apm_cli.utils.atomic_io import atomic_write_text

    heal = RetiredMcpHeal(removed={}, kept={}, deleted_files=[])
    for destination in RETIRED_PROJECT_MCP_DESTINATIONS:
        names = set(owned.get(destination.runtime, ()))
        path = project_root / destination.relative_path
        if not names or path.is_symlink() or not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, UnicodeError, ValueError):
            _log.debug("Skipping unreadable retired MCP file %s", path, exc_info=True)
            continue
        servers = data.get(destination.servers_key) if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            continue
        client = destination.adapter(project_root)
        hashes = native_hashes.get(destination.runtime, {})
        total = len(servers)
        for name in sorted(names):
            stored = stored_configs.get(name)
            info = (
                {"name": name, "is_self_defined": True}
                if isinstance(stored, dict) and stored.get("registry") is False
                else None
            )
            key = next(
                (
                    candidate
                    for candidate in (name, MCPClientAdapter.determine_config_key(name, None, info))
                    if candidate in servers
                ),
                None,
            )
            if key is None:
                continue
            if _written_by_apm(
                client,
                name,
                servers[key],
                stored_config=stored,
                native_hash=hashes.get(name),
            ):
                del servers[key]
                heal.removed.setdefault(destination.runtime, set()).add(name)
            else:
                heal.kept.setdefault(destination.runtime, set()).add(name)
        removed_here = total - len(servers)
        if not removed_here:
            continue
        scaffolding_only = all(
            value in ([], {}) for key, value in data.items() if key != destination.servers_key
        )
        if not servers and removed_here == total and scaffolding_only:
            path.unlink()
            heal.deleted_files.append(destination.relative_path)
            continue
        text = json.dumps(data, indent=2)
        if raw.endswith("\n"):
            text += "\n"
        atomic_write_text(path, text)
    return heal


def apply_retired_mcp_heal(
    heal: RetiredMcpHeal,
    target_servers: dict[str, set[str]],
    native_hashes: dict[str, dict[str, str]],
) -> None:
    """Drop healed names from the runtime that owned them in the retired file."""
    for runtime, names in heal.removed.items():
        if runtime in target_servers:
            remaining = set(target_servers[runtime]) - names
            if remaining:
                target_servers[runtime] = remaining
            else:
                del target_servers[runtime]
        hashes = native_hashes.get(runtime)
        if hashes:
            for name in names:
                hashes.pop(name, None)


def report_retired_mcp_heal(heal: RetiredMcpHeal, logger: Any) -> None:
    """One line per healed or kept server, plus each file APM deleted."""
    labels = {d.runtime: d.relative_path for d in RETIRED_PROJECT_MCP_DESTINATIONS}
    for runtime, names in sorted(heal.removed.items()):
        for name in sorted(names):
            logger.progress(
                f"Removed APM-managed MCP server '{name}' from {labels[runtime]}; "
                "Copilot project MCP servers now live in .mcp.json"
            )
    for path in heal.deleted_files:
        logger.progress(f"Removed empty {path} (APM created it and no longer writes it)")
    for runtime, names in sorted(heal.kept.items()):
        for name in sorted(names):
            logger.warning(
                f"Kept MCP server '{name}' in {labels[runtime]} because it was changed "
                "after APM wrote it. APM now writes Copilot servers to .mcp.json; move it "
                "there or delete it."
            )
