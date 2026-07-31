"""Heavy-lifting helpers extracted from update.py to stay under the 800-line budget."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.command_logger import CommandLogger, InstallLogger
    from ..core.scope import InstallScope
    from ..deps.lockfile import LockFile


@dataclasses.dataclass(frozen=True)
class _LifecycleCtx:
    """Infra and lifecycle parameters for _finalize_dep_update.

    Groups the MCP/LSP infrastructure paths and the staged package used for
    lifecycle scripts so _finalize_dep_update stays within the PLR0913 budget
    (12 arguments).
    """

    mcp_lsp_project_root: Path | None
    apm_dir: Path | None
    lock_path: Path | None
    existing_lock: Any
    staged_apm_package: Any


def _local_dep_install_path(dependency: Any, modules_dir: Path) -> Path:
    """Compute the canonical install path for a local locked dependency.

    Mirrors main's ``DependencyReference.get_install_path`` logic for local
    deps: transitive local deps (``declaring_parent`` set) use a hashed parent
    slot so sibling packages don't collide in ``_local/``.
    """
    import hashlib
    from pathlib import Path as _Path

    pkg_dir_name = _Path(dependency.local_path).name
    if dependency.declaring_parent:
        identity = dependency.anchored_local_path or dependency.local_path
        parent_slot = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        return modules_dir / "_local" / parent_slot / pkg_dir_name
    return modules_dir / "_local" / pkg_dir_name


def _module_cache_needs_rehydration(
    lockfile: LockFile | None,
    modules_dir: Path,
) -> bool:
    """Return whether a locked dependency tree has no materialized cache."""
    if lockfile is None:
        return False
    has_locked_dependency = False
    for key, dependency in lockfile.dependencies.items():
        if key == ".":
            continue
        has_locked_dependency = True
        if dependency.source == "local" and dependency.local_path:
            install_path = _local_dep_install_path(dependency, modules_dir)
        else:
            install_path = dependency.to_dependency_ref().get_install_path(modules_dir)
        if install_path.exists():
            return False
    return has_locked_dependency


def _run_mcp_lsp_integration(
    *,
    scope: InstallScope,
    project_root: Path,
    existing_lock: LockFile | None,
    lock_path: Path,
    target: str | list[str] | None,
    diagnostics: Any,
    logger: InstallLogger,
    verbose: bool,
) -> None:
    """Reconcile MCP and LSP servers against the current apm.yml.

    ``apm update`` calls ``_install_apm_dependencies`` directly rather than
    going through ``_install_apm_packages`` (see ``commands/install.py``),
    so it must separately run the same MCP/LSP integration that helper
    performs. Mirrors ``_install_apm_packages``'s ordering: clear the
    apm.yml parse cache, re-parse the on-disk manifest (revision pins are
    already applied by this point), then reconcile MCP and LSP servers.
    """
    from apm_cli.core.scope import InstallScope, get_modules_dir
    from apm_cli.install.lsp import run_lsp_integration
    from apm_cli.install.mcp import run_mcp_integration
    from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
    from apm_cli.policy.install_preflight import PolicyBlockError
    from apm_cli.utils.console import _rich_error

    clear_apm_yml_cache()
    apm_package = APMPackage.from_apm_yml(Path("apm.yml"))

    old_mcp_servers: set = set()
    old_mcp_configs: dict = {}
    old_mcp_provenance: dict = {}
    old_mcp_target_servers: dict = {}
    if existing_lock:
        old_mcp_servers = set(existing_lock.mcp_servers)
        old_mcp_configs = dict(existing_lock.mcp_configs)
        old_mcp_provenance = dict(existing_lock.mcp_config_provenance)
        old_mcp_target_servers = dict(existing_lock.mcp_target_servers)
    trusted_transitive_configs = {
        name: (old_mcp_provenance[name], config)
        for name, config in old_mcp_configs.items()
        if name in old_mcp_provenance and config.get("registry") is False
    }

    apm_modules_path = get_modules_dir(scope)
    user_scope = scope is InstallScope.USER

    try:
        _mcp_count, mcp_apm_config = run_mcp_integration(
            apm_package=apm_package,
            mcp_deps=apm_package.get_all_mcp_dependencies(),
            apm_modules_path=apm_modules_path,
            lock_path=lock_path,
            old_mcp_servers=old_mcp_servers,
            old_mcp_configs=old_mcp_configs,
            old_mcp_provenance=old_mcp_provenance,
            old_mcp_target_servers=old_mcp_target_servers,
            trusted_transitive_configs=trusted_transitive_configs,
            project_root=project_root,
            user_scope=user_scope,
            should_install=True,
            logger=logger,
            diagnostics=diagnostics,
            verbose=verbose,
            explicit_target=target,
            scope=scope,
        )
    except PolicyBlockError:
        _rich_error(
            "MCP server(s) blocked by org policy. "
            "APM packages remain installed; MCP configs were NOT written."
        )
        logger.render_summary()
        sys.exit(1)

    from apm_cli.install.lsp.integration import LSPIntegrationContext

    run_lsp_integration(
        apm_package=apm_package,
        apm_modules_path=apm_modules_path,
        lock_path=lock_path,
        existing_lock=existing_lock,
        project_root=project_root,
        logger=logger,
        diagnostics=diagnostics,
        ctx=LSPIntegrationContext(
            user_scope=user_scope,
            should_install=True,
            explicit_target=target if isinstance(target, str) else None,
            scope=scope,
            target_context=(mcp_apm_config, target, scope),
        ),
    )


def _fire_update_scripts(
    event_name: str,
    *,
    apm_package: Any,
    scope: Any,
    logger: CommandLogger | None,
    verbose: bool,
) -> None:
    """Build a script runner and fire an update lifecycle event.

    Best-effort: all exceptions are swallowed so scripts never block
    the update flow.
    """
    import contextlib

    with contextlib.suppress(Exception):
        from apm_cli.core.lifecycle_scripts import (
            LifecycleEvent,
            PackageInfo,
            build_runner_from_context,
        )

        project_root = None
        pkg_path = getattr(apm_package, "package_path", None)
        if pkg_path is not None:
            project_root = str(pkg_path)

        runner = build_runner_from_context(
            logger=logger,
            verbose=verbose,
            project_root=project_root,
        )

        pkg_infos = []
        for dep in apm_package.get_apm_dependencies():
            pkg_infos.append(PackageInfo(name=dep.repo_url or str(dep), reference=dep.reference))

        scope_name = scope.value if hasattr(scope, "value") else str(scope)
        event = LifecycleEvent.create(
            event=event_name,
            packages=pkg_infos,
            scope=scope_name,
            working_directory=project_root,
        )

        runner.fire(event_name, event)
