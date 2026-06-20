"""Standalone MCP lifecycle orchestrator.

Owns all MCP dependency resolution, installation, stale cleanup, and lockfile
persistence logic.  This is NOT a BaseIntegrator subclass  -- MCP integration is
config-level orchestration (registry APIs, runtime configs, lockfile tracking),
not file-level deployment (copy/collision/sync).

The existing adapters (client/, package_manager/) and registry operations
(registry/operations.py) are *used* by this class, not modified.
"""

import builtins
import copy
import logging
import re
import warnings
from datetime import datetime, timezone
from pathlib import Path

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.integration._shared import deduplicate_deps
from apm_cli.integration.mcp_config_clean import (
    _clean_claude_config as _clean_claude_config,
)
from apm_cli.integration.mcp_config_clean import (
    _clean_json_mcp_config as _clean_json_mcp_config,
)
from apm_cli.integration.mcp_config_clean import (
    _clean_toml_mcp_config as _clean_toml_mcp_config,
)
from apm_cli.integration.mcp_vscode import (
    _is_vscode_available as _is_vscode_available,
)
from apm_cli.runtime.utils import find_runtime_binary
from apm_cli.utils.console import (
    _get_console,  # noqa: F401 -- re-exported; mcp_integrator_install imports this via lazy import
    _rich_success,
)

