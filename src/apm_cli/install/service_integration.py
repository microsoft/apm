"""Shared MCP and LSP reconciliation after package installation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from apm_cli.constants import InstallMode

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.scope import InstallScope
    from apm_cli.core.target_detection import EffectiveTargetDecision
    from apm_cli.deps.lockfile import LockFile


class ServiceCommandContext(Protocol):
    """Command fields consumed by native service reconciliation."""

    project_root: Path
    scope: InstallScope
    install_mode: InstallMode
    logger: InstallLogger
    runtime: str | None
    exclude: str | None
    trust_transitive_mcp: bool
    no_policy: bool
    verbose: bool


@dataclass(frozen=True)
class ServiceIntegrationResult:
    """Counts and the shared target decision produced by reconciliation."""

    mcp_count: int
    lsp_count: int
    target_decision: EffectiveTargetDecision | None


def run_service_integrations(
    ctx: ServiceCommandContext,
    *,
    apm_package: Any,
    mcp_deps: list[Any],
    lock_path: Path,
    existing_lock: LockFile | None,
    old_mcp_servers: set[str],
    old_mcp_configs: dict[str, Any],
    old_mcp_provenance: dict[str, str | list[str]],
    old_mcp_target_servers: dict[str, list[str]],
    old_mcp_target_servers_present: bool,
    diagnostics: Any,
    explicit_target: str | list[str] | None,
    target_decision: EffectiveTargetDecision | None,
) -> ServiceIntegrationResult:
    """Resolve once, then reconcile package-declared MCP and LSP services."""
    from apm_cli.core.scope import InstallScope, get_modules_dir
    from apm_cli.core.target_detection import resolve_package_target_decision
    from apm_cli.install.lsp import run_lsp_integration
    from apm_cli.install.mcp import run_mcp_integration

    should_install = ctx.install_mode != InstallMode.APM
    lsp_deps = apm_package.get_lsp_dependencies()
    if not isinstance(lsp_deps, list):
        ctx.logger.verbose_detail("LSP dependencies were not a list; defaulting to empty")
        lsp_deps = []
    old_lsp_servers = set(existing_lock.lsp_servers) if existing_lock else set()
    if (
        target_decision is None
        and should_install
        and (mcp_deps or lsp_deps or old_mcp_servers or old_lsp_servers)
    ):
        target_decision = resolve_package_target_decision(
            ctx.project_root,
            package=apm_package,
            explicit_target=explicit_target,
            user_scope=ctx.scope is InstallScope.USER,
        )

    apm_modules_path = get_modules_dir(ctx.scope)
    mcp_count, mcp_apm_config = run_mcp_integration(
        apm_package=apm_package,
        mcp_deps=mcp_deps,
        apm_modules_path=apm_modules_path,
        lock_path=lock_path,
        old_mcp_servers=old_mcp_servers,
        old_mcp_configs=old_mcp_configs,
        old_mcp_provenance=old_mcp_provenance,
        old_mcp_target_servers=old_mcp_target_servers,
        old_mcp_target_servers_present=old_mcp_target_servers_present,
        project_root=ctx.project_root,
        user_scope=ctx.scope is InstallScope.USER,
        should_install=should_install,
        logger=ctx.logger,
        diagnostics=diagnostics,
        runtime=ctx.runtime,
        exclude=ctx.exclude,
        trust_transitive_mcp=ctx.trust_transitive_mcp,
        no_policy=ctx.no_policy,
        verbose=ctx.verbose,
        explicit_target=target_decision.value if target_decision else None,
        target_decision=target_decision,
        scope=ctx.scope,
    )
    lsp_count = run_lsp_integration(
        apm_package=apm_package,
        apm_modules_path=apm_modules_path,
        lock_path=lock_path,
        existing_lock=existing_lock,
        project_root=ctx.project_root,
        user_scope=ctx.scope is InstallScope.USER,
        should_install=should_install,
        logger=ctx.logger,
        diagnostics=diagnostics,
        runtime=ctx.runtime,
        exclude=ctx.exclude,
        target_context=(
            mcp_apm_config,
            target_decision.value if target_decision else None,
            ctx.scope,
        ),
        target_decision=target_decision,
        fail_on_write_error=True,
    )
    return ServiceIntegrationResult(mcp_count, lsp_count, target_decision)
