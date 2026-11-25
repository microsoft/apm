"""MCP ``install`` orchestration (strangler-fig extraction from ``MCPIntegrator``).

Keeps ``MCPIntegrator.install`` as a thin delegate so public API and test patch
paths stay stable while this module owns the full install flow.
"""

from __future__ import annotations

import builtins
import copy
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yaml import YAMLError

from apm_cli.adapters.client._remote_shapes import harness_label
from apm_cli.core.apm_yml import (
    ConflictingTargetsError,
    EmptyTargetsListError,
    UnknownTargetError,
)
from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.core.scope import manifest_file
from apm_cli.core.target_catalog import accepted_target_values
from apm_cli.install import verdict_record
from apm_cli.install.errors import (
    InstallFailureAlreadyRendered,
    MCPNativeConflictError,
    RequiredIntegrationError,
)
from apm_cli.integration.mcp_install_summary import mark_unknown_noop_forms
from apm_cli.integration.mcp_registry_hints import (
    CHOOSING_MCP_REGISTRY_DOCS_URL,  # noqa: F401 -- re-exported for tests
    _emit_registry_choice_hint,
    _registry_choice_hint_is_safe,
)
from apm_cli.integration.mcp_user_scope_runtimes import user_scope_runtime_labels
from apm_cli.runtime.utils import find_runtime_binary
from apm_cli.utils.console import STATUS_SYMBOLS, _rich_echo
from apm_cli.utils.yaml_io import load_yaml

if TYPE_CHECKING:
    from apm_cli.core.scope import InstallScope
    from apm_cli.core.target_detection import EffectiveTargetDecision

_log = logging.getLogger(__name__)

# IntelliJ owns a fail-closed atomic user-config contract. Other adapters retain
# their existing best-effort behavior when a composed target set includes an
# unavailable optional runtime.
_STRICT_CONFIG_FAILURE_RUNTIMES = frozenset({"intellij"})


class _PrevalidatedRegistryServers(dict[str, dict]):
    """Registry metadata plus trusted provenance from direct prevalidation."""

    def __init__(
        self, servers: dict[str, dict], *, source: str | None, registry_url: str | None
    ) -> None:
        from apm_cli.registry.client import normalize_mcp_registry_url

        super().__init__(servers)
        self.source = source
        self.registry_url = (
            normalize_mcp_registry_url(registry_url) if registry_url is not None else None
        )


# Precedence layers a direct ``apm install --mcp NAME`` can resolve from. The
# manifest-only ``explicit`` layer (an apm.yml ``registry:`` URL) keeps its
# existing guidance.
_DIRECT_INSTALL_REGISTRY_SOURCES = frozenset({"default", "flag", "env", "config"})


def _validate_registry_servers(
    operations: Any,
    server_names: list[str],
    *,
    dependency_count: int,
    verbose: bool,
    logger: Any,
    fail_closed: bool = False,
    suggest_registry_choice: bool = False,
    server_info_cache: dict[str, dict] | None = None,
    retry_args: tuple[str, ...] = (),
) -> list[str]:
    """Validate registry identities and raise before any target write."""
    # Name the registry: one install can look up servers in several (#4244).
    registry_url = getattr(getattr(operations, "registry_client", None), "registry_url", None)
    if isinstance(registry_url, str):
        from apm_cli.registry.client import redact_mcp_registry_url

        logger.mcp_lookup_heartbeat(
            len(server_names), registry=redact_mcp_registry_url(registry_url)
        )
    else:
        logger.mcp_lookup_heartbeat(len(server_names))
    if verbose:
        logger.verbose_detail(f"Validating {dependency_count} registry servers...")
    if fail_closed:
        valid_servers, invalid_servers = operations.validate_servers_exist(
            server_names,
            fail_closed=True,
            server_info_cache=server_info_cache,
        )
    else:
        valid_servers, invalid_servers = operations.validate_servers_exist(
            server_names,
            server_info_cache=server_info_cache,
        )
    if invalid_servers:
        from apm_cli.registry import client as registry_api

        registry_client = operations.registry_client
        registry_url = registry_api.redact_mcp_registry_url(registry_client.registry_url)
        logger.error(
            f"Server(s) not found in registry {registry_url}: {', '.join(invalid_servers)}"
        )
        registry_source = getattr(registry_client, "registry_url_source", None)
        if (
            suggest_registry_choice
            and registry_source in _DIRECT_INSTALL_REGISTRY_SOURCES
            and _registry_choice_hint_is_safe(invalid_servers, retry_args)
        ):
            _emit_registry_choice_hint(
                logger,
                invalid_servers,
                registry_url=registry_url,
                registry_source=registry_source,
                retry_args=retry_args,
            )
        elif registry_source == "explicit":
            logger.progress(
                "Set MCP_REGISTRY_URL to this endpoint, then run "
                "'apm mcp search <query>' to find available servers"
            )
        else:
            logger.progress("Run 'apm mcp search <query>' to find available servers")
        raise RuntimeError(f"Cannot install {len(invalid_servers)} missing server(s)")
    return valid_servers


def prevalidate_registry_dependencies(
    mcp_deps: list,
    *,
    registry_url: str | None,
    verbose: bool,
    logger: Any,
    registry_source: str | None = None,
    retry_args: tuple[str, ...] = (),
) -> dict[str, dict]:
    """Resolve direct-install registry identities before persistent writes."""
    from apm_cli.registry.operations import MCPServerOperations

    server_names = [dep.name if hasattr(dep, "name") else dep for dep in mcp_deps]
    operations = MCPServerOperations(registry_url=registry_url, registry_source=registry_source)
    server_info_cache: dict[str, dict] = {}
    valid_servers = _validate_registry_servers(
        operations,
        server_names,
        dependency_count=len(mcp_deps),
        verbose=verbose,
        logger=logger,
        fail_closed=True,
        suggest_registry_choice=True,
        server_info_cache=server_info_cache,
        retry_args=retry_args,
    )
    return _PrevalidatedRegistryServers(
        {name: server_info_cache[name] for name in valid_servers},
        source=registry_source,
        registry_url=registry_url,
    )


class _TargetSelectionSource(str, Enum):
    """Source that supplied the MCP target set before compatibility gates."""

    RUNTIME = "runtime"
    TARGET = "target"
    MANIFEST = "manifest"
    DISCOVERY = "discovery"
    FALLBACK = "fallback"
    INVALID_MANIFEST = "invalid-manifest"

    __str__ = str.__str__
    __format__ = str.__format__


@dataclass(frozen=True)
class ResolvedMcpTargets:
    """MCP target runtimes resolved once per install run (``None``: none can accept)."""

    runtimes: tuple[str, ...] | None

    def runtime_list(self) -> list[str] | None:
        """Return a mutable copy of the runtimes, or ``None``."""
        return None if self.runtimes is None else list(self.runtimes)


def resolve_mcp_targets(**kwargs: Any) -> ResolvedMcpTargets:
    """Resolve and announce MCP targets once; ``run_mcp_install`` reuses them (#4089).

    Takes the keyword arguments of :func:`_resolve_target_runtimes`.
    """
    runtimes = _resolve_target_runtimes(**kwargs)
    return ResolvedMcpTargets(None if runtimes is None else tuple(runtimes))


def _announce_registry_endpoint(registry_client: Any, logger: Any) -> None:
    """Name the registry endpoint a dependency group resolves against.

    Defaults are quiet, overrides are visible: a redirect away from the public
    registry -- an apm.yml ``registry:`` URL, ``MCP_REGISTRY_URL``, or a
    persisted ``apm config set mcp-registry-url`` -- changes which bytes an
    install trusts, so it is stated rather than inferred. Mirrors the
    diagnostic ``apm install --mcp NAME`` already emits (#2740).
    """
    from apm_cli.registry.client import REGISTRY_SOURCE_LABELS, redact_mcp_registry_url

    source = registry_client.registry_url_source
    if source == "default":
        return
    # The only non-ambient source on this path is a per-dependency registry: URL.
    label = REGISTRY_SOURCE_LABELS.get(source, "from --registry or the apm.yml registry field")
    safe_url = redact_mcp_registry_url(registry_client.registry_url)
    logger.progress(f"Using MCP registry: {safe_url} ({label})", symbol="info")


