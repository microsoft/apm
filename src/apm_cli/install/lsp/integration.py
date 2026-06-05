"""LSP server integration for the APM install pipeline.

Mirrors the MCP integration pattern with runtime-neutral target selection.
"""

import builtins
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.models.apm_package import APMPackage


def run_lsp_integration(
    *,
    apm_package: "APMPackage",
    apm_modules_path: Path,
    lock_path: Path,
    existing_lock: "LockFile | None",
    project_root: Path,
    user_scope: bool,
    should_install: bool,
    logger,
    diagnostics=None,
    runtime: str | None = None,
    exclude: str | None = None,
    apm_config: dict | None = None,
    explicit_target: str | list[str] | None = None,
    scope=None,
    target_context: tuple[dict | None, str | list[str] | None, object] | None = None,
) -> int:
    """Run LSP server integration after APM package installation.

    Mirrors the MCP integration pattern:
    1. Collect direct + transitive LSP deps
    2. Deduplicate (first occurrence wins)
    3. Resolve runtime targets
    4. Install to each target's LSP config
    5. Clean up stale servers
    6. Update lockfile

    Args:
        apm_package: Root APM package with LSP deps.
        apm_modules_path: Path to apm_modules directory.
        lock_path: Path to apm.lock.yaml.
        existing_lock: Previously loaded lockfile (for old LSP state).
        project_root: Project root directory.
        user_scope: If True, write to user-scope runtime config paths.
        should_install: Whether LSP integration should run (same gate as MCP).
        logger: Install logger instance.
        diagnostics: Optional DiagnosticCollector.
        runtime: Optional runtime override.
        exclude: Optional runtime exclusion.
        apm_config: Parsed apm.yml target metadata for project-scope gating.
        explicit_target: Explicit target selected by CLI or manifest.
        scope: Optional InstallScope for user/project filtering.
        target_context: Compact `(apm_config, explicit_target, scope)` tuple
            used by the install command to keep entry-point glue small.

    Returns:
        Number of LSP servers configured.
    """
    from apm_cli.integration.lsp_integrator import LSPIntegrator

    lsp_deps = apm_package.get_lsp_dependencies()

    # Capture old LSP servers from lockfile
    old_lsp_servers: builtins.set = builtins.set()
    old_lsp_configs: builtins.dict = {}
    if existing_lock:
        old_lsp_servers = builtins.set(existing_lock.lsp_servers)
        old_lsp_configs = builtins.dict(existing_lock.lsp_configs)

    # Collect transitive LSP deps from installed packages
    if should_install and apm_modules_path.exists():
        transitive_lsp = LSPIntegrator.collect_transitive(
            apm_modules_path,
            lock_path,
            diagnostics=diagnostics,
        )
        if transitive_lsp:
            logger.verbose_detail(f"Collected {len(transitive_lsp)} transitive LSP dependency(ies)")
            lsp_deps = LSPIntegrator.deduplicate(lsp_deps + transitive_lsp)

    lsp_count = 0
    new_lsp_servers: builtins.set = builtins.set()

    if target_context is not None:
        apm_config, explicit_target, scope = target_context

    target_runtimes = None
    if should_install and (lsp_deps or old_lsp_servers):
        target_runtimes = LSPIntegrator.resolve_target_runtimes(
            project_root=project_root,
            user_scope=user_scope,
            runtime=runtime,
            exclude=exclude,
            apm_config=apm_config,
            explicit_target=explicit_target,
            scope=scope,
            logger=logger,
        )

    if should_install and lsp_deps:
        lsp_count = LSPIntegrator.install(
            lsp_deps,
            project_root=project_root,
            user_scope=user_scope,
            logger=logger,
            diagnostics=diagnostics,
            target_runtimes=target_runtimes,
        )
        new_lsp_servers = LSPIntegrator.get_server_names(lsp_deps)
        new_lsp_configs = LSPIntegrator.get_server_configs(lsp_deps)

        # Remove stale LSP servers
        stale_lsp = old_lsp_servers - new_lsp_servers
        if stale_lsp:
            LSPIntegrator.remove_stale(
                stale_lsp,
                project_root=project_root,
                user_scope=user_scope,
                logger=logger,
                target_runtimes=target_runtimes,
            )

        # Persist LSP servers in lockfile
        LSPIntegrator.update_lockfile(new_lsp_servers, lock_path, lsp_configs=new_lsp_configs)

    elif should_install and not lsp_deps:
        # No LSP deps -- remove any old APM-managed servers
        if old_lsp_servers:
            LSPIntegrator.remove_stale(
                old_lsp_servers,
                project_root=project_root,
                user_scope=user_scope,
                logger=logger,
                target_runtimes=target_runtimes,
            )
            LSPIntegrator.update_lockfile(builtins.set(), lock_path, lsp_configs={})
        logger.verbose_detail("No LSP dependencies found in apm.yml")

    elif not should_install and old_lsp_servers:
        # --only=apm: restore old LSP servers
        LSPIntegrator.update_lockfile(old_lsp_servers, lock_path, lsp_configs=old_lsp_configs)

    return lsp_count
