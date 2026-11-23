"""Project native MCP collisions before an install writes anything (#4268).

The commit gate (install/commit_gate.py) calls :func:`project_native_mcp_conflicts`
before the first deployed byte, and ``apm install --mcp`` (install/mcp/command.py)
before it writes apm.yml (#4759). It asks the canonical comparator
(:func:`~apm_cli.install.mcp.ownership.compare_native_mcp_connection`) about
every self-defined server the install will write, and reports a
``CONFLICT`` with the same diagnostic the MCP write path renders. The
projection only flags pairs whose answer cannot depend on ownership state:
servers the lockfile never recorded, compared as unmanaged. Anything else
(recorded claims, registry servers whose render needs the registry) is left
to the MCP write path, which keeps the same comparison as its backstop.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def recorded_mcp_names(lockfile: Any) -> frozenset[str]:
    """Every MCP server name *lockfile* records ownership or a declaration for."""
    if lockfile is None:
        return frozenset()
    names = set(lockfile.mcp_servers or ()) | set(lockfile.mcp_configs or {})
    for servers in (lockfile.mcp_target_servers or {}).values():
        names.update(servers)
    return frozenset(names)


def _needs_input(info: dict) -> bool:
    """Whether rendering *info* may need a value the MCP stage resolves or prompts for.

    Any placeholder (docker args, remote headers or URLs) counts: an adapter
    may prompt for it, and the install resolves it from group-level inputs.
    """
    if "${" in str(info.get("docker") or "") or "${" in str(info.get("remotes") or ""):
        return True
    for package in info.get("packages") or ():
        if not isinstance(package, dict):
            continue
        if package.get("environmentVariables") or package.get("environment_variables"):
            return True
        for field in ("runtime_arguments", "package_arguments"):
            if any(
                isinstance(arg, dict) and arg.get("variables") for arg in package.get(field) or ()
            ):
                return True
    return False


def _requested(dep: Any) -> tuple[dict, dict | None] | None:
    """The info the MCP stage renders for *dep*, and its env overrides; None when unknowable.

    A registry server is looked up as its install looks it up (the snapshot
    policy judged first). One whose render needs prompted values, or whose
    lookup fails, is left to the MCP write.
    """
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    if getattr(dep, "is_self_defined", False):
        return MCPIntegrator._build_self_defined_info(dep), None
    if not getattr(dep, "is_registry_resolved", False):
        return None
    try:
        from apm_cli.integration.mcp_integrator_install import (
            _overlaid_server_infos,
            _registry_group_key,
        )
        from apm_cli.integration.mcp_transport_resolution import policy_checked_registry_info
        from apm_cli.registry.operations import MCPServerOperations

        operations = MCPServerOperations(registry_url=_registry_group_key(dep))
        checked = policy_checked_registry_info(operations.registry_client.registry_url, dep.name)
        known = {dep.name: checked} if checked is not None else None
        info = _overlaid_server_infos(operations, [dep.name], {dep.name: dep}, known=known).get(
            dep.name
        )
    except Exception:  # an unreachable registry is reported by the install itself
        return None
    # Declared env feeds the group-wide overrides the install merges across
    # registry servers, so such a server is left to the MCP write.
    if not isinstance(info, dict) or dep.env or _needs_input(info):
        return None
    return info, {}


def _recorded_baselines(lockfile: Any, name: str) -> list[str | None]:
    """The native hashes any runtime recorded for *name*, or one hashless baseline."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    hashes = {
        hashes[name]
        for hashes in DeploymentLedgerCodec.mcp_native_hashes(lockfile).values()
        if name in hashes
    }
    return sorted(hashes) or [None]


def _claims_settle_in_the_mcp_stage(lockfile: Any, *, replay_claims: bool) -> bool:
    """Recorded claims an older release wrote, or a ``--frozen`` replay, settle only there."""
    from apm_cli.install.mcp.retired_destinations import lock_holds_untranslated_mcp_claims

    return replay_claims or lock_holds_untranslated_mcp_claims(
        lockfile.apm_version, lockfile.mcp_target_servers
    )


