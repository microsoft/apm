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
import json
import logging
import re
import shutil
import warnings
from collections.abc import MutableMapping
from datetime import datetime, timezone
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.integration.mcp_config_view import (
    _collect_transitive_compat,
    _deduplicate,
    _get_server_configs,
    _get_server_provenance,
)
from apm_cli.runtime.utils import find_runtime_binary
from apm_cli.utils.atomic_io import atomic_write_text, write_text_lf
from apm_cli.utils.console import (
    _get_console,  # noqa: F401 -- re-exported; mcp_integrator_install imports this via lazy import
    _rich_error,
    _rich_info,
    _rich_success,
)

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


def _clean_json_mcp_config(
    config_path: Path,
    stale_names: builtins.set,
    logger,
    label: str,
    servers_key: str = "mcpServers",
    trailing_newline: bool = False,
    use_rich: bool = False,
) -> int:
    """Remove stale entries from a JSON-based MCP config file.

    Args:
        config_path: Path to the JSON config file.
        stale_names: Set of server names to remove (expanded form).
        logger: Command logger for progress messages.
        label: Human-readable config label used in log messages.
        servers_key: Key under which MCP servers are stored (default: ``"mcpServers"``).
        trailing_newline: When True, append a trailing newline after JSON serialisation.
        use_rich: When True, emit removal notices via ``_rich_success``; otherwise use
            ``logger.progress``.

    Returns:
        Number of entries removed.
    """
    if not config_path.exists():
        return 0
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        servers = config.get(servers_key, {})
        removed = [n for n in stale_names if n in servers]
        for name in removed:
            del servers[name]
        if removed:
            text = json.dumps(config, indent=2)
            if trailing_newline:
                text += "\n"
            write_text_lf(config_path, text)
            for name in removed:
                msg = f"Removed stale MCP server '{name}' from {label}"
                if use_rich:
                    _rich_success(msg, symbol="check")
                else:
                    logger.progress(msg)
        return len(removed)
    except Exception:
        _log.debug("Failed to clean stale MCP servers from %s", label, exc_info=True)
        return 0


def _clean_toml_mcp_config(
    config_path: Path,
    stale_names: builtins.set,
    label: str,
    logger=None,
    use_rich: bool = True,
) -> int:
    """Remove stale entries from a TOML-based MCP config file.

    Args:
        config_path: Path to the TOML config file.
        stale_names: Set of server names to remove (expanded form).
        label: Human-readable config label used in log messages.
        logger: Optional command logger for progress messages. When provided
            and *use_rich* is False, removal notices use ``logger.progress``.
        use_rich: When True (default), emit removal notices via ``_rich_success``;
            otherwise use ``logger.progress``.

    Returns:
        Number of entries removed.
    """
    if not config_path.exists():
        return 0
    try:
        config = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        servers = config.get("mcp_servers", {})
        if not isinstance(servers, MutableMapping):
            return 0
        removed = [n for n in stale_names if n in servers]
        for name in removed:
            del servers[name]
        if removed:
            atomic_write_text(config_path, tomlkit.dumps(config), new_file_mode=0o600)
            for name in removed:
                msg = f"Removed stale MCP server '{name}' from {label}"
                if use_rich:
                    _rich_success(msg, symbol="check")
                elif logger is not None:
                    logger.progress(msg)
        return len(removed)
    except (OSError, TOMLKitError, UnicodeDecodeError):
        _log.debug("Failed to clean stale MCP servers from %s", label, exc_info=True)
        return 0