def _install_registry_group(  # noqa: PLR0913
    operations: Any,
    group_dep_names: list,
    group_dep_map: dict,
    group_deps: list,
    target_runtimes: list,
    stored_mcp_configs: dict,
    servers_to_update: builtins.set,
    successful_updates: builtins.set,
    project_root: Any,
    user_scope: bool,
    verbose: bool,
    console: Any,
    logger: Any,
    managed_target_servers: dict[str, set[str]] | None,
    prevalidated_servers: dict[str, dict] | None = None,
    fail_on_write_error: bool = False,
    native_hashes: dict[str, dict[str, str]] | None = None,
    written_connections: set[tuple[str, str]] | None = None,
) -> int:
    """Process one group of registry deps through a single ``MCPServerOperations`` instance.

    All deps in ``group_deps`` share the same target registry (either the
    default or a per-dep override URL).  ``servers_to_update`` and
    ``successful_updates`` are mutated in-place; the function returns the
    number of servers newly configured or updated in this group.
    """
    # Lazy import: only available after MCPIntegrator finishes loading.
    from apm_cli.install.mcp.ownership import (
        NativeMcpConnectionState,
        claim_shared_destination,
        compare_native_mcp_connection,
        native_comparison_clients,
        peer_wrote_shared_entry,
        retain_native_receipt,
    )
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.integration.mcp_transport_resolution import policy_checked_registry_info

    configured_count = 0
    failed_installations: list[str] = []
    native_conflicts: list[str] = []
    if written_connections is None:
        written_connections = set()
    protected_clients = native_comparison_clients(
        target_runtimes, project_root=project_root, user_scope=user_scope
    )
    registry_server_cache: dict[str, dict] = prevalidated_servers or {}

    _announce_registry_endpoint(operations.registry_client, logger)

    if prevalidated_servers is not None and set(group_dep_names) <= prevalidated_servers.keys():
        valid_servers = group_dep_names
    else:
        valid_servers = _validate_registry_servers(
            operations,
            group_dep_names,
            dependency_count=len(group_deps),
            verbose=verbose,
            logger=logger,
            server_info_cache=registry_server_cache,
        )

    if valid_servers:
        servers_to_install = operations.check_servers_needing_installation(
            target_runtimes,
            valid_servers,
            project_root=project_root,
            user_scope=user_scope,
        )
        already_configured_candidates = [
            dep for dep in valid_servers if dep not in servers_to_install
        ]

        # Detect declaration drift; native comparison needs it for every server.
        drift_scope = valid_servers if protected_clients else already_configured_candidates
        if stored_mcp_configs and drift_scope:
            drifted_reg_deps = [group_dep_map[n] for n in drift_scope if n in group_dep_map]
            drifted = MCPIntegrator._detect_mcp_config_drift(
                drifted_reg_deps,
                stored_mcp_configs,
            )
            if drifted:
                servers_to_update.update(drifted)
                MCPIntegrator._append_drifted_to_install_list(servers_to_install, drifted)
        already_configured_servers = [
            dep for dep in already_configured_candidates if dep not in servers_to_update
        ]
        if protected_clients:
            # Names/registry IDs cannot prove that a native connection is unchanged.
            servers_to_install = list(valid_servers)
            already_configured_servers = []

        if not servers_to_install:
            if console:
                for dep in already_configured_servers:
                    console.print(
                        f"|  [green]{STATUS_SYMBOLS['check']}[/green] {dep} "
                        f"[dim](already configured)[/dim]"
                    )
            else:
                logger.success("All registry MCP servers already configured")
        else:
            if already_configured_servers:
                if console:
                    for dep in already_configured_servers:
                        console.print(
                            f"|  [green]{STATUS_SYMBOLS['check']}[/green] {dep} "
                            f"[dim](already configured)[/dim]"
                        )
                else:
                    logger.verbose_detail(
                        "Already configured registry MCP servers: "
                        f"{', '.join(already_configured_servers)}"
                    )

            # Batch fetch server info once
            if verbose:
                logger.verbose_detail(f"Installing {len(servers_to_install)} servers...")
            # Write exactly the registry snapshot org policy judged.
            known = dict(registry_server_cache)
            for name in servers_to_install:
                checked = policy_checked_registry_info(
                    operations.registry_client.registry_url, name
                )
                if checked is not None:
                    known[name] = checked
            server_info_cache = _overlaid_server_infos(
                operations, servers_to_install, group_dep_map, known=known
            )

            # Collect env and runtime variables
            shared_env_vars = operations.collect_environment_variables(
                servers_to_install, server_info_cache
            )
            for server_name in servers_to_install:
                dep = group_dep_map.get(server_name)
                if dep and dep.env:
                    shared_env_vars.update(dep.env)
            shared_runtime_vars = operations.collect_runtime_variables(
                servers_to_install, server_info_cache
            )

            # Install for each target runtime
            for dep in servers_to_install:
                is_update = dep in servers_to_update
                action_text = "Updating" if is_update else "Configuring"
                if console:
                    console.print(f"|  [cyan]{STATUS_SYMBOLS['running']}[/cyan]  {dep}")
                    console.print(
                        f"|     +- {action_text} for "
                        f"{', '.join([harness_label(rt) for rt in target_runtimes])}..."
                    )
                else:
                    logger.progress(
                        f"{dep}: {action_text.lower()} for {', '.join(target_runtimes)}..."
                    )

                successful_runtimes: list[str] = []
                failed_runtimes: list[str] = []
                for rt in target_runtimes:
                    if _skip_unsupported_remote_transport(
                        rt,
                        dep,
                        server_info_cache.get(dep),
                        logger,
                        project_root=project_root,
                        user_scope=user_scope,
                        stored_config=stored_mcp_configs.get(dep),
                        managed_target_servers=managed_target_servers,
                        native_hashes=native_hashes,
                        registry_info=True,
                    ):
                        continue
                    installed_servers: set[str] = set()
                    written_hashes: dict[str, str] = {}
                    managed_fields: set[str] = set()
                    claim_shared_destination(
                        managed_target_servers,
                        native_hashes,
                        rt,
                        dep,
                        user_scope=user_scope,
                        written_connections=written_connections,
                    )
                    replace_existing = is_update and (
                        managed_target_servers is None
                        or dep in managed_target_servers.get(rt, set())
                    )
                    if rt in protected_clients:
                        client = protected_clients[rt]
                        stale_receipts: set[str] = set()
                        state = compare_native_mcp_connection(
                            client,
                            dep,
                            requested_info=server_info_cache.get(dep),
                            stored_config=stored_mcp_configs.get(dep),
                            managed=managed_target_servers is not None
                            and dep in managed_target_servers.get(rt, set()),
                            managed_fields=managed_fields,
                            native_hash=(native_hashes or {}).get(rt, {}).get(dep),
                            registry_request=True,
                            declaration_changed=is_update,
                            env_overrides=shared_env_vars,
                            runtime_vars=shared_runtime_vars,
                            stale_receipts=stale_receipts,
                        )
                        if state in {
                            NativeMcpConnectionState.MATCHING_FOREIGN,
                            NativeMcpConnectionState.CONFLICT,
                        }:
                            if managed_target_servers is not None:
                                managed_target_servers.get(rt, set()).discard(dep)
                            if native_hashes is not None:
                                native_hashes.get(rt, {}).pop(dep, None)
                        if state is NativeMcpConnectionState.CONFLICT:
                            _report_native_conflict(
                                dep,
                                rt,
                                client,
                                logger,
                                native_conflicts,
                                render=lambda client=client, dep=dep: client.render_server_config(
                                    server_info_cache[dep],
                                    env_overrides=shared_env_vars,
                                    runtime_vars=shared_runtime_vars,
                                ),
                            )
                            continue
                        if state in {
                            NativeMcpConnectionState.MATCHING_MANAGED,
                            NativeMcpConnectionState.MATCHING_FOREIGN,
                        }:
                            if state is NativeMcpConnectionState.MATCHING_MANAGED:
                                if managed_target_servers is not None:
                                    managed_target_servers.setdefault(rt, set()).add(dep)
                                retain_native_receipt(
                                    native_hashes,
                                    rt,
                                    client,
                                    dep,
                                    requested_info=server_info_cache.get(dep),
                                    replace=dep in stale_receipts,
                                )
                            if peer_wrote_shared_entry(
                                rt, dep, written_connections, user_scope=user_scope
                            ):
                                successful_runtimes.append(rt)
                            logger.verbose_detail(f"{dep} already matches {rt}; no write")
                            continue
                        replace_existing = state is NativeMcpConnectionState.UPDATE_MANAGED
                    if verbose:
                        logger.verbose_detail(f"Configuring {rt}...")
                    if MCPIntegrator._install_for_runtime(
                        rt,
                        [dep],
                        shared_env_vars,
                        server_info_cache,
                        shared_runtime_vars,
                        project_root=project_root,
                        user_scope=user_scope,
                        logger=logger,
                        replace_existing=replace_existing,
                        installed_servers=installed_servers,
                        written_native_hashes=written_hashes,
                        managed_fields=managed_fields,
                    ):
                        successful_runtimes.append(rt)
                        if dep in installed_servers:
                            _record_managed_server(
                                managed_target_servers,
                                rt,
                                dep,
                                native_hashes=native_hashes,
                                written_hashes=written_hashes,
                                written_connections=written_connections,
                                requested_info=server_info_cache.get(dep),
                            )
                    else:
                        failed_runtimes.append(rt)

                if successful_runtimes:
                    if console:
                        label = "updated" if is_update else "configured"
                        console.print(
                            f"|  [green]{STATUS_SYMBOLS['check']}[/green]  {dep} -> "
                            f"{', '.join([harness_label(rt) for rt in successful_runtimes])}"
                            f" [dim]({label})[/dim]"
                        )
                        if not user_scope:
                            _print_user_wide_configs(console, successful_runtimes)
                    configured_count += 1
                    if is_update:
                        successful_updates.add(dep)
                if failed_runtimes:
                    strict_failures = sorted(
                        failed_runtimes
                        if fail_on_write_error
                        else set(failed_runtimes) & _STRICT_CONFIG_FAILURE_RUNTIMES
                    )
                    if strict_failures:
                        failed_installations.append(f"{dep} ({', '.join(strict_failures)})")
                    if console:
                        console.print(
                            f"|  [red]{STATUS_SYMBOLS['cross']}[/red]  {dep}  "
                            f"-- failed for {', '.join(sorted(failed_runtimes))}"
                        )
                    else:
                        logger.error(f"{dep} -- failed for {', '.join(sorted(failed_runtimes))}")

    if native_conflicts:
        message = "MCP installation incomplete: " + ", ".join(native_conflicts)
        logger.error(message)
        raise MCPNativeConflictError(message, wrote_connections=bool(written_connections))
    _raise_strict_config_failures(failed_installations, console=console, logger=logger)
    return configured_count


