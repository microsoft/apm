"""Native MCP ownership baselines, safe cleanup, and legacy migration."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class NativeMcpConnectionState(Enum):
    """Native connection comparison; equality alone never establishes ownership."""

    ABSENT = "absent"
    MATCHING_MANAGED = "matching-managed"
    MATCHING_FOREIGN = "matching-foreign"
    UPDATE_MANAGED = "update-managed"
    CONFLICT = "conflict"


# ``(server, runtime)`` pairs a ``--frozen`` install replays without writing:
# the lockfile records the server but no ownership for that runtime, and the
# runtime's native config has no entry for it (for example a transport the
# harness refuses). Scoped like the frozen lockfile-write suppression.
_frozen_unrecorded_pairs: ContextVar[frozenset[tuple[str, str]]] = ContextVar(
    "apm_frozen_unrecorded_mcp_pairs", default=frozenset()
)


@contextmanager
def frozen_unrecorded_mcp_pairs(pairs: frozenset[tuple[str, str]]) -> Iterator[None]:
    """Make *pairs* skip-only for the MCP stage of one ``--frozen`` install."""
    token = _frozen_unrecorded_pairs.set(frozenset(pairs))
    try:
        yield
    finally:
        _frozen_unrecorded_pairs.reset(token)


def current_frozen_unrecorded_mcp_pairs() -> frozenset[tuple[str, str]]:
    """The pairs the current ``--frozen`` install deferred (empty otherwise)."""
    return _frozen_unrecorded_pairs.get()


def is_frozen_unrecorded_mcp_pair(name: str, runtime: str) -> bool:
    """Whether the current ``--frozen`` install must not configure *name* for *runtime*."""
    return (name, runtime) in _frozen_unrecorded_pairs.get()


_adoptable_root_names: ContextVar[frozenset[str]] = ContextVar(
    "apm_adoptable_root_mcp_names", default=frozenset()
)


@contextmanager
def adoptable_root_mcp_entries(names: frozenset[str]) -> Iterator[None]:
    """Let one install adopt the root ``.mcp.json`` entries of *names* for ``copilot``.

    For a pre-0.37 lock's declared claims that a ``--frozen`` install already
    moved (no retired file holds them, see
    :class:`~apm_cli.install.mcp.retired_destinations.RetiredMcpClaims`), the
    lock could not record that write. An entry equal to the requested render
    is that write; any other entry stays a conflict.
    """
    token = _adoptable_root_names.set(frozenset(names))
    try:
        yield
    finally:
        _adoptable_root_names.reset(token)


def native_comparison_clients(
    target_runtimes: list[str], *, project_root: Any, user_scope: bool
) -> dict[str, Any]:
    """Return the adapters whose existing native entries are compared by content.

    Single owner of which runtimes preserve and report a differing same-name
    connection (#3239) rather than treating a name match as configured (#3681).
    """
    from apm_cli.factory import ClientFactory

    clients = {}
    for runtime in target_runtimes:
        try:
            client = ClientFactory.create_client(
                runtime, project_root=project_root, user_scope=user_scope
            )
        except ValueError:
            continue  # unsupported runtimes are reported by the install itself
        if client.compares_native_connections:
            clients[runtime] = client
    return clients


def mcp_native_hash(native: dict) -> str:
    """Fingerprint a written native entry without retaining its resolved secrets."""
    encoded = json.dumps(native, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _native_config_key(
    client: Any,
    name: str,
    *,
    requested_info: dict | None = None,
    stored_config: dict | None = None,
) -> str:
    """Return the native key for *name*; self-defined servers keep their declared name."""
    info = requested_info
    if info is None and isinstance(stored_config, dict) and stored_config.get("registry") is False:
        info = {"name": name, "is_self_defined": True}
    return client.determine_config_key(name, None, info)


def declarations_share_native_key(
    runtimes: Iterable[str], name: str, first: dict | None, second: dict | None
) -> bool:
    """Whether declarations *first* and *second* key *name* to one native entry on every runtime."""
    from apm_cli.factory import ClientFactory

    for runtime in runtimes:
        adapter = ClientFactory.adapter_class(runtime)
        if adapter is not None and _native_config_key(
            adapter, name, stored_config=first
        ) != _native_config_key(adapter, name, stored_config=second):
            return False
    return True


def written_native_hash(
    client: Any,
    name: str,
    *,
    requested_info: dict | None,
    existing_configs: Mapping[str, Any] | None = None,
) -> str | None:
    """Fingerprint the entry *client* just wrote for *name*, read back from its config.

    Adapters that fingerprint their own render (Claude, Codex) record it in
    ``written_mcp_hashes``; every other adapter is fingerprinted from the same
    native view :func:`compare_native_mcp_connection` reads later. Both hash
    ``client.managed_server_config(native)``, so an adapter overriding it keeps
    fingerprint and comparison consistent.
    """
    from apm_cli.core.conflict_detector import MCPConflictDetector

    if name in client.written_mcp_hashes:
        return client.written_mcp_hashes[name]
    existing = (
        MCPConflictDetector(client).get_existing_server_configs()
        if existing_configs is None
        else existing_configs
    )
    native = existing.get(_native_config_key(client, name, requested_info=requested_info))
    return (
        mcp_native_hash(client.managed_server_config(native)) if isinstance(native, dict) else None
    )


def _requested_plugin_root(requested_info: dict | None) -> str | None:
    """Bind a stored declaration's ``${CLAUDE_PLUGIN_ROOT}`` to the current package root."""
    from apm_cli.deps.plugin_root_render import PLUGIN_ROOT_INFO_KEY

    return (requested_info or {}).get(PLUGIN_ROOT_INFO_KEY)


def retain_native_receipt(
    native_hashes: dict[str, dict[str, str]] | None,
    runtime: str,
    client: Any,
    name: str,
    *,
    requested_info: dict | None,
    replace: bool = False,
) -> None:
    """Keep a fingerprint for a managed entry that already matched, without writing.

    A lock rebuilt without its deployment rows would otherwise lose the receipt
    of an unchanged managed connection on the next install. *replace* refreshes
    a receipt :func:`compare_native_mcp_connection` reported as stale.
    """
    if native_hashes is None or (not replace and name in native_hashes.get(runtime, {})):
        return
    fingerprint = written_native_hash(client, name, requested_info=requested_info)
    if fingerprint is not None:
        native_hashes.setdefault(runtime, {})[name] = fingerprint


def compare_native_mcp_connection(
    client: Any,
    name: str,
    *,
    requested_info: dict | None,
    stored_config: dict | None,
    managed: bool,
    existing_configs: Mapping[str, Any] | None = None,
    managed_fields: set[str] | None = None,
    native_hash: str | None = None,
    registry_request: bool = False,
    env_overrides: dict | None = None,
    runtime_vars: dict | None = None,
    declaration_changed: bool = True,
    stale_receipts: set[str] | None = None,
) -> NativeMcpConnectionState:
    """Compare a connection with its requested and previously recorded native shapes.

    An APM-written entry whose declaration did not change is kept as written on
    adapters that do not refresh environment-derived renders, so a different
    credential source or resolved value never churns the native file. *name*
    is added to *stale_receipts* when the recorded fingerprint predates a
    ``--frozen`` heal, so the caller refreshes the receipt without a write.
    """
    from apm_cli.core.conflict_detector import MCPConflictDetector
    from apm_cli.deps.plugin_root_render import (
        RECORDED_RENDER_INFO_KEY,
        portable_rewrite_equivalent,
    )
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.models.dependency.mcp import MCPDependency

    existing = (
        MCPConflictDetector(client).get_existing_server_configs()
        if existing_configs is None
        else existing_configs
    )
    key = _native_config_key(
        client, name, requested_info=requested_info, stored_config=stored_config
    )
    if key not in existing:
        return NativeMcpConnectionState.ABSENT
    native = existing[key]
    requested = None
    if requested_info is not None:
        requested = (
            client.render_server_config(
                requested_info, env_overrides=env_overrides, runtime_vars=runtime_vars
            )
            if registry_request
            else client.render_server_config(requested_info)
        )
    recorded_info = (requested_info or {}).get(RECORDED_RENDER_INFO_KEY)
    if (
        managed
        and isinstance(recorded_info, dict)
        and isinstance(native, dict)
        and client.matches_managed_server_config(native, client.render_server_config(recorded_info))
    ):
        # --frozen over a pre-#3825 lock: the entry is exactly what that lock
        # recorded, so keep its bytes rather than write the lock-sourced root.
        return NativeMcpConnectionState.MATCHING_MANAGED
    comparison = native
    matches_baseline = False
    retired_shape = False
    adoptable = (
        not managed
        and requested is not None
        and client.target_name == "copilot"
        and name in _adoptable_root_names.get()
    )
    if adoptable and isinstance(native, dict):
        # --frozen wrote this entry from apm.yml, or from the locked declaration
        # before apm.yml changed. Compared by JSON type too: true is not 1.
        renders = [requested]
        if isinstance(stored_config, dict) and stored_config.get("registry") is False:
            try:
                renders.append(
                    client.render_server_config(
                        MCPIntegrator._build_self_defined_info(
                            MCPDependency.from_dict(stored_config)
                        )
                    )
                )
            except (TypeError, ValueError):
                logger.debug("Unusable locked declaration for %s", name)
        comparable = client.managed_server_config(native)
        matches_baseline = any(client.json_identical(comparable, render) for render in renders)
    if managed and isinstance(stored_config, dict):
        try:
            dependency = MCPDependency.from_dict(stored_config)
            legacy_baseline = requested
            baseline_info = requested_info
            render_env = (
                {"env_overrides": env_overrides, "runtime_vars": runtime_vars}
                if registry_request
                else {}
            )
            if dependency.is_self_defined:
                baseline_info = MCPIntegrator._build_self_defined_info(
                    dependency, plugin_root=_requested_plugin_root(requested_info)
                )
                render_env = {}
                expected = client.render_server_config(baseline_info)
                legacy_baseline = expected
                if requested is not None:
                    comparison = client.project_managed_server_config(
                        native, baseline=expected, requested=requested
                    )
                matches_baseline = client.matches_managed_server_config(comparison, expected)
                if (
                    matches_baseline
                    and stale_receipts is not None
                    and native_hash is not None
                    and isinstance(native, dict)
                    and client.matches_managed_server_config(native, expected)
                    and mcp_native_hash(client.managed_server_config(native)) != native_hash
                ):
                    # A --frozen heal rewrote the entry but kept the lock receipt.
                    stale_receipts.add(name)
                if not matches_baseline and native_hash is not None and isinstance(native, dict):
                    # The entry is exactly what APM recorded writing; its render
                    # moved since (a re-pointed skill launcher), so update it.
                    matches_baseline = (
                        mcp_native_hash(client.managed_server_config(native)) == native_hash
                    )
                    if matches_baseline:
                        comparison = native
                if not matches_baseline and isinstance(native, dict):
                    # A pre-#3825 baseline recorded at another clone's root, and
                    # the entry is this project's portable render of it (written
                    # by a --frozen install that could not refresh the receipt).
                    matches_baseline = portable_rewrite_equivalent(
                        expected,
                        client.managed_server_config(native),
                        client._plugin_root_reference(),
                    )
                    if matches_baseline:
                        comparison = native
                if matches_baseline and managed_fields is not None:
                    managed_fields.update(expected)
            elif native_hash is not None and isinstance(native, dict):
                comparable = client.managed_server_config(native)
                matches_baseline = mcp_native_hash(comparable) == native_hash
                if not matches_baseline:
                    # A --frozen heal rewrote the shape but kept the lock receipt.
                    matches_baseline = _healed_since_receipt(
                        client,
                        native,
                        baseline_info,
                        current=legacy_baseline,
                        native_hash=native_hash,
                        render_env=render_env,
                    ) or _moved_since_receipt(
                        client,
                        native,
                        baseline_info,
                        current=legacy_baseline,
                        native_hash=native_hash,
                        render_env=render_env,
                    )
                    if matches_baseline and stale_receipts is not None:
                        stale_receipts.add(name)
                if matches_baseline and managed_fields is not None:
                    managed_fields.update(comparable)
            # An entry an earlier APM release rendered differently (expanded
            # references, values later moved to env_vars) is still managed when
            # the adapter can prove that release wrote it.
            if not matches_baseline and legacy_baseline is not None:
                recorded_write = (
                    None
                    if native_hash is None or not isinstance(native, dict)
                    else mcp_native_hash(client.managed_server_config(native)) == native_hash
                )
                if client.matches_legacy_managed_server_config(
                    native, legacy_baseline, recorded_write=recorded_write
                ):
                    matches_baseline = True
                    if managed_fields is not None:
                        managed_fields.update(client.managed_server_config(native))
            # An APM-written entry in a remote shape an earlier release used
            # (retired_shape) is rewritten even when the declaration is unchanged.
            retired_shape = _in_retired_shape(
                client,
                native,
                baseline_info,
                current=legacy_baseline,
                fingerprint=None
                if native_hash is None or not isinstance(native, dict)
                else mcp_native_hash(client.managed_server_config(native)) == native_hash,
                render_env=render_env,
            )
            if retired_shape and not matches_baseline:
                matches_baseline = True
                if managed_fields is not None:
                    managed_fields.update(client.managed_server_config(native))
        except (TypeError, ValueError):
            logger.debug("Unusable MCP baseline for %s on %s", name, client.target_name)
    elif managed and requested is not None and native_hash is not None and isinstance(native, dict):
        # Install comparison without a stored declaration (a shared-file peer
        # wrote the entry earlier in this first install): the peer's recorded
        # fingerprint is the proof. Removal (no request) still needs a baseline.
        comparable = client.managed_server_config(native)
        matches_baseline = mcp_native_hash(comparable) == native_hash
        if matches_baseline and managed_fields is not None:
            managed_fields.update(comparable)
    if requested is not None:
        requested_native = comparison if matches_baseline else native
        matches_request = (
            isinstance(requested_native, dict)
            and client.json_identical(client.managed_server_config(requested_native), requested)
            if registry_request
            else client.matches_managed_server_config(requested_native, requested)
        )
        if matches_request:
            return (
                NativeMcpConnectionState.MATCHING_MANAGED
                if matches_baseline
                else NativeMcpConnectionState.MATCHING_FOREIGN
            )
    if not matches_baseline:
        return NativeMcpConnectionState.CONFLICT
    if retired_shape:
        return NativeMcpConnectionState.UPDATE_MANAGED
    if not declaration_changed and not client.refreshes_environment_renders:
        return NativeMcpConnectionState.MATCHING_MANAGED
    return NativeMcpConnectionState.UPDATE_MANAGED


def managed_unsupported_connection(
    client: Any,
    name: str,
    *,
    requested_info: dict | None,
    stored_config: dict | None,
    native_hash: str | None,
) -> str | None:
    """Return the native key of an APM-written entry for a transport *client* now refuses.

    The entry is APM's when the lockfile fingerprint matches it or it equals an
    earlier APM render of the same declaration; anything else (including a
    foreign same-name entry) returns ``None`` and is never removed.
    """
    from apm_cli.core.conflict_detector import MCPConflictDetector
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.models.dependency.mcp import MCPDependency

    existing = MCPConflictDetector(client).get_existing_server_configs()
    key = _native_config_key(
        client, name, requested_info=requested_info, stored_config=stored_config
    )
    native = existing.get(key)
    if not isinstance(native, dict):
        return None
    if native_hash is not None:
        # A recorded fingerprint is the proof: a mismatch means the entry was
        # edited after APM wrote it, so no earlier render can claim it.
        matches = mcp_native_hash(client.managed_server_config(native)) == native_hash
        return key if matches else None
    info = requested_info
    try:
        if isinstance(stored_config, dict):
            dependency = MCPDependency.from_dict(stored_config)
            if dependency.is_self_defined:
                info = MCPIntegrator._build_self_defined_info(
                    dependency, plugin_root=_requested_plugin_root(requested_info)
                )
        legacy = client.legacy_managed_renders(info) if info is not None else ()
    except (TypeError, ValueError):
        return None
    if any(client.matches_managed_server_config(native, render) for render in legacy):
        return key
    return None


def _shape_delta(current: dict, legacy: dict) -> set[str]:
    """Keys whose value differs between the current and a legacy render (the shape change)."""
    return {key for key in current.keys() | legacy.keys() if current.get(key) != legacy.get(key)}


def _in_retired_shape(
    client: Any,
    native: object,
    info: dict | None,
    *,
    current: dict | None,
    fingerprint: bool | None,
    render_env: dict,
) -> bool:
    """Return whether *native* is an APM render in a remote shape since retired.

    *fingerprint* is ``True``/``False`` when the lockfile recorded a fingerprint
    that does or does not match *native*, ``None`` when none was recorded. With
    a match, *native* qualifies when it carries a legacy render's remote keys;
    without a fingerprint, only when it equals a legacy render of the same
    declaration (``client.legacy_managed_renders``). A mismatching fingerprint
    means the entry was edited after APM wrote it, so it never qualifies.
    Other differences (for example an env value) never count, so a fingerprinted
    entry that differs only through the environment is not churned.
    """
    if fingerprint is False:
        return False
    if info is None or current is None or not isinstance(native, dict):
        return False
    if client.matches_managed_server_config(native, current):
        return False
    for legacy in client.legacy_managed_renders(info, **render_env):
        if fingerprint is None and client.matches_managed_server_config(native, legacy):
            return True
        shape_keys = _shape_delta(current, legacy)
        if fingerprint and all(native.get(key) == legacy.get(key) for key in shape_keys):
            return True
    return False


def _healed_since_receipt(
    client: Any,
    native: dict,
    info: dict | None,
    *,
    current: dict | None,
    native_hash: str,
    render_env: dict,
) -> bool:
    """Return whether *native* is a recorded APM write that a ``--frozen`` heal reshaped.

    ``--frozen`` rewrites a retired-shape entry but never writes the lockfile,
    so its fingerprint still describes the retired shape. *native* qualifies
    only when its remote shape keys equal the current render's and swapping
    them back to a legacy render's values reproduces the recorded fingerprint
    exactly; any other edit changes the fingerprint and never qualifies.
    """
    if info is None or current is None:
        return False
    for legacy in client.legacy_managed_renders(info, **render_env):
        shape_keys = _shape_delta(current, legacy)
        if not shape_keys or any(native.get(key) != current.get(key) for key in shape_keys):
            continue
        unhealed = {key: value for key, value in native.items() if key not in shape_keys}
        unhealed.update({key: legacy[key] for key in shape_keys if key in legacy})
        if mcp_native_hash(client.managed_server_config(unhealed)) == native_hash:
            return True
    return False


def _moved_since_receipt(
    client: Any,
    native: dict,
    info: dict | None,
    *,
    current: dict | None,
    native_hash: str,
    render_env: dict,
) -> bool:
    """Return whether *native* is the current render of a write the receipt proves.

    The recorded fingerprint equals an earlier APM render of the same
    declaration (for example the retired Copilot project entry a ``--frozen``
    heal moved to ``.mcp.json`` without rewriting the lockfile), and *native*
    is exactly what APM renders for it now. Any edit fails the second check.
    """
    if info is None or current is None or not client.matches_managed_server_config(native, current):
        return False
    return any(
        mcp_native_hash(legacy) == native_hash
        for legacy in client.legacy_managed_renders(info, **render_env)
    )


def project_mcp_cleanup_reachable(
    project_root: Path, opt_in_dir: str, *, runtime_scoped: bool
) -> bool:
    """Return whether stale entries may be removed from a project runtime config.

    The runtime's opt-in directory (``.claude/``, ``.opencode/``) gates first
    writes. Cleanup scoped to one runtime comes from the ownership ledger, so it
    stays reachable after install or uninstall prunes that directory because it
    held only APM-deployed files (#3664). Broadcast cleanup (no runtime) still
    skips a project that never opted in to the runtime.
    """
    return runtime_scoped or (project_root / opt_in_dir).is_dir()


def require_removable_mcp_connections(
    client: Any,
    names: set[str],
    stored_configs: dict[str, dict],
    native_hashes: dict[str, str] | None = None,
) -> set[str]:
    """Refuse removal when recorded names do not prove unchanged native content."""
    from apm_cli.core.conflict_detector import MCPConflictDetector
    from apm_cli.install.errors import RequiredIntegrationError

    existing = MCPConflictDetector(client).get_existing_server_configs() if names else {}
    conflicts = [
        name
        for name in sorted(names)
        if compare_native_mcp_connection(
            client,
            name,
            requested_info=None,
            stored_config=stored_configs.get(name),
            managed=True,
            existing_configs=existing,
            native_hash=(native_hashes or {}).get(name),
        )
        is NativeMcpConnectionState.CONFLICT
    ]
    if conflicts:
        raise RequiredIntegrationError(
            f"Preserved MCP connection(s) {', '.join(conflicts)} in "
            f"{client.target_name} config ({client.get_config_path()}): "
            "the native connection differs from its managed baseline or the baseline "
            "cannot be verified. Reconcile these connections manually (rename or remove "
            "only the entries you intend to retire), then retry the same command. "
            "--force does not take ownership of user connections."
        )
    return {
        _native_config_key(client, name, stored_config=stored_configs.get(name)) for name in names
    }


# Project-scope runtimes whose adapters write the same native file and render
# one identical entry per server: Claude Code and Copilot share workspace-root
# ``.mcp.json`` (#3866). A claim by one is a claim by the others, so neither
# reads the other's entry as foreign, and retiring one runtime never removes
# an entry another active runtime still owns.
SHARED_PROJECT_MCP_DESTINATIONS: tuple[frozenset[str], ...] = (frozenset({"claude", "copilot"}),)


def shared_destination_peers(runtime: str, *, user_scope: bool) -> frozenset[str]:
    """Return the runtimes that write *runtime*'s native MCP file (project scope)."""
    if user_scope:
        return frozenset()
    for group in SHARED_PROJECT_MCP_DESTINATIONS:
        if runtime in group:
            return group - {runtime}
    return frozenset()


def claim_shared_destination(
    managed_target_servers: dict[str, set[str]] | None,
    native_hashes: dict[str, dict[str, str]] | None,
    runtime: str,
    name: str,
    *,
    user_scope: bool,
    written_connections: set[tuple[str, str]] | None = None,
) -> None:
    """Adopt a peer's claim on a shared-file entry before comparing it.

    A peer that owns *name* in the same file vouches for the entry, so
    *runtime* compares it as managed against the peer's fingerprint. When the
    peer rewrote the entry in this run, or *runtime* recorded no fingerprint of
    its own (an older lockfile), the peer's fingerprint replaces ours.
    """
    if managed_target_servers is None:
        return
    for peer in sorted(shared_destination_peers(runtime, user_scope=user_scope)):
        if name not in managed_target_servers.get(peer, set()):
            continue
        owned = name in managed_target_servers.get(runtime, set())
        peer_wrote = written_connections is not None and (peer, name) in written_connections
        own_hash = (native_hashes or {}).get(runtime, {}).get(name)
        if owned and not peer_wrote and own_hash is not None:
            return
        managed_target_servers.setdefault(runtime, set()).add(name)
        peer_hash = (native_hashes or {}).get(peer, {}).get(name)
        if native_hashes is not None and peer_hash is not None:
            native_hashes.setdefault(runtime, {})[name] = peer_hash
        return


def peer_wrote_shared_entry(
    runtime: str,
    name: str,
    written_connections: set[tuple[str, str]] | None,
    *,
    user_scope: bool,
) -> bool:
    """Whether a shared-file peer wrote *name* in this run (so *runtime* is configured too)."""
    return any(
        (peer, name) in (written_connections or ())
        for peer in shared_destination_peers(runtime, user_scope=user_scope)
    )


def inherit_shared_destination_claims(
    managed_target_servers: dict[str, set[str]],
    native_hashes: dict[str, dict[str, str]] | None,
    *,
    active_runtimes: set[str],
    user_scope: bool,
) -> None:
    """Pass a retiring runtime's shared-file claims to an active peer.

    The entry stays in the shared file, so the active peer owns it from now
    on instead of reading it as foreign (and the retiring runtime's cleanup
    keeps it, see :func:`names_owned_by_active_peers`).
    """
    for runtime in sorted(set(managed_target_servers) - active_runtimes):
        for peer in sorted(shared_destination_peers(runtime, user_scope=user_scope)):
            if peer not in active_runtimes:
                continue
            owned = managed_target_servers.setdefault(peer, set())
            for name in sorted(set(managed_target_servers[runtime]) - owned):
                owned.add(name)
                peer_hash = (native_hashes or {}).get(runtime, {}).get(name)
                if native_hashes is not None and peer_hash is not None:
                    native_hashes.setdefault(peer, {}).setdefault(name, peer_hash)


def transfer_deselected_shared_claims(
    managed_target_servers: dict[str, set[str]],
    native_hashes: dict[str, dict[str, str]] | None,
    *,
    selected: Mapping[str, set[str]],
    user_scope: bool,
) -> None:
    """Hand a shared-file claim to the active peer a server's ``targets:`` now selects.

    The entry in the shared file (.mcp.json) is the same one, so the peer
    owns it with the deselected runtime's receipt instead of reading it as
    foreign while that runtime's cleanup removes it.
    """
    for runtime in sorted(set(managed_target_servers) & set(selected)):
        dropped = managed_target_servers[runtime] - selected[runtime]
        for peer in sorted(shared_destination_peers(runtime, user_scope=user_scope)):
            if peer not in selected:
                continue
            owned = managed_target_servers.setdefault(peer, set())
            for name in sorted((dropped & selected[peer]) - owned):
                owned.add(name)
                receipt = (native_hashes or {}).get(runtime, {}).get(name)
                if native_hashes is not None and receipt is not None:
                    native_hashes.setdefault(peer, {}).setdefault(name, receipt)


def names_owned_by_active_peers(
    runtime: str,
    names: set[str],
    active_target_servers: Mapping[str, set[str]],
    *,
    user_scope: bool,
) -> set[str]:
    """Names in *names* a shared-file peer still owns, so retiring *runtime* keeps them."""
    kept: set[str] = set()
    for peer in shared_destination_peers(runtime, user_scope=user_scope):
        kept |= names & set(active_target_servers.get(peer, ()))
    return kept


def deselected_mcp_claims(owned: set[str], declared: set[str], selected: set[str]) -> set[str]:
    """Owned, still-declared servers a target's ``targets:`` selection no longer includes."""
    return (set(owned) & set(declared)) - set(selected)


def retain_deferred_mcp_claims(
    current: dict[str, set[str]],
    previous: dict[str, set[str]],
    requested_names: set[str],
) -> None:
    """Retain untouched claims without restoring revoked current-name ownership."""
    for target, names in previous.items():
        deferred = names - requested_names if target in current else names
        current.setdefault(target, set()).update(deferred)


def resolve_mcp_target_servers(
    *,
    recorded_target_servers: dict[str, set[str]],
    ownership_present: bool,
    server_names: set[str],
    stored_configs: dict[str, dict],
    project_root: Path | None,
    user_scope: bool,
) -> dict[str, set[str]]:
    """Return recorded ownership, adopting exact legacy baselines only when absent."""
    target_servers = {runtime: set(servers) for runtime, servers in recorded_target_servers.items()}
    if target_servers or ownership_present:
        return target_servers
    return adopt_legacy_mcp_target_servers(
        server_names=server_names,
        stored_configs=stored_configs,
        project_root=project_root,
        user_scope=user_scope,
    )


def migrate_legacy_project_target_servers(
    target_servers: dict[str, set[str]],
    *,
    active_runtimes: set[str],
    user_scope: bool,
) -> None:
    """Move project ``vscode`` ownership (a removed target, #3866) to ``copilot``.

    Lockfiles written before the removal keep replaying: a server the lock
    records for ``vscode`` counts as recorded for ``copilot``.
    """
    if user_scope or "copilot" not in active_runtimes:
        return
    legacy_servers = target_servers.pop("vscode", set())
    if legacy_servers:
        target_servers.setdefault("copilot", set()).update(legacy_servers)


def adopt_legacy_mcp_target_servers(
    *,
    server_names: set[str],
    stored_configs: dict[str, dict],
    project_root: Path | None,
    user_scope: bool,
) -> dict[str, set[str]]:
    """Adopt legacy native entries only when they exactly match their baseline."""
    from apm_cli.core.conflict_detector import MCPConflictDetector
    from apm_cli.factory import ClientFactory
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.models.dependency.mcp import MCPDependency, opencode_enabled_matches

    baselines: dict[str, Any] = {}
    for name in sorted(server_names):
        raw = stored_configs.get(name)
        if not isinstance(raw, dict):
            continue
        try:
            dependency = MCPDependency.from_dict(raw)
        except (TypeError, ValueError):
            continue
        if not dependency.is_self_defined:
            continue
        baselines[name] = dependency

    if not baselines:
        return {}

    adopted: dict[str, set[str]] = {}
    for runtime in ClientFactory.supported_clients():
        try:
            client = ClientFactory.create_client(
                runtime,
                project_root=project_root,
                user_scope=user_scope,
            )
            existing_configs = [MCPConflictDetector(client).get_existing_server_configs()]
            legacy_reader = getattr(client, "get_legacy_current_config", None)
            if callable(legacy_reader):
                legacy_config = legacy_reader()
                legacy_servers = legacy_config.get(client.mcp_servers_key)
                if isinstance(legacy_servers, dict):
                    existing_configs.append(legacy_servers)
        except Exception:
            logger.debug("Could not inspect legacy MCP target %s", runtime, exc_info=True)
            continue

        for name, dependency in baselines.items():
            try:
                expected = client.render_server_config(
                    MCPIntegrator._build_self_defined_info(dependency)
                )
            except Exception:
                logger.debug(
                    "Could not render legacy MCP baseline %s for %s",
                    name,
                    runtime,
                    exc_info=True,
                )
                continue
            native_baselines = [expected]
            candidates = existing_configs
            if runtime == "codex":
                from apm_cli.adapters.client.codex import CodexClientAdapter

                # Earlier Codex renders added an empty id to self-defined
                # entries. A nonempty ID, including its new comment encoding,
                # still disqualifies a native entry from this exact baseline.
                native_baselines.append({**expected, "id": ""})
                candidates = [
                    existing
                    for existing in candidates
                    if isinstance(existing.get(name), dict)
                    and not CodexClientAdapter.get_registry_id(existing[name])
                ]
            if any(
                existing.get(name) in native_baselines
                and (runtime != "opencode" or opencode_enabled_matches(existing[name], expected))
                for existing in candidates
            ):
                adopted.setdefault(runtime, set()).add(name)
    return adopted


# Runtimes that always keyed self-defined servers by their full declared name,
# so a short-name entry there can never be a truncated write by older APM.
_DECLARED_KEY_RUNTIMES = frozenset({"vscode"})


def reconcile_legacy_truncated_key(
    dep_name: str,
    runtimes: list[str],
    *,
    previously_owned: dict[str, set[str]],
    previous_native_hashes: dict[str, dict[str, str]],
    stored_config: dict | None,
    claimed_keys: set,
    project_root,
    user_scope: bool,
    logger,
    fail_on_write_error: bool = False,
) -> None:
    """Replace or flag the pre-fix truncated config key of a self-defined server.

    Older releases wrote ``com.example/srv`` under its final segment (``srv``).
    Now that the declared name is the key, the short entry is removed only when
    the ownership ledger proves APM wrote *dep_name* to that runtime (and, on
    runtimes with native baselines, that its content is unchanged); otherwise
    it is left in place and named (never its contents) so the user can decide.
    """
    from apm_cli.adapters.client.base import MCPClientAdapter
    from apm_cli.core.conflict_detector import MCPConflictDetector
    from apm_cli.core.scope import InstallScope
    from apm_cli.factory import ClientFactory
    from apm_cli.install.errors import RequiredIntegrationError
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    legacy_key = MCPClientAdapter.determine_config_key(dep_name, None)
    if legacy_key == dep_name or legacy_key in claimed_keys:
        return
    scope = InstallScope.USER if user_scope else InstallScope.PROJECT
    # Runtimes that share one config file (Claude Code and Copilot CLI both
    # read .mcp.json) report a left legacy entry once (#4289).
    warned_paths: set[str] = set()
    for rt in runtimes:
        if rt in _DECLARED_KEY_RUNTIMES:
            continue
        try:
            client = ClientFactory.create_client(
                rt, project_root=project_root, user_scope=user_scope
            )
            existing = MCPConflictDetector(client).get_existing_server_configs()
        except Exception as exc:
            logger.verbose_detail(f"Skipped legacy MCP entry check for {rt}: {exc}")
            continue
        if legacy_key not in existing:
            continue
        config_path = client.get_config_path()
        if dep_name in previously_owned.get(rt, set()):
            legacy_hash = previous_native_hashes.get(rt, {}).get(dep_name)
            try:
                MCPIntegrator.remove_stale(
                    {legacy_key},
                    runtime=rt,
                    project_root=project_root,
                    user_scope=user_scope,
                    logger=logger,
                    scope=scope,
                    fail_on_write_error=fail_on_write_error,
                    stored_configs={legacy_key: stored_config} if stored_config else {},
                    native_hashes={rt: {legacy_key: legacy_hash}} if legacy_hash else None,
                )
            except RequiredIntegrationError:
                if str(config_path) in warned_paths:
                    continue
                warned_paths.add(str(config_path))
                logger.warning(
                    f"Left existing MCP server '{legacy_key}' in {config_path}: it changed "
                    f"since APM wrote it for '{dep_name}'. Remove it manually if it is "
                    f"an outdated copy of '{dep_name}'."
                )
                continue
            logger.progress(f"Renamed MCP server '{legacy_key}' to '{dep_name}' in {config_path}")
        elif str(config_path) not in warned_paths:
            warned_paths.add(str(config_path))
            logger.warning(
                f"Left existing MCP server '{legacy_key}' in {config_path}: APM has no "
                f"record of writing it for '{dep_name}'. Remove it manually if it is "
                f"an outdated copy of '{dep_name}'."
            )
