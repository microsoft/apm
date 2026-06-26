"""Standalone LSP lifecycle orchestrator.

Owns LSP dependency resolution, installation, stale cleanup, and lockfile
persistence logic. LSP config is written through runtime targets so vendor
specific path and field differences stay isolated behind a neutral interface.
"""

from __future__ import annotations

import builtins
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.integration._shared import deduplicate_deps, resolve_locked_apm_yml_paths
from apm_cli.runtime.utils import find_runtime_binary
from apm_cli.utils.atomic_io import write_text_lf

_log = logging.getLogger(__name__)

_LSP_SERVERS_KEY = "lspServers"
_CLAUDE_LANGUAGE_KEY = "extensionToLanguage"
_COPILOT_LANGUAGE_KEY = "fileExtensions"
_LEGACY_DEFAULT_TARGETS = ("claude",)
_LSP_TARGET_ORDER = ("copilot", "claude")


@dataclass(frozen=True)
class _LSPTargetSpec:
    """On-disk LSP config contract for one runtime target."""

    runtime: str
    project_relative_path: tuple[str, ...]
    user_relative_path: tuple[str, ...]
    language_key: str
    project_servers_key: str | None
    user_servers_key: str | None
    project_label: str
    user_label: str

    def path(self, project_root: Path, *, user_scope: bool) -> Path:
        """Return the config path for this target and scope."""
        if user_scope:
            return Path.home().joinpath(*self.user_relative_path)
        return project_root.joinpath(*self.project_relative_path)

    def servers_key(self, *, user_scope: bool) -> str | None:
        """Return the wrapper key for this scope, or None for top-level maps."""
        return self.user_servers_key if user_scope else self.project_servers_key

    def label(self, *, user_scope: bool) -> str:
        """Return a human-readable config path label."""
        return self.user_label if user_scope else self.project_label


_LSP_TARGET_SPECS: dict[str, _LSPTargetSpec] = {
    "claude": _LSPTargetSpec(
        runtime="claude",
        project_relative_path=(".lsp.json",),
        user_relative_path=(".claude.json",),
        language_key=_CLAUDE_LANGUAGE_KEY,
        project_servers_key=None,
        user_servers_key=_LSP_SERVERS_KEY,
        project_label=".lsp.json",
        user_label="~/.claude.json",
    ),
    "copilot": _LSPTargetSpec(
        runtime="copilot",
        project_relative_path=(".github", "lsp.json"),
        user_relative_path=(".copilot", "lsp-config.json"),
        language_key=_COPILOT_LANGUAGE_KEY,
        project_servers_key=_LSP_SERVERS_KEY,
        user_servers_key=_LSP_SERVERS_KEY,
        project_label=".github/lsp.json",
        user_label="~/.copilot/lsp-config.json",
    ),
}


