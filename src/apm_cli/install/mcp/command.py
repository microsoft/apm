"""Orchestrator for the ``apm install --mcp`` code path.

Extracted from ``commands/install.py`` per the architecture-invariants
LOC budget. ``run_mcp_install`` composes the sibling MCP modules
(``args``, ``entry``, ``writer``, ``warnings``, ``registry``) into the
user-visible install flow:

    parse args -> build entry -> warn -> write apm.yml -> integrate
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import click

from apm_cli.core.scope import manifest_file

from ..errors import InstallFailureAlreadyRendered, MCPNativeConflictError

if TYPE_CHECKING:
    from apm_cli.core.scope import InstallScope
    from apm_cli.core.target_detection import EffectiveTargetDecision

from .args import parse_env_pairs, parse_header_pairs
from .entry import build_mcp_entry
from .ownership import names_owned_by_active_peers
from .registry import registry_env_override
from .retired_destinations import (
    RetiredMcpClaims,
    heal_retired_project_mcp_destinations,
    healable_names,
    report_retired_mcp_heal,
    restore_pending_retired_claims,
    split_mcp_claims,
)
from .warnings import warn_shell_metachars, warn_ssrf_url
from .writer import add_mcp_to_apm_yml

# APM Dependencies (conditional import for graceful degradation).
# Mirrors the pattern in ``commands/install.py`` so the success/log
# behaviour around a missing optional dep is symmetric across the two
# code paths (package install vs. MCP install).
APM_DEPS_AVAILABLE = False
try:
    from ...deps.lockfile import (
        LockFile,
        LockfileConflictError,
        LockfileRefusal,
        get_lockfile_path,
        migrate_lockfile_if_needed,
    )
    from ...integration.mcp_integrator import MCPIntegrator

    APM_DEPS_AVAILABLE = True
except ImportError:
    pass


def user_manifest_bootstrap_config(
    manifest_path: Path,
    supported_runtimes: list[str],
    target: str | Sequence[str] | None,
    runtime: str | Sequence[str] | None,
) -> dict | None:
    """Config for the user manifest ``apm install -g --mcp`` creates, if any.

    ``None`` when the user manifest already exists.

    An explicit ``--target`` / ``--runtime`` is recorded under ``targets:``
    through the manifest target declaration owner, so the new manifest never
    holds a token the reader rejects (#3870); a target with no ``targets:``
    spelling is refused here, before anything is written.
    """
    from ...core.project_name import resolve_bootstrap_project_name
    from ...models.manifest_bootstrap import bootstrap_config
    from ...models.manifest_targets import (
        IdeTargetError,
        TargetRewriteError,
        bootstrap_target_names,
    )

    if manifest_path.exists():
        return None
    config = bootstrap_config(resolve_bootstrap_project_name(Path.home().name))
    flag, requested = ("--target", target) if target is not None else ("--runtime", runtime)
    if requested is None:
        return config
    try:
        config["targets"] = list(bootstrap_target_names(supported_runtimes))
    except TargetRewriteError as exc:
        if exc.token is None:
            raise
        rendered = requested if isinstance(requested, str) else ",".join(requested)
        label = manifest_path.as_posix()
        reason = (
            f"{exc}"
            if isinstance(exc, IdeTargetError)
            else f"target '{exc.token}' has no 'targets:' equivalent, so later "
            "'apm install -g' runs could not read the new manifest."
        )
        # An IDE is a refused value, not a usage mistake: exit 1 like every
        # other apm.yml writer (#3878).
        error = click.ClickException if isinstance(exc, IdeTargetError) else click.UsageError
        raise error(
            f"Cannot create {label} for {flag} {rendered}: {reason} Nothing was written. "
            f"Create {label} with just 'name:' and 'version:', then rerun; {flag} "
            f"{rendered} will apply to that install only."
        ) from None
    return config


def run_mcp_policy_preflight(
    *,
    policy_root: Path,
    mcp_name: str,
    transport: str | None,
    url: str | None,
    command_argv: Sequence[str] | None,
    no_policy: bool,
    logger,
    target_decision: EffectiveTargetDecision,
    registry: tuple[str | None, str | None] = (None, None),
) -> None:
    """Run policy checks for a direct MCP install before any persistent write.

    *registry* is the ``(url, source)`` pair from ``_resolve_registry_url``;
    the registry override is active so the transport check resolves a
    registry server against the same registry the install will query.
    *policy_root* is the scope manifest root (``$APM_HOME`` under ``-g``).
    """
    registry_url, registry_source = registry
    from ...models.dependency.mcp import MCPDependency
    from ...policy.install_preflight import PolicyBlockError, run_policy_preflight

    resolved_transport = transport
    if resolved_transport is None:
        if command_argv:
            resolved_transport = "stdio"
        elif url:
            resolved_transport = "http"
    dependency = MCPDependency(
        name=mcp_name,
        transport=resolved_transport,
        registry=False if url or command_argv else None,
        url=url,
    )
    try:
        with registry_env_override(
            registry_url,
            allow_http=registry_source == "flag",
            source=registry_source,
        ):
            run_policy_preflight(
                project_root=policy_root,
                mcp_deps=[dependency],
                no_policy=no_policy,
                logger=logger,
                dry_run=logger.dry_run,
                effective_target=target_decision.canonical_targets,
                effective_target_source=target_decision.source,
            )
    except PolicyBlockError:
        logger.render_summary()
        raise click.exceptions.Exit(1) from None


def run_mcp_install(  # noqa: PLR0913
    *,
    mcp_name: str,
    transport: str | None,
    url: str | None,
    env_pairs: Sequence[str] | None,
    header_pairs: Sequence[str] | None,
    mcp_version: str | None,
    command_argv: Sequence[str] | None,
    dev: bool,
    force: bool,
    runtime: str | None,
    exclude: str | None,
    logger,
    apm_dir: Path,
    scope: InstallScope | None,
    target: str | list[str] | None = None,
    target_decision: EffectiveTargetDecision | None = None,
    registry_url: str | None = None,
    registry_allow_http: bool = True,
    registry_source: str | None = None,
    initial_manifest_config: dict | None = None,
    retry_args: tuple[str, ...] = (),
) -> None:
    """Execute the --mcp install path. ``registry_url`` is the validated
    --registry value; the caller resolved precedence vs MCP_REGISTRY_URL.
    ``manifest_path`` is derived from ``apm_dir`` (``apm_dir / 'apm.yml'``)."""

    manifest_path = manifest_file(apm_dir)
    verbose = logger.verbose
    from ...models.dependency.mcp import MCPDependency

    env = parse_env_pairs(env_pairs)
    headers = parse_header_pairs(header_pairs)

    # Build entry (validates through MCPDependency).  Convert ValueError
    # to UsageError so the CLI exits 2 with the model wording.
    try:
        entry, is_self_defined = build_mcp_entry(
            mcp_name,
            transport=transport,
            url=url,
            env=env,
            headers=headers,
            version=mcp_version,
            command_argv=command_argv,
            registry_url=registry_url,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc))  # noqa: B904

    # F5 + F7 warnings -- do not block.  Source the stdio command from the
    # CLI input rather than the built ``entry``: ``entry`` is ``str`` for
    # bare-string registry shorthand and ``dict`` otherwise, so ``entry.get``
    # is unsafe.
    warn_ssrf_url(url, logger)
    stdio_command = command_argv[0] if command_argv else None
    warn_shell_metachars(env, logger, command=stdio_command)

    # Build MCPDependency for install.  ``entry`` may be a bare string.
    if isinstance(entry, str):
        dep = MCPDependency.from_string(entry)
    else:
        dep = MCPDependency.from_dict(entry)

    prevalidated_registry_servers = None
    if not is_self_defined:
        if not APM_DEPS_AVAILABLE:
            raise click.ClickException(
                f"MCP registry validation is unavailable for '{mcp_name}'. "
                "Install the package dependencies and retry; no state was changed"
            )
        with registry_env_override(
            registry_url,
            allow_http=registry_allow_http,
            source=registry_source,
        ):
            try:
                prevalidated_registry_servers = MCPIntegrator.prevalidate_registry_dependencies(
                    [dep],
                    registry_url=registry_url,
                    registry_source=registry_source,
                    verbose=verbose,
                    logger=logger,
                    retry_args=retry_args,
                )
            except Exception as exc:
                from ...registry.client import RegistryEgressRefusedError

                detail = str(exc)
                if isinstance(exc, RegistryEgressRefusedError) or detail.startswith(
                    "Could not reach MCP registry"
                ):
                    logger.error(f"{detail} No state was changed.")
                    raise InstallFailureAlreadyRendered(detail) from None
                if detail.startswith("Cannot install ") and "missing server" in detail:
                    raise InstallFailureAlreadyRendered(detail) from None
                logger.verbose_detail(f"MCP registry validation error: {detail}")
                raise click.ClickException(
                    f"MCP registry validation failed for '{mcp_name}'. Check the server "
                    "name and registry reachability/configuration, then retry; "
                    "no state was changed"
                ) from None

    if getattr(logger, "dry_run", False) is True:
        logger.dry_run_notice(f"would add MCP server '{mcp_name}' to {manifest_path}")
        return

    if initial_manifest_config is not None:
        from ...models.manifest_bootstrap import write_minimal_manifest

        apm_dir.mkdir(parents=True, exist_ok=True)
        write_minimal_manifest(initial_manifest_config, target_path=manifest_path)
        logger.success(f"Created {manifest_path}")

    status, _diff = add_mcp_to_apm_yml(
        mcp_name,
        entry,
        dev=dev,
        force=force,
        manifest_path=manifest_path,
        logger=logger,
    )
    if status == "skipped":
        logger.progress(f"MCP server '{mcp_name}' already declared; checking integrations")

    # Install just this MCP via the integrator and update lockfile.
    # ``registry_env_override`` exports MCP_REGISTRY_URL for THIS call so
    # MCPServerOperations() (constructed deep inside MCPIntegrator.install)
    # picks up the override; prior env restored on exit.
    if APM_DEPS_AVAILABLE:
        if target is not None and logger:
            rendered_target = target if isinstance(target, str) else ", ".join(target)
            logger.verbose_detail(f"Target: {rendered_target}")
        with registry_env_override(
            registry_url,
            allow_http=registry_allow_http,
            source=registry_source,
        ):
            try:
                # Migrate before creating apm.lock.yaml so legacy state is not shadowed.
                migrate_lockfile_if_needed(apm_dir)
                _mcp_lock_path = get_lockfile_path(apm_dir)
                _existing_lock = LockFile.read(_mcp_lock_path)
                from ...core.deployment_ledger import DeploymentLedgerCodec

                native_hashes = (
                    DeploymentLedgerCodec.mcp_native_hashes(_existing_lock)
                    if _existing_lock
                    else {}
                )
                old_native_hashes = {
                    target_name: dict(hashes) for target_name, hashes in native_hashes.items()
                }
                old_servers = set(_existing_lock.mcp_servers) if _existing_lock else set()
                old_configs = dict(_existing_lock.mcp_configs) if _existing_lock else {}
                old_provenance = (
                    dict(_existing_lock.mcp_config_provenance) if _existing_lock else {}
                )
                old_target_servers = (
                    {
                        target_name: set(server_names)
                        for target_name, server_names in _existing_lock.mcp_target_servers.items()
                    }
                    if _existing_lock
                    else {}
                )
                target_servers_present = (
                    _existing_lock._mcp_target_servers_present if _existing_lock else True
                )
                from ...core.scope import InstallScope

                user_scope = scope is InstallScope.USER
                retired = RetiredMcpClaims({}, {})
                if _existing_lock:
                    from .ownership import resolve_mcp_target_servers

                    old_target_servers = resolve_mcp_target_servers(
                        recorded_target_servers=old_target_servers,
                        ownership_present=target_servers_present,
                        server_names=old_servers,
                        stored_configs=old_configs,
                        project_root=apm_dir,
                        user_scope=user_scope,
                    )
                    # Claims naming the retired Copilot MCP files are healed by
                    # proof, never compared or cleaned as .mcp.json ownership.
                    retired = split_mcp_claims(
                        old_target_servers,
                        native_hashes,
                        apm_version=_existing_lock.apm_version,
                        project_root=apm_dir,
                        user_scope=user_scope,
                        stored_configs=old_configs,
                    )
                requested_target_servers = {
                    target_name: server_names & {mcp_name}
                    for target_name, server_names in old_target_servers.items()
                    if server_names & {mcp_name}
                }
                from ...integration.mcp_integrator_install import _validate_target_configs

                _validate_target_configs(
                    list(requested_target_servers),
                    project_root=apm_dir,
                    user_scope=user_scope,
                    console=None,
                    logger=logger,
                )
                # Legacy --runtime remains a direct override. Target values
                # are projected through the shared decision below.
                conflict = None
                try:
                    MCPIntegrator.install(
                        [dep],
                        runtime,
                        exclude,
                        verbose,
                        stored_mcp_configs=old_configs,
                        explicit_target=target,
                        target_decision=target_decision,
                        fail_on_write_error=True,
                        scope=scope,
                        project_root=apm_dir,
                        user_scope=user_scope,
                        managed_target_servers=requested_target_servers,
                        prevalidated_registry_servers=prevalidated_registry_servers,
                        native_hashes=native_hashes,
                    )
                except MCPNativeConflictError as exc:
                    if not exc.wrote_connections:
                        raise
                    conflict = exc
                new_names = MCPIntegrator.get_server_names([dep])
                new_configs = MCPIntegrator.get_server_configs([dep])
                merged_names = old_servers | new_names
                merged_configs = dict(old_configs)
                merged_configs.update(new_configs)
                merged_provenance = dict(old_provenance)
                merged_provenance.pop(mcp_name, None)
                merged_target_servers = {
                    target_name: set(server_names)
                    for target_name, server_names in old_target_servers.items()
                }
                for target_name, server_names in requested_target_servers.items():
                    merged_target_servers.setdefault(target_name, set()).discard(mcp_name)
                    merged_target_servers.setdefault(target_name, set()).update(server_names)
                if retired.names:
                    healed = healable_names(
                        {runtime: names & {mcp_name} for runtime, names in retired.names.items()},
                        declared={mcp_name},
                        copilot_claims=requested_target_servers.get("copilot")
                        if "copilot" in requested_target_servers
                        else None,
                    )
                    report_retired_mcp_heal(
                        heal_retired_project_mcp_destinations(
                            healed,
                            stored_configs=old_configs,
                            native_hashes=retired.hashes,
                            project_root=apm_dir,
                        ),
                        logger,
                    )
                    restore_pending_retired_claims(
                        retired,
                        healed,
                        target_servers=merged_target_servers,
                        native_hashes=native_hashes,
                        project_root=apm_dir,
                        stored_configs=old_configs,
                    )
                retired_targets = {
                    previous_target: {mcp_name}
                    for previous_target, server_names in old_target_servers.items()
                    if mcp_name in server_names and previous_target not in requested_target_servers
                }
                if retired_targets and conflict is None:
                    # Cleanup can fail after a destination write. Keep both deployments
                    # durably owned until removal succeeds, so retry cannot orphan either.
                    MCPIntegrator.update_lockfile(
                        merged_names,
                        _mcp_lock_path,
                        mcp_configs=merged_configs,
                        mcp_target_servers=merged_target_servers,
                        mcp_config_provenance=merged_provenance,
                        native_hashes=native_hashes,
                        logger=logger,
                        fail_on_write_error=True,
                    )
                    for previous_target, retired in retired_targets.items():
                        # An entry in a shared file (.mcp.json) stays while the
                        # newly selected peer owns it; only the claim retires.
                        removable = retired - names_owned_by_active_peers(
                            previous_target,
                            retired,
                            requested_target_servers,
                            user_scope=user_scope,
                        )
                        if removable:
                            MCPIntegrator.remove_stale(
                                removable,
                                runtime=previous_target,
                                project_root=apm_dir,
                                user_scope=user_scope,
                                scope=scope,
                                fail_on_write_error=True,
                                stored_configs=old_configs,
                                native_hashes=old_native_hashes,
                            )
                        merged_target_servers[previous_target].difference_update(retired)
                MCPIntegrator.update_lockfile(
                    merged_names,
                    _mcp_lock_path,
                    mcp_configs=merged_configs,
                    mcp_target_servers=merged_target_servers,
                    mcp_config_provenance=merged_provenance,
                    native_hashes=native_hashes,
                    logger=logger,
                    fail_on_write_error=True,
                )
                if conflict is not None:
                    raise conflict
            except InstallFailureAlreadyRendered:
                raise
            except LockfileConflictError as exc:
                # The conflict diagnostic names only the lockfile path and the
                # manual next step, so it carries no raw parser content.
                logger.error(str(exc))
                logger.error("MCP server written to apm.yml but tool integration failed.")
                raise click.ClickException(f"MCP integration failed for '{mcp_name}'") from exc
            except Exception as exc:
                refusal = exc if isinstance(exc, LockfileRefusal) else exc.__cause__
                if isinstance(refusal, LockfileRefusal):
                    logger.error(str(refusal))
                    logger.error("MCP server written to apm.yml but tool integration failed.")
                    raise click.ClickException(f"MCP integration failed for '{mcp_name}'") from exc
                # Keep the raw exception (which may contain internal paths,
                # credentials, or stack-trace fragments) at verbose level
                # only; surface a fixed actionable string to the user, then
                # fail with exit 1 so CI does not see a green run on a
                # partial-failure path (apm.yml mutated, integration didn't
                # complete).
                logger.verbose_detail(f"MCP integration error: {exc}")
                logger.error(
                    "MCP server written to apm.yml but tool integration "
                    "failed. Run with --verbose for details."
                )
                raise click.ClickException(f"MCP integration failed for '{mcp_name}'")  # noqa: B904

    if status == "skipped":
        logger.success(f"Verified MCP server '{mcp_name}' integration", symbol="check")
        return

    verb = "Replaced" if status == "replaced" else "Added"
    logger.success(f"{verb} MCP server '{mcp_name}'", symbol="check")
    if isinstance(entry, dict):
        chosen_transport = entry.get("transport") or "registry"
    else:
        chosen_transport = "registry"
    logger.tree_item(f"  transport: {chosen_transport}")
    logger.tree_item(f"  apm.yml: {manifest_path}")