def _skip_unsupported_remote_transport(
    runtime: str,
    name: str,
    server_info: dict | None,
    logger: Any,
    *,
    project_root: Any,
    user_scope: bool,
    stored_config: dict | None = None,
    managed_target_servers: dict[str, set[str]] | None = None,
    native_hashes: dict[str, dict[str, str]] | None = None,
    registry_info: bool = False,
) -> bool:
    """Warn and skip a remote whose transport *runtime* cannot load (never claimed).

    Under ``--frozen`` a pair deferred because the lockfile never claimed it
    (``is_frozen_unrecorded_mcp_pair``) is refused here when its transport turns
    out to be supported, so a frozen install never writes a new claim.

    Runs before any native comparison or write, so the server is not rendered
    or recorded for that runtime and the install continues. An entry an earlier
    APM release wrote for it (proven by fingerprint or an earlier render) is
    removed and its claim dropped; any other same-name entry is left alone.
    """
    if not isinstance(server_info, dict):
        return False
    from apm_cli.adapters.client._remote_shapes import (
        skip_unsupported_remote,
        unsupported_remote_transport,
    )
    from apm_cli.factory import ClientFactory
    from apm_cli.install.mcp.native_runtimes import retire_runtime_claim
    from apm_cli.install.mcp.ownership import is_frozen_unrecorded_mcp_pair

    try:
        client = ClientFactory.create_client(
            runtime, project_root=project_root, user_scope=user_scope
        )
    except ValueError:
        return False
    transport = unsupported_remote_transport(client, server_info)
    if transport is None:
        if is_frozen_unrecorded_mcp_pair(name, runtime):
            from apm_cli.install.errors import frozen_unrecorded_reason
            from apm_cli.install.service import frozen_services_error

            raise frozen_services_error([frozen_unrecorded_reason("MCP", name, runtime)])
        return False
    skip_unsupported_remote(client, name, transport, warn=logger.warning)
    retire_runtime_claim(
        client,
        runtime,
        name,
        server_info if registry_info else None,
        logger,
        stored_config=stored_config,
        managed_target_servers=managed_target_servers,
        native_hashes=native_hashes,
    )
    return True


def _remove_native_entry(client: Any, key: str, logger: Any) -> None:
    """Remove exactly *key* from *client*'s native MCP config, failing loudly."""
    from apm_cli.adapters.client._remote_shapes import remote_shape_of
    from apm_cli.adapters.client.hermes import HermesClientAdapter
    from apm_cli.adapters.client.opencode import V2_MCP_SETTINGS, OpenCodeClientAdapter
    from apm_cli.integration.mcp_integrator import (
        _clean_hermes_mcp_config,
        _clean_json_mcp_config,
        _clean_toml_mcp_config,
    )

    path = Path(client.get_config_path())
    label = f"{remote_shape_of(client).label} MCP config"
    if isinstance(client, HermesClientAdapter):
        _clean_hermes_mcp_config(path, {key}, logger, fail_on_write_error=True)
        return
    if path.suffix == ".toml":
        _clean_toml_mcp_config(
            path, {key}, label, logger=logger, use_rich=False, fail_on_write_error=True
        )
        return
    nested: dict[str, Any] = {}
    if isinstance(client, OpenCodeClientAdapter):
        # OpenCode v2 keeps servers under mcp.servers beside the v1 entries.
        nested = {
            "nested_servers": OpenCodeClientAdapter.native_v2_servers,
            "nested_settings": V2_MCP_SETTINGS,
        }
    _clean_json_mcp_config(
        path,
        {key},
        logger,
        label,
        servers_key=client.mcp_servers_key,
        trailing_newline=client.json_config_trailing_newline,
        fail_on_write_error=True,
        **nested,
    )


def _resolve_frozen_deferred_pairs(
    mcp_deps: list,
    target_runtimes: list[str],
    *,
    project_root: Any,
    user_scope: bool,
) -> None:
    """Refuse, before any native write, a deferred ``--frozen`` pair its harness can load.

    ``InstallService.enforce_frozen_services`` defers an unrecorded pair whose
    transport only the registry payload can decide; this resolves every such
    pair against its overlaid payload so a refused replay changes nothing.
    """
    from apm_cli.adapters.client._remote_shapes import unsupported_remote_transport
    from apm_cli.factory import ClientFactory
    from apm_cli.install.errors import frozen_unrecorded_reason
    from apm_cli.install.mcp.ownership import current_frozen_unrecorded_mcp_pairs
    from apm_cli.install.service import frozen_services_error
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.registry.operations import MCPServerOperations

    pairs = {pair for pair in current_frozen_unrecorded_mcp_pairs() if pair[1] in target_runtimes}
    if not pairs:
        return
    deferred_names = {name for name, _runtime in pairs}
    infos: dict[str, dict] = {}
    groups: dict[str | None, list] = {}
    for dep in mcp_deps:
        name = dep.name if hasattr(dep, "name") else dep
        if name not in deferred_names:
            continue
        if getattr(dep, "is_self_defined", False):
            infos[name] = MCPIntegrator._build_self_defined_info(dep)
        else:
            groups.setdefault(_registry_group_key(dep), []).append(dep)
    for registry_url, deps in groups.items():
        infos.update(
            _overlaid_server_infos(
                MCPServerOperations(registry_url=registry_url),
                [dep.name if hasattr(dep, "name") else dep for dep in deps],
                {dep.name: dep for dep in deps if hasattr(dep, "name")},
            )
        )
    reasons = []
    for name, runtime in sorted(pairs):
        info = infos.get(name)
        client = ClientFactory.create_client(
            runtime, project_root=project_root, user_scope=user_scope
        )
        if info is None or unsupported_remote_transport(client, info) is None:
            reasons.append(frozen_unrecorded_reason("MCP", name, runtime))
    if reasons:
        raise frozen_services_error(reasons)