class LSPIntegrator:
    """LSP lifecycle orchestrator: dependency resolution, installation, and cleanup.

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
    # Name/config extraction
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

    @staticmethod
    def _base_server_entries(lsp_deps: list) -> dict[str, dict]:
        """Build target-neutral server entries keyed by server name."""
        servers: dict[str, dict] = {}
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
        return servers

    @staticmethod
    def _entry_for_target(entry: dict, spec: _LSPTargetSpec) -> dict:
        """Translate a neutral LSP entry to one target's on-disk schema."""
        out = dict(entry)
        snake_case_extensions = out.pop("extension_to_language", None)
        extension_to_language = out.pop(_CLAUDE_LANGUAGE_KEY, None)
        file_extensions = out.pop(_COPILOT_LANGUAGE_KEY, None)
        language_map = extension_to_language or file_extensions or snake_case_extensions
        if language_map is not None:
            out[spec.language_key] = language_map
        if spec.language_key == _COPILOT_LANGUAGE_KEY and "args" not in out:
            out["args"] = []
        return out

    @staticmethod
    def _servers_for_target(servers: dict[str, dict], spec: _LSPTargetSpec) -> dict[str, dict]:
        """Translate all server entries to one target's schema."""
        return {
            name: LSPIntegrator._entry_for_target(entry, spec) for name, entry in servers.items()
        }

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_target_runtimes(
        *,
        project_root=None,
        user_scope: bool = False,
        runtime: str | None = None,
        exclude: str | None = None,
        apm_config: dict | None = None,
        explicit_target: str | list[str] | None = None,
        scope=None,
        logger=None,
    ) -> list[str]:
        """Resolve runtime targets for LSP writes using MCP target mechanics."""
        if logger is None:
            logger = NullCommandLogger()
        project_root_path = Path(project_root) if project_root is not None else Path.cwd()

        if scope is not None:
            try:
                from apm_cli.core.scope import InstallScope

                if scope is InstallScope.USER:
                    user_scope = True
                elif scope is InstallScope.PROJECT:
                    user_scope = False
            except ImportError:
                pass

        if runtime:
            candidates = [runtime] if runtime in _LSP_TARGET_SPECS else []
        else:
            candidates = []
            if find_runtime_binary("copilot") is not None:
                candidates.append("copilot")
            if (project_root_path / ".claude").is_dir() or find_runtime_binary(
                "claude"
            ) is not None:
                candidates.append("claude")

        if exclude:
            candidates = [target for target in candidates if target != exclude]

        if not candidates:
            return []

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        target_runtimes = MCPIntegrator._gate_project_scoped_runtimes(
            candidates,
            user_scope=user_scope,
            project_root=project_root_path,
            apm_config=apm_config,
            explicit_target=explicit_target,
        )

        if not target_runtimes:
            return []

        if user_scope:
            from apm_cli.factory import ClientFactory

            supported = []
            skipped = []
            for target in target_runtimes:
                try:
                    client = ClientFactory.create_client(target)
                except ValueError:
                    skipped.append(target)
                    continue
                if client.supports_user_scope:
                    supported.append(target)
                else:
                    skipped.append(target)
            if skipped:
                logger.warning(
                    "Skipped workspace-only runtimes at user scope: "
                    f"{', '.join(sorted(skipped))} -- omit --global to install these"
                )
            target_runtimes = supported

        return [target for target in _LSP_TARGET_ORDER if target in target_runtimes]

    # ------------------------------------------------------------------
    # JSON write helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_json_object(config_path: Path) -> dict:
        """Read a JSON object from disk, returning an empty object on malformed input."""
        if not config_path.exists():
            return {}
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _write_target_config(
        spec: _LSPTargetSpec,
        servers: dict[str, dict],
        *,
        project_root: Path,
        user_scope: bool,
    ) -> builtins.set:
        """Merge servers into one target config and return changed server names."""
        config_path = spec.path(project_root, user_scope=user_scope)
        config = LSPIntegrator._read_json_object(config_path)
        servers_key = spec.servers_key(user_scope=user_scope)

        if servers_key is None:
            existing = config
            if not isinstance(existing, dict):
                existing = {}
                config = existing
        else:
            existing = config.get(servers_key, {})
            if not isinstance(existing, dict):
                existing = {}
            config[servers_key] = existing

        changed: builtins.set = builtins.set()
        for name, server_config in servers.items():
            if existing.get(name) != server_config:
                changed.add(name)
            existing[name] = server_config

        config_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_lf(config_path, json.dumps(config, indent=2) + "\n")
        return changed

    @staticmethod
    def _clean_target_config(
        spec: _LSPTargetSpec,
        stale_names: builtins.set,
        *,
        project_root: Path,
        user_scope: bool,
    ) -> list[str]:
        """Remove stale names from one target config and return removed names."""
        config_path = spec.path(project_root, user_scope=user_scope)
        if not config_path.exists():
            return []
        config = LSPIntegrator._read_json_object(config_path)
        servers_key = spec.servers_key(user_scope=user_scope)

        servers = config if servers_key is None else config.get(servers_key, {})
        if not isinstance(servers, dict):
            return []

        removed = [name for name in stale_names if name in servers]
        for name in removed:
            del servers[name]
        if removed:
            if servers_key is not None:
                config[servers_key] = servers
            write_text_lf(config_path, json.dumps(config, indent=2) + "\n")
        return removed

    # ------------------------------------------------------------------
    # Stale server cleanup
    # ------------------------------------------------------------------

    @staticmethod
    def remove_stale(
        stale_names: builtins.set,
        project_root=None,
        user_scope: bool = False,
        logger=None,
        target_runtimes: list[str] | None = None,
    ) -> None:
        """Remove LSP server entries no longer required by any dependency."""
        if logger is None:
            logger = NullCommandLogger()
        if not stale_names:
            return

        project_root_path = Path(project_root) if project_root is not None else Path.cwd()
        runtimes = target_runtimes if target_runtimes is not None else list(_LEGACY_DEFAULT_TARGETS)

        for runtime in runtimes:
            spec = _LSP_TARGET_SPECS.get(runtime)
            if spec is None:
                continue
            try:
                removed = LSPIntegrator._clean_target_config(
                    spec,
                    stale_names,
                    project_root=project_root_path,
                    user_scope=user_scope,
                )
                for name in removed:
                    logger.progress(
                        f"Removed stale LSP server '{name}' from {spec.label(user_scope=user_scope)}"
                    )
            except Exception:
                _log.debug(
                    "Failed to clean stale LSP servers from %s",
                    spec.label(user_scope=user_scope),
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
        """Update the lockfile with the current set of APM-managed LSP servers."""
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
        target_runtimes: list[str] | None = None,
    ) -> int:
        """Install LSP dependencies by writing target-specific runtime config."""
        if logger is None:
            logger = NullCommandLogger()
        if not lsp_deps:
            return 0

        project_root_path = Path(project_root) if project_root is not None else Path.cwd()
        runtimes = target_runtimes if target_runtimes is not None else list(_LEGACY_DEFAULT_TARGETS)
        runtimes = [runtime for runtime in runtimes if runtime in _LSP_TARGET_SPECS]
        if not runtimes:
            logger.warning("No LSP-compatible runtimes detected")
            return 0

        base_servers = LSPIntegrator._base_server_entries(lsp_deps)
        if not base_servers:
            return 0

        changed_servers: builtins.set = builtins.set()
        for runtime in runtimes:
            spec = _LSP_TARGET_SPECS[runtime]
            servers = LSPIntegrator._servers_for_target(base_servers, spec)
            try:
                changed = LSPIntegrator._write_target_config(
                    spec,
                    servers,
                    project_root=project_root_path,
                    user_scope=user_scope,
                )
                changed_servers.update(changed)
                if changed:
                    logger.progress(
                        f"Configured {len(changed)} LSP server(s) in "
                        f"{spec.label(user_scope=user_scope)}"
                    )
            except Exception as exc:
                _log.debug(
                    "Failed to write LSP config to %s",
                    spec.label(user_scope=user_scope),
                    exc_info=True,
                )
                if diagnostics:
                    diagnostics.warn(
                        f"Failed to write LSP config to {spec.path(project_root_path, user_scope=user_scope)}: "
                        f"{exc}. Check file permissions or run with --verbose for details."
                    )

        return len(changed_servers)
