"""Lockfile persistence for APM-managed MCP server state.

Extracted from ``mcp_integrator`` to keep that module within the per-file
line guardrail. ``MCPIntegrator.update_lockfile`` remains the public entry
point and delegates here.

RULE B: every collaborator (``LockFile``, ``get_lockfile_path``,
``installed_apm_version``, ``_rich_warning``, ``_log``) is reached through
the ``mcp_integrator`` module object at call time, so
``@patch("apm_cli.integration.mcp_integrator.<name>")`` still intercepts.
"""

from __future__ import annotations

import builtins
import copy
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.command_logger import CommandLogger


def write_mcp_state(
    mcp_server_names: builtins.set,
    lock_path: Path | None = None,
    *,
    mcp_configs: builtins.dict | None = None,
    mcp_target_servers: builtins.dict | None = None,
    mcp_config_provenance: builtins.dict | None = None,
    logger: CommandLogger | None = None,
) -> None:
    """Persist the current set of APM-managed MCP servers to the lockfile.

    Raises:
        LockfileFormatError: If the existing lockfile is malformed.
        OSError: If the atomic lockfile write fails.
    """
    import apm_cli.integration.mcp_integrator as _mi

    if lock_path is None:
        lock_path = _mi.get_lockfile_path(Path.cwd())
    # A project whose apm.yml declares only MCP dependencies never enters
    # the APM install pipeline, so nothing else creates apm.lock.yaml --
    # yet `apm audit` counts MCP dependencies when deciding a lockfile is
    # required, and failed with "run 'apm install'" right after a
    # successful install (#2373). Establish the lockfile here when there
    # is MCP state to record. Calls that clear the last server keep the
    # early return: they must not conjure a lockfile for a project that
    # never had one.
    creating = not lock_path.exists()
    if creating and not (mcp_server_names or mcp_configs):
        return
    try:
        existing_lockfile = None if creating else _mi.LockFile.read(lock_path)
        if existing_lockfile is None and not creating:
            return
        lockfile = (
            _mi.LockFile(apm_version=_mi.installed_apm_version())
            if existing_lockfile is None
            else copy.deepcopy(existing_lockfile)
        )
        _apply_mcp_state(lockfile, mcp_server_names, mcp_configs, mcp_target_servers)
        if mcp_config_provenance is not None:
            lockfile.mcp_config_provenance = mcp_config_provenance
        if lockfile.mcp_config_provenance:
            lockfile.mcp_config_provenance = {
                name: pkg
                for name, pkg in lockfile.mcp_config_provenance.items()
                if name in lockfile.mcp_configs
            }
        if existing_lockfile is not None and lockfile.is_semantically_equivalent(existing_lockfile):
            _mi._log.debug("MCP lockfile unchanged -- skipping write")
            return
        lockfile.generated_at = _mi.datetime.now(_mi.timezone.utc).isoformat()
        lockfile.save(lock_path)
    except Exception:
        _mi._log.debug(
            "MCP lockfile persistence failed at %s",
            lock_path,
            exc_info=True,
        )
        if creating:
            _warn_create_failed(lock_path, logger)
        raise


def _apply_mcp_state(
    lockfile,
    mcp_server_names: builtins.set,
    mcp_configs: builtins.dict | None,
    mcp_target_servers: builtins.dict | None,
) -> None:
    """Write server names, configs and per-target servers onto ``lockfile``."""
    lockfile.mcp_servers = sorted(mcp_server_names)
    if mcp_configs is not None:
        lockfile.mcp_configs = mcp_configs
    if mcp_target_servers is not None:
        from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

        DeploymentLedgerCodec.replace_mcp_target_servers(
            lockfile,
            {
                target: sorted(servers)
                for target, servers in sorted(mcp_target_servers.items())
                if servers
            },
        )


def _warn_create_failed(lock_path: Path, logger: CommandLogger | None) -> None:
    """Surface a failed lockfile CREATE at warning level.

    Failing to UPDATE leaves a usable lockfile behind, but failing to CREATE
    one reproduces #2373 exactly -- install reports success and the next
    audit says "run 'apm install'". Debug level would hide the fix silently
    not applying.
    """
    import apm_cli.integration.mcp_integrator as _mi

    message = (
        f"Could not write {lock_path.name}; 'apm audit' will report it as "
        "missing. Ensure the directory is writable and re-run 'apm install'."
    )
    if logger is not None:
        logger.warning(message)
    else:
        _mi._rich_warning(message, symbol="warning")