def project_native_mcp_conflicts(
    dependencies: Iterable[Any],
    target_runtimes: list[str],
    *,
    lockfile: Any,
    project_root: Any,
    user_scope: bool,
    logger: Any,
    replay_claims: bool = False,
) -> list[str]:
    """Report each ``name (runtime)`` collision the MCP write would hit, without writing.

    A server the lockfile never recorded is compared as unmanaged (#4268). A
    recorded one is compared as managed against every baseline the lockfile
    holds for it, and counts only when each of them conflicts: then the MCP
    stage's comparison, whatever ownership it settles on, conflicts too (#4741).
    """
    from apm_cli.adapters.client._remote_shapes import unsupported_remote_transport
    from apm_cli.install.mcp.native_runtimes import native_runtimes_of
    from apm_cli.install.mcp.ownership import (
        NativeMcpConnectionState,
        compare_native_mcp_connection,
        native_comparison_clients,
    )
    from apm_cli.integration.mcp_integrator_install import (
        _codex_expanded_env_names,
        _report_native_conflict,
        mcp_dependency_runtimes,
    )

    clients = native_comparison_clients(
        target_runtimes, project_root=project_root, user_scope=user_scope
    )
    recorded = recorded_mcp_names(lockfile)
    settle_later = bool(recorded) and _claims_settle_in_the_mcp_stage(
        lockfile, replay_claims=replay_claims
    )
    conflicts: list[str] = []
    for dep in dependencies:
        name = getattr(dep, "name", None)
        if not isinstance(name, str) or (name in recorded and settle_later):
            continue
        requested = _requested(dep)
        if requested is None:
            continue
        info, env = requested
        registry = env is not None
        stored = (lockfile.mcp_configs or {}).get(name) if name in recorded else None
        baselines = _recorded_baselines(lockfile, name) if name in recorded else [None]
        for runtime in mcp_dependency_runtimes(dep, target_runtimes):
            client = clients.get(runtime)
            if (
                client is None
                or runtime in native_runtimes_of(dep)
                or unsupported_remote_transport(client, info) is not None
            ):
                continue
            try:
                states = {
                    compare_native_mcp_connection(
                        client,
                        name,
                        requested_info=info,
                        stored_config=stored,
                        managed=name in recorded,
                        native_hash=baseline,
                        registry_request=registry,
                        env_overrides=env,
                        runtime_vars={} if registry else None,
                    )
                    for baseline in baselines
                }
            except (OSError, TypeError, ValueError):
                continue  # an unreadable config is reported by the MCP stage itself
            if states == {NativeMcpConnectionState.CONFLICT}:
                _report_native_conflict(
                    name,
                    runtime,
                    client,
                    logger,
                    conflicts,
                    expanded_env=None
                    if registry
                    else _codex_expanded_env_names(client, name, info),
                    render=lambda client=client, info=info, env=env: (
                        client.render_server_config(info, env_overrides=env, runtime_vars={})
                        if env is not None
                        else client.render_server_config(info)
                    ),
                )
    return conflicts


class _Silent:
    """Discard target-resolution output; the MCP stage announces its own targets."""

    verbose = False

    def __getattr__(self, _name: str) -> Any:
        return lambda *_args, **_kwargs: None


def refuse_projected_mcp_collisions(
    dependencies: Iterable[Any],
    *,
    lockfile: Any,
    project_root: Any,
    user_scope: bool,
    logger: Any,
    replay_claims: bool = False,
    **targets: Any,
) -> None:
    """Refuse before any write when a projected MCP server collides (#4268, #4741).

    *targets* are the ``runtime``, ``exclude``, ``apm_config``,
    ``explicit_target``, ``target_decision`` and ``scope`` the MCP stage
    resolves its runtimes from.
    """
    from apm_cli.install.errors import MCPNativeConflictError
    from apm_cli.integration.mcp_integrator_install import resolve_mcp_targets

    runtimes = resolve_mcp_targets(
        **targets,
        project_root=project_root,
        user_scope=user_scope,
        logger=_Silent(),
        verbose=False,
        console=None,
        materialize=False,
    ).runtime_list()
    conflicts = project_native_mcp_conflicts(
        dependencies,
        runtimes or [],
        lockfile=lockfile,
        project_root=project_root,
        user_scope=user_scope,
        logger=logger,
        replay_claims=replay_claims,
    )
    if conflicts:
        message = "MCP installation incomplete: " + ", ".join(conflicts)
        logger.error(message)
        raise MCPNativeConflictError(message, wrote_connections=False)
