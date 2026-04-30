"""Orchestrator for the ``apm install --mcp`` code path.

Extracted from ``commands/install.py`` per the architecture-invariants
LOC budget. ``run_mcp_install`` composes the sibling MCP modules
(``args``, ``entry``, ``writer``, ``warnings``, ``registry``) into the
user-visible install flow:

    parse args -> build entry -> warn -> write apm.yml -> integrate
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

import click

from .args import parse_env_pairs, parse_header_pairs
from .entry import build_mcp_entry
from .registry import registry_env_override
from .warnings import warn_shell_metachars, warn_ssrf_url
from .writer import add_mcp_to_apm_yml


# APM Dependencies (conditional import for graceful degradation).
# Mirrors the pattern in ``commands/install.py`` so the success/log
# behaviour around a missing optional dep is symmetric across the two
# code paths (package install vs. MCP install).
APM_DEPS_AVAILABLE = False
try:
    from ...deps.lockfile import LockFile, get_lockfile_path
    from ...integration.mcp_integrator import MCPIntegrator

    APM_DEPS_AVAILABLE = True
except ImportError:
    pass


def run_mcp_install(
    *,
    mcp_name: str,
    transport: Optional[str],
    url: Optional[str],
    env_pairs: Optional[Sequence[str]],
    header_pairs: Optional[Sequence[str]],
    mcp_version: Optional[str],
    command_argv: Optional[Sequence[str]],
    dev: bool,
    force: bool,
    runtime: Optional[str],
    exclude: Optional[str],
    verbose: bool,
    logger,
    manifest_path: Path,
    apm_dir: Path,
    scope: Optional[str],
    registry_url: Optional[str] = None,
) -> None:
    """Execute the --mcp install path. ``registry_url`` is the validated
    --registry value; the caller resolved precedence vs MCP_REGISTRY_URL."""
    from ...models.dependency.mcp import MCPDependency

    env = parse_env_pairs(env_pairs)
    headers = parse_header_pairs(header_pairs)

    # Build entry (validates through MCPDependency).  Convert ValueError
    # to UsageError so the CLI exits 2 with the model wording.
    try:
        entry, _is_self_defined = build_mcp_entry(
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
        raise click.UsageError(str(exc))

    # F5 + F7 warnings -- do not block.  Source the stdio command from the
    # CLI input rather than the built ``entry``: ``entry`` is ``str`` for
    # bare-string registry shorthand and ``dict`` otherwise, so ``entry.get``
    # is unsafe.
    warn_ssrf_url(url, logger)
    stdio_command = command_argv[0] if command_argv else None
    warn_shell_metachars(env, logger, command=stdio_command)

    # Write to apm.yml.
    status, _diff = add_mcp_to_apm_yml(
        mcp_name,
        entry,
        dev=dev,
        force=force,
        manifest_path=manifest_path,
        logger=logger,
    )

    if status == "skipped":
        logger.progress(f"MCP server '{mcp_name}' unchanged")
        return

    # Build MCPDependency for install.  ``entry`` may be a bare string.
    if isinstance(entry, str):
        dep = MCPDependency.from_string(entry)
    else:
        dep = MCPDependency.from_dict(entry)

    # Install just this MCP via the integrator and update lockfile.
    # ``registry_env_override`` exports MCP_REGISTRY_URL for THIS call so
    # MCPServerOperations() (constructed deep inside MCPIntegrator.install)
    # picks up the override; prior env restored on exit.
    if APM_DEPS_AVAILABLE:
        if registry_url and logger and verbose:
            logger.verbose_detail(f"Registry: {registry_url}")
        with registry_env_override(registry_url):
            try:
                _existing_lock = LockFile.read(get_lockfile_path(apm_dir))
                old_servers = set(_existing_lock.mcp_servers) if _existing_lock else set()
                old_configs = dict(_existing_lock.mcp_configs) if _existing_lock else {}
                MCPIntegrator.install(
                    [dep], runtime, exclude, verbose,
                    stored_mcp_configs=old_configs,
                    scope=scope,
                )
                new_names = MCPIntegrator.get_server_names([dep])
                new_configs = MCPIntegrator.get_server_configs([dep])
                merged_names = old_servers | new_names
                merged_configs = dict(old_configs)
                merged_configs.update(new_configs)
                MCPIntegrator.update_lockfile(merged_names, mcp_configs=merged_configs)
            except Exception as exc:
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
                raise click.ClickException(
                    f"MCP integration failed for '{mcp_name}'"
                )

    verb = "Replaced" if status == "replaced" else "Added"
    logger.success(f"{verb} MCP server '{mcp_name}'", symbol="check")
    if isinstance(entry, dict):
        chosen_transport = entry.get("transport") or "registry"
    else:
        chosen_transport = "registry"
    logger.tree_item(f"  transport: {chosen_transport}")
    logger.tree_item(f"  apm.yml: {manifest_path}")
