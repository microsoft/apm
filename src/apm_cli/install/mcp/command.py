"""Orchestrator for the ``apm install --mcp`` code path.

Extracted from ``commands/install.py`` per the architecture-invariants
LOC budget. ``run_mcp_install`` composes the sibling MCP modules
(``args``, ``entry``, ``writer``, ``warnings``, ``registry``) into the
user-visible install flow:

    parse args -> build entry -> warn -> write apm.yml -> integrate
"""

from __future__ import annotations

import click

from .args import parse_env_pairs, parse_header_pairs
from .entry import _MCPEntryOpts, build_mcp_entry
from .flags import MCPInstallParams
from .registry import registry_env_override
from .warnings import warn_shell_metachars, warn_ssrf_url
from .writer import _MCPWriteOpts, add_mcp_to_apm_yml

# APM Dependencies (conditional import for graceful degradation).
# Mirrors the pattern in ``commands/install.py`` so the success/log
# behaviour around a missing optional dep is symmetric across the two
# code paths (package install vs. MCP install).
APM_DEPS_AVAILABLE = False
try:
    from ...deps.lockfile import LockFile, get_lockfile_path
    from ...integration.mcp_integrator import MCPIntegrator
    from ...integration.mcp_integrator_install.opts import MCPInstallOpts as _MCPInstallOpts

    APM_DEPS_AVAILABLE = True
except ImportError:
    pass


def _run_mcp_integrator(dep, params: MCPInstallParams, logger) -> None:
    if params.registry_url and logger and params.verbose:
        logger.verbose_detail(f"Registry: {params.registry_url}")
    with registry_env_override(params.registry_url):
        _mcp_lock_path = get_lockfile_path(params.apm_dir)
        _existing_lock = LockFile.read(_mcp_lock_path)
        old_servers = set(_existing_lock.mcp_servers) if _existing_lock else set()
        old_configs = dict(_existing_lock.mcp_configs) if _existing_lock else {}
        MCPIntegrator.install(
            [dep],
            _MCPInstallOpts(
                runtime=params.runtime,
                exclude=params.exclude,
                verbose=params.verbose,
                stored_mcp_configs=old_configs,
                scope=params.scope,
            ),
        )
        new_names = MCPIntegrator.get_server_names([dep])
        new_configs = MCPIntegrator.get_server_configs([dep])
        merged_names = old_servers | new_names
        merged_configs = dict(old_configs)
        merged_configs.update(new_configs)
        MCPIntegrator.update_lockfile(merged_names, _mcp_lock_path, mcp_configs=merged_configs)


def _create_mcp_dependency(entry):
    from ...models.dependency.mcp import MCPDependency

    if isinstance(entry, str):
        return MCPDependency.from_string(entry)
    return MCPDependency.from_dict(entry)


def run_mcp_install(params: MCPInstallParams | None = None, **kwargs: object) -> None:
    """Execute the --mcp install path. ``registry_url`` is the validated
    --registry value; the caller resolved precedence vs MCP_REGISTRY_URL."""
    if params is None:
        params = MCPInstallParams(**kwargs)

    env = parse_env_pairs(params.env_pairs)
    headers = parse_header_pairs(params.header_pairs)

    try:
        entry, _is_self_defined = build_mcp_entry(
            params.mcp_name,
            opts=_MCPEntryOpts(
                transport=params.transport,
                url=params.url,
                env=env,
                headers=headers,
                version=params.mcp_version,
                command_argv=params.command_argv,
                registry_url=params.registry_url,
            ),
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    warn_ssrf_url(params.url, params.logger)
    stdio_command = params.command_argv[0] if params.command_argv else None
    warn_shell_metachars(env, params.logger, command=stdio_command)

    status, _diff = add_mcp_to_apm_yml(
        params.mcp_name,
        entry,
        opts=_MCPWriteOpts(
            dev=params.dev,
            force=params.force,
            manifest_path=params.manifest_path,
            logger=params.logger,
        ),
    )
    if status == "skipped":
        params.logger.progress(f"MCP server '{params.mcp_name}' unchanged")
        return

    dep = _create_mcp_dependency(entry)
    if APM_DEPS_AVAILABLE:
        try:
            _run_mcp_integrator(dep, params, params.logger)
        except Exception as exc:
            params.logger.verbose_detail(f"MCP integration error: {exc}")
            params.logger.error(
                "MCP server written to apm.yml but tool integration "
                "failed. Run with --verbose for details."
            )
            raise click.ClickException(f"MCP integration failed for '{params.mcp_name}'") from exc

    verb = "Replaced" if status == "replaced" else "Added"
    chosen_transport = entry.get("transport") if isinstance(entry, dict) else None
    params.logger.success(f"{verb} MCP server '{params.mcp_name}'", symbol="check")
    params.logger.tree_item(f"  transport: {chosen_transport or 'registry'}")
    params.logger.tree_item(f"  apm.yml: {params.manifest_path}")