def _clean_claude_config(
    config_path: Path,
    stale_names: builtins.set,
    logger,
    is_user_scope: bool = False,
) -> int:
    """Remove stale entries from a Claude Code JSON config file.

    Handles both the project-level ``.mcp.json`` and the user-level
    ``~/.claude.json``, which share the same JSON structure but differ in
    scope-validation requirements and log labels.

    Args:
        config_path: Path to the Claude JSON config file.
        stale_names: Set of server names to remove (expanded form).
        logger: Command logger for progress messages.
        is_user_scope: When True, validates that the top-level config is a dict
            (``~/.claude.json`` guard) and uses the user-scope log label.

    Returns:
        Number of entries removed.
    """
    label = "~/.claude.json" if is_user_scope else ".mcp.json"
    if not config_path.exists():
        return 0
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if is_user_scope and not isinstance(config, dict):
            return 0
        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict):
            servers = {}
        removed = [n for n in stale_names if n in servers]
        for name in removed:
            del servers[name]
        if removed:
            write_text_lf(config_path, json.dumps(config, indent=2) + "\n")
            for name in removed:
                logger.progress(f"Removed stale MCP server '{name}' from {label}")
        return len(removed)
    except Exception:
        _log.debug("Failed to clean stale MCP servers from %s", label, exc_info=True)
        return 0


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
        """Compatibility delegate for canonical MCP source traversal."""
        return _collect_transitive_compat(
            apm_modules_dir,
            lock_path,
            trust_private,
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
        return _deduplicate(deps)

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
        return _get_server_configs(mcp_deps)

    @staticmethod
    def get_server_provenance(mcp_deps: list) -> builtins.dict:
        """Extract transitive provenance as {name: declaring_package} from MCP deps.

        Only servers carrying a ``resolved_by`` (set by
        :meth:`collect_transitive` for servers declared by a sub-package)
        are included. Servers declared directly in the root manifest have
        ``resolved_by is None`` and are omitted -- absence means "direct",
        mirroring the dependency-side convention. Because ``mcp_deps`` is the
        final deduplicated list (root entries listed first, first-wins), a
        server declared both in the root and transitively resolves to the
        root entry and is correctly treated as direct here (#2081).
        """
        return _get_server_provenance(mcp_deps)

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
                        write_text_lf(intellij_mcp, _json.dumps(config, indent=2))
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
        mcp_target_servers: builtins.dict | None = None,
        mcp_config_provenance: builtins.dict | None = None,
    ) -> None:
        """Update the lockfile with the current set of APM-managed MCP server names.

        Accepts the lock path directly to avoid a redundant disk read when the
        caller already has it.

        Args:
            mcp_server_names: Set of MCP server names to persist.
            lock_path: Path to the lockfile.  Defaults to ``apm.lock.yaml`` in CWD.
            mcp_configs: Keyword-only.  When provided, overwrites ``mcp_configs``
                         in the lockfile (used for drift-detection baseline).
            mcp_target_servers: Keyword-only. Per-target APM-owned server names.
            mcp_config_provenance: Keyword-only.  When provided, overwrites
                         ``mcp_config_provenance`` (name -> declaring package for
                         transitively-contributed servers). Passed in lockstep
                         with ``mcp_configs`` so the two never diverge (#2081).
                         ``None`` leaves the existing value untouched.
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
            if mcp_target_servers is not None:
                lockfile.mcp_target_servers = {
                    target: sorted(servers)
                    for target, servers in sorted(mcp_target_servers.items())
                    if servers
                }
            if mcp_config_provenance is not None:
                lockfile.mcp_config_provenance = mcp_config_provenance
            # Invariant: provenance only carries entries that still have a live
            # config. Prune dangling keys unconditionally so a caller that
            # rewrites mcp_configs without an explicit provenance argument (e.g.
            # the single-server ``apm install mcp`` path) can never leave a
            # stale entry that would exempt a genuinely orphaned server (#2081).
            if lockfile.mcp_config_provenance:
                lockfile.mcp_config_provenance = {
                    name: pkg
                    for name, pkg in lockfile.mcp_config_provenance.items()
                    if name in lockfile.mcp_configs
                }
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
        """Install MCP dependencies for a specific runtime.

        Returns True if all deps were configured successfully, False otherwise.
        """
        if logger is None:
            logger = NullCommandLogger()
        try:
            from apm_cli.core.operations import install_package

            all_ok = True
            for dep in mcp_deps:
                logger.verbose_detail(f"  Installing {dep}...")
                try:
                    result = install_package(
                        runtime,
                        dep,
                        shared_env_vars=shared_env_vars,
                        server_info_cache=server_info_cache,
                        shared_runtime_vars=shared_runtime_vars,
                        project_root=project_root,
                        user_scope=user_scope,
                    )
                    if result["failed"]:
                        logger.error(f"  Failed to install {dep}")
                        all_ok = False
                    elif logger and runtime == "codex":
                        from apm_cli.factory import ClientFactory

                        config_path = ClientFactory.create_client(
                            runtime,
                            project_root=project_root,
                            user_scope=user_scope,
                        ).get_config_path()
                        _log.debug("Codex config written to %s", config_path)
                        logger.verbose_detail(f"  Codex config: {config_path}")
                except Exception as install_error:
                    _log.debug(
                        "Failed to install MCP dep %s for runtime %s",
                        dep,
                        runtime,
                        exc_info=True,
                    )
                    logger.error(f"  Failed to install {dep}: {install_error}")
                    all_ok = False

            # Emit aggregated post-install diagnostics for runtimes that
            # support runtime env-var substitution (currently Copilot CLI).
            # Safe no-op for runtimes whose adapter doesn't aggregate state.
            try:
                if runtime == "copilot":
                    from apm_cli.adapters.client.copilot import CopilotClientAdapter

                    CopilotClientAdapter.emit_install_run_summary()
            except Exception:
                _log.debug("Failed to emit install-run summary", exc_info=True)

            return all_ok

        except ImportError as e:
            logger.warning(f"Core operations not available for runtime {runtime}: {e}")
            logger.progress(f"Dependencies for {runtime}: {', '.join(mcp_deps)}")
            return False
        except ValueError as e:
            from apm_cli.factory import ClientFactory

            supported_runtimes = ", ".join(sorted(ClientFactory.supported_clients()))
            logger.warning(f"Runtime {runtime} not supported: {e}")
            logger.progress(f"Supported runtimes: {supported_runtimes}")
            return False
        except Exception as e:
            _log.debug("Unexpected error installing for runtime %s", runtime, exc_info=True)
            logger.error(f"Error installing for runtime {runtime}: {e}")
            return False

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
        """Filter *target_runtimes* against the project's active targets.

        UX parity with ``apm install`` for apm dependencies: the active
        target set (explicit ``--target`` > ``targets:`` field >
        directory-signal detection) is the whitelist for MCP writes. Any
        runtime outside that set is skipped with an info line naming both
        what was dropped and the active set, so users can audit the
        decision input without re-reading apm.yml (#1335).

        Strict resolution model -- mirrors :func:`resolve_targets`,
        the same call ``apm install`` uses
        (``install/phases/targets.py:233``):

          - flag > yaml-targets > directory signals (no permissive
            "fallback to copilot" greenfield default);
          - no flag, no ``targets:``, and no harness-signal directory ->
            :class:`NoHarnessError` (red ``[x]``, write nothing);
          - multiple ambiguous signals with no disambiguation ->
            :class:`AmbiguousHarnessError` (same fail-closed shape).

        ``explicit_target`` accepts ``str``, ``list[str]``, or a CSV
        string (``"claude,copilot"``) -- the latter is produced by
        legacy callers; it is normalized to a list before the resolver
        is invoked so the canonical-name validator does not reject it as
        one unknown token.

        A malformed ``targets:`` field (conflicting ``target:`` +
        ``targets:``, ``targets: []``, or unknown canonical name) likewise
        fails closed: nothing is written.

        Exit semantics differ deliberately from ``install/phases/targets.py``:
        the canonical install phase calls ``raise SystemExit(2)`` when
        resolution fails; this gate may be invoked mid-bundle (see
        ``install/local_bundle_handler``) where a hard exit would corrupt
        partial state, so we render the same red ``[x]`` voice and return
        an empty list (fail-closed-continue).

        At user scope the package's declared ``targets:`` field and the
        ``--target`` flag are still respected: a package that declares
        ``targets: copilot`` is only installed for Copilot, even when
        Kiro (or another runtime) is detected on the host.  When neither
        the package nor the CLI restricts targets, all detected runtimes
        pass through (backward-compatible).
        """
        from apm_cli.core.apm_yml import (
            ConflictingTargetsError,
            EmptyTargetsListError,
            UnknownTargetError,
            parse_targets_field,
        )
        from apm_cli.integration.targets import RUNTIME_TO_CANONICAL_TARGET

        # --- step 1: parse declared targets (fail-closed on any invalid form)
        yaml_targets: list[str] | None = None
        if apm_config:
            try:
                parsed = parse_targets_field(apm_config)
                yaml_targets = parsed if parsed else None
            except (
                ConflictingTargetsError,
                EmptyTargetsListError,
                UnknownTargetError,
            ) as exc:
                _rich_error(
                    "Skipping all MCP config writes -- apm.yml 'targets' field is invalid.",
                    symbol="error",
                )
                _rich_error(str(exc), symbol="")
                _log.debug(
                    "parse_targets_field failed; failing closed (no MCP writes)",
                    exc_info=True,
                )
                return []

        # --- step 2: normalize CSV explicit_target sugar to a list -----
        flag: str | list[str] | None
        if isinstance(explicit_target, str) and "," in explicit_target:
            flag = [t.strip() for t in explicit_target.split(",") if t.strip()]
        else:
            flag = explicit_target

        if flag is not None:
            tokens = [flag] if isinstance(flag, str) else list(flag)
            flag = [RUNTIME_TO_CANONICAL_TARGET.get(t, t) for t in tokens]

        # --- step 2b: user-scope short path ----------------------------
        # At user scope directory-signal detection is meaningless (there
        # is no "project root" to probe).  If the package or the CLI
        # restricts targets we filter here; otherwise every detected
        # runtime passes through unchanged (backward-compatible default).
        if user_scope:
            # Collect the canonical target names from flag / yaml.
            active: set[str] | None = None
            if flag is not None:
                tokens_list = flag if isinstance(flag, list) else [flag]
                # "all" is a special passthrough -- mirror resolve_targets.
                if "all" in tokens_list:
                    return target_runtimes
                active = set(tokens_list)
            elif yaml_targets is not None:
                active = set(yaml_targets)

            if active is None:
                return target_runtimes

            out = [
                rt for rt in target_runtimes if RUNTIME_TO_CANONICAL_TARGET.get(rt, rt) in active
            ]
            dropped = sorted(set(target_runtimes) - set(out))
            if dropped:
                active_csv = ", ".join(sorted(active)) or "<none>"
                _rich_info(
                    f"Skipped MCP config for {', '.join(dropped)} (active targets: {active_csv}, scope: global)",
                    symbol="info",
                )
                _log.debug(
                    "Active-targets gate dropped (user scope): %s (active=%s)",
                    dropped,
                    sorted(active),
                )
            return out

        # --- step 3 (project scope): delegate to the v2 resolver -------
        from apm_cli.core.errors import (
            AmbiguousHarnessError,
            NoHarnessError,
        )
        from apm_cli.core.target_detection import resolve_targets

        root = project_root or Path.cwd()
        try:
            resolved = resolve_targets(root, flag=flag, yaml_targets=yaml_targets)
        except (NoHarnessError, AmbiguousHarnessError) as exc:
            _rich_error(
                "Skipping all MCP config writes -- could not resolve active targets.",
                symbol="error",
            )
            _rich_error(str(exc), symbol="")
            _log.debug(
                "resolve_targets failed; failing closed (no MCP writes)",
                exc_info=True,
            )
            return []

        active = set(resolved.targets)

        out = [rt for rt in target_runtimes if RUNTIME_TO_CANONICAL_TARGET.get(rt, rt) in active]
        dropped = sorted(set(target_runtimes) - set(out))
        if dropped:
            # Mirror the canonical `Targets: X  (source: Y)` provenance shape
            # (install/phases/targets.py:265, core/target_detection.py:777):
            # double-space before the parenthetical. The "or '<none>'" guard is
            # defensive -- an empty active set is unreachable when
            # _resolve_targets_v2 succeeded, but if a future contract change
            # widens that contract we surface "<none>" rather than render
            # "(active targets: )" which reads as a renderer bug.
            active_csv = ", ".join(sorted(active)) or "<none>"
            _rich_info(
                f"Skipped MCP config for {', '.join(dropped)}  (active targets: {active_csv})",
                symbol="info",
            )
            _log.debug(
                "Active-targets gate dropped: %s (active=%s)",
                dropped,
                sorted(active),
            )
        return out

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
        explicit_target: str | list[str] | None = None,
        logger=None,
        diagnostics=None,
        scope=None,
        managed_target_servers: builtins.dict | None = None,
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
            managed_target_servers: Mutable per-target ownership state. Existing
                entries are reconciled to active targets and successful writes
                are recorded in place.

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
            managed_target_servers=managed_target_servers,
        )
