"""MCP install path handler for the install command."""

from __future__ import annotations

import sys
from pathlib import Path


def handle_mcp_install(  # noqa: PLR0913
    *,
    mcp_name,
    transport,
    url,
    env_pairs,
    header_pairs,
    mcp_version,
    command_argv,
    dev,
    force,
    runtime,
    exclude,
    verbose,
    dry_run,
    logger,
    no_policy,
    validated_registry_url,
    resolve_registry_url,
    validate_mcp_dry_run_entry,
    run_mcp_install,
):
    """Execute the ``--mcp`` install path (MCP server add).

    Resolves registry URL, runs policy preflight, handles dry-run,
    and delegates to :func:`run_mcp_install` for the actual installation.
    Called from :func:`install` when ``--mcp`` is specified; the caller
    returns immediately after this function completes.
    """
    from ...core.scope import (
        InstallScope,
        get_apm_dir,
        get_manifest_path,
    )

    # Apply CLI > env > default precedence; emit override diagnostic.
    resolved_registry_url, _registry_source = resolve_registry_url(
        validated_registry_url,
        logger=logger,
    )
    mcp_scope = InstallScope.PROJECT
    mcp_manifest_path = get_manifest_path(mcp_scope)
    mcp_apm_dir = get_apm_dir(mcp_scope)
    # -- W2-mcp-preflight: policy enforcement before MCP install --
    # Build a lightweight MCPDependency for policy evaluation.
    # This mirrors _build_mcp_entry routing but we only need the
    # fields that policy checks inspect (name, transport, registry).
    from ...models.dependency.mcp import MCPDependency as _MCPDep
    from ...policy.install_preflight import (
        PolicyBlockError,
        run_policy_preflight,
    )

    _is_self_defined = bool(url or command_argv)
    _preflight_transport = transport
    if _preflight_transport is None:
        if command_argv:
            _preflight_transport = "stdio"
        elif url:
            _preflight_transport = "http"
    _preflight_dep = _MCPDep(
        name=mcp_name,
        transport=_preflight_transport,
        registry=False if _is_self_defined else None,
        url=url,
    )

    try:
        _pf_result, _pf_active = run_policy_preflight(
            project_root=Path.cwd(),
            mcp_deps=[_preflight_dep],
            no_policy=no_policy,
            logger=logger,
            dry_run=dry_run,
        )
    except PolicyBlockError:
        # Diagnostics already emitted by the helper + logger.
        logger.render_summary()
        sys.exit(1)

    if dry_run:
        # C1: validate eagerly so dry-run rejects what real install would.
        validate_mcp_dry_run_entry(
            mcp_name,
            transport=transport,
            url=url,
            env=env_pairs,
            headers=header_pairs,
            version=mcp_version,
            command_argv=command_argv,
            registry_url=resolved_registry_url,
        )
        logger.dry_run_notice(f"would add MCP server '{mcp_name}' to {mcp_manifest_path}")
        return
    run_mcp_install(
        mcp_name=mcp_name,
        transport=transport,
        url=url,
        env_pairs=env_pairs,
        header_pairs=header_pairs,
        mcp_version=mcp_version,
        command_argv=command_argv,
        dev=dev,
        force=force,
        runtime=runtime,
        exclude=exclude,
        verbose=verbose,
        logger=logger,
        manifest_path=mcp_manifest_path,
        apm_dir=mcp_apm_dir,
        scope=mcp_scope,
        registry_url=validated_registry_url,
    )