_log = logging.getLogger(__name__)


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
    def collect_transitive(
        apm_modules_dir: Path,
        lock_path: Path | None = None,
        trust_private: bool = False,
        logger=None,
        diagnostics=None,
    ) -> list:
        """Collect MCP deps from resolved packages (see mcp_runtime_ops.collect_transitive)."""
        from apm_cli.integration import mcp_runtime_ops

        return mcp_runtime_ops.collect_transitive(
            apm_modules_dir,
            lock_path=lock_path,
            trust_private=trust_private,
            logger=logger,
            diagnostics=diagnostics,
        )

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    @staticmethod
    def deduplicate(deps: list) -> list:
        """Deduplicate MCP dependencies by name; first occurrence wins.

        Root deps are listed before transitive, so root overlays take
        precedence.
        """
        return deduplicate_deps(deps)

    # ------------------------------------------------------------------
    # Server info helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_self_defined_info(dep) -> dict:
        """Build a synthetic server_info dict from a self-defined MCPDependency.

        Mimics the structure returned by the MCP registry so that existing
        adapter code can consume self-defined deps without changes.
        """
        info: dict = {"name": dep.name}

        # For stdio self-defined deps, store raw command/args so adapters
        # can bypass registry-specific formatting (npm, docker, etc.).
        if dep.transport == "stdio" or (
            dep.transport not in ("http", "sse", "streamable-http") and dep.command
        ):
            info["_raw_stdio"] = {
                "command": dep.command or dep.name,
                "args": list(dep.args) if dep.args else [],
                "env": dict(dep.env) if dep.env else {},
            }

        if dep.transport in ("http", "sse", "streamable-http"):
            # Build as a remote endpoint
            remote = {
                "transport_type": dep.transport,
                "url": dep.url or "",
            }
            if dep.headers:
                remote["headers"] = [{"name": k, "value": v} for k, v in dep.headers.items()]
            info["remotes"] = [remote]
        else:
            # Build as a stdio package
            env_vars = []
            if dep.env:
                env_vars = [{"name": k, "description": "", "required": True} for k in dep.env]

            runtime_args = []
            if dep.args:
                if isinstance(dep.args, builtins.list):
                    runtime_args = [{"is_required": True, "value_hint": a} for a in dep.args]
                elif isinstance(dep.args, builtins.dict):
                    runtime_args = [
                        {"is_required": True, "value_hint": v} for v in dep.args.values()
                    ]

            info["packages"] = [
                {
                    "runtime_hint": dep.command or dep.name,
                    "name": dep.name,
                    "registry_name": "self-defined",
                    "runtime_arguments": runtime_args,
                    "package_arguments": [],
                    "environment_variables": env_vars,
                }
            ]

        # Embed tools override for adapters to pick up
        if dep.tools:
            info["_apm_tools_override"] = dep.tools

        # Pass through harness-specific extra keys for adapters to merge
        if dep.extra:
            info["_extra"] = dict(dep.extra)

        return info

    @staticmethod
    def _apply_overlay(server_info_cache: dict, dep) -> None:
        """Apply MCPDependency overlay fields onto cached server_info (in-place).

        Modifies the server_info dict in *server_info_cache[dep.name]* to
        reflect overlay preferences (transport selection, env, headers, tools).
        """
        info = server_info_cache.get(dep.name)
        if not info:
            return

        # Transport overlay: select matching transport from available options
        if dep.transport:
            if dep.transport in ("http", "sse", "streamable-http"):
                # User prefers remote transport  -- remove packages to force remote path
                if info.get("remotes"):
                    info.pop("packages", None)
            elif dep.transport == "stdio":
                # User prefers stdio  -- remove remotes to force package path
                if info.get("packages"):
                    info.pop("remotes", None)

        # Package type overlay: select specific package registry (npm, pypi, oci)
        if dep.package and "packages" in info:
            filtered = [
                p
                for p in info["packages"]
                if p.get("registry_name", "").lower() == dep.package.lower()
            ]
            if filtered:
                info["packages"] = filtered

        # Headers overlay: merge into remote headers
        if dep.headers and "remotes" in info:
            for remote in info["remotes"]:
                existing_headers = remote.get("headers", [])
                if isinstance(existing_headers, builtins.list):
                    for k, v in dep.headers.items():
                        existing_headers.append({"name": k, "value": v})
                    remote["headers"] = existing_headers
                elif isinstance(existing_headers, builtins.dict):
                    existing_headers.update(dep.headers)

        # Args overlay: merge into package runtime arguments
        if dep.args and "packages" in info:
            for pkg in info["packages"]:
                existing_args = pkg.get("runtime_arguments", [])
                if isinstance(dep.args, builtins.list):
                    for arg in dep.args:
                        existing_args.append({"value_hint": str(arg)})
                elif isinstance(dep.args, builtins.dict):
                    for k, v in dep.args.items():
                        existing_args.append({"value_hint": f"--{k}={v}"})
                pkg["runtime_arguments"] = existing_args

        # Tools overlay: embed for adapters to pick up
        if dep.tools:
            info["_apm_tools_override"] = dep.tools

        # Pass through harness-specific extra keys for adapters to merge
        if dep.extra:
            info["_extra"] = dict(dep.extra)

        # Warn about overlay fields not yet applied at install time
        if dep.version:
            warnings.warn(
                f"MCP overlay field 'version' on '{dep.name}' is not yet applied "
                f"at install time and will be ignored.",
                stacklevel=2,
            )

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
        runtime: str = None,  # noqa: RUF013
        exclude: str = None,  # noqa: RUF013
        project_root=None,
        user_scope: bool = False,
        logger=None,
        scope=None,
    ) -> None:
        """Remove MCP server entries that are no longer required by any dependency.

        Cleans up runtime configuration files only for the runtimes that were
        actually targeted during installation.  *stale_names* contains MCP
        dependency references (e.g. ``"io.github.github/github-mcp-server"``).
        For Copilot CLI and Codex, config keys are derived from the last path
        segment, so we match against both the full reference and the short name.

        Args:
            scope: InstallScope (PROJECT or USER).  When USER, only
                global-capable runtimes are cleaned.
        """
        if logger is None:
            logger = NullCommandLogger()
        if not stale_names:
            return

        # Determine which runtimes to clean, mirroring install-time logic.
        # Derived from ClientFactory so adding a new MCP-capable target
        # extends cleanup automatically (no parallel list to maintain).
        from apm_cli.factory import ClientFactory

        all_runtimes = ClientFactory.supported_clients()
        if runtime:  # noqa: SIM108
            target_runtimes = {runtime}
        else:
            target_runtimes = builtins.set(all_runtimes)
        if exclude:
            target_runtimes.discard(exclude)

        # Scope filtering: at USER scope, only clean global-capable runtimes.
        from apm_cli.core.scope import InstallScope

        if scope is InstallScope.USER:
            from apm_cli.factory import ClientFactory as _CF

            supported = builtins.set()
            for rt in target_runtimes:
                try:
                    if _CF.create_client(rt).supports_user_scope:
                        supported.add(rt)
                except ValueError:
                    pass
            target_runtimes = supported

        # Claude Code: when scope is unspecified, fail safely toward the project
        # config only -- never touch ~/.claude.json on the user's behalf without
        # an explicit USER scope, since that file is shared across all Claude
        # Code projects on the host.
        clean_claude_project = "claude" in target_runtimes and scope is not InstallScope.USER
        clean_claude_user = "claude" in target_runtimes and scope is InstallScope.USER
        if "claude" in target_runtimes and scope is None:
            logger.progress(
                "Claude Code stale cleanup: scope unspecified -- defaulting to "
                "project .mcp.json only; pass -g/--global to also clean ~/.claude.json"
            )

        # Build an expanded set that includes both the full reference and the
        # last-segment short name so we match config keys in every runtime.
        expanded_stale: builtins.set = builtins.set()
        for n in stale_names:
            expanded_stale.add(n)
            if "/" in n:
                expanded_stale.add(n.rsplit("/", 1)[-1])

        project_root_path = Path(project_root) if project_root is not None else Path.cwd()

        # Per-runtime cleanup -- each helper reads, diffs, writes, and logs.
        if "vscode" in target_runtimes:
            _clean_json_mcp_config(
                project_root_path / ".vscode" / "mcp.json",
                expanded_stale,
                logger,
                ".vscode/mcp.json",
                servers_key="servers",
            )

        if "copilot" in target_runtimes:
            _clean_json_mcp_config(
                Path.home() / ".copilot" / "mcp-config.json",
                expanded_stale,
                logger,
                "Copilot CLI config",
                use_rich=True,
            )

        # Clean the scope-resolved Codex config.toml (mcp_servers section)
        if "codex" in target_runtimes:
            from apm_cli.factory import ClientFactory

            codex_cfg = Path(
                ClientFactory.create_client(
                    "codex",
                    project_root=project_root,
                    user_scope=user_scope,
                ).get_config_path()
            )
            _clean_toml_mcp_config(codex_cfg, expanded_stale, "Codex CLI config")

        if "cursor" in target_runtimes:
            _clean_json_mcp_config(
                project_root_path / ".cursor" / "mcp.json",
                expanded_stale,
                logger,
                ".cursor/mcp.json",
                use_rich=True,
            )

        # Clean opencode.json (only if .opencode/ directory exists)
        if "opencode" in target_runtimes:
            if (project_root_path / ".opencode").is_dir():
                _clean_json_mcp_config(
                    project_root_path / "opencode.json",
                    expanded_stale,
                    logger,
                    "opencode.json",
                    servers_key="mcp",
                )

        if "windsurf" in target_runtimes:
            _clean_json_mcp_config(
                Path.home() / ".codeium" / "windsurf" / "mcp_config.json",
                expanded_stale,
                logger,
                "Windsurf config",
                use_rich=True,
            )

        if "kiro" in target_runtimes:
            from apm_cli.factory import ClientFactory

            kiro_cfg = Path(
                ClientFactory.create_client(
                    "kiro",
                    project_root=project_root_path,
                    user_scope=user_scope or scope is InstallScope.USER,
                ).get_config_path()
            )
            _clean_json_mcp_config(
                kiro_cfg,
                expanded_stale,
                logger,
                "Kiro MCP config",
                use_rich=True,
            )

        # Clean JetBrains Copilot user-scope mcp.json
        if "intellij" in target_runtimes:
            from apm_cli.adapters.client.intellij import _intellij_config_dir
            from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

            try:
                intellij_mcp = _intellij_config_dir() / "mcp.json"
            except PathTraversalError:
                _log.debug(
                    "Skipping JetBrains Copilot stale cleanup: config dir unavailable",
                    exc_info=True,
                )
                intellij_mcp = None
            if intellij_mcp is not None and intellij_mcp.exists():
                try:
                    import json as _json

                    ensure_path_within(intellij_mcp, Path.home())
                    config = _json.loads(intellij_mcp.read_text(encoding="utf-8"))
                    servers = config.get("servers")
                    if not isinstance(servers, dict):
                        servers = {}
                        config["servers"] = servers
                    removed = [n for n in expanded_stale if n in servers]
                    for name in removed:
                        del servers[name]
                    if removed:
                        intellij_mcp.write_text(_json.dumps(config, indent=2), encoding="utf-8")
                        for name in removed:
                            _rich_success(
                                f"Removed stale MCP server '{name}' from {intellij_mcp}",
                                symbol="check",
                            )
                except (OSError, ValueError):
                    _log.debug(
                        "Failed to clean stale MCP servers from JetBrains Copilot config",
                        exc_info=True,
                    )

        # Clean .gemini/settings.json (only if .gemini/ directory exists)
        if "gemini" in target_runtimes:
            _clean_json_mcp_config(
                project_root_path / ".gemini" / "settings.json",
                expanded_stale,
                logger,
                ".gemini/settings.json",
            )

        # Clean .agents/mcp_config.json (only if .agents/ directory exists)
        if "antigravity" in target_runtimes:
            if (project_root_path / ".agents").is_dir():
                _clean_json_mcp_config(
                    project_root_path / ".agents" / "mcp_config.json",
                    expanded_stale,
                    logger,
                    ".agents/mcp_config.json",
                )

        # Clean Claude Code project .mcp.json (only if .claude/ directory exists)
        if clean_claude_project:
            if (project_root_path / ".claude").is_dir():
                _clean_claude_config(
                    project_root_path / ".mcp.json",
                    expanded_stale,
                    logger,
                )

        # Clean Claude Code user ~/.claude.json (USER scope only)
        if clean_claude_user:
            _clean_claude_config(
                Path.home() / ".claude.json",
                expanded_stale,
                logger,
                is_user_scope=True,
            )

    # ------------------------------------------------------------------
    # Lockfile persistence
    # ------------------------------------------------------------------

    @staticmethod
    def update_lockfile(
        mcp_server_names: builtins.set,
        lock_path: Path | None = None,
        *,
        mcp_configs: builtins.dict | None = None,
    ) -> None:
        """Update the lockfile with the current set of APM-managed MCP server names.

        Accepts the lock path directly to avoid a redundant disk read when the
        caller already has it.

        Args:
            mcp_server_names: Set of MCP server names to persist.
            lock_path: Path to the lockfile.  Defaults to ``apm.lock.yaml`` in CWD.
            mcp_configs: Keyword-only.  When provided, overwrites ``mcp_configs``
                         in the lockfile (used for drift-detection baseline).
        """
        if lock_path is None:
            lock_path = get_lockfile_path(Path.cwd())
        if not lock_path.exists():
            return
        try:
            existing_lockfile = LockFile.read(lock_path)
            if existing_lockfile is None:
                return
            lockfile = copy.deepcopy(existing_lockfile)
            lockfile.mcp_servers = sorted(mcp_server_names)
            if mcp_configs is not None:
                lockfile.mcp_configs = mcp_configs
            if lockfile.is_semantically_equivalent(existing_lockfile):
                _log.debug("MCP lockfile unchanged -- skipping write")
                return
            lockfile.generated_at = datetime.now(timezone.utc).isoformat()
            lockfile.save(lock_path)
        except Exception:
            _log.debug(
                "Failed to update MCP servers in lockfile at %s",
                lock_path,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Runtime detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_runtimes(scripts: dict) -> list[str]:
        """Extract runtime commands from apm.yml scripts."""
        # CRITICAL: Use builtins.set explicitly to avoid Click command collision!
        detected = builtins.set()

        for script_name, command in scripts.items():  # noqa: B007
            if re.search(r"\bcopilot\b", command):
                detected.add("copilot")
            if re.search(r"\bcodex\b", command):
                detected.add("codex")
            if re.search(r"\bgemini\b", command):
                detected.add("gemini")
            if re.search(r"\bclaude\b", command):
                detected.add("claude")
            if re.search(r"\bllm\b", command):
                detected.add("llm")
            if re.search(r"\bwindsurf\b", command):
                detected.add("windsurf")
            if re.search(r"\bkiro\b", command):
                detected.add("kiro")
            if re.search(r"\bantigravity\b|\bagy\b", command):
                detected.add("antigravity")

        return builtins.list(detected)

    @staticmethod
    def _filter_runtimes(detected_runtimes: list[str]) -> list[str]:
        """Filter to only runtimes that are actually installed and support MCP."""
        from apm_cli.factory import ClientFactory

        # First filter to only MCP-compatible runtimes
        try:
            mcp_compatible = []
            for rt in detected_runtimes:
                try:
                    ClientFactory.create_client(rt)
                    mcp_compatible.append(rt)
                except ValueError:
                    continue

            # Then filter to only installed runtimes
            try:
                from apm_cli.runtime.manager import RuntimeManager

                manager = RuntimeManager()
                return [rt for rt in mcp_compatible if manager.is_runtime_available(rt)]
            except ImportError:
                available = []
                for rt in mcp_compatible:
                    if find_runtime_binary(rt):
                        available.append(rt)
                return available

        except ImportError:
            # Derived from ClientFactory; see _MCP_CLIENT_REGISTRY.
            from apm_cli.factory import ClientFactory

            mcp_compatible = [
                rt for rt in detected_runtimes if rt in ClientFactory.supported_clients()
            ]
            return [rt for rt in mcp_compatible if find_runtime_binary(rt)]

    # ------------------------------------------------------------------
    # Per-runtime installation
    # ------------------------------------------------------------------

    @staticmethod
    def _install_for_runtime(
        runtime: str,
        mcp_deps: list[str],
        shared_env_vars: dict = None,  # noqa: RUF013
        server_info_cache: dict = None,  # noqa: RUF013
        shared_runtime_vars: dict = None,  # noqa: RUF013
        project_root=None,
        user_scope: bool = False,
        logger=None,
    ) -> bool:
        """Install MCP deps for a runtime (see mcp_runtime_ops.install_for_runtime)."""
        from apm_cli.integration import mcp_runtime_ops

        return mcp_runtime_ops.install_for_runtime(
            runtime,
            mcp_deps,
            shared_env_vars=shared_env_vars,
            server_info_cache=server_info_cache,
            shared_runtime_vars=shared_runtime_vars,
            project_root=project_root,
            user_scope=user_scope,
            logger=logger,
        )

    # ------------------------------------------------------------------
    # Main orchestrator
    # ------------------------------------------------------------------

    @staticmethod
    def _gate_project_scoped_runtimes(
        target_runtimes: list[str],
        *,
        user_scope: bool,
        project_root,
        apm_config: dict | None,
        explicit_target: str | list[str] | None,
    ) -> list[str]:
        """Filter runtimes against active project targets (see mcp_runtime_ops)."""
        from apm_cli.integration import mcp_runtime_ops

        return mcp_runtime_ops.gate_project_scoped_runtimes(
            target_runtimes,
            user_scope=user_scope,
            project_root=project_root,
            apm_config=apm_config,
            explicit_target=explicit_target,
        )

    @staticmethod
    def install(
        mcp_deps: list,
        runtime: str = None,  # noqa: RUF013
        exclude: str = None,  # noqa: RUF013
        verbose: bool = False,
        apm_config: dict = None,  # noqa: RUF013
        stored_mcp_configs: dict = None,  # noqa: RUF013
        project_root=None,
        user_scope: bool = False,
        explicit_target: str | None = None,
        logger=None,
        diagnostics=None,
        scope=None,
    ) -> int:
        """Install MCP dependencies.

        Args:
            mcp_deps: List of MCP dependency entries (registry strings or
                MCPDependency objects).
            runtime: Target specific runtime only.
            exclude: Exclude specific runtime from installation.
            verbose: Show detailed installation information.
            apm_config: The parsed apm.yml configuration dict (optional).
                When not provided, the method loads it from disk.
            stored_mcp_configs: Previously stored MCP configs from lockfile
                for diff-aware installation.  When provided, servers whose
                manifest config has changed are re-applied automatically.
            project_root: Project root for repo-local runtime configs.
            user_scope: Whether runtime configuration is being resolved at user scope.
            explicit_target: Explicit target selected by CLI or manifest.
            scope: InstallScope (PROJECT or USER). When USER, only
                runtimes whose adapter declares ``supports_user_scope``
                are targeted; workspace-only runtimes are skipped.

        Returns:
            Number of MCP servers newly configured or updated.
        """
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        return run_mcp_install(
            mcp_deps,
            runtime=runtime,
            exclude=exclude,
            verbose=verbose,
            apm_config=apm_config,
            stored_mcp_configs=stored_mcp_configs,
            project_root=project_root,
            user_scope=user_scope,
            explicit_target=explicit_target,
            logger=logger,
            diagnostics=diagnostics,
            scope=scope,
        )
