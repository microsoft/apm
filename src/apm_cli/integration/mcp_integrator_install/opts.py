"""Dataclass parameter objects for the MCP install orchestration chain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MCPInstallOpts:
    """Bundled optional arguments for MCP install functions.

    Passed through the call chain:
    ``MCPIntegrator.install`` → ``install_delegate.install``
    → ``run_mcp_install`` → ``_resolve_runtimes``.
    """

    runtime: str | None = None
    exclude: str | None = None
    verbose: bool = False
    apm_config: dict | None = None
    stored_mcp_configs: dict | None = None
    project_root: Any = None
    user_scope: bool = False
    explicit_target: str | None = None
    logger: Any = None
    diagnostics: Any = None
    scope: Any = None  # InstallScope | None


@dataclass(frozen=True, slots=True)
class _ResolveRuntimesOpts:
    """Bundled arguments for :func:`_resolve_runtimes`."""

    runtime: str | None
    exclude: str | None
    verbose: bool
    apm_config: dict | None
    project_root: Any
    user_scope: bool
    explicit_target: str | None
    scope: Any  # InstallScope | None
    logger: Any
    console: Any
    mcp_integrator_cls: Any
    is_vscode_available: Any


@dataclass(frozen=True, slots=True)
class RuntimeDetectionOpts:
    """Optional arguments for runtime-detection logging."""

    verbose: bool
    console: Any
    logger: Any
    installed: list[str]
    scripts: list[str]
    targets: list[str]


@dataclass(frozen=True, slots=True)
class RuntimeDispatchOpts:
    """Optional arguments for per-runtime installation."""

    shared_env_vars: dict | None = None
    server_info_cache: dict | None = None
    shared_runtime_vars: dict | None = None
    project_root: Any = None
    user_scope: bool = False
    logger: Any = None


@dataclass(frozen=True, slots=True)
class RuntimeInstallRequest:
    """One MCP server installation request across runtimes."""

    name: str
    install_names: list[str]
    env_vars: dict
    server_info_cache: dict
    runtime_vars: dict | None = None
    is_update: bool = False
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RegistryInstallRequest:
    """Optional arguments for registry-backed install loops."""

    registry_deps: list
    registry_dep_names: list[str]
    registry_dep_map: dict[str, object]
    stored_mcp_configs: dict
    servers_to_update: set
    successful_updates: set


@dataclass(frozen=True, slots=True)
class MCPStaleOpts:
    """Optional arguments for stale MCP cleanup."""

    runtime: str | None = None
    exclude: str | None = None
    project_root: Any = None
    user_scope: bool = False
    logger: Any = None
    scope: Any = None
