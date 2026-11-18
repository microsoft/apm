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
    from apm_cli.utils.atomic_io import atomic_write_text

    heal = RetiredMcpHeal(removed={}, kept={}, deleted_files=[])
    for destination in RETIRED_PROJECT_MCP_DESTINATIONS:
        names = set(owned.get(destination.runtime, ()))
        found = _read_retired(destination, project_root) if names else None
        if found is None:
            continue
        path, raw, data, servers = found
        client = destination.adapter(project_root)
        hashes = native_hashes.get(destination.runtime, {})
        total = len(servers)
        for name in sorted(names):
            stored = stored_configs.get(name)
            key = _retired_key(name, stored, servers)
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


def _read_retired(
    destination: RetiredMcpDestination, project_root: Path
) -> tuple[Path, str, dict, dict] | None:
    """The retired file's path, text, document and server map, or None when unusable."""
    from apm_cli.utils.path_security import has_symlink_component

    path = project_root / destination.relative_path
    # A linked file or parent directory may point outside the project.
    if has_symlink_component(project_root, path) or not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        _log.debug("Skipping unreadable retired MCP file %s", path, exc_info=True)
        return None
    servers = data.get(destination.servers_key) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return None
    return path, raw, data, servers


def _retired_key(name: str, stored: object, servers: Mapping[str, Any]) -> str | None:
    from apm_cli.adapters.client.base import MCPClientAdapter

    info = (
        {"name": name, "is_self_defined": True}
        if isinstance(stored, dict) and stored.get("registry") is False
        else None
    )
    return next(
        (
            candidate
            for candidate in (name, MCPClientAdapter.determine_config_key(name, None, info))
            if candidate in servers
        ),
        None,
    )


def _retired_entry_hashes(
    names: Mapping[str, set[str]],
    *,
    project_root: Path,
    stored_configs: Mapping[str, Any],
    proof: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    """Fingerprint each named entry found in its retired file.

    With *proof*, only entries APM provably wrote are fingerprinted (the
    recorded fingerprint, or that release's render of the same declaration).
    """
    from apm_cli.install.mcp.ownership import mcp_native_hash

    found_hashes: dict[str, dict[str, str]] = {}
    for destination in RETIRED_PROJECT_MCP_DESTINATIONS:
        wanted = names.get(destination.runtime) or set()
        found = _read_retired(destination, project_root) if wanted else None
        if found is None:
            continue
        servers = found[3]
        client = destination.adapter(project_root)
        for name in wanted:
            stored = stored_configs.get(name)
            key = _retired_key(name, stored, servers)
            if key is None or not isinstance(servers[key], dict):
                continue
            if proof is not None and not _written_by_apm(
                client,
                name,
                servers[key],
                stored_config=stored,
                native_hash=proof.get(destination.runtime, {}).get(name),
            ):
                continue
            found_hashes.setdefault(destination.runtime, {})[name] = mcp_native_hash(
                client.managed_server_config(servers[key])
            )
    return found_hashes


@dataclass
class RetiredMcpClaims:
    """Lock MCP claims that name APM-written entries in the retired project files."""

    names: dict[str, set[str]]
    hashes: dict[str, dict[str, str]]


def split_mcp_claims(
    target_servers: dict[str, Any],
    native_hashes: dict[str, dict[str, str]],
    *,
    apm_version: str | None,
    project_root: Path,
    user_scope: bool,
    stored_configs: Mapping[str, Any] | None = None,
) -> RetiredMcpClaims:
    """Split the retired-destination claims off a lock's MCP ownership (the one reader).

    Every reader of ``mcp_target_servers`` calls this before acting on a claim,
    so a claim that names a retired file is never treated as live ``.mcp.json``
    ownership. *target_servers* and *native_hashes* are reduced in place to the
    live claims; the returned claims (keyed by retired runtime, with their
    recorded fingerprints as proof) are for the heal alone.

    A ``copilot`` claim names a retired file when its recorded fingerprint
    matches that file's entry (how :func:`restore_pending_retired_claims`
    keeps an unhealed claim); a name whose fingerprint matches its
    ``.mcp.json`` entry is settled and live. A lock written before 0.37 is
    also translated by version: its ``vscode`` claims and its other unsettled
    ``copilot`` claims name the retired files.
    """
    if user_scope:
        return RetiredMcpClaims({}, {})
    stored_configs = stored_configs or {}
    copilot = target_servers.get("copilot")
    recorded = dict(native_hashes.get("copilot") or {})
    vscode = set(target_servers.pop("vscode", None) or ())
    vscode_hashes = native_hashes.pop("vscode", None) or {}
    names: dict[str, set[str]] = {"copilot": set(), "vscode": vscode}
    hashes: dict[str, dict[str, str]] = {
        "copilot": {},
        "vscode": {name: vscode_hashes[name] for name in vscode if name in vscode_hashes},
    }
    candidates = set(copilot or ()) - _settled_in_root_mcp_json(
        recorded, project_root, stored_configs
    )
    found = _retired_entry_hashes(
        {"copilot": candidates, "vscode": candidates},
        project_root=project_root,
        stored_configs=stored_configs,
    )
    raw = lock_predates_root_mcp_json(apm_version)
    for name in candidates:
        runtime = next(
            (
                runtime
                for runtime in ("copilot", "vscode")
                if name in recorded and found.get(runtime, {}).get(name) == recorded[name]
            ),
            "copilot" if raw else None,
        )
        if runtime is not None:
            names[runtime].add(name)
    for name in names["copilot"] | (names["vscode"] & candidates):
        runtime = "copilot" if name in names["copilot"] else "vscode"
        if name in recorded:
            hashes[runtime][name] = recorded[name]
    moved = names["copilot"] | (names["vscode"] & candidates)
    if copilot and moved:
        target_servers["copilot"] = type(copilot)(name for name in copilot if name not in moved)
        for name in moved:
            native_hashes.get("copilot", {}).pop(name, None)
    return RetiredMcpClaims(
        {runtime: claimed for runtime, claimed in names.items() if claimed},
        {runtime: proof for runtime, proof in hashes.items() if proof},
    )


def restore_pending_retired_claims(
    retired: RetiredMcpClaims,
    healed: Mapping[str, set[str]],
    *,
    target_servers: dict[str, set[str]],
    native_hashes: dict[str, dict[str, str]],
    project_root: Path,
    stored_configs: Mapping[str, Any],
) -> None:
    """Keep the retired claims this run did not heal, for a later heal.

    Only an entry APM provably wrote is kept. It is recorded under ``copilot``
    (no lock records ``vscode``) with the fingerprint of the entry as found,
    which is how :func:`split_mcp_claims` later tells the file it names. A name
    the lock already claims for ``.mcp.json`` keeps that claim.
    """
    left = {
        runtime: set(names) - set(healed.get(runtime, ()))
        for runtime, names in retired.names.items()
    }
    proven = _retired_entry_hashes(
        left, project_root=project_root, stored_configs=stored_configs, proof=retired.hashes
    )
    for runtime in ("copilot", "vscode"):
        for name in sorted(left.get(runtime, ())):
            fingerprint = proven.get(runtime, {}).get(name)
            claims = target_servers.get("copilot", set())
            if fingerprint is None or name in claims:
                continue
            target_servers.setdefault("copilot", set()).add(name)
            native_hashes.setdefault("copilot", {})[name] = fingerprint


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
    if logger is None:
        return
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