def _overlaid_server_infos(
    operations: Any,
    names: list[str],
    dep_map: dict[str, Any],
    *,
    known: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Registry server info for *names*, fetched once and overlaid per declaration.

    The one fetch-then-overlay step every registry install path uses. Entries
    are deep copies, so overlaying never mutates a cached registry payload.
    """
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    infos = {name: (known or {})[name] for name in names if name in (known or {})}
    unresolved = [name for name in names if name not in infos]
    if unresolved:
        infos.update(operations.batch_fetch_server_info(unresolved))
    infos = {name: copy.deepcopy(info) for name, info in infos.items()}
    for name in names:
        dep = dep_map.get(name)
        if dep:
            MCPIntegrator._apply_overlay(infos, dep)
    return infos


def _registry_group_key(dep: Any) -> str | None:
    """Normalized per-dependency registry URL; ``None`` for the default registry."""
    dep_registry = getattr(dep, "registry", None)
    if not isinstance(dep_registry, str):
        return None
    from apm_cli.registry.client import normalize_mcp_registry_url

    return normalize_mcp_registry_url(dep_registry)


def _ascii_names(names: list[str]) -> str:
    """Join manifest-authored names for console output, escaping non-ASCII."""
    return ", ".join(ascii(name)[1:-1] for name in names)


def _codex_expanded_env_names(client: Any, name: str, requested_info: dict) -> list[str]:
    """Env names a Codex project entry stores as values but the declaration now forwards."""
    if getattr(client, "target_name", "") != "codex" or client.user_scope:
        return []
    from apm_cli.core.conflict_detector import MCPConflictDetector

    native = MCPConflictDetector(client).get_existing_server_configs()
    return client.expanded_env_names(
        native.get(client.determine_config_key(name, None)),
        client.render_server_config(requested_info),
    )


def _report_native_conflict(
    name: str,
    runtime: str,
    client: Any,
    logger: Any,
    conflicts: list[str],
    expanded_env: list[str] | None = None,
    render: Any = None,
) -> None:
    """Report a preserved native collision identically for both dependency sources.

    *render* returns the requested native entry, letting the adapter add
    target-specific guidance (names only) about the preserved connection.
    """
    conflicts.append(f"{name} ({runtime})")
    guidance = None
    if render is not None:
        try:
            guidance = client.native_conflict_guidance(name, render())
        except (TypeError, ValueError):
            _log.debug("No conflict guidance for %s on %s", name, runtime, exc_info=True)
    if guidance:
        logger.error(
            f"MCP server '{name}' conflicts with the existing {runtime} "
            f"connection in {client.get_config_path()}. Preserved that connection. "
            f"{guidance}\n    --force does not take ownership of user connections."
        )
        return
    logger.error(
        f"MCP server '{name}' conflicts with the existing {runtime} "
        f"connection in {client.get_config_path()}. Preserved that connection. "
        "Rename or remove the conflicting native connection yourself, then "
        "retry the same install command. --force does not take ownership "
        "of user connections."
        + (
            f" The connection stores values for {_ascii_names(expanded_env)} that the "
            "declaration now forwards through env_vars. Reconcile them by hand, then "
            "retry. If that file was ever shared or committed, rotate the credential "
            "because Git history still holds it."
            if expanded_env
            else ""
        )
    )


def _validate_target_configs(
    target_runtimes: list[str],
    *,
    project_root: Path | None,
    user_scope: bool,
    console: Any,
    logger: Any,
) -> None:
    """Validate native configs before no-op detection or retarget side effects."""
    if not user_scope:
        return
    from apm_cli.factory import ClientFactory

    unsafe_runtimes = [
        rt
        for rt in target_runtimes
        if not ClientFactory.create_client(
            rt, project_root=project_root, user_scope=user_scope
        ).validate_config_for_install()
    ]
    _raise_strict_config_failures(unsafe_runtimes, console=console, logger=logger)


def _record_managed_server(
    managed_target_servers: dict[str, set[str]] | None,
    runtime: str,
    server_name: str,
    *,
    native_hashes: dict[str, dict[str, str]] | None = None,
    written_hashes: dict[str, str] | None = None,
    written_connections: set[tuple[str, str]] | None = None,
    requested_info: dict | None = None,
) -> None:
    """Record a server only after APM successfully writes its target config."""
    from apm_cli.install.mcp.write_receipts import note_mcp_write

    note_mcp_write(runtime, server_name, (written_hashes or {}).get(server_name), requested_info)
    if managed_target_servers is not None:
        managed_target_servers.setdefault(runtime, set()).add(server_name)
    if native_hashes is not None and written_hashes and server_name in written_hashes:
        native_hashes.setdefault(runtime, {})[server_name] = written_hashes[server_name]
    if written_connections is not None:
        written_connections.add((runtime, server_name))


def _raise_strict_config_failures(
    failed_installations: list[str],
    *,
    console,
    logger,
) -> None:
    """Render one failure footer, then fail without a success summary."""
    if not failed_installations:
        return
    message = (
        "MCP configuration failed for selected runtime(s): "
        + ", ".join(failed_installations)
        + ". Fix the failed runtime MCP config and rerun the original install command, "
        "retaining --global and any --target selection."
    )
    if console:
        _rich_echo(f"{STATUS_SYMBOLS['cross']} {message}", color="red")
    else:
        logger.error(message)
    raise InstallFailureAlreadyRendered(message)


def _migrate_intellij_managed_config(
    target_runtimes: list[str],
    managed_target_servers: dict[str, set[str]] | None,
    *,
    project_root,
    user_scope: bool,
    logger,
) -> None:
    """Route provenance-owned IntelliJ path migration through its adapter."""
    if "intellij" not in target_runtimes or managed_target_servers is None:
        return
    from apm_cli.factory import ClientFactory

    client = ClientFactory.create_client(
        "intellij",
        project_root=project_root,
        user_scope=user_scope,
    )
    from apm_cli.adapters.client.intellij import IntelliJConfigError

    try:
        migrated = client.migrate_legacy_managed_servers(
            builtins.set(managed_target_servers.get("intellij", set()))
        )
    except IntelliJConfigError as exc:
        logger.error(str(exc))
        raise InstallFailureAlreadyRendered(str(exc)) from exc
    if migrated:
        count = len(migrated)
        logger.success(
            f"Migrated {count} IntelliJ MCP server{'s' if count != 1 else ''} "
            f"to {client.get_config_path()}",
            symbol="check",
        )


def _discover_installed_runtimes(project_root_path, *, user_scope: bool) -> list[str]:
    """Detect which MCP-capable runtimes are installed on the host.

    Each runtime is opt-in via a binary-on-PATH and/or directory-presence
    signal (see per-runtime comments).  The primary path constructs each
    adapter via :class:`ClientFactory`; the ``ImportError`` fallback degrades
    to a binary/directory probe when optional deps are unavailable.
    """
    from apm_cli.integration.mcp_integrator import _is_vscode_available
    from apm_cli.integration.targets import KNOWN_TARGETS

    # Directory-signal opt-in runtimes: name -> required dir under the scope
    # root.  OpenCode's user root (~/.config/opencode) differs from its
    # project marker, so its signal comes from the target profile.
    dir_signal = {
        "cursor": ".cursor",
        "opencode": KNOWN_TARGETS["opencode"].effective_root(user_scope),
        "gemini": ".gemini",
        "windsurf": ".windsurf",
        "kiro": ".kiro",
    }
    try:
        from apm_cli.factory import ClientFactory
        from apm_cli.runtime.manager import RuntimeManager

        manager = RuntimeManager()
        installed_runtimes: list[str] = []

        for runtime_name in [
            "copilot",
            "codex",
            "cursor",
            "opencode",
            "gemini",
            "windsurf",
            "kiro",
            "claude",
            "intellij",
        ]:
            try:
                if not _runtime_is_present(
                    runtime_name, project_root_path, manager, dir_signal, user_scope=user_scope
                ):
                    continue
                ClientFactory.create_client(
                    runtime_name,
                    project_root=project_root_path,
                    user_scope=user_scope,
                )
                installed_runtimes.append(runtime_name)
            except (ValueError, ImportError):
                continue
        return installed_runtimes
    except ImportError:
        return _discover_installed_runtimes_fallback(
            project_root_path, _is_vscode_available, user_scope=user_scope
        )


def _runtime_is_present(
    runtime_name, project_root_path, manager, dir_signal, *, user_scope: bool
) -> bool:
    """Return ``True`` when *runtime_name*'s opt-in presence signal fires."""
    from apm_cli.integration.mcp_integrator import _is_vscode_available

    if runtime_name == "copilot" and not user_scope:
        # VS Code's Copilot harness reads the workspace-root .mcp.json that the
        # copilot target writes (#3866), so VS Code presence selects copilot.
        if _is_vscode_available(project_root=project_root_path):
            return True
    if runtime_name == "kiro" and user_scope:
        return True
    if runtime_name in dir_signal:
        return (project_root_path / dir_signal[runtime_name]).is_dir()
    if runtime_name == "claude":
        # .claude/ (project-scope) OR `claude` on PATH (user-scope). The PATH
        # gate prevents writing ~/.claude.json on hosts without Claude Code.
        return (project_root_path / ".claude").is_dir() or find_runtime_binary("claude") is not None
    if runtime_name == "intellij":
        # JetBrains Copilot writes its config dir on first run -> presence
        # reliably signals the plugin is installed.
        from apm_cli.adapters.client.intellij import _intellij_config_dir

        return _intellij_config_dir().is_dir()
    return manager.is_runtime_available(runtime_name)


def _discover_installed_runtimes_fallback(
    project_root_path, _is_vscode_available, *, user_scope: bool
) -> list[str]:
    """Binary/directory-only runtime probe used when adapters fail to import."""
    from apm_cli.integration.targets import KNOWN_TARGETS

    installed_runtimes = [rt for rt in ["copilot", "codex"] if find_runtime_binary(rt) is not None]
    if (
        not user_scope
        and "copilot" not in installed_runtimes
        and _is_vscode_available(project_root=project_root_path)
    ):
        installed_runtimes.insert(0, "copilot")
    for name, signal in (
        ("cursor", ".cursor"),
        ("opencode", KNOWN_TARGETS["opencode"].effective_root(user_scope)),
        ("gemini", ".gemini"),
        ("windsurf", ".windsurf"),
        ("kiro", ".kiro"),
    ):
        if (name == "kiro" and user_scope) or (project_root_path / signal).is_dir():
            installed_runtimes.append(name)
    # Claude Code: directory-presence OR binary-on-PATH
    if (project_root_path / ".claude").is_dir() or find_runtime_binary("claude") is not None:
        installed_runtimes.append("claude")
    # JetBrains Copilot: user-scope config directory presence
    try:
        from apm_cli.adapters.client.intellij import _intellij_config_dir

        if _intellij_config_dir().is_dir():
            installed_runtimes.append("intellij")
    except (ImportError, ValueError):
        # ValueError (PathTraversalError) when LOCALAPPDATA/XDG_DATA_HOME is
        # misconfigured -- treat as "not installed" rather than crash.
        pass
    return installed_runtimes


def _declared_manifest_target_runtimes(
    apm_config: dict | None,
) -> tuple[list[str] | None, bool]:
    """Return canonical manifest targets and whether the declaration is valid.

    Delegates to the manifest target declaration owner, the same
    parser the v2 file-deployment target resolver and
    :meth:`MCPIntegrator._gate_project_scoped_runtimes` both use, so a
    manifest-declared target list is interpreted identically everywhere
    (including folding the legacy ``all`` value back to auto-detect).

    The first tuple item is ``None`` when the manifest is unrestricted (no
    ``targets:``/``target:`` key, or legacy ``all``), which permits
    local-machine runtime discovery. The second item is ``False`` for malformed
    declarations. That state blocks discovery while leaving
    ``_gate_project_scoped_runtimes`` to render the established fail-closed
    error.
    """
    if not apm_config:
        return None, True

    try:
        from apm_cli.models.manifest_targets import manifest_target_restriction

        parsed = manifest_target_restriction(apm_config)
    except (ConflictingTargetsError, EmptyTargetsListError, UnknownTargetError):
        return None, False
    if not parsed:
        return None, True
    from apm_cli.core.target_detection import EffectiveTargetDecision

    projected = EffectiveTargetDecision(parsed, "apm.yml").runtime_targets
    return list(projected or ()), True


def _resolve_target_runtimes(
    runtime: str | None,
    exclude: str | None,
    verbose: bool,
    apm_config: dict | None,
    project_root,
    user_scope: bool,
    explicit_target: str | list[str] | None,
    target_decision: EffectiveTargetDecision | None,
    scope: InstallScope | None,
    logger,
    console,
    materialize: bool = True,
) -> list[str] | None:
    """Detect, filter, and gate the target runtimes for MCP installation.

    Returns a (possibly empty) list of runtime names to target, or ``None``
    when the caller should immediately return 0 (e.g. all runtimes excluded,
    no user-scope-capable runtimes available). ``materialize=False`` resolves
    read-only, creating no target directory (the #4268 install preflight).
    """
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    selection_source: _TargetSelectionSource
    if runtime:
        # Single runtime mode - skip auto-discovery entirely.
        target_runtimes: list[str] = [runtime]
        selection_source = _TargetSelectionSource.RUNTIME
    elif target_decision is not None and target_decision.runtime_targets is not None:
        target_runtimes = list(
            target_decision.runtime_targets_for_scope(user_scope=user_scope) or ()
        )
        if target_decision.source == "apm.yml":
            selection_source = _TargetSelectionSource.MANIFEST
        elif target_decision.source.startswith("auto-detect"):
            selection_source = _TargetSelectionSource.DISCOVERY
        else:
            selection_source = _TargetSelectionSource.TARGET
    elif explicit_target is not None:
        # A plural --target value is already parser-normalized. Use that exact
        # runtime set instead of broad discovery so selecting IntelliJ does not
        # also select adjacent runtimes that share its Copilot policy profile.
        target_runtimes = (
            [explicit_target] if isinstance(explicit_target, str) else list(explicit_target)
        )
        selection_source = _TargetSelectionSource.TARGET
    else:
        # Manifest loading/parsing (and the user-facing warnings
        # parse_targets_field can emit, e.g. legacy `targets: [all]`) is
        # deferred to this branch -- irrelevant, and wasted filesystem I/O,
        # whenever the caller already pinned a runtime/target explicitly.
        project_root_path = Path(project_root) if project_root is not None else Path.cwd()

        if apm_config is None:
            apm_yml = manifest_file(project_root_path)
            try:
                if apm_yml.exists():
                    apm_config = load_yaml(apm_yml)
            except (OSError, UnicodeError, YAMLError) as exc:
                raise RuntimeError(
                    f"Unable to load MCP targets from {apm_yml}; fix the manifest before retrying"
                ) from exc

        declared_targets, manifest_valid = _declared_manifest_target_runtimes(apm_config)
        if not manifest_valid:
            # Do not inspect machine-local signals for an invalid declaration.
            # The shared gate below re-parses and renders the canonical error.
            target_runtimes = []
            selection_source = _TargetSelectionSource.INVALID_MANIFEST
            logger.verbose_detail(
                "Manifest target declaration is invalid; machine discovery skipped"
            )
        elif declared_targets is not None:
            # apm.yml declares `targets:` explicitly -- that is the deterministic,
            # committed source of truth for MCP ownership too. Using it instead of
            # local-machine runtime auto-discovery keeps `mcp_target_servers` (and
            # the deployment ledger `runtime` field) byte-identical across
            # developers with different harnesses installed, instead of each
            # `apm install` "stealing" MCP ownership toward whatever the current
            # machine happens to have (issue #2298).
            target_runtimes = declared_targets
            selection_source = _TargetSelectionSource.MANIFEST
            logger.verbose_detail(
                "Resolved MCP targets from apm.yml declaration: "
                f"{', '.join(target_runtimes)} (machine discovery skipped)"
            )
        else:
            # Step 1: Get all installed runtimes on the system
            installed_runtimes = _discover_installed_runtimes(
                project_root_path, user_scope=user_scope
            )

            # Step 2: Get runtimes referenced in apm.yml scripts
            script_runtimes = MCPIntegrator._detect_runtimes(
                apm_config.get("scripts", {}) if apm_config else {}
            )

            # Step 3: Target runtimes BOTH installed AND referenced in scripts
            if script_runtimes:
                target_runtimes = [rt for rt in installed_runtimes if rt in script_runtimes]

                if verbose:
                    if console:
                        console.print(
                            f"|  [cyan]{STATUS_SYMBOLS['info']}  Runtime Detection[/cyan]"
                        )
                        console.print(f"|     +- Installed: {', '.join(installed_runtimes)}")
                        console.print(f"|     +- Used in scripts: {', '.join(script_runtimes)}")
                        if target_runtimes:
                            console.print(
                                f"|     +- Target: {', '.join(target_runtimes)} "
                                f"(available + used in scripts)"
                            )
                        console.print("|")
                    else:
                        logger.verbose_detail(
                            f"Installed runtimes: {', '.join(installed_runtimes)}"
                        )
                        logger.verbose_detail(f"Script runtimes: {', '.join(script_runtimes)}")
                        if target_runtimes:
                            logger.verbose_detail(f"Target runtimes: {', '.join(target_runtimes)}")

                if not target_runtimes:
                    logger.warning("Scripts reference runtimes that are not installed")
                    logger.progress("Install missing runtimes with: apm runtime setup <runtime>")
            else:
                target_runtimes = installed_runtimes
                if target_runtimes:
                    if verbose:
                        logger.verbose_detail(
                            f"No scripts detected, using all installed runtimes: "
                            f"{', '.join(target_runtimes)}"
                        )
                else:
                    logger.warning("No MCP-compatible runtimes installed")
                    logger.progress("Install a runtime with: apm runtime setup copilot")

            # Fall back to Copilot (workspace-root .mcp.json) only if no
            # runtimes are installed at all.
            if not target_runtimes and not installed_runtimes:
                target_runtimes = ["copilot"]
                selection_source = _TargetSelectionSource.FALLBACK
            else:
                selection_source = _TargetSelectionSource.DISCOVERY

    # Not every accepted target can host MCP servers: `grok-build` writes
    # native .grok configuration but has no MCP client adapter.  Drop
    # non-MCP-capable runtimes here so that expanding `--target all` does
    # not hard-fail the install on a runtime that cannot take MCP config.
    # ClientFactory is the single source of truth for MCP capability.
    # Direct `--runtime` calls bypass this: the adapter layer owns the
    # warning/error behavior for legacy runtime strings.
    if selection_source is not _TargetSelectionSource.RUNTIME:
        from apm_cli.factory import ClientFactory as _McpCF

        mcp_capable = _McpCF.supported_clients()
        non_mcp = [rt for rt in target_runtimes if rt not in mcp_capable]
        if non_mcp:
            target_runtimes = [rt for rt in target_runtimes if rt in mcp_capable]
            logger.progress(f"Skipped targets without MCP support: {', '.join(sorted(non_mcp))}")
        if not target_runtimes and non_mcp:
            logger.warning(
                "No selected target supports MCP -- skipping MCP configuration. "
                f"Targets without an MCP client: {', '.join(sorted(non_mcp))}. "
                "Re-run with an MCP-capable target (for example "
                "`--target copilot`) to install MCP servers."
            )
            return None

    # Exclusion narrows every selected source, including explicit CLI choices.
    # Apply it before progress output so the message names the narrowed set.
    if exclude:
        from apm_cli.integration.mcp_user_scope_runtimes import filter_excluded_mcp_runtimes

        target_runtimes = filter_excluded_mcp_runtimes(target_runtimes, exclude)
        # Invalid manifests continue to the shared gate for canonical rendering.
        if not target_runtimes and selection_source is not _TargetSelectionSource.INVALID_MANIFEST:
            logger.warning(
                f"All selected MCP runtimes excluded (--exclude {exclude}), "
                "skipping MCP configuration"
            )
            return None

    if selection_source is _TargetSelectionSource.RUNTIME:
        logger.progress(f"Targeting specific runtime: {', '.join(target_runtimes)}")
    elif selection_source is _TargetSelectionSource.TARGET:
        runtime_label = "runtime" if len(target_runtimes) == 1 else "runtimes"
        logger.progress(f"Targeting specific {runtime_label}: {', '.join(target_runtimes)}")
    elif selection_source is _TargetSelectionSource.MANIFEST:
        from apm_cli.core.target_detection import provenance_line

        # The install targets phase may already have announced the targets.
        logger.announce_targets(provenance_line(target_runtimes, "apm.yml"))
    elif selection_source is _TargetSelectionSource.FALLBACK:
        logger.progress("No runtimes installed, using VS Code as fallback")
    elif (
        selection_source is _TargetSelectionSource.DISCOVERY
        and target_runtimes
        and not verbose
        and console is None
    ):
        # Machine discovery is intentionally the unrestricted-manifest fallback.
        logger.progress(
            f"Detected runtimes: {', '.join(target_runtimes)} "
            "(auto-detected; add targets: to apm.yml for consistent results across machines)"
        )

    if (
        materialize
        and not user_scope
        and target_decision is not None
        and target_decision.canonical_targets is not None
    ):
        from apm_cli.integration.targets import (
            TargetRootObstructedError,
            materialize_project_target_profiles,
        )

        root = Path(project_root) if project_root is not None else Path.cwd()
        try:
            materialize_project_target_profiles(root, target_decision.canonical_targets)
        except (OSError, TargetRootObstructedError) as exc:
            raise RequiredIntegrationError(
                f"Cannot prepare target configuration directory: {exc}. "
                "Check directory permissions, then retry."
            ) from exc

    # Codex MCP is project-scoped: only configure it when Codex is an
    # active project target (silent skip, same as Cursor/OpenCode/Gemini).
    # Claude Code is gated identically: a host-wide `claude` binary should
    # not opt every APM project into `.mcp.json` writes.
    # Preserve direct-call compatibility for unknown legacy runtime strings:
    # the adapter layer owns their warning/error behavior. Known runtime names
    # are equivalent to explicit --target and therefore outrank manifest/signals.
    apply_project_gate = runtime is None or runtime in accepted_target_values("install")
    if apply_project_gate:
        target_runtimes = MCPIntegrator._gate_project_scoped_runtimes(
            target_runtimes,
            user_scope=user_scope,
            project_root=project_root,
            apm_config=apm_config,
            explicit_target=runtime or explicit_target,
            target_decision=target_decision,
        )

    # Explicit runtime/exclusion/gating can leave nothing to configure.
    if not target_runtimes:
        return None

    # Scope filtering: at USER scope, keep only global-capable runtimes.
    # Applied after both explicit --runtime and auto-discovery paths.
    from apm_cli.core.scope import InstallScope as _IS

    if scope is _IS.USER:
        from apm_cli.factory import ClientFactory as _CF

        pre_filter = list(target_runtimes)
        filtered_runtimes = []
        for rt in target_runtimes:
            try:
                client = _CF.create_client(rt)
            except ValueError:
                continue
            if client.supports_user_scope:
                filtered_runtimes.append(rt)
        target_runtimes = filtered_runtimes
        skipped = set(pre_filter) - set(target_runtimes)
        if skipped:
            msg = (
                f"Skipped workspace-only runtimes at user scope: "
                f"{', '.join(sorted(skipped))}"
                f" -- omit --global to install these"
            )
            logger.warning(msg)
        if not target_runtimes:
            logger.warning(
                "No runtimes support user-scope MCP installation "
                f"(supported: {', '.join(user_scope_runtime_labels())})"
            )
            return None

    return target_runtimes


def _print_user_wide_configs(console: Any, runtimes: list[str]) -> None:
    """Name config files a project install wrote outside each harness's own dir.

    Root ``.mcp.json`` (Copilot, Claude Code; the 0.37 Copilot move) and
    user-wide files (Windsurf has no workspace config) are named once (#3994).
    """
    from apm_cli.factory import ClientFactory

    shared = [rt for rt in runtimes if ClientFactory.project_destination_is_root_mcp_json(rt)]
    if shared:
        console.print(
            f"|     +- wrote: .mcp.json ({', '.join(harness_label(rt) for rt in shared)})",
            markup=False,
            highlight=False,
        )
    for rt in runtimes:
        path = ClientFactory.user_wide_config_display(rt)
        if path:
            console.print(
                f"|     +- user-wide: {path} (all {harness_label(rt)} workspaces)",
                markup=False,
                highlight=False,
            )


def _declared_mcp_targets(dep: Any) -> list[str] | None:
    declared = getattr(dep, "targets", None)
    return list(declared) if isinstance(declared, (list, tuple)) and declared else None


def mcp_dependency_runtimes(dep: Any, target_runtimes: list[str]) -> list[str]:
    """Narrow the run's runtimes to a dependency's ``targets:``; never widen them."""
    from apm_cli.core.target_detection import EffectiveTargetDecision
    from apm_cli.models.dependency.subsets import canonical_target_name

    declared = _declared_mcp_targets(dep)
    if declared is None:
        return list(target_runtimes)
    admitted = set(EffectiveTargetDecision(declared, "dependencies.mcp").runtime_equivalents)
    return [rt for rt in target_runtimes if rt in admitted or canonical_target_name(rt) in declared]


def mcp_servers_for_runtime(mcp_deps: list, runtime: str, *, named_only: bool = False) -> set[str]:
    """Declared servers *runtime* receives; ``named_only`` keeps those whose targets name it."""
    return {
        dep.name if hasattr(dep, "name") else dep
        for dep in mcp_deps
        if (isinstance(dep, str) or hasattr(dep, "name"))
        and (_declared_mcp_targets(dep) is not None or not named_only)
        and mcp_dependency_runtimes(dep, [runtime])
    }


def _install_self_defined_deps(
    self_defined_deps: list,
    target_runtimes: list[str],
    stored_mcp_configs: dict,
    servers_to_update: builtins.set,
    successful_updates: builtins.set,
    project_root,
    user_scope: bool,
    verbose: bool,
    console,
    logger,
    managed_target_servers: dict[str, set[str]] | None,
    fail_on_write_error: bool = False,
    native_hashes: dict[str, dict[str, str]] | None = None,
    written_connections: set[tuple[str, str]] | None = None,
    registry_dep_names: list[str] | None = None,
) -> int:
    """Install self-defined (``registry: false``) MCP deps for all target runtimes.

    Mutates ``servers_to_update`` and ``successful_updates`` in-place.
    Returns the number of servers newly configured or updated.
    """
    from apm_cli.adapters.client.base import MCPClientAdapter
    from apm_cli.install.mcp.native_runtimes import (
        native_runtimes_of,
        retire_native_runtime_claims,
    )
    from apm_cli.install.mcp.ownership import (
        NativeMcpConnectionState,
        claim_shared_destination,
        compare_native_mcp_connection,
        native_comparison_clients,
        peer_wrote_shared_entry,
        reconcile_legacy_truncated_key,
        retain_native_receipt,
    )
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    configured_count = 0
    failed_installations: list[str] = []
    native_conflicts: list[str] = []
    if written_connections is None:
        written_connections = set()
    retire_native_runtime_claims(
        [dep for dep in self_defined_deps if native_runtimes_of(dep)],
        target_runtimes,
        logger,
        project_root=project_root,
        user_scope=user_scope,
        stored_mcp_configs=stored_mcp_configs,
        managed_target_servers=managed_target_servers,
        native_hashes=native_hashes,
    )
    # Runtimes that load a server natively from its plugin never get APM's copy.
    self_defined_deps = [
        dep for dep in self_defined_deps if not set(target_runtimes) <= native_runtimes_of(dep)
    ]
    protected_clients = native_comparison_clients(
        target_runtimes, project_root=project_root, user_scope=user_scope
    )
    self_defined_names = [dep.name for dep in self_defined_deps]
    previously_owned = {
        rt: builtins.set(names) for rt, names in (managed_target_servers or {}).items()
    }
    previous_native_hashes = {rt: dict(hashes) for rt, hashes in (native_hashes or {}).items()}
    claimed_keys = builtins.set(self_defined_names) | {
        MCPClientAdapter.determine_config_key(name, None) for name in registry_dep_names or ()
    }
    self_defined_to_install = MCPIntegrator._check_self_defined_servers_needing_installation(
        self_defined_names,
        target_runtimes,
        project_root=project_root,
        user_scope=user_scope,
    )
    if protected_clients:
        # Native content must be checked even when every requested name exists.
        self_defined_to_install = list(self_defined_names)
    already_configured_candidates_sd = [
        name for name in self_defined_names if name not in self_defined_to_install
    ]

    # Detect config drift for "already configured" self-defined servers
    if stored_mcp_configs:
        drifted_sd = MCPIntegrator._detect_mcp_config_drift(
            self_defined_deps,
            stored_mcp_configs,
        )
        if drifted_sd:
            servers_to_update.update(drifted_sd)
            MCPIntegrator._append_drifted_to_install_list(self_defined_to_install, drifted_sd)
    already_configured_self_defined = [
        name for name in already_configured_candidates_sd if name not in servers_to_update
    ]
    mark_unknown_noop_forms(
        servers_to_update,
        already_configured_self_defined,
        configuring=self_defined_to_install,
        uncompared=[rt for rt in target_runtimes if rt not in protected_clients],
    )

    if already_configured_self_defined:
        if console:
            for name in already_configured_self_defined:
                console.print(
                    f"|  [green]{STATUS_SYMBOLS['check']}[/green] {name} "
                    f"[dim](already configured)[/dim]"
                )
        else:
            count = len(already_configured_self_defined)
            logger.success(f"{count} self-defined server(s) already configured")
            for name in already_configured_self_defined:
                logger.verbose_detail(f"{name} already configured, skipping")

    for dep in self_defined_deps:
        if dep.name not in self_defined_to_install:
            continue

        is_update = dep.name in servers_to_update
        synthetic_info = MCPIntegrator._build_self_defined_info(dep)
        self_defined_cache = {dep.name: synthetic_info}
        transport_label = dep.transport or "stdio"
        # Stdio env values live in _raw_stdio and resolve in the adapter
        # pipeline; env_overrides would shadow os.environ with the raw
        # placeholder string.
        self_defined_env = {} if transport_label == "stdio" else dep.env or {}
        action_text = "Updating" if is_update else "Configuring"
        if console:
            console.print(
                f"|  [cyan]{STATUS_SYMBOLS['running']}[/cyan]  {dep.name} "
                f"[dim](self-defined, {transport_label})[/dim]"
            )
            console.print(
                f"|     +- {action_text} for {', '.join([harness_label(rt) for rt in target_runtimes])}..."
            )
        else:
            logger.progress(
                f"{dep.name}: {action_text.lower()} for {', '.join(target_runtimes)}..."
            )

        successful_runtimes: list[str] = []
        failed_runtimes: list[str] = []
        for rt in target_runtimes:
            if rt in native_runtimes_of(dep):
                continue
            if _skip_unsupported_remote_transport(
                rt,
                dep.name,
                synthetic_info,
                logger,
                project_root=project_root,
                user_scope=user_scope,
                stored_config=stored_mcp_configs.get(dep.name),
                managed_target_servers=managed_target_servers,
                native_hashes=native_hashes,
            ):
                continue
            installed_servers: set[str] = set()
            managed_fields: set[str] = set()
            written_hashes: dict[str, str] = {}
            claim_shared_destination(
                managed_target_servers,
                native_hashes,
                rt,
                dep.name,
                user_scope=user_scope,
                written_connections=written_connections,
            )
            replace_existing = is_update and (
                managed_target_servers is None or dep.name in managed_target_servers.get(rt, set())
            )
            if rt in protected_clients:
                client = protected_clients[rt]
                stale_receipts: set[str] = set()
                state = compare_native_mcp_connection(
                    client,
                    dep.name,
                    requested_info=synthetic_info,
                    stored_config=stored_mcp_configs.get(dep.name),
                    managed=managed_target_servers is not None
                    and dep.name in managed_target_servers.get(rt, set()),
                    managed_fields=managed_fields,
                    native_hash=(native_hashes or {}).get(rt, {}).get(dep.name),
                    declaration_changed=is_update,
                    stale_receipts=stale_receipts,
                )
                if (
                    state
                    in {
                        NativeMcpConnectionState.MATCHING_FOREIGN,
                        NativeMcpConnectionState.CONFLICT,
                    }
                    and managed_target_servers is not None
                ):
                    managed_target_servers.get(rt, set()).discard(dep.name)
                    if native_hashes is not None:
                        native_hashes.get(rt, {}).pop(dep.name, None)
                forwarded = _codex_expanded_env_names(client, dep.name, synthetic_info)
                if state is NativeMcpConnectionState.CONFLICT:
                    _report_native_conflict(
                        dep.name,
                        rt,
                        client,
                        logger,
                        native_conflicts,
                        expanded_env=forwarded,
                        render=lambda client=client, info=synthetic_info: (
                            client.render_server_config(info)
                        ),
                    )
                    continue
                if forwarded and state is NativeMcpConnectionState.UPDATE_MANAGED:
                    logger.warning(
                        f"APM replaced a previously written value for {_ascii_names(forwarded)} "
                        f"in {client.get_config_path()} with env_vars. If that file was ever "
                        "shared or committed, rotate the credential because Git history "
                        "still holds it."
                    )
                if state in {
                    NativeMcpConnectionState.MATCHING_MANAGED,
                    NativeMcpConnectionState.MATCHING_FOREIGN,
                }:
                    if state is NativeMcpConnectionState.MATCHING_MANAGED:
                        if managed_target_servers is not None:
                            managed_target_servers.setdefault(rt, set()).add(dep.name)
                        retain_native_receipt(
                            native_hashes,
                            rt,
                            client,
                            dep.name,
                            requested_info=synthetic_info,
                            replace=dep.name in stale_receipts,
                        )
                    if peer_wrote_shared_entry(
                        rt, dep.name, written_connections, user_scope=user_scope
                    ):
                        successful_runtimes.append(rt)
                    logger.verbose_detail(f"{dep.name} already matches {rt}; no write")
                    continue
                replace_existing = state is NativeMcpConnectionState.UPDATE_MANAGED
            if verbose:
                logger.verbose_detail(f"Configuring {dep.name} for {rt}...")
            if MCPIntegrator._install_for_runtime(
                rt,
                [dep.name],
                self_defined_env,
                self_defined_cache,
                project_root=project_root,
                user_scope=user_scope,
                logger=logger,
                replace_existing=replace_existing,
                installed_servers=installed_servers,
                managed_fields=managed_fields,
                written_native_hashes=written_hashes,
            ):
                successful_runtimes.append(rt)
                if dep.name in installed_servers:
                    _record_managed_server(
                        managed_target_servers,
                        rt,
                        dep.name,
                        native_hashes=native_hashes,
                        written_hashes=written_hashes,
                        written_connections=written_connections,
                        requested_info=synthetic_info,
                    )
            else:
                failed_runtimes.append(rt)

        reconcile_legacy_truncated_key(
            dep.name,
            successful_runtimes,
            previously_owned=previously_owned,
            previous_native_hashes=previous_native_hashes,
            stored_config=stored_mcp_configs.get(dep.name),
            claimed_keys=claimed_keys,
            project_root=project_root,
            user_scope=user_scope,
            logger=logger,
            fail_on_write_error=fail_on_write_error,
        )

        if successful_runtimes:
            if console:
                label = "updated" if is_update else "configured"
                console.print(
                    f"|  [green]{STATUS_SYMBOLS['check']}[/green]  {dep.name} -> "
                    f"{', '.join([harness_label(rt) for rt in successful_runtimes])}"
                    f" [dim]({label})[/dim]"
                )
                if not user_scope:
                    _print_user_wide_configs(console, successful_runtimes)
            configured_count += 1
            if is_update:
                successful_updates.add(dep.name)
        if failed_runtimes:
            verdict_record.unknown("mcp failure")
            strict_failures = sorted(
                failed_runtimes
                if fail_on_write_error
                else set(failed_runtimes) & _STRICT_CONFIG_FAILURE_RUNTIMES
            )
            if strict_failures:
                failed_installations.append(f"{dep.name} ({', '.join(strict_failures)})")
            if console:
                console.print(
                    f"|  [red]{STATUS_SYMBOLS['cross']}[/red]  {dep.name}  "
                    f"-- failed for {', '.join(sorted(failed_runtimes))}"
                )
            else:
                logger.error(f"{dep.name} -- failed for {', '.join(sorted(failed_runtimes))}")

    if native_conflicts:
        message = "MCP installation incomplete: " + ", ".join(native_conflicts)
        logger.error(message)
        raise MCPNativeConflictError(message, wrote_connections=bool(written_connections))
    _raise_strict_config_failures(failed_installations, console=console, logger=logger)
    return configured_count


def _print_mcp_summary(
    console,
    configured_count: int,
    successful_updates: builtins.set,
) -> None:
    """Print the MCP install summary footer panel."""
    if not console:
        return
    if configured_count > 0:
        verdict_record.unknown("mcp configured servers")  # a write is never a no-op
        # Use successful_updates (not servers_to_update) for accurate counts.
        # servers_to_update = all drift-detected servers (some may have failed).
        # successful_updates = servers that were re-applied AND succeeded.
        update_count = builtins.len(successful_updates)
        new_count = configured_count - update_count
        parts = []
        if new_count > 0:
            parts.append(f"configured {new_count} server{'s' if new_count != 1 else ''}")
        if update_count > 0:
            parts.append(f"updated {update_count} server{'s' if update_count != 1 else ''}")
        console.print(f"[green]{STATUS_SYMBOLS['success']} {', '.join(parts).capitalize()}[/green]")
    else:
        console.print(f"[green]{STATUS_SYMBOLS['success']} All servers up to date[/green]")


def run_mcp_install(  # noqa: PLR0913
    mcp_deps: list,
    runtime: str | None = None,
    exclude: str | None = None,
    verbose: bool = False,
    apm_config: dict | None = None,
    stored_mcp_configs: dict | None = None,
    project_root=None,
    user_scope: bool = False,
    explicit_target: str | list[str] | None = None,
    target_decision: EffectiveTargetDecision | None = None,
    logger=None,
    diagnostics=None,
    scope: InstallScope | None = None,
    managed_target_servers: dict[str, set[str]] | None = None,
    prevalidated_registry_servers: dict[str, dict] | None = None,
    fail_on_write_error: bool = False,
    native_hashes: dict[str, dict[str, str]] | None = None,
    resolved_targets: ResolvedMcpTargets | None = None,
) -> int:
    """Install MCP dependencies.

    Args:
        mcp_deps: List of MCP dependency entries (registry strings or
            MCPDependency objects).
        runtime: Target specific runtime only.
        exclude: Exclude specific runtime from installation.
        verbose: Show detailed installation information.
        apm_config: The parsed apm.yml configuration dict (optional).
            When not provided, this function loads ``apm.yml`` from the project
            root if it exists.
        stored_mcp_configs: Previously stored MCP configs from lockfile
            for diff-aware installation.  When provided, servers whose
            manifest config has changed are re-applied automatically.
        project_root: Project root for repo-local runtime configs.
        user_scope: Whether runtime configuration is being resolved at user scope.
        explicit_target: Explicit target selected by CLI or manifest.
        scope: InstallScope (PROJECT or USER). When USER, only
            runtimes whose adapter declares ``supports_user_scope``
            are targeted; workspace-only runtimes are skipped.
        managed_target_servers: Mutable per-target APM ownership state.
        resolved_targets: Targets this run already resolved (``--frozen``
            checks them first); the install reuses them as they are.

    Returns:
        Number of MCP servers newly configured or updated.
    """
    # Local import: ``mcp_integrator`` must finish loading before this module
    # is first imported (``MCPIntegrator.install`` delegates here lazily).
    from apm_cli.integration.mcp_integrator import _get_console

    if logger is None:
        logger = NullCommandLogger()
    if not mcp_deps:
        logger.warning("No MCP dependencies found in apm.yml")
        return 0

    from apm_cli.core.scope import InstallScope

    # The explicit scope enum takes precedence over the raw user_scope bool
    # so callers cannot accidentally mix user-scope runtime filtering with
    # project-scope config writes (or the inverse).
    if scope is InstallScope.USER:
        user_scope = True
    elif scope is InstallScope.PROJECT:
        user_scope = False

    # Split into registry-resolved and self-defined deps
    # Backward compat: plain strings are treated as registry deps
    registry_deps = [
        dep
        for dep in mcp_deps
        if isinstance(dep, str)
        or (hasattr(dep, "is_registry_resolved") and dep.is_registry_resolved)
    ]
    self_defined_deps = [
        dep for dep in mcp_deps if hasattr(dep, "is_self_defined") and dep.is_self_defined
    ]
    registry_dep_names = [dep.name if hasattr(dep, "name") else dep for dep in registry_deps]

    console = _get_console()
    # Track servers that were re-applied due to config drift
    servers_to_update: builtins.set = builtins.set()
    # Track successful updates separately so the summary counts are accurate
    # even when some drift-detected servers fail to install.
    successful_updates: builtins.set = builtins.set()
    if stored_mcp_configs is None:
        stored_mcp_configs = {}

    # Start MCP section with clean header
    if console:
        try:
            from rich.text import Text

            header = Text()
            header.append("+- MCP Servers (", style="cyan")
            header.append(str(len(mcp_deps)), style="cyan bold")
            header.append(")", style="cyan")
            console.print(header)
        except Exception:
            logger.progress(f"Installing MCP dependencies ({len(mcp_deps)})...")
    else:
        logger.progress(f"Installing MCP dependencies ({len(mcp_deps)})...")

    # Runtime detection, gating, and scope filtering (once per run, #4089)
    target_runtimes = (
        resolved_targets
        or resolve_mcp_targets(
            runtime=runtime,
            exclude=exclude,
            verbose=verbose,
            apm_config=apm_config,
            project_root=project_root,
            user_scope=user_scope,
            explicit_target=explicit_target,
            target_decision=target_decision,
            scope=scope,
            logger=logger,
            console=console,
        )
    ).runtime_list()
    if target_runtimes is None:
        if fail_on_write_error:
            raise RequiredIntegrationError(
                "MCP dependencies are declared, but no effective target can accept "
                "their configuration. Choose a supported --target and retry."
            )
        return 0

    _validate_target_configs(
        target_runtimes,
        project_root=project_root,
        user_scope=user_scope,
        console=console,
        logger=logger,
    )
    _resolve_frozen_deferred_pairs(
        mcp_deps, target_runtimes, project_root=project_root, user_scope=user_scope
    )
    claude_targeted = "claude" in target_runtimes
    if claude_targeted:
        # Claude's diagnostics cover every server of this run, so they are
        # emitted once at the end rather than after the first server.
        from apm_cli.adapters.client.claude import ClaudeClientAdapter

        ClaudeClientAdapter.reset_install_run_state()
    if "copilot" in target_runtimes and not user_scope:
        from apm_cli.adapters.client.copilot_project import CopilotProjectClientAdapter

        CopilotProjectClientAdapter.reset_install_run_state()
    if managed_target_servers is not None:
        from apm_cli.install.mcp.ownership import migrate_legacy_project_target_servers

        migrate_legacy_project_target_servers(
            managed_target_servers,
            active_runtimes=set(target_runtimes),
            user_scope=user_scope,
        )

    _migrate_intellij_managed_config(
        target_runtimes,
        managed_target_servers,
        project_root=project_root,
        user_scope=user_scope,
        logger=logger,
    )

    if managed_target_servers is not None:
        from apm_cli.install.mcp.ownership import inherit_shared_destination_claims

        active_targets = set(target_runtimes)
        inherit_shared_destination_claims(
            managed_target_servers,
            native_hashes,
            active_runtimes=active_targets,
            user_scope=user_scope,
        )
        from apm_cli.install.mcp.ownership import transfer_deselected_shared_claims

        selected = {target: mcp_servers_for_runtime(mcp_deps, target) for target in active_targets}
        transfer_deselected_shared_claims(
            managed_target_servers, native_hashes, selected=selected, user_scope=user_scope
        )
        for target in list(managed_target_servers):
            if target not in active_targets:
                del managed_target_servers[target]
            else:
                managed_target_servers[target] &= selected[target]
        for target in target_runtimes:
            managed_target_servers.setdefault(target, set())

    if "codex" in target_runtimes and managed_target_servers is not None:
        from apm_cli.adapters.client.codex import CodexClientAdapter
        from apm_cli.core.conflict_detector import MCPConflictDetector
        from apm_cli.install.mcp.ownership import (
            NativeMcpConnectionState,
            compare_native_mcp_connection,
        )

        client = CodexClientAdapter(project_root=project_root, user_scope=user_scope)
        owned_names = managed_target_servers.get("codex", set())
        existing = MCPConflictDetector(client).get_existing_server_configs() if owned_names else {}
        verified_names = {
            name
            for name in owned_names
            if compare_native_mcp_connection(
                client,
                name,
                requested_info=None,
                stored_config=stored_mcp_configs.get(name),
                managed=True,
                existing_configs=existing,
            )
            is NativeMcpConnectionState.UPDATE_MANAGED
        }
        migrated = client.migrate_legacy_managed_servers(verified_names)
        if migrated:
            count = len(migrated)
            logger.success(
                f"Repaired {count} Codex MCP {'entry' if count == 1 else 'entries'}",
                symbol="check",
            )

    dep_runtimes = {id(dep): mcp_dependency_runtimes(dep, target_runtimes) for dep in mcp_deps}
    scoped_out: set[int] = set()
    for dep in mcp_deps:
        declared = _declared_mcp_targets(dep)
        if declared is not None and not dep_runtimes[id(dep)]:
            scoped_out.add(id(dep))
            # Logged inline: an MCP-only run that changes nothing never
            # renders the diagnostics summary, which would hide the skip.
            logger.warning(
                f"{dep.name}: MCP server targets [{', '.join(declared)}] do not overlap "
                f"active install targets [{', '.join(target_runtimes)}]; skipping"
            )

    # Use the new registry operations module for better server detection
    configured_count = 0
    written_connections: set[tuple[str, str]] = set()
    native_conflicts: list[str] = []

    # --- Registry-based deps ---
    if registry_dep_names:
        verdict_record.unknown("registry mcp")  # its no-op form is not known
        try:
            from apm_cli.registry.operations import MCPServerOperations

            # Group deps by their per-dep registry URL so each group is
            # resolved against the correct registry endpoint.
            # Plain strings (backward-compat) and deps with registry=None go to
            # the default group (key=None).  Only str values trigger routing.
            registry_groups: builtins.dict[tuple, list] = {}
            for dep in registry_deps:
                key = (tuple(dep_runtimes[id(dep)]), _registry_group_key(dep))
                if id(dep) not in scoped_out:
                    registry_groups.setdefault(key, []).append(dep)

            for (group_runtimes, group_registry_url), group_deps_list in registry_groups.items():
                group_dep_names = [
                    dep.name if hasattr(dep, "name") else dep for dep in group_deps_list
                ]
                group_dep_map = {dep.name: dep for dep in group_deps_list if hasattr(dep, "name")}
                group_prevalidated = prevalidated_registry_servers
                group_registry_source = None
                if isinstance(group_prevalidated, _PrevalidatedRegistryServers):
                    if (
                        group_registry_url == group_prevalidated.registry_url
                        and set(group_dep_names) <= group_prevalidated.keys()
                    ):
                        group_registry_source = group_prevalidated.source
                    else:
                        group_prevalidated = None
                if group_registry_source is not None:
                    operations = MCPServerOperations(
                        registry_url=group_registry_url,
                        registry_source=group_registry_source,
                    )
                else:
                    operations = MCPServerOperations(registry_url=group_registry_url)
                try:
                    configured_count += _install_registry_group(
                        operations=operations,
                        group_dep_names=group_dep_names,
                        group_dep_map=group_dep_map,
                        group_deps=group_deps_list,
                        target_runtimes=list(group_runtimes),
                        stored_mcp_configs=stored_mcp_configs,
                        servers_to_update=servers_to_update,
                        successful_updates=successful_updates,
                        project_root=project_root,
                        user_scope=user_scope,
                        verbose=verbose,
                        console=console,
                        logger=logger,
                        managed_target_servers=managed_target_servers,
                        prevalidated_servers=group_prevalidated,
                        fail_on_write_error=fail_on_write_error,
                        native_hashes=native_hashes,
                        written_connections=written_connections,
                    )
                except MCPNativeConflictError as exc:
                    native_conflicts.append(str(exc))

        except ImportError:
            logger.warning("Registry operations not available")
            logger.error("Cannot validate MCP servers without registry operations")
            raise RuntimeError("Registry operations module required for MCP installation")  # noqa: B904

    # --- Self-defined deps (registry: false) ---
    self_defined_groups: builtins.dict[tuple, list] = {}
    for dep in self_defined_deps:
        if id(dep) not in scoped_out:
            self_defined_groups.setdefault(tuple(dep_runtimes[id(dep)]), []).append(dep)
    for group_runtimes, group_self_defined in self_defined_groups.items():
        try:
            configured_count += _install_self_defined_deps(
                self_defined_deps=group_self_defined,
                target_runtimes=list(group_runtimes),
                stored_mcp_configs=stored_mcp_configs,
                servers_to_update=servers_to_update,
                successful_updates=successful_updates,
                project_root=project_root,
                user_scope=user_scope,
                verbose=verbose,
                console=console,
                logger=logger,
                managed_target_servers=managed_target_servers,
                fail_on_write_error=fail_on_write_error,
                native_hashes=native_hashes,
                written_connections=written_connections,
                registry_dep_names=registry_dep_names,
            )
        except MCPNativeConflictError as exc:
            native_conflicts.append(str(exc))

    if native_conflicts:
        if claude_targeted:
            ClaudeClientAdapter.emit_install_run_summary()
        raise MCPNativeConflictError(
            "\n".join(native_conflicts), wrote_connections=bool(written_connections)
        )

    # Close the panel
    _print_mcp_summary(console, configured_count, successful_updates)
    if claude_targeted:
        ClaudeClientAdapter.emit_install_run_summary()

    return configured_count
