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
* Other keys and the file itself are kept. ``.vscode/mcp.json`` is deleted
  only when APM created it: every server it held was removed by this heal and
  nothing but empty scaffolding (VS Code's ``inputs: []``) remains in it.
  ``.github/mcp.json`` is never deleted (it is a target-detection signal).

The heal runs after the install wrote ``.mcp.json``, so an entry leaves its
retired file only once its replacement exists (:func:`healable_names`).
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
    """One project MCP file APM no longer writes.

    ``deletable``: whether the heal may delete the file once APM's entries are
    gone. ``.github/mcp.json`` is kept, because it is a ``copilot``
    target-detection signal for projects that declare no targets.
    """

    runtime: str
    relative_path: str
    servers_key: str
    adapter: Callable[[Path], Any]
    deletable: bool


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
        deletable=False,
    ),
    RetiredMcpDestination(
        "vscode",
        ".vscode/mcp.json",  # architecture-authority-exempt: retired MCP destination
        "servers",
        _legacy_vscode_adapter,
        deletable=True,
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
    from apm_cli.utils.path_security import has_symlink_component

    heal = RetiredMcpHeal(removed={}, kept={}, deleted_files=[])
    for destination in RETIRED_PROJECT_MCP_DESTINATIONS:
        names = set(owned.get(destination.runtime, ()))
        path = project_root / destination.relative_path
        # A linked file or parent directory may point outside the project.
        if not names or has_symlink_component(project_root, path) or not path.is_file():
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
        if destination.deletable and not servers and removed_here == total and scaffolding_only:
            path.unlink()
            heal.deleted_files.append(destination.relative_path)
            continue
        text = json.dumps(data, indent=2)
        if raw.endswith("\n"):
            text += "\n"
        atomic_write_text(path, text)
    return heal


def take_retired_ownership(
    target_servers: dict[str, set[str]],
    *,
    native_hashes: Mapping[str, Mapping[str, str]],
    project_root: Path,
    live_hashes: dict[str, dict[str, str]] | None = None,
    stored_configs: Mapping[str, Any] | None = None,
) -> dict[str, set[str]]:
    """Split the ownership that names entries in retired destinations off *target_servers*.

    ``copilot`` stays (its claims now mean ``.mcp.json``; the copy is the
    heal's view of ``.github/mcp.json``). ``vscode`` is removed outright: its
    only destination is retired, so no cleanup may reach ``.vscode/mcp.json``
    except the proof-checked heal. A name whose recorded ``copilot``
    fingerprint already matches its ``.mcp.json`` entry was moved by an
    earlier install and is settled, so the heal never revisits it. An
    unsettled name's ``copilot`` fingerprint describes its ``.github/mcp.json``
    entry, so it is dropped from *live_hashes* (the heal keeps its snapshot as
    proof) and the ``.mcp.json`` comparison does not read that entry as edited.
    """
    settled = _settled_in_root_mcp_json(
        native_hashes.get("copilot", {}), project_root, stored_configs or {}
    )
    retired = {
        destination.runtime: set(target_servers.get(destination.runtime, ())) - settled
        for destination in RETIRED_PROJECT_MCP_DESTINATIONS
        if target_servers.get(destination.runtime)
    }
    target_servers.pop("vscode", None)
    for name in retired.get("copilot", ()):
        (live_hashes or {}).get("copilot", {}).pop(name, None)
    return {runtime: names for runtime, names in retired.items() if names}


def _settled_in_root_mcp_json(
    hashes: Mapping[str, str], project_root: Path, stored_configs: Mapping[str, Any]
) -> set[str]:
    """Names whose recorded ``copilot`` fingerprint matches their ``.mcp.json`` entry.

    Lock names are registry names; the entry sits under the adapter's native key.
    """
    if not hashes:
        return set()
    from apm_cli.adapters.client.copilot_project import CopilotProjectClientAdapter
    from apm_cli.install.mcp.ownership import _native_config_key, mcp_native_hash
    from apm_cli.utils.path_security import has_symlink_component

    client = CopilotProjectClientAdapter(project_root=project_root)
    path = Path(client.get_config_path())
    if has_symlink_component(project_root, path) or not path.is_file():
        return set()
    servers = client.get_current_config().get(client.mcp_servers_key) or {}
    settled = set()
    for name, recorded in hashes.items():
        key = _native_config_key(client, name, stored_config=stored_configs.get(name))
        entry = servers.get(name, servers.get(key))
        if (
            isinstance(entry, dict)
            and mcp_native_hash(client.managed_server_config(entry)) == recorded
        ):
            settled.add(name)
    return settled


def healable_names(
    retired: dict[str, set[str]],
    *,
    declared: set[str],
    copilot_claims: set[str] | None,
) -> dict[str, set[str]]:
    """Names whose retired entries may go once this install has settled.

    An entry moves only after Copilot owns its replacement in ``.mcp.json``;
    an entry no longer declared, or left behind because ``copilot`` is no
    longer selected, is simply stale. A declared name Copilot failed to write
    keeps its old entry until a later install succeeds.
    """
    return {
        runtime: {
            name
            for name in names
            if copilot_claims is None or name not in declared or name in copilot_claims
        }
        for runtime, names in retired.items()
    }


def report_retired_mcp_heal(heal: RetiredMcpHeal, logger: Any) -> None:
    """One line per healed or kept server, plus each file APM deleted."""
    labels = {d.runtime: d.relative_path for d in RETIRED_PROJECT_MCP_DESTINATIONS}
    for runtime, names in sorted(heal.removed.items()):
        for name in sorted(names):
            logger.progress(f"Removed APM-managed MCP server '{name}' from {labels[runtime]}")
    for path in heal.deleted_files:
        logger.progress(f"Removed empty {path} (APM created it and no longer writes it)")
    for runtime, names in sorted(heal.kept.items()):
        for name in sorted(names):
            logger.warning(
                f"Kept MCP server '{name}' in {labels[runtime]} because it was changed "
                f"after APM wrote it. Copilot now uses the '{name}' in .mcp.json, which "
                "APM writes from apm.yml; put your changes in apm.yml and delete this entry."
            )
    if heal.removed:
        logger.progress(
            "If those files were committed with resolved credentials, rotate them: "
            "removing the entries does not erase them from Git history."
        )
