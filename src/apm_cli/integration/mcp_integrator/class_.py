"""Standalone MCP lifecycle orchestrator.

Owns all MCP dependency resolution, installation, stale cleanup, and lockfile
persistence logic.  This is NOT a BaseIntegrator subclass  -- MCP integration is
config-level orchestration (registry APIs, runtime configs, lockfile tracking),
not file-level deployment (copy/collision/sync).

The existing adapters (client/, package_manager/) and registry operations
(registry/operations.py) are *used* by this class, not modified.
"""

import builtins
import logging
import shutil
from pathlib import Path
from typing import Any

from apm_cli.integration.mcp_integrator_install.opts import MCPStaleOpts, RuntimeDispatchOpts

_log = logging.getLogger(__name__)


def _is_vscode_available(project_root: Path | str | None = None) -> bool:
    """Return True when VS Code can be targeted for MCP configuration.

    VS Code is considered available when either:
    - the ``code`` CLI command is on PATH (the standard case), or
    - a ``.vscode/`` directory exists in the resolved project root
      (common on macOS where the user hasn't run "Install 'code' command
      in PATH" from the VS Code command palette).

    Args:
        project_root: Project root to inspect for a `.vscode/` directory when
            explicit project context is provided. Falls back to CWD when unset.
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    return shutil.which("code") is not None or (root / ".vscode").is_dir()


class MCPIntegrator:
    """MCP lifecycle orchestrator  -- dependency resolution, installation, and cleanup.

    All methods are static: the class is a logical namespace, not a stateful
    object.  This keeps the extraction minimal and preserves the original
    call-site semantics exactly.
    """

    # ------------------------------------------------------------------
    # Dependency resolution
    # ------------------------------------------------------------------

    @staticmethod
    @staticmethod
    def collect_transitive(
        apm_modules_dir: Path,
        lock_path: Path | None = None,
        trust_private: bool = False,
        logger=None,
        diagnostics=None,
    ) -> list:
        return _collect.collect_transitive(
            apm_modules_dir, lock_path, trust_private, logger, diagnostics
        )

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    @staticmethod
    @staticmethod
    def deduplicate(deps: list) -> list:
        return _collect.deduplicate(deps)

    # ------------------------------------------------------------------
    # Server info helpers
    # ------------------------------------------------------------------

    @staticmethod
    @staticmethod
    def _build_self_defined_info(dep) -> dict:
        return _overlay._build_self_defined_info(dep)

    @staticmethod
    @staticmethod
    def _apply_overlay(server_info_cache: dict, dep) -> None:
        return _overlay._apply_overlay(server_info_cache, dep)

    # ------------------------------------------------------------------
    # Name extraction
    # ------------------------------------------------------------------

    @staticmethod
    def get_server_names(mcp_deps: list) -> builtins.set:
        """Extract unique server names from a list of MCP dependencies."""
        names: builtins.set = builtins.set()
        for dep in mcp_deps:
            if hasattr(dep, "name"):
                names.add(dep.name)
            elif isinstance(dep, str):
                names.add(dep)
        return names

    @staticmethod
    def get_server_configs(mcp_deps: list) -> builtins.dict:
        """Extract server configs as {name: config_dict} from MCP dependencies."""
        configs: builtins.dict = {}
        for dep in mcp_deps:
            if hasattr(dep, "to_dict") and hasattr(dep, "name"):
                configs[dep.name] = dep.to_dict()
            elif isinstance(dep, str):
                configs[dep] = {"name": dep}
        return configs

    @staticmethod
    def _append_drifted_to_install_list(
        install_list: builtins.list,
        drifted: builtins.set,
    ) -> None:
        """Append drifted server names to *install_list* without duplicates.

        Appends in sorted order to guarantee deterministic CLI output.
        Names already present in *install_list* are skipped.
        """
        existing = builtins.set(install_list)
        for name in builtins.sorted(drifted):
            if name not in existing:
                install_list.append(name)

    @staticmethod
    def _detect_mcp_config_drift(
        mcp_deps: list,
        stored_configs: builtins.dict,
    ) -> builtins.set:
        """Return names of MCP deps whose manifest config differs from stored.

        Compares each dependency's current serialized config against the
        previously stored config in the lockfile.  Only dependencies that
        have a stored baseline *and* whose config has changed are returned.
        """
        drifted: builtins.set = builtins.set()
        for dep in mcp_deps:
            if not hasattr(dep, "to_dict") or not hasattr(dep, "name"):
                continue
            current_config = dep.to_dict()
            stored = stored_configs.get(dep.name)
            if stored is not None and stored != current_config:
                drifted.add(dep.name)
        return drifted

    @staticmethod
    def _check_self_defined_servers_needing_installation(
        dep_names: list,
        target_runtimes: list,
        project_root=None,
        user_scope: bool = False,
    ) -> list:
        """Return self-defined MCP servers missing from at least one runtime.

        Self-defined servers have no registry UUID, so installation checks use
        the runtime config keys directly. Runtime config reads are cached per
        runtime to avoid repeating the same client setup for every dependency.
        """
        try:
            from apm_cli.core.conflict_detector import MCPConflictDetector
            from apm_cli.factory import ClientFactory
        except ImportError:
            return list(dep_names)

        runtime_existing = {}
        runtime_failures = []
        for runtime in target_runtimes:
            try:
                client = ClientFactory.create_client(
                    runtime,
                    project_root=project_root,
                    user_scope=user_scope,
                )
                detector = MCPConflictDetector(client)
                runtime_existing[runtime] = detector.get_existing_server_configs()
            except Exception:
                runtime_failures.append(runtime)

        servers_needing_installation = []
        for dep_name in dep_names:
            if runtime_failures:
                servers_needing_installation.append(dep_name)
                continue
            for runtime in target_runtimes:
                if dep_name not in runtime_existing.get(runtime, {}):
                    servers_needing_installation.append(dep_name)
                    break

        return servers_needing_installation

    # ------------------------------------------------------------------
    # Stale server cleanup
    # ------------------------------------------------------------------

    @staticmethod
    def remove_stale(
        stale_names: builtins.set,
        opts: MCPStaleOpts | None = None,
        **legacy_kwargs,
    ) -> None:
        if opts is None:
            opts = MCPStaleOpts(
                runtime=legacy_kwargs.get("runtime"),
                exclude=legacy_kwargs.get("exclude"),
                project_root=legacy_kwargs.get("project_root"),
                user_scope=legacy_kwargs.get("user_scope", False),
                logger=legacy_kwargs.get("logger"),
                scope=legacy_kwargs.get("scope"),
            )
        return _cleanup.remove_stale(stale_names, opts)

    # ------------------------------------------------------------------
    # Lockfile persistence
    # ------------------------------------------------------------------

    @staticmethod
    @staticmethod
    def update_lockfile(
        mcp_server_names: builtins.set,
        lock_path: Path | None = None,
        *,
        mcp_configs: builtins.dict | None = None,
    ) -> None:
        return _lockfile_sync.update_lockfile(mcp_server_names, lock_path, mcp_configs=mcp_configs)

    # ------------------------------------------------------------------
    # Runtime detection
    # ------------------------------------------------------------------

    @staticmethod
    @staticmethod
    def _detect_runtimes(scripts: dict) -> list[str]:
        return _runtime_dispatch._detect_runtimes(scripts)

    @staticmethod
    @staticmethod
    def _filter_runtimes(detected_runtimes: list[str]) -> list[str]:
        return _runtime_dispatch._filter_runtimes(detected_runtimes)

    # ------------------------------------------------------------------
    # Per-runtime installation
    # ------------------------------------------------------------------

    @staticmethod
    def _install_for_runtime(
        runtime: str,
        mcp_deps: list[str],
        opts: RuntimeDispatchOpts | None = None,
        *,
        logger: Any = None,
    ) -> bool:
        if opts is None and logger is not None:
            opts = RuntimeDispatchOpts(logger=logger)
        return _runtime_dispatch._install_for_runtime(runtime, mcp_deps, opts)

    # ------------------------------------------------------------------
    # Main orchestrator
    # ------------------------------------------------------------------

    @staticmethod
    @staticmethod
    def _gate_project_scoped_runtimes(
        target_runtimes: list[str],
        *,
        user_scope: bool,
        project_root,
        apm_config: dict | None,
        explicit_target: str | list[str] | None,
    ) -> list[str]:
        return _runtime_dispatch._gate_project_scoped_runtimes(
            target_runtimes,
            user_scope=user_scope,
            project_root=project_root,
            apm_config=apm_config,
            explicit_target=explicit_target,
        )

    @staticmethod
    def install(
        mcp_deps: list,
        opts=None,
        **kwargs,
    ) -> int:
        from apm_cli.integration.mcp_integrator_install.opts import MCPInstallOpts

        if isinstance(opts, MCPInstallOpts):
            return _install_delegate.install(mcp_deps, opts)

        runtime = kwargs.get("runtime")
        resolved_runtime = runtime if runtime is not None else opts
        install_opts = MCPInstallOpts(
            runtime=resolved_runtime,
            exclude=kwargs.get("exclude"),
            verbose=kwargs.get("verbose", False),
            apm_config=kwargs.get("apm_config"),
            stored_mcp_configs=kwargs.get("stored_mcp_configs"),
            project_root=kwargs.get("project_root"),
            user_scope=kwargs.get("user_scope", False),
            explicit_target=kwargs.get("explicit_target"),
            logger=kwargs.get("logger"),
            diagnostics=kwargs.get("diagnostics"),
            scope=kwargs.get("scope"),
        )
        return _install_delegate.install(mcp_deps, install_opts)


from . import cleanup as _cleanup
from . import collect as _collect
from . import install_delegate as _install_delegate
from . import lockfile_sync as _lockfile_sync
from . import overlay as _overlay
from . import runtime_dispatch as _runtime_dispatch
