"""Standalone LSP lifecycle orchestrator.

Owns all LSP dependency resolution, installation, stale cleanup, and lockfile
persistence logic. Mirrors the MCPIntegrator pattern but is simpler since
LSP is Claude Code-only (no multi-runtime targeting).

Claude Code reads LSP config from `.lsp.json` at the project/plugin root.
"""

import builtins
import json
import logging
from pathlib import Path

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.integration._shared import deduplicate_deps, resolve_locked_apm_yml_paths

_log = logging.getLogger(__name__)


class LSPIntegrator:
    """LSP lifecycle orchestrator -- dependency resolution, installation, and cleanup.

    All methods are static: the class is a logical namespace, not a stateful
    object.
    """

    # ------------------------------------------------------------------
    # Dependency resolution
    # ------------------------------------------------------------------

    @staticmethod
    def collect_transitive(
        apm_modules_dir: Path,
        lock_path: Path | None = None,
        logger=None,
        diagnostics=None,
    ) -> list:
        """Collect LSP dependencies from resolved APM packages listed in apm.lock.

        Only scans apm.yml files for packages present in apm.lock to avoid
        picking up stale/orphaned packages from previous installs.
        Falls back to scanning all apm.yml files if no lock file is available.

        All LSP servers from installed packages are trusted (unlike MCP,
        LSP has no registry vs self-defined distinction).
        """
        if logger is None:
            logger = NullCommandLogger()
        if not apm_modules_dir.exists():
            return []

        from apm_cli.models.apm_package import APMPackage

        resolved, _ = resolve_locked_apm_yml_paths(apm_modules_dir, lock_path)
        apm_yml_paths = resolved if resolved is not None else apm_modules_dir.rglob("apm.yml")

        collected = []
        for apm_yml_path in apm_yml_paths:
            try:
                pkg = APMPackage.from_apm_yml(apm_yml_path)
                lsp = pkg.get_lsp_dependencies()
                if lsp:
                    collected.extend(lsp)
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
        """Deduplicate LSP dependencies by name; first occurrence wins.

        Root deps are listed before transitive, so root overlays take
        precedence.
        """
        return deduplicate_deps(deps)

    # ------------------------------------------------------------------
    # Name extraction
    # ------------------------------------------------------------------

    @staticmethod
    def get_server_names(lsp_deps: list) -> builtins.set:
        """Extract unique server names from a list of LSP dependencies."""
        names: builtins.set = builtins.set()
        for dep in lsp_deps:
            if hasattr(dep, "name"):
                names.add(dep.name)
            elif isinstance(dep, str):
                names.add(dep)
        return names

    @staticmethod
    def get_server_configs(lsp_deps: list) -> builtins.dict:
        """Extract server configs as {name: config_dict} from LSP dependencies."""
        configs: builtins.dict = {}
        for dep in lsp_deps:
            if hasattr(dep, "to_dict") and hasattr(dep, "name"):
                configs[dep.name] = dep.to_dict()
            elif isinstance(dep, str):
                configs[dep] = {"name": dep}
        return configs

    # ------------------------------------------------------------------
    # Stale server cleanup
    # ------------------------------------------------------------------

    @staticmethod
    def remove_stale(
        stale_names: builtins.set,
        project_root=None,
        user_scope: bool = False,
        logger=None,
    ) -> None:
        """Remove LSP server entries that are no longer required by any dependency.

        Cleans up .lsp.json at project root or ~/.claude.json for user scope.

        Args:
            stale_names: Set of LSP server names to remove.
            project_root: Project root directory.
            user_scope: If True, clean user-level config (~/.claude.json).
            logger: Optional logger instance.
        """
        if logger is None:
            logger = NullCommandLogger()
        if not stale_names:
            return

        project_root_path = Path(project_root) if project_root is not None else Path.cwd()

        # Clean project .lsp.json
        if not user_scope:
            lsp_json = project_root_path / ".lsp.json"
            if lsp_json.exists():
                try:
                    config = json.loads(lsp_json.read_text(encoding="utf-8"))
                    if isinstance(config, dict):
                        removed = [n for n in stale_names if n in config]
                        for name in removed:
                            del config[name]
                        if removed:
                            lsp_json.write_text(
                                json.dumps(config, indent=2) + "\n", encoding="utf-8"
                            )
                            for name in removed:
                                logger.progress(f"Removed stale LSP server '{name}' from .lsp.json")
                except Exception:
                    _log.debug(
                        "Failed to clean stale LSP servers from .lsp.json",
                        exc_info=True,
                    )

        # Clean user ~/.claude.json (lspServers section)
        if user_scope:
            claude_user = Path.home() / ".claude.json"
            if claude_user.exists():
                try:
                    config = json.loads(claude_user.read_text(encoding="utf-8"))
                    if isinstance(config, dict):
                        servers = config.get("lspServers", {})
                        if isinstance(servers, dict):
                            removed = [n for n in stale_names if n in servers]
                            for name in removed:
                                del servers[name]
                            if removed:
                                claude_user.write_text(
                                    json.dumps(config, indent=2) + "\n", encoding="utf-8"
                                )
                                for name in removed:
                                    logger.progress(
                                        f"Removed stale LSP server '{name}' from ~/.claude.json"
                                    )
                except Exception:
                    _log.debug(
                        "Failed to clean stale LSP servers from ~/.claude.json",
                        exc_info=True,
                    )

    # ------------------------------------------------------------------
    # Lockfile persistence
    # ------------------------------------------------------------------

    @staticmethod
    def update_lockfile(
        lsp_server_names: builtins.set,
        lock_path: Path | None = None,
        *,
        lsp_configs: builtins.dict | None = None,
    ) -> None:
        """Update the lockfile with the current set of APM-managed LSP server names.

        Args:
            lsp_server_names: Set of LSP server names to persist.
            lock_path: Path to the lockfile. Defaults to ``apm.lock.yaml`` in CWD.
            lsp_configs: Keyword-only. When provided, overwrites ``lsp_configs``
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
            lockfile.lsp_servers = sorted(lsp_server_names)
            if lsp_configs is not None:
                lockfile.lsp_configs = lsp_configs
            lockfile.save(lock_path)
        except Exception:
            _log.debug(
                "Failed to update LSP servers in lockfile at %s",
                lock_path,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Main orchestrator
    # ------------------------------------------------------------------

    @staticmethod
    def install(
        lsp_deps: list,
        project_root=None,
        user_scope: bool = False,
        logger=None,
        diagnostics=None,
    ) -> int:
        """Install LSP dependencies by writing to .lsp.json.

        Args:
            lsp_deps: List of LSP dependency entries (LSPDependency objects).
            project_root: Project root for .lsp.json location.
            user_scope: If True, write to ~/.claude.json instead.
            logger: Optional logger instance.
            diagnostics: Optional DiagnosticCollector for warnings.

        Returns:
            Number of LSP servers newly configured or updated.
        """
        if logger is None:
            logger = NullCommandLogger()
        if not lsp_deps:
            return 0

        project_root_path = Path(project_root) if project_root is not None else Path.cwd()

        # Build server config dict
        servers: builtins.dict = {}
        for dep in lsp_deps:
            if hasattr(dep, "to_lsp_json_entry") and hasattr(dep, "name"):
                servers[dep.name] = dep.to_lsp_json_entry()
            elif hasattr(dep, "name") and hasattr(dep, "to_dict"):
                entry = dep.to_dict()
                entry.pop("name", None)
                servers[dep.name] = entry
            elif isinstance(dep, dict) and "name" in dep:
                name = dep["name"]
                entry = {k: v for k, v in dep.items() if k != "name"}
                servers[name] = entry

        if not servers:
            return 0

        count = 0

        if user_scope:
            # Write to ~/.claude.json lspServers section
            claude_user = Path.home() / ".claude.json"
            try:
                if claude_user.exists():
                    config = json.loads(claude_user.read_text(encoding="utf-8"))
                else:
                    config = {}

                if not isinstance(config, dict):
                    config = {}

                existing = config.get("lspServers", {})
                if not isinstance(existing, dict):
                    existing = {}

                for name, cfg in servers.items():
                    if name not in existing or existing[name] != cfg:
                        count += 1
                    existing[name] = cfg

                config["lspServers"] = existing
                claude_user.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

                if count > 0:
                    logger.progress(f"Configured {count} LSP server(s) in ~/.claude.json")

            except Exception as exc:
                _log.debug("Failed to write LSP config to ~/.claude.json", exc_info=True)
                if diagnostics:
                    diagnostics.warn(
                        f"Failed to write LSP config to {claude_user}: {exc}. "
                        "Check file permissions or run with --verbose for details."
                    )
        else:
            # Write to project .lsp.json
            lsp_json = project_root_path / ".lsp.json"
            try:
                if lsp_json.exists():
                    existing = json.loads(lsp_json.read_text(encoding="utf-8"))
                    if not isinstance(existing, dict):
                        existing = {}
                else:
                    existing = {}

                for name, cfg in servers.items():
                    if name not in existing or existing[name] != cfg:
                        count += 1
                    existing[name] = cfg

                lsp_json.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

                if count > 0:
                    logger.progress(f"Configured {count} LSP server(s) in .lsp.json")

            except Exception as exc:
                _log.debug("Failed to write LSP config to .lsp.json", exc_info=True)
                if diagnostics:
                    diagnostics.warn(
                        f"Failed to write LSP config to {lsp_json}: {exc}. "
                        "Check file permissions or run with --verbose for details."
                    )

        return count
