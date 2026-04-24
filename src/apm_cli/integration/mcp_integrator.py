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
import re
import shutil
import warnings
from pathlib import Path
from typing import List, Optional

import click

from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.utils.console import (
    _get_console,
    _rich_error,
    _rich_info,
    _rich_success,
    _rich_warning,
)

_log = logging.getLogger(__name__)


def _is_vscode_available() -> bool:
    """Return True when VS Code can be targeted for MCP configuration.

    VS Code is considered available when either:
    - the ``code`` CLI command is on PATH (the standard case), or
    - a ``.vscode/`` directory exists in the current working directory
      (common on macOS where the user hasn't run "Install 'code' command
      in PATH" from the VS Code command palette).
    """
    return shutil.which("code") is not None or (Path.cwd() / ".vscode").is_dir()


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
        lock_path: Optional[Path] = None,
        trust_private: bool = False,
        logger=None,
        diagnostics=None,
    ) -> list:
        """Collect MCP dependencies from resolved APM packages listed in apm.lock.

        Only scans apm.yml files for packages present in apm.lock to avoid
        picking up stale/orphaned packages from previous installs.
        Falls back to scanning all apm.yml files if no lock file is available.

        Self-defined servers (registry: false) from direct dependencies
        (depth == 1) are auto-trusted.  Self-defined servers from transitive
        dependencies (depth > 1) are skipped with a warning unless
        *trust_private* is True.
        """
        if not apm_modules_dir.exists():
            return []

        from apm_cli.models.apm_package import APMPackage

        # Build set of expected apm.yml paths from apm.lock
        locked_paths = None
        direct_paths: builtins.set = builtins.set()
        lockfile = None
        if lock_path and lock_path.exists():
            lockfile = LockFile.read(lock_path)
            if lockfile is not None:
                locked_paths = builtins.set()
                for dep in lockfile.get_package_dependencies():
                    if dep.repo_url:
                        yml = (
                            apm_modules_dir / dep.repo_url / dep.virtual_path / "apm.yml"
                            if dep.virtual_path
                            else apm_modules_dir / dep.repo_url / "apm.yml"
                        )
                        locked_paths.add(yml.resolve())
                        if dep.depth == 1:
                            direct_paths.add(yml.resolve())

        # Prefer iterating lock-derived paths directly (existing files only).
        # Fall back to full scan only when lock parsing is unavailable.
        if locked_paths is not None:
            apm_yml_paths = [path for path in sorted(locked_paths) if path.exists()]
        else:
            apm_yml_paths = apm_modules_dir.rglob("apm.yml")

        collected = []
        for apm_yml_path in apm_yml_paths:
            try:
                pkg = APMPackage.from_apm_yml(apm_yml_path)
                mcp = pkg.get_mcp_dependencies()
                if mcp:
                    is_direct = apm_yml_path.resolve() in direct_paths
                    for dep in mcp:
                        if hasattr(dep, "is_self_defined") and dep.is_self_defined:
                            if is_direct:
                                if logger:
                                    logger.verbose_detail(
                                        f"Trusting direct dependency MCP '{dep.name}' "
                                        f"from '{pkg.name}'"
                                    )
                                else:
                                    _rich_info(
                                        f"Trusting direct dependency MCP '{dep.name}' "
                                        f"from '{pkg.name}'"
                                    )
                            elif trust_private:
                                if logger:
                                    logger.verbose_detail(
                                        f"Trusting self-defined MCP server '{dep.name}' "
                                        f"from transitive package '{pkg.name}' (--trust-transitive-mcp)"
                                    )
                                else:
                                    _rich_info(
                                        f"Trusting self-defined MCP server '{dep.name}' "
                                        f"from transitive package '{pkg.name}' (--trust-transitive-mcp)"
                                    )
                            else:
                                _trust_msg = (
                                    f"Transitive package '{pkg.name}' declares self-defined "
                                    f"MCP server '{dep.name}' (registry: false). "
                                    f"Re-declare it in your apm.yml or use --trust-transitive-mcp."
                                )
                                if diagnostics:
                                    diagnostics.warn(_trust_msg)
                                elif logger:
                                    logger.warning(_trust_msg)
                                else:
                                    _rich_warning(_trust_msg)
                                continue
                        collected.append(dep)
            except Exception:
                _log.debug(
                    "Skipping package at %s: failed to parse apm.yml",
                    apm_yml_path,
                    exc_info=True,
                )
                continue
        return collected

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    @staticmethod
    def deduplicate(deps: list) -> list:
        """Deduplicate MCP dependencies by name; first occurrence wins.

        Root deps are listed before transitive, so root overlays take
        precedence.
        """
        seen_names: builtins.set = builtins.set()
        result = []
        for dep in deps:
            if hasattr(dep, "name"):
                name = dep.name
            elif isinstance(dep, dict):
                name = dep.get("name", "")
            else:
                name = str(dep)
            if not name:
                if dep not in result:
                    result.append(dep)
                continue
            if name not in seen_names:
                seen_names.add(name)
                result.append(dep)
        return result

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
                remote["headers"] = [
                    {"name": k, "value": v} for k, v in dep.headers.items()
                ]
            info["remotes"] = [remote]
        else:
            # Build as a stdio package
            env_vars = []
            if dep.env:
                env_vars = [
                    {"name": k, "description": "", "required": True} for k in dep.env
                ]

            runtime_args = []
            if dep.args:
                if isinstance(dep.args, builtins.list):
                    runtime_args = [
                        {"is_required": True, "value_hint": a} for a in dep.args
                    ]
                elif isinstance(dep.args, builtins.dict):
                    runtime_args = [
                        {"is_required": True, "value_hint": v}
                        for v in dep.args.values()
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
                if "remotes" in info and info["remotes"]:
                    info.pop("packages", None)
            elif dep.transport == "stdio":
                # User prefers stdio  -- remove remotes to force package path
                if "packages" in info and info["packages"]:
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

        # Warn about overlay fields not yet applied at install time
        if dep.version:
            warnings.warn(
                f"MCP overlay field 'version' on '{dep.name}' is not yet applied "
                f"at install time and will be ignored.",
                stacklevel=2,
            )
        if isinstance(dep.registry, str):
            warnings.warn(
                f"MCP overlay field 'registry' on '{dep.name}' is not yet applied "
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
                client = ClientFactory.create_client(runtime)
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
        runtime: str = None,
        exclude: str = None,
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
        if not stale_names:
            return

        # Determine which runtimes to clean, mirroring install-time logic.
        all_runtimes = {"vscode", "copilot", "codex", "cursor", "opencode"}
        if runtime:
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

        # Build an expanded set that includes both the full reference and the
        # last-segment short name so we match config keys in every runtime.
        expanded_stale: builtins.set = builtins.set()
        for n in stale_names:
            expanded_stale.add(n)
            if "/" in n:
                expanded_stale.add(n.rsplit("/", 1)[-1])

        # Clean .vscode/mcp.json
        if "vscode" in target_runtimes:
            vscode_mcp = Path.cwd() / ".vscode" / "mcp.json"
            if vscode_mcp.exists():
                try:
                    import json as _json

                    config = _json.loads(vscode_mcp.read_text(encoding="utf-8"))
                    servers = config.get("servers", {})
                    removed = [n for n in expanded_stale if n in servers]
                    for name in removed:
                        del servers[name]
                    if removed:
                        vscode_mcp.write_text(
                            _json.dumps(config, indent=2), encoding="utf-8"
                        )
                        for name in removed:
                            if logger:
                                logger.progress(
                                    f"Removed stale MCP server '{name}' from .vscode/mcp.json"
                                )
                            else:
                                _rich_info(
                                    f"+ Removed stale MCP server '{name}' from .vscode/mcp.json"
                                )
                except Exception:
                    _log.debug(
                        "Failed to clean stale MCP servers from .vscode/mcp.json",
                        exc_info=True,
                    )

        # Clean ~/.copilot/mcp-config.json
        if "copilot" in target_runtimes:
            copilot_mcp = Path.home() / ".copilot" / "mcp-config.json"
            if copilot_mcp.exists():
                try:
                    import json as _json

                    config = _json.loads(copilot_mcp.read_text(encoding="utf-8"))
                    servers = config.get("mcpServers", {})
                    removed = [n for n in expanded_stale if n in servers]
                    for name in removed:
                        del servers[name]
                    if removed:
                        copilot_mcp.write_text(
                            _json.dumps(config, indent=2), encoding="utf-8"
                        )
                        for name in removed:
                            _rich_info(
                                f"+ Removed stale MCP server '{name}' from Copilot CLI config"
                            )
                except Exception:
                    _log.debug(
                        "Failed to clean stale MCP servers from Copilot CLI config",
                        exc_info=True,
                    )

        # Clean ~/.codex/config.toml (mcp_servers section)
        if "codex" in target_runtimes:
            codex_cfg = Path.home() / ".codex" / "config.toml"
            if codex_cfg.exists():
                try:
                    import toml as _toml

                    config = _toml.loads(codex_cfg.read_text(encoding="utf-8"))
                    servers = config.get("mcp_servers", {})
                    removed = [n for n in expanded_stale if n in servers]
                    for name in removed:
                        del servers[name]
                    if removed:
                        codex_cfg.write_text(_toml.dumps(config), encoding="utf-8")
                        for name in removed:
                            _rich_info(
                                f"+ Removed stale MCP server '{name}' from Codex CLI config"
                            )
                except Exception:
                    _log.debug(
                        "Failed to clean stale MCP servers from Codex CLI config",
                        exc_info=True,
                    )

        # Clean .cursor/mcp.json (only if .cursor/ directory exists)
        if "cursor" in target_runtimes:
            cursor_mcp = Path.cwd() / ".cursor" / "mcp.json"
            if cursor_mcp.exists():
                try:
                    import json as _json

                    config = _json.loads(cursor_mcp.read_text(encoding="utf-8"))
                    servers = config.get("mcpServers", {})
                    removed = [n for n in expanded_stale if n in servers]
                    for name in removed:
                        del servers[name]
                    if removed:
                        cursor_mcp.write_text(
                            _json.dumps(config, indent=2), encoding="utf-8"
                        )
                        for name in removed:
                            _rich_info(
                                f"+ Removed stale MCP server '{name}' from .cursor/mcp.json"
                            )
                except Exception:
                    _log.debug(
                        "Failed to clean stale MCP servers from .cursor/mcp.json",
                        exc_info=True,
                    )

        # Clean opencode.json (only if .opencode/ directory exists)
        if "opencode" in target_runtimes:
            opencode_cfg = Path.cwd() / "opencode.json"
            if opencode_cfg.exists() and (Path.cwd() / ".opencode").is_dir():
                try:
                    import json as _json

                    config = _json.loads(opencode_cfg.read_text(encoding="utf-8"))
                    servers = config.get("mcp", {})
                    removed = [n for n in expanded_stale if n in servers]
                    for name in removed:
                        del servers[name]
                    if removed:
                        opencode_cfg.write_text(
                            _json.dumps(config, indent=2), encoding="utf-8"
                        )
                        for name in removed:
                            if logger:
                                logger.progress(
                                    f"Removed stale MCP server '{name}' from opencode.json"
                                )
                            else:
                                _rich_info(
                                    f"+ Removed stale MCP server '{name}' from opencode.json"
                                )
                except Exception:
                    _log.debug(
                        "Failed to clean stale MCP servers from opencode.json",
                        exc_info=True,
                    )

    # ------------------------------------------------------------------
    # Lockfile persistence
    # ------------------------------------------------------------------

    @staticmethod
    def update_lockfile(
        mcp_server_names: builtins.set,
        lock_path: Optional[Path] = None,
        *,
        mcp_configs: Optional[builtins.dict] = None,
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
            lockfile = LockFile.read(lock_path)
            if lockfile is None:
                return
            lockfile.mcp_servers = sorted(mcp_server_names)
            if mcp_configs is not None:
                lockfile.mcp_configs = mcp_configs
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
    def _detect_runtimes(scripts: dict) -> List[str]:
        """Extract runtime commands from apm.yml scripts."""
        # CRITICAL: Use builtins.set explicitly to avoid Click command collision!
        detected = builtins.set()

        for script_name, command in scripts.items():
            if re.search(r"\bcopilot\b", command):
                detected.add("copilot")
            if re.search(r"\bcodex\b", command):
                detected.add("codex")
            if re.search(r"\bllm\b", command):
                detected.add("llm")

        return builtins.list(detected)

    @staticmethod
    def _filter_runtimes(detected_runtimes: List[str]) -> List[str]:
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
                    if shutil.which(rt):
                        available.append(rt)
                return available

        except ImportError:
            mcp_compatible = [
                rt for rt in detected_runtimes if rt in ["vscode", "copilot", "cursor", "opencode"]
            ]
            return [rt for rt in mcp_compatible if shutil.which(rt)]

    # ------------------------------------------------------------------
    # Per-runtime installation
    # ------------------------------------------------------------------

    @staticmethod
    def _install_for_runtime(
        runtime: str,
        mcp_deps: List[str],
        shared_env_vars: dict = None,
        server_info_cache: dict = None,
        shared_runtime_vars: dict = None,
        logger=None,
    ) -> bool:
        """Install MCP dependencies for a specific runtime.

        Returns True if all deps were configured successfully, False otherwise.
        """
        try:
            from apm_cli.core.operations import install_package
            from apm_cli.factory import ClientFactory

            # Get the appropriate client for the runtime
            client = ClientFactory.create_client(runtime)

            all_ok = True
            for dep in mcp_deps:
                if logger:
                    logger.verbose_detail(f"  Installing {dep}...")
                else:
                    click.echo(f"  Installing {dep}...")
                try:
                    result = install_package(
                        runtime,
                        dep,
                        shared_env_vars=shared_env_vars,
                        server_info_cache=server_info_cache,
                        shared_runtime_vars=shared_runtime_vars,
                    )
                    if result["failed"]:
                        if logger:
                            logger.error(f"  Failed to install {dep}")
                        else:
                            click.echo(f"  x Failed to install {dep}")
                        all_ok = False
                except Exception as install_error:
                    _log.debug(
                        "Failed to install MCP dep %s for runtime %s",
                        dep,
                        runtime,
                        exc_info=True,
                    )
                    if logger:
                        logger.error(f"  Failed to install {dep}: {install_error}")
                    else:
                        click.echo(f"  x Failed to install {dep}: {install_error}")
                    all_ok = False
            return all_ok

        except ImportError as e:
            if logger:
                logger.warning(f"Core operations not available for runtime {runtime}: {e}")
                logger.progress(f"Dependencies for {runtime}: {', '.join(mcp_deps)}")
            else:
                _rich_warning(f"Core operations not available for runtime {runtime}: {e}")
                _rich_info(f"Dependencies for {runtime}: {', '.join(mcp_deps)}")
            return False
        except ValueError as e:
            if logger:
                logger.warning(f"Runtime {runtime} not supported: {e}")
                logger.progress("Supported runtimes: vscode, copilot, codex, cursor, opencode, llm")
            else:
                _rich_warning(f"Runtime {runtime} not supported: {e}")
                _rich_info("Supported runtimes: vscode, copilot, codex, cursor, opencode, llm")
            return False
        except Exception as e:
            _log.debug(
                "Unexpected error installing for runtime %s", runtime, exc_info=True
            )
            if logger:
                logger.error(f"Error installing for runtime {runtime}: {e}")
            else:
                _rich_error(f"Error installing for runtime {runtime}: {e}")
            return False

    # ------------------------------------------------------------------
    # Main orchestrator
    # ------------------------------------------------------------------

    @staticmethod
    def install(
        mcp_deps: list,
        runtime: str = None,
        exclude: str = None,
        verbose: bool = False,
        apm_config: dict = None,
        stored_mcp_configs: dict = None,
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
            scope: InstallScope (PROJECT or USER).  When USER, only
                runtimes whose adapter declares ``supports_user_scope``
                are targeted; workspace-only runtimes are skipped.

        Returns:
            Number of MCP servers newly configured or updated.
        """
        if not mcp_deps:
            if logger:
                logger.warning("No MCP dependencies found in apm.yml")
            else:
                _rich_warning("No MCP dependencies found in apm.yml")
            return 0

        # Split into registry-resolved and self-defined deps
        # Backward compat: plain strings are treated as registry deps
        registry_deps = [
            dep
            for dep in mcp_deps
            if isinstance(dep, str)
            or (hasattr(dep, "is_registry_resolved") and dep.is_registry_resolved)
        ]
        self_defined_deps = [
            dep
            for dep in mcp_deps
            if hasattr(dep, "is_self_defined") and dep.is_self_defined
        ]
        registry_dep_names = [
            dep.name if hasattr(dep, "name") else dep for dep in registry_deps
        ]
        registry_dep_map = {
            dep.name: dep for dep in registry_deps if hasattr(dep, "name")
        }

        console = _get_console()
        # Track servers that were re-applied due to config drift
        servers_to_update: builtins.set = builtins.set()
        # Track successful updates separately so the summary counts are accurate
        # even when some drift-detected servers fail to install.
        successful_updates: builtins.set = builtins.set()
        if stored_mcp_configs is None:
            stored_mcp_configs = {}

        # Start MCP section with clean header
        if console:
            try:
                from rich.text import Text

                header = Text()
                header.append("+- MCP Servers (", style="cyan")
                header.append(str(len(mcp_deps)), style="cyan bold")
                header.append(")", style="cyan")
                console.print(header)
            except Exception:
                if logger:
                    logger.progress(f"Installing MCP dependencies ({len(mcp_deps)})...")
                else:
                    _rich_info(f"Installing MCP dependencies ({len(mcp_deps)})...")
        else:
            if logger:
                logger.progress(f"Installing MCP dependencies ({len(mcp_deps)})...")
            else:
                _rich_info(f"Installing MCP dependencies ({len(mcp_deps)})...")

        # Runtime detection and multi-runtime installation
        if runtime:
            # Single runtime mode
            target_runtimes = [runtime]
            if logger:
                logger.progress(f"Targeting specific runtime: {runtime}")
            else:
                _rich_info(f"Targeting specific runtime: {runtime}")
        else:
            if apm_config is None:
                # Lazy load  -- only when the caller doesn't provide it
                try:
                    apm_yml = Path("apm.yml")
                    if apm_yml.exists():
                        from apm_cli.utils.yaml_io import load_yaml
                        apm_config = load_yaml(apm_yml)
                except Exception:
                    apm_config = None

            # Step 1: Get all installed runtimes on the system
            try:
                from apm_cli.factory import ClientFactory
                from apm_cli.runtime.manager import RuntimeManager

                manager = RuntimeManager()
                installed_runtimes = []

                for runtime_name in ["copilot", "codex", "vscode", "cursor", "opencode"]:
                    try:
                        if runtime_name == "vscode":
                            if _is_vscode_available():
                                ClientFactory.create_client(runtime_name)
                                installed_runtimes.append(runtime_name)
                        elif runtime_name == "cursor":
                            # Cursor is opt-in: only target when .cursor/ exists
                            if (Path.cwd() / ".cursor").is_dir():
                                ClientFactory.create_client(runtime_name)
                                installed_runtimes.append(runtime_name)
                        elif runtime_name == "opencode":
                            # OpenCode is opt-in: only target when .opencode/ exists
                            if (Path.cwd() / ".opencode").is_dir():
                                ClientFactory.create_client(runtime_name)
                                installed_runtimes.append(runtime_name)
                        else:
                            if manager.is_runtime_available(runtime_name):
                                ClientFactory.create_client(runtime_name)
                                installed_runtimes.append(runtime_name)
                    except (ValueError, ImportError):
                        continue
            except ImportError:
                installed_runtimes = [
                    rt
                    for rt in ["copilot", "codex"]
                    if shutil.which(rt) is not None
                ]
                # VS Code: check binary on PATH or .vscode/ directory presence
                if _is_vscode_available():
                    installed_runtimes.append("vscode")
                # Cursor is directory-presence based, not binary-based
                if (Path.cwd() / ".cursor").is_dir():
                    installed_runtimes.append("cursor")
                # OpenCode is directory-presence based
                if (Path.cwd() / ".opencode").is_dir():
                    installed_runtimes.append("opencode")

            # Step 2: Get runtimes referenced in apm.yml scripts
            script_runtimes = MCPIntegrator._detect_runtimes(
                apm_config.get("scripts", {}) if apm_config else {}
            )

            # Step 3: Target runtimes BOTH installed AND referenced in scripts
            if script_runtimes:
                target_runtimes = [
                    rt for rt in installed_runtimes if rt in script_runtimes
                ]

                if verbose:
                    if console:
                        console.print("|  [cyan][i]  Runtime Detection[/cyan]")
                        console.print(
                            f"|     +- Installed: {', '.join(installed_runtimes)}"
                        )
                        console.print(
                            f"|     +- Used in scripts: {', '.join(script_runtimes)}"
                        )
                        if target_runtimes:
                            console.print(
                                f"|     +- Target: {', '.join(target_runtimes)} "
                                f"(available + used in scripts)"
                            )
                        console.print("|")
                    elif logger:
                        logger.verbose_detail(
                            f"Installed runtimes: {', '.join(installed_runtimes)}"
                        )
                        logger.verbose_detail(
                            f"Script runtimes: {', '.join(script_runtimes)}"
                        )
                        if target_runtimes:
                            logger.verbose_detail(
                                f"Target runtimes: {', '.join(target_runtimes)}"
                            )
                    else:
                        _rich_info(
                            f"Installed runtimes: {', '.join(installed_runtimes)}"
                        )
                        _rich_info(
                            f"Script runtimes: {', '.join(script_runtimes)}"
                        )
                        if target_runtimes:
                            _rich_info(
                                f"Target runtimes: {', '.join(target_runtimes)}"
                            )

                if not target_runtimes:
                    if logger:
                        logger.warning(
                            "Scripts reference runtimes that are not installed"
                        )
                        logger.progress(
                            "Install missing runtimes with: apm runtime setup <runtime>"
                        )
                    else:
                        _rich_warning(
                            "Scripts reference runtimes that are not installed"
                        )
                        _rich_info(
                            "Install missing runtimes with: apm runtime setup <runtime>"
                        )
            else:
                target_runtimes = installed_runtimes
                if target_runtimes:
                    if verbose:
                        if logger:
                            logger.verbose_detail(
                                f"No scripts detected, using all installed runtimes: "
                                f"{', '.join(target_runtimes)}"
                            )
                        else:
                            _rich_info(
                                f"No scripts detected, using all installed runtimes: "
                                f"{', '.join(target_runtimes)}"
                            )
                else:
                    if logger:
                        logger.warning("No MCP-compatible runtimes installed")
                        logger.progress("Install a runtime with: apm runtime setup copilot")
                    else:
                        _rich_warning("No MCP-compatible runtimes installed")
                        _rich_info("Install a runtime with: apm runtime setup copilot")

            # Apply exclusions
            if exclude:
                target_runtimes = [r for r in target_runtimes if r != exclude]

            # All runtimes excluded  -- nothing to configure
            if not target_runtimes and installed_runtimes:
                if logger:
                    logger.warning(
                        f"All installed runtimes excluded (--exclude {exclude}), "
                        "skipping MCP configuration"
                    )
                else:
                    _rich_warning(
                        f"All installed runtimes excluded (--exclude {exclude}), "
                        "skipping MCP configuration"
                    )
                return 0

            # Fall back to VS Code only if no runtimes are installed at all
            if not target_runtimes and not installed_runtimes:
                target_runtimes = ["vscode"]
                if logger:
                    logger.progress("No runtimes installed, using VS Code as fallback")
                else:
                    _rich_info("No runtimes installed, using VS Code as fallback")

        # Scope filtering: at USER scope, keep only global-capable runtimes.
        # Applied after both explicit --runtime and auto-discovery paths.
        from apm_cli.core.scope import InstallScope

        if scope is InstallScope.USER:
            from apm_cli.factory import ClientFactory as _CF

            pre_filter = list(target_runtimes)
            filtered_runtimes = []
            for rt in target_runtimes:
                try:
                    client = _CF.create_client(rt)
                except ValueError:
                    continue
                if client.supports_user_scope:
                    filtered_runtimes.append(rt)
            target_runtimes = filtered_runtimes
            skipped = set(pre_filter) - set(target_runtimes)
            if skipped:
                msg = (
                    f"Skipped workspace-only runtimes at user scope: "
                    f"{', '.join(sorted(skipped))}"
                    f" -- omit --global to install these"
                )
                if logger:
                    logger.warning(msg)
                else:
                    _rich_info(msg, symbol="info")
            if not target_runtimes:
                if logger:
                    logger.warning(
                        "No runtimes support user-scope MCP installation "
                        "(supported: copilot, codex)"
                    )
                else:
                    _rich_warning(
                        "No runtimes support user-scope MCP installation "
                        "(supported: copilot, codex)",
                        symbol="warning",
                    )
                return 0

        # Use the new registry operations module for better server detection
        configured_count = 0

        # --- Registry-based deps ---
        if registry_dep_names:
            try:
                from apm_cli.registry.operations import MCPServerOperations

                operations = MCPServerOperations()

                # Early validation: check all servers exist in registry (fail-fast)
                if verbose:
                    if logger:
                        logger.verbose_detail(
                            f"Validating {len(registry_deps)} registry servers..."
                        )
                    else:
                        _rich_info(
                            f"Validating {len(registry_deps)} registry servers..."
                        )
                valid_servers, invalid_servers = operations.validate_servers_exist(
                    registry_dep_names
                )

                if invalid_servers:
                    if logger:
                        logger.error(
                            f"Server(s) not found in registry: {', '.join(invalid_servers)}"
                        )
                        logger.progress(
                            "Run 'apm mcp search <query>' to find available servers"
                        )
                    else:
                        _rich_error(
                            f"Server(s) not found in registry: {', '.join(invalid_servers)}"
                        )
                        _rich_info(
                            "Run 'apm mcp search <query>' to find available servers"
                        )
                    raise RuntimeError(
                        f"Cannot install {len(invalid_servers)} missing server(s)"
                    )

                if valid_servers:
                    servers_to_install = (
                        operations.check_servers_needing_installation(
                            target_runtimes, valid_servers
                        )
                    )
                    already_configured_candidates = [
                        dep
                        for dep in valid_servers
                        if dep not in servers_to_install
                    ]

                    # Detect config drift for "already configured" servers
                    if stored_mcp_configs and already_configured_candidates:
                        drifted_reg_deps = [
                            registry_dep_map[n]
                            for n in already_configured_candidates
                            if n in registry_dep_map
                        ]
                        drifted = MCPIntegrator._detect_mcp_config_drift(
                            drifted_reg_deps, stored_mcp_configs,
                        )
                        if drifted:
                            servers_to_update.update(drifted)
                            MCPIntegrator._append_drifted_to_install_list(servers_to_install, drifted)
                    already_configured_servers = [
                        dep
                        for dep in already_configured_candidates
                        if dep not in servers_to_update
                    ]

                    if not servers_to_install:
                        if console:
                            for dep in already_configured_servers:
                                console.print(
                                    f"|  [green]+[/green] {dep} "
                                    f"[dim](already configured)[/dim]"
                                )
                        elif logger:
                            logger.success(
                                "All registry MCP servers already configured"
                            )
                        else:
                            _rich_success(
                                "All registry MCP servers already configured"
                            )
                    else:
                        if already_configured_servers:
                            if console:
                                for dep in already_configured_servers:
                                    console.print(
                                        f"|  [green]+[/green] {dep} "
                                        f"[dim](already configured)[/dim]"
                                    )
                            elif logger:
                                logger.verbose_detail(
                                    "Already configured registry MCP servers: "
                                    f"{', '.join(already_configured_servers)}"
                                )
                            elif verbose:
                                _rich_info(
                                    "Already configured registry MCP servers: "
                                    f"{', '.join(already_configured_servers)}"
                                )

                        # Batch fetch server info once
                        if verbose:
                            if logger:
                                logger.verbose_detail(
                                    f"Installing {len(servers_to_install)} servers..."
                                )
                            else:
                                _rich_info(
                                    f"Installing {len(servers_to_install)} servers..."
                                )
                        server_info_cache = operations.batch_fetch_server_info(
                            servers_to_install
                        )

                        # Apply overlays
                        for server_name in servers_to_install:
                            dep = registry_dep_map.get(server_name)
                            if dep:
                                MCPIntegrator._apply_overlay(
                                    server_info_cache, dep
                                )

                        # Collect env and runtime variables
                        shared_env_vars = (
                            operations.collect_environment_variables(
                                servers_to_install, server_info_cache
                            )
                        )
                        for server_name in servers_to_install:
                            dep = registry_dep_map.get(server_name)
                            if dep and dep.env:
                                shared_env_vars.update(dep.env)
                        shared_runtime_vars = (
                            operations.collect_runtime_variables(
                                servers_to_install, server_info_cache
                            )
                        )

                        # Install for each target runtime
                        for dep in servers_to_install:
                            is_update = dep in servers_to_update
                            if console:
                                action_text = "Updating" if is_update else "Configuring"
                                console.print(f"|  [cyan]>[/cyan]  {dep}")
                                console.print(
                                    f"|     +- {action_text} for "
                                    f"{', '.join([rt.title() for rt in target_runtimes])}..."
                                )

                            any_ok = False
                            for rt in target_runtimes:
                                if verbose:
                                    if logger:
                                        logger.verbose_detail(f"Configuring {rt}...")
                                    else:
                                        _rich_info(f"Configuring {rt}...")
                                if MCPIntegrator._install_for_runtime(
                                    rt,
                                    [dep],
                                    shared_env_vars,
                                    server_info_cache,
                                    shared_runtime_vars,
                                    logger=logger,
                                ):
                                    any_ok = True

                            if any_ok:
                                if console:
                                    label = "updated" if is_update else "configured"
                                    console.print(
                                        f"|  [green]+[/green]  {dep} -> "
                                        f"{', '.join([rt.title() for rt in target_runtimes])}"
                                        f" [dim]({label})[/dim]"
                                    )
                                configured_count += 1
                                if is_update:
                                    successful_updates.add(dep)
                            elif console:
                                console.print(
                                    f"|  [red]x[/red]  {dep}  -- "
                                    f"failed for all runtimes"
                                )

            except ImportError:
                if logger:
                    logger.warning("Registry operations not available")
                    logger.error(
                        "Cannot validate MCP servers without registry operations"
                    )
                else:
                    _rich_warning("Registry operations not available")
                    _rich_error(
                        "Cannot validate MCP servers without registry operations"
                    )
                raise RuntimeError(
                    "Registry operations module required for MCP installation"
                )

        # --- Self-defined deps (registry: false) ---
        if self_defined_deps:
            self_defined_names = [dep.name for dep in self_defined_deps]
            self_defined_to_install = (
                MCPIntegrator._check_self_defined_servers_needing_installation(
                    self_defined_names,
                    target_runtimes,
                )
            )
            already_configured_candidates_sd = [
                name
                for name in self_defined_names
                if name not in self_defined_to_install
            ]

            # Detect config drift for "already configured" self-defined servers
            if stored_mcp_configs and already_configured_candidates_sd:
                drifted_sd_deps = [
                    dep for dep in self_defined_deps
                    if dep.name in already_configured_candidates_sd
                ]
                drifted_sd = MCPIntegrator._detect_mcp_config_drift(
                    drifted_sd_deps, stored_mcp_configs,
                )
                if drifted_sd:
                    servers_to_update.update(drifted_sd)
                    MCPIntegrator._append_drifted_to_install_list(self_defined_to_install, drifted_sd)
            already_configured_self_defined = [
                name
                for name in already_configured_candidates_sd
                if name not in servers_to_update
            ]

            if already_configured_self_defined:
                if console:
                    for name in already_configured_self_defined:
                        console.print(
                            f"|  [green]+[/green] {name} "
                            f"[dim](already configured)[/dim]"
                        )
                elif logger:
                    for name in already_configured_self_defined:
                        logger.verbose_detail(f"{name} already configured, skipping")
                elif verbose:
                    for name in already_configured_self_defined:
                        _rich_info(f"{name} already configured, skipping")
                else:
                    names_str = ", ".join(already_configured_self_defined)
                    _rich_success(
                        f"{len(already_configured_self_defined)} self-defined "
                        f"server(s) already configured, skipping: {names_str}"
                    )

            for dep in self_defined_deps:
                if dep.name not in self_defined_to_install:
                    continue

                is_update = dep.name in servers_to_update
                synthetic_info = MCPIntegrator._build_self_defined_info(dep)
                self_defined_cache = {dep.name: synthetic_info}
                self_defined_env = dep.env or {}

                if console:
                    transport_label = dep.transport or "stdio"
                    action_text = "Updating" if is_update else "Configuring"
                    console.print(
                        f"|  [cyan]>[/cyan]  {dep.name} "
                        f"[dim](self-defined, {transport_label})[/dim]"
                    )
                    console.print(
                        f"|     +- {action_text} for "
                        f"{', '.join([rt.title() for rt in target_runtimes])}..."
                    )

                any_ok = False
                for rt in target_runtimes:
                    if verbose:
                        if logger:
                            logger.verbose_detail(f"Configuring {dep.name} for {rt}...")
                        else:
                            _rich_info(f"Configuring {dep.name} for {rt}...")
                    if MCPIntegrator._install_for_runtime(
                        rt,
                        [dep.name],
                        self_defined_env,
                        self_defined_cache,
                        logger=logger,
                    ):
                        any_ok = True

                if any_ok:
                    if console:
                        label = "updated" if is_update else "configured"
                        console.print(
                            f"|  [green]+[/green]  {dep.name} -> "
                            f"{', '.join([rt.title() for rt in target_runtimes])}"
                            f" [dim]({label})[/dim]"
                        )
                    configured_count += 1
                    if is_update:
                        successful_updates.add(dep.name)
                elif console:
                    console.print(
                        f"|  [red]x[/red]  {dep.name}  -- "
                        f"failed for all runtimes"
                    )

        # Close the panel
        if console:
            if configured_count > 0:
                # Use successful_updates (not servers_to_update) for accurate counts.
                # servers_to_update = all drift-detected servers (some may have failed).
                # successful_updates = servers that were re-applied AND succeeded.
                update_count = builtins.len(successful_updates)
                new_count = configured_count - update_count
                parts = []
                if new_count > 0:
                    parts.append(
                        f"configured {new_count} "
                        f"server{'s' if new_count != 1 else ''}"
                    )
                if update_count > 0:
                    parts.append(
                        f"updated {update_count} "
                        f"server{'s' if update_count != 1 else ''}"
                    )
                console.print(f"+- [green]{', '.join(parts).capitalize()}[/green]")
            else:
                console.print("+- [green]All servers up to date[/green]")

        return configured_count
