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
* Other keys and the file itself are kept. A retired file is deleted only
  when APM created it: every server it held was removed by this heal and
  nothing but empty scaffolding (VS Code's ``inputs: []``) remains in it.
  ``.github/mcp.json`` is also a ``copilot`` target-detection signal, so it is
  deleted only when ``copilot`` stays selected without it: ``apm.yml``
  declares it, or another ``copilot`` signal remains (#4524). Its presence is
  also the evidence that a ``--frozen`` install moved a pre-0.37 claim (the
  lock is not restamped), so ``--frozen`` keeps it emptied and the next
  install, which records the move, deletes it.

The heal runs after the install wrote ``.mcp.json``, so an entry leaves its
retired file only once its replacement exists (:func:`healable_names`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetiredMcpDestination:
    """One project MCP file APM no longer writes.

    ``deletable``: whether the heal may delete the file once APM's entries are
    gone. ``detection_signal``: the file also selects ``runtime`` for
    projects that declare no targets, so it is deleted only while that
    runtime stays selected without it, and never by a ``--frozen`` heal,
    whose unrestamped lock still needs the file as proof of the move.
    """

    runtime: str
    relative_path: str
    servers_key: str
    adapter: Callable[[Path], Any]
    deletable: bool
    detection_signal: bool = False


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
        deletable=True,
        detection_signal=True,
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
# PEP 440 public version (optional ``v`` and epoch) plus local label, as
# recorded in ``apm_version``; only the leading release numbers are read.
_PEP440_RELEASE_PREFIX = re.compile(
    r"^v?(?:\d+!)?(\d+)(?:\.(\d+))?(?:\.\d+)*"
    r"(?:[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?\d*)?"
    r"(?:-\d+|[-_.]?(?:post|rev|r)[-_.]?\d*)?"
    r"(?:[-_.]?dev[-_.]?\d*)?"
    r"(?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?$",
    re.IGNORECASE,
)


def lock_predates_root_mcp_json(apm_version: str | None) -> bool:
    """Whether a lockfile was written when ``copilot`` meant ``.github/mcp.json``.

    Only such lockfiles name entries in the retired files; a newer lock's
    ``copilot`` ownership refers to ``.mcp.json`` and never heals anything.
    A stamp that names no release (missing, empty, or ``unknown``) proves no
    newer meaning, so it heals like a known pre-0.37 version (#4169).
    """
    if not isinstance(apm_version, str) or not apm_version:
        return True
    # Stdlib only: ``packaging`` is not a runtime dependency (#3908).
    match = _PEP440_RELEASE_PREFIX.match(apm_version.strip())
    if match is None:
        return True
    release = (int(match.group(1)), int(match.group(2) or 0))
    return release < _FIRST_ROOT_MCP_JSON_RELEASE


def lock_holds_untranslated_mcp_claims(
    apm_version: str | None, target_servers: Mapping[str, Any]
) -> bool:
    """Whether a lock still records ``copilot``/``vscode`` claims in their pre-0.37 meaning.

    Its ``apm_version`` is the only record of that meaning, so a writer that
    carries these claims forward keeps it; the MCP phase stamps the running
    version once :func:`split_mcp_claims` has translated them.
    """
    return lock_predates_root_mcp_json(apm_version) and any(
        target_servers.get(runtime) for runtime in ("copilot", "vscode")
    )


def _selected_without(destination: RetiredMcpDestination, project_root: Path) -> bool:
    """Whether ``destination.runtime`` stays selected once its retired file is gone."""
    from apm_cli.core.scope import manifest_file
    from apm_cli.core.target_detection import detect_signals
    from apm_cli.models.manifest_targets import read_manifest_target_declaration

    declaration = read_manifest_target_declaration(manifest_file(project_root), lenient=True)
    from apm_cli.core.errors import TargetResolutionError

    try:
        declared = declaration.restriction() if declaration is not None else None
    except (TargetResolutionError, ValueError):
        declared = None  # an unreadable declaration keeps the signal file
    if declared and destination.runtime in declared:
        return True
    return any(
        signal.target == destination.runtime and signal.source != destination.relative_path
        for signal in detect_signals(project_root)
    )


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
    keep_signal_files: bool,
) -> RetiredMcpHeal:
    """Remove APM-written entries from retired project MCP files (never user entries).

    A retired file left with nothing but scaffolding is deleted when APM
    created it: this heal removed every server it held, or it holds none and
    the lock claims names there (an earlier ``--frozen`` heal emptied it).
    ``keep_signal_files`` keeps such a file when it is also a target
    detection signal; ``run_mcp_integration`` sets it for a ``--frozen``
    replay from ``InstallService.frozen_disposition``.
    """
    from apm_cli.integration.config_file_provenance import (
        delete_emptied_config,
        recorded_config_write,
    )
    from apm_cli.utils.json_config import write_json_config

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
        scaffolding_only = all(
            value in ([], {}) for key, value in data.items() if key != destination.servers_key
        )
        emptied = (
            destination.deletable
            and not servers
            and removed_here == total
            and scaffolding_only
            and not (destination.detection_signal and keep_signal_files)
            and (not destination.detection_signal or _selected_without(destination, project_root))
        )
        if delete_emptied_config(path, root=project_root, empty=emptied):
            heal.deleted_files.append(destination.relative_path)
            continue
        if not removed_here:
            continue
        with recorded_config_write(path):
            write_json_config(path, data, default_trailing_newline=raw.endswith("\n"))
    return heal


@dataclass(frozen=True)
class RetiredMcpPreview:
    """Retired file -> APM-written servers the next project install takes out of it.

    ``moves``: servers Copilot rewrites at ``.mcp.json``. ``removals``: servers
    no longer declared, or no longer written there (another target, or Copilot
    not selected). A declared server Copilot does not receive stays put and is
    in neither.
    """

    moves: dict[str, list[str]] = field(default_factory=dict)
    removals: dict[str, list[str]] = field(default_factory=dict)


def preview_retired_mcp_moves(
    lockfile: Any,
    project_root: Path,
    *,
    declared: Collection[Any] = (),
    target_runtimes: Collection[str] | None = None,
) -> RetiredMcpPreview:
    """Preview what the next project install heals out of the retired files.

    Read-only preview for ``install --dry-run`` (#3967). It reads the claims
    through :func:`split_mcp_claims`, applies the heal's own proof and splits
    the result with :func:`healable_names`, the install's own rule, given the
    *declared* MCP dependencies and the MCP *target_runtimes* the install
    would select.
    """
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.integration.mcp_integrator_install import mcp_servers_for_runtime

    target_servers = {runtime: set(names) for runtime, names in lockfile.mcp_target_servers.items()}
    claims = split_mcp_claims(
        target_servers,
        DeploymentLedgerCodec.mcp_native_hashes(lockfile),
        apm_version=lockfile.apm_version,
        project_root=project_root,
        user_scope=False,
        stored_configs=lockfile.mcp_configs,
    )
    provable: dict[str, set[str]] = {}
    for destination in RETIRED_PROJECT_MCP_DESTINATIONS:
        names = claims.names.get(destination.runtime)
        found = _read_retired(destination, project_root) if names else None
        if found is None:
            continue
        servers = found[3]
        client = destination.adapter(project_root)
        hashes = claims.hashes.get(destination.runtime, {})
        for name in names:
            stored = lockfile.mcp_configs.get(name)
            key = _retired_key(name, stored, servers)
            if key is not None and _written_by_apm(
                client, name, servers[key], stored_config=stored, native_hash=hashes.get(name)
            ):
                provable.setdefault(destination.runtime, set()).add(name)
    declared_deps = list(declared)
    copilot_claims = (
        mcp_servers_for_runtime(declared_deps, "copilot")
        if "copilot" in (target_runtimes or ())
        else None
    )
    healable = healable_names(
        provable,
        declared=set(MCPIntegrator.get_server_names(declared_deps)),
        copilot_claims=copilot_claims,
    )
    preview = RetiredMcpPreview()
    for runtime, names in healable.items():
        retired = _RETIRED_PATHS[runtime]
        for name in sorted(names):
            moved = copilot_claims is not None and name in copilot_claims
            bucket = preview.moves if moved else preview.removals
            bucket.setdefault(retired, []).append(name)
    return preview


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


def _retired_entries(
    names: Mapping[str, set[str]],
    *,
    project_root: Path,
    stored_configs: Mapping[str, Any],
    proof: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, dict[str, dict]]:
    """The comparable form of each named entry found in its retired file.

    With *proof*, only entries APM provably wrote are returned (the recorded
    fingerprint, or that release's render of the same declaration).
    """
    entries: dict[str, dict[str, dict]] = {}
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
            entries.setdefault(destination.runtime, {})[name] = client.managed_server_config(
                servers[key]
            )
    return entries


# Kept claims record a receipt, not a plain fingerprint: a sha256 over this
# domain, the retired file and the entry. It never equals an entry's plain
# fingerprint, so no reader (nor an older APM) mistakes it for proof of a
# .mcp.json write, and it names the file the claim belongs to.
_RETIRED_RECEIPT_DOMAIN = "apm-retired-mcp-receipt:v1"
_RETIRED_PATHS = {d.runtime: d.relative_path for d in RETIRED_PROJECT_MCP_DESTINATIONS}


def _retired_receipt(runtime: str, comparable: dict) -> str:
    encoded = json.dumps(comparable, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    payload = f"{_RETIRED_RECEIPT_DOMAIN}\n{_RETIRED_PATHS[runtime]}\n{encoded}"
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class RetiredMcpClaims:
    """Lock MCP claims that name APM-written entries in the retired project files."""

    names: dict[str, set[str]]
    hashes: dict[str, dict[str, str]]
    # Names a ``--frozen`` install already moved: no retired file holds them,
    # so their root entry may be APM's own unrecorded write.
    moved_unrecorded: set[str] = field(default_factory=set)


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

    A ``copilot`` claim names a retired file when it records that file's
    receipt for the entry there (how :func:`restore_pending_retired_claims`
    keeps an unhealed claim). A lock written before 0.37 is also translated by
    version: its ``vscode`` claims and its ``copilot`` claims not settled in
    ``.mcp.json`` (fingerprint matching the entry there) name the retired
    files. In a newer lock any other claim is live.
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
    # A kept claim's receipt names its retired file; it is matched first, so
    # an identical .mcp.json entry can never make it look settled.
    from apm_cli.install.mcp.ownership import mcp_native_hash

    copilot_names = set(copilot or ())
    entries = _retired_entries(
        {"copilot": copilot_names, "vscode": copilot_names},
        project_root=project_root,
        stored_configs=stored_configs,
    )
    kept: set[str] = set()
    for name in copilot_names:
        for runtime in ("copilot", "vscode"):
            entry = entries.get(runtime, {}).get(name)
            if entry is not None and recorded.get(name) == _retired_receipt(runtime, entry):
                names[runtime].add(name)
                hashes[runtime][name] = mcp_native_hash(entry)
                kept.add(name)
                break
    live = {name: fingerprint for name, fingerprint in recorded.items() if name not in kept}
    candidates = (
        copilot_names - kept - _settled_in_root_mcp_json(live, project_root, stored_configs)
    )
    moved = set(kept)
    moved_unrecorded: set[str] = set()
    if lock_predates_root_mcp_json(apm_version):
        names["copilot"] |= candidates
        hashes["copilot"].update({name: live[name] for name in candidates if name in live})
        moved |= candidates
        # The heal never deletes .github/mcp.json, so a copilot claim counts as
        # moved only while that file is still there.
        github_kept = (project_root / _RETIRED_PATHS["copilot"]).is_file()
        moved_unrecorded = _absent_from_retired_files(
            (candidates if github_kept else set()) | vscode,
            project_root=project_root,
            stored_configs=stored_configs,
        )
    if copilot and moved:
        target_servers["copilot"] = type(copilot)(name for name in copilot if name not in moved)
        for name in moved:
            native_hashes.get("copilot", {}).pop(name, None)
    return RetiredMcpClaims(
        {runtime: claimed for runtime, claimed in names.items() if claimed},
        {runtime: proof for runtime, proof in hashes.items() if proof},
        moved_unrecorded,
    )


def moved_root_entry_fingerprints(
    names: set[str], *, project_root: Path, stored_configs: Mapping[str, Any]
) -> dict[str, str]:
    """Fingerprints of the ``.mcp.json`` entries a ``--frozen`` install moved for *names*.

    For moved, no longer declared self-defined servers
    (:attr:`RetiredMcpClaims.moved_unrecorded`): an entry identical, by JSON
    type and value, to APM's render of the locked declaration is APM's write,
    so it is removed like any proven stale entry. Registry entries cannot be
    re-rendered from the lock and stay where they are.
    """
    if not names:
        return {}
    from apm_cli.adapters.client.copilot_project import CopilotProjectClientAdapter
    from apm_cli.install.mcp.ownership import _native_config_key, mcp_native_hash
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.models.dependency.mcp import MCPDependency
    from apm_cli.utils.path_security import has_symlink_component

    client = CopilotProjectClientAdapter(project_root=project_root)
    path = Path(client.get_config_path())
    if has_symlink_component(project_root, path) or not path.is_file():
        return {}
    servers = client.get_current_config().get(client.mcp_servers_key) or {}
    proven: dict[str, str] = {}
    for name in sorted(names):
        stored = stored_configs.get(name)
        if not isinstance(stored, dict) or stored.get("registry") is not False:
            continue
        entry = servers.get(_native_config_key(client, name, stored_config=stored))
        if not isinstance(entry, dict):
            continue
        try:
            render = client.render_server_config(
                MCPIntegrator._build_self_defined_info(MCPDependency.from_dict(stored))
            )
        except (TypeError, ValueError):
            continue
        comparable = client.managed_server_config(entry)
        if client.json_identical(comparable, render):
            proven[name] = mcp_native_hash(comparable)
    return proven


def _absent_from_retired_files(
    names: set[str], *, project_root: Path, stored_configs: Mapping[str, Any]
) -> set[str]:
    """The *names* neither retired file holds (an unreadable file holds them all)."""
    from apm_cli.utils.path_security import has_symlink_component

    absent = set(names)
    for destination in RETIRED_PROJECT_MCP_DESTINATIONS:
        path = project_root / destination.relative_path
        if not absent or not (path.exists() or path.is_symlink()):
            continue
        found = (
            None
            if has_symlink_component(project_root, path)
            else _read_retired(destination, project_root)
        )
        if found is None:
            return set()
        servers = found[3]
        absent = {
            name for name in absent if _retired_key(name, stored_configs.get(name), servers) is None
        }
    return absent


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
    (no lock records ``vscode``) with the retired receipt of the entry as
    found, which is how :func:`split_mcp_claims` later tells the file it names.
    A name the lock already claims for ``.mcp.json`` keeps that claim; a name
    kept in both retired files keeps only its ``.github/mcp.json`` claim.
    """
    left = {
        runtime: set(names) - set(healed.get(runtime, ()))
        for runtime, names in retired.names.items()
    }
    proven = _retired_entries(
        left, project_root=project_root, stored_configs=stored_configs, proof=retired.hashes
    )
    for runtime in ("copilot", "vscode"):
        for name in sorted(left.get(runtime, ())):
            entry = proven.get(runtime, {}).get(name)
            if entry is None or name in target_servers.get("copilot", set()):
                continue
            target_servers.setdefault("copilot", set()).add(name)
            native_hashes.setdefault("copilot", {})[name] = _retired_receipt(runtime, entry)


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
