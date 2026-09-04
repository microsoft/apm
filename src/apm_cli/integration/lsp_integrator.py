"""Runtime target adapter for LSP configuration.

Owns target paths, shapes, file writes, and cleanup mechanics. The install LSP
pipeline owns collection, trust filtering, and lifecycle reconciliation.
"""

from __future__ import annotations

import builtins
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.integration._shared import (
    deduplicate_deps,
    resolve_locked_apm_yml_sources,
)
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.runtime.utils import find_runtime_binary
from apm_cli.utils.atomic_io import write_text_lf

if TYPE_CHECKING:
    from apm_cli.core.target_detection import EffectiveTargetDecision

_log = logging.getLogger(__name__)

_LSP_SERVERS_KEY = "lspServers"
_CLAUDE_LANGUAGE_KEY = "extensionToLanguage"
_COPILOT_LANGUAGE_KEY = "fileExtensions"
_CLAUDE_STARTUP_TIMEOUT_KEY = "startupTimeout"
_COPILOT_STARTUP_TIMEOUT_KEY = "warmupTimeoutMs"
_LEGACY_DEFAULT_TARGETS = ("claude",)
_LSP_TARGET_ORDER = ("copilot", "claude")


@dataclass(frozen=True)
class _LSPTargetSpec:
    """On-disk LSP config contract for one runtime target."""

    runtime: str
    project_relative_path: tuple[str, ...]
    user_relative_path: tuple[str, ...]
    language_key: str
    startup_timeout_key: str
    project_servers_key: str | None
    user_servers_key: str | None
    project_label: str
    user_label: str
    project_config_defaults: tuple[tuple[str, str], ...] = ()
    user_config_defaults: tuple[tuple[str, str], ...] = ()
    cleanup_empty_relative_dirs: tuple[tuple[str, ...], ...] = ()

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

    def config_defaults(self, *, user_scope: bool) -> tuple[tuple[str, str], ...]:
        """Return required top-level defaults for this target and scope."""
        return self.user_config_defaults if user_scope else self.project_config_defaults

    def cleanup_empty_dirs(self, project_root: Path, *, user_scope: bool) -> tuple[Path, ...]:
        """Return APM-owned directories this target may remove when empty."""
        root = Path.home() if user_scope else project_root
        return tuple(root.joinpath(*parts) for parts in self.cleanup_empty_relative_dirs)


@dataclass(frozen=True)
class _PreparedTargetConfig:
    """Validated target config ready for one atomic file write."""

    path: Path
    content: str
    changed: builtins.set


_LSP_TARGET_SPECS: dict[str, _LSPTargetSpec] = {
    "claude": _LSPTargetSpec(
        runtime="claude",
        project_relative_path=(
            ".claude",
            "skills",
            "apm-lsp",
            ".claude-plugin",
            "plugin.json",
        ),
        user_relative_path=(
            ".claude",
            "skills",
            "apm-lsp",
            ".claude-plugin",
            "plugin.json",
        ),
        language_key=_CLAUDE_LANGUAGE_KEY,
        startup_timeout_key=_CLAUDE_STARTUP_TIMEOUT_KEY,
        project_servers_key=_LSP_SERVERS_KEY,
        user_servers_key=_LSP_SERVERS_KEY,
        project_label=".claude/skills/apm-lsp/.claude-plugin/plugin.json",
        user_label="~/.claude/skills/apm-lsp/.claude-plugin/plugin.json",
        project_config_defaults=(("name", "apm-lsp"),),
        user_config_defaults=(("name", "apm-lsp"),),
        cleanup_empty_relative_dirs=(
            (".claude", "skills", "apm-lsp", ".claude-plugin"),
            (".claude", "skills", "apm-lsp"),
        ),
    ),
    "copilot": _LSPTargetSpec(
        runtime="copilot",
        project_relative_path=(".github", "lsp.json"),
        user_relative_path=(".copilot", "lsp-config.json"),
        language_key=_COPILOT_LANGUAGE_KEY,
        startup_timeout_key=_COPILOT_STARTUP_TIMEOUT_KEY,
        project_servers_key=_LSP_SERVERS_KEY,
        user_servers_key=_LSP_SERVERS_KEY,
        project_label=".github/lsp.json",
        user_label="~/.copilot/lsp-config.json",
    ),
}


class LSPIntegrator:
    """Adapt runtime-neutral LSP declarations to target configuration files.

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

        Declaring-package provenance is attached so the install pipeline can
        enforce executable approval before exposing transitive servers.
        """
        if logger is None:
            logger = NullCommandLogger()
        if not apm_modules_dir.exists():
            return []

        from apm_cli.models.apm_package import APMPackage

        resolved, _ = resolve_locked_apm_yml_sources(apm_modules_dir, lock_path)
        if resolved is None:
            apm_yml_sources = [
                (
                    path,
                    None,
                )
                for path in apm_modules_dir.rglob("apm.yml")
            ]
        else:
            apm_yml_sources = resolved

        collected = []
        for apm_yml_path, locked_dependency in apm_yml_sources:
            try:
                pkg = APMPackage.from_apm_yml(apm_yml_path)
                lsp = pkg.get_lsp_dependencies()
                if lsp:
                    if locked_dependency is None:
                        owner = apm_yml_path.parent.relative_to(apm_modules_dir).as_posix()
                        approval_keys: tuple[str, ...] = ()
                    else:
                        from apm_cli.security.executables import (
                            locked_dependency_approval_keys,
                        )

                        owner = locked_dependency.get_unique_key()
                        approval_keys = locked_dependency_approval_keys(locked_dependency)
                    collected.extend(
                        replace(
                            dependency,
                            resolved_by=owner,
                            approval_keys=approval_keys,
                        )
                        for dependency in lsp
                    )
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
        startup_timeout = out.pop(_CLAUDE_STARTUP_TIMEOUT_KEY, None)
        warmup_timeout = out.pop(_COPILOT_STARTUP_TIMEOUT_KEY, None)
        timeout = startup_timeout if startup_timeout is not None else warmup_timeout
        if timeout is not None:
            out[spec.startup_timeout_key] = timeout
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
        target_decision: EffectiveTargetDecision | None = None,
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
            try:
                from apm_cli.core.target_detection import EffectiveTargetDecision

                runtime_targets = (
                    EffectiveTargetDecision(
                        runtime,
                        "--target flag",
                    ).lsp_targets
                    or ()
                )
            except KeyError:
                runtime_targets = ()
            candidates = [target for target in runtime_targets if target in _LSP_TARGET_SPECS]
        elif target_decision is not None and target_decision.lsp_targets is not None:
            candidates = [
                target for target in target_decision.lsp_targets if target in _LSP_TARGET_SPECS
            ]
        else:
            candidates = []
            if find_runtime_binary("copilot") is not None:
                candidates.append("copilot")
            if (project_root_path / ".claude").is_dir() or find_runtime_binary(
                "claude"
            ) is not None:
                candidates.append("claude")

        if exclude:
            exclusions = {exclude}
            try:
                from apm_cli.core.target_detection import EffectiveTargetDecision

                exclusions.update(
                    EffectiveTargetDecision(exclude, "--exclude").canonical_targets or ()
                )
            except KeyError:
                pass
            candidates = [target for target in candidates if target not in exclusions]

        if not candidates:
            return []

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        target_runtimes = MCPIntegrator._gate_project_scoped_runtimes(
            candidates,
            user_scope=user_scope,
            project_root=project_root_path,
            apm_config=apm_config,
            explicit_target=explicit_target,
            target_decision=target_decision,
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

    @staticmethod
    def supported_target_runtimes(target_runtimes: list[str]) -> list[str]:
        """Return requested runtimes that have an LSP target adapter."""
        requested = set(target_runtimes)
        return [target for target in _LSP_TARGET_ORDER if target in requested]

    # ------------------------------------------------------------------
    # JSON write helpers
    # ------------------------------------------------------------------

    @staticmethod
    def reserved_project_skill_names(skills_dir: Path, project_root: Path) -> set[str]:
        """Return LSP-owned names nested under one target's skills directory."""
        reserved: set[str] = set()
        for spec in _LSP_TARGET_SPECS.values():
            parts = spec.project_relative_path
            if len(parts) < 3 or parts[1] != "skills":
                continue
            if skills_dir == project_root.joinpath(*parts[:2]):
                reserved.add(parts[2])
        return reserved

    @staticmethod
    def _target_config_path(
        spec: _LSPTargetSpec,
        project_root: Path,
        *,
        user_scope: bool,
    ) -> Path:
        """Resolve a target config through the canonical deployment-path gate."""
        if user_scope:
            user_root = Path.home()
            relative_path = Path(*spec.user_relative_path).as_posix()
            return BaseIntegrator.resolve_deploy_path(
                relative_path,
                user_root,
                allowed_prefixes=(relative_path,),
            )
        relative_path = Path(*spec.project_relative_path).as_posix()
        return BaseIntegrator.resolve_deploy_path(relative_path, project_root)

    @staticmethod
    def _read_json_object(config_path: Path, *, fail_on_error: bool = False) -> dict:
        """Read a JSON object from disk, returning an empty object on malformed input."""
        if not config_path.exists():
            return {}
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if fail_on_error:
                raise
            return {}
        if isinstance(data, dict):
            return data
        if fail_on_error:
            raise ValueError(f"{config_path} must contain a JSON object")
        return {}

    @staticmethod
    def _prepare_target_config(
        spec: _LSPTargetSpec,
        servers: dict[str, dict],
        *,
        project_root: Path,
        user_scope: bool,
        managed_server_names: builtins.set | None = None,
        force: bool = False,
    ) -> _PreparedTargetConfig:
        """Validate and render one target config without writing it."""
        config_path = LSPIntegrator._target_config_path(
            spec,
            project_root,
            user_scope=user_scope,
        )
        managed_names = managed_server_names or builtins.set()
        protected_plugin = bool(spec.config_defaults(user_scope=user_scope))
        skill_root = config_path.parent.parent
        if protected_plugin and skill_root.exists() and not force:
            foreign_entries = [
                entry.name for entry in skill_root.iterdir() if entry.name != ".claude-plugin"
            ]
            if config_path.parent.exists():
                foreign_entries.extend(
                    f".claude-plugin/{entry.name}"
                    for entry in config_path.parent.iterdir()
                    if entry != config_path
                )
            if foreign_entries:
                raise FileExistsError(
                    f"{spec.label(user_scope=user_scope)} shares its reserved skill "
                    f"directory with unowned content ({', '.join(sorted(foreign_entries))}); "
                    "move that content or rerun with --force"
                )
        if (
            protected_plugin
            and not managed_names
            and skill_root.exists()
            and not config_path.exists()
        ):
            if any(skill_root.iterdir()) and not force:
                raise FileExistsError(
                    f"{spec.label(user_scope=user_scope)} is inside an existing "
                    "skill directory not owned by APM; remove it or rerun with --force"
                )
        config_exists = config_path.exists()
        config = LSPIntegrator._read_json_object(
            config_path,
            fail_on_error=protected_plugin and not force,
        )
        for key, value in spec.config_defaults(user_scope=user_scope):
            current_default = config.get(key)
            if config_exists and current_default != value and not force:
                raise FileExistsError(
                    f"{spec.label(user_scope=user_scope)} is owned by plugin "
                    f"'{current_default or '(unnamed)'}', not APM; remove it or "
                    "rerun with --force"
                )
            config[key] = value
        servers_key = spec.servers_key(user_scope=user_scope)

        if servers_key is None:
            existing = config
            if not isinstance(existing, dict):
                existing = {}
                config = existing
        else:
            existing = config.get(servers_key, {})
            if not isinstance(existing, dict):
                if protected_plugin and not force:
                    raise ValueError(
                        f"{spec.label(user_scope=user_scope)} has a non-object "
                        f"'{servers_key}' value; repair it or rerun with --force"
                    )
                existing = {}
            config[servers_key] = existing

        changed: builtins.set = builtins.set()
        for name, server_config in servers.items():
            if (
                protected_plugin
                and name in existing
                and existing[name] != server_config
                and name not in managed_names
                and not force
            ):
                raise FileExistsError(
                    f"LSP server '{name}' already exists in "
                    f"{spec.label(user_scope=user_scope)} and is not managed by APM; "
                    "rename it or rerun with --force"
                )
            if existing.get(name) != server_config:
                changed.add(name)
            existing[name] = server_config

        return _PreparedTargetConfig(
            path=config_path,
            content=json.dumps(config, indent=2) + "\n",
            changed=changed,
        )

    @staticmethod
    def _write_prepared_target_config(prepared: _PreparedTargetConfig) -> builtins.set:
        """Write one config after every target in the install has validated."""
        prepared.path.parent.mkdir(parents=True, exist_ok=True)
        write_text_lf(prepared.path, prepared.content)
        return prepared.changed

    @staticmethod
    def _write_target_config(
        spec: _LSPTargetSpec,
        servers: dict[str, dict],
        *,
        project_root: Path,
        user_scope: bool,
        managed_server_names: builtins.set | None = None,
        force: bool = False,
    ) -> builtins.set:
        """Validate, merge, and write one target config."""
        prepared = LSPIntegrator._prepare_target_config(
            spec,
            servers,
            project_root=project_root,
            user_scope=user_scope,
            managed_server_names=managed_server_names,
            force=force,
        )
        return LSPIntegrator._write_prepared_target_config(prepared)

    @staticmethod
    def _clean_target_config(
        spec: _LSPTargetSpec,
        stale_names: builtins.set,
        *,
        project_root: Path,
        user_scope: bool,
        fail_on_write_error: bool = False,
    ) -> list[str]:
        """Remove stale names from one target config and return removed names."""
        config_path = LSPIntegrator._target_config_path(
            spec,
            project_root,
            user_scope=user_scope,
        )
        if not config_path.exists():
            return []
        config = LSPIntegrator._read_json_object(
            config_path,
            fail_on_error=fail_on_write_error,
        )
        for key, value in spec.config_defaults(user_scope=user_scope):
            if config.get(key) != value:
                if fail_on_write_error:
                    raise ValueError(f"{spec.label(user_scope=user_scope)} is not owned by APM")
                return []
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
            owned_keys = {key for key, _value in spec.config_defaults(user_scope=user_scope)}
            if servers_key is not None:
                owned_keys.add(servers_key)
            if not servers and owned_keys and set(config) <= owned_keys:
                config_path.unlink()
                for directory in spec.cleanup_empty_dirs(project_root, user_scope=user_scope):
                    try:
                        directory.rmdir()
                    except OSError:
                        break
            else:
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
        fail_on_write_error: bool = False,
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
                    fail_on_write_error=fail_on_write_error,
                )
                if removed:
                    noun = "server" if len(removed) == 1 else "servers"
                    removed_names = ", ".join(sorted(removed))
                    logger.progress(
                        f"Removed {len(removed)} stale LSP {noun} ({removed_names}) from "
                        f"{spec.label(user_scope=user_scope)}"
                    )
                    for name in removed:
                        logger.verbose_detail(f"Removed stale LSP server: {name}")
                    if runtime == "claude" and not user_scope:
                        logger.progress(
                            "  |-- run /reload-plugins or restart Claude Code to activate"
                        )
            except Exception as exc:
                _log.debug(
                    "Failed to clean stale LSP servers from %s",
                    spec.label(user_scope=user_scope),
                    exc_info=True,
                )
                if fail_on_write_error:
                    from apm_cli.install.errors import RequiredIntegrationError

                    raise RequiredIntegrationError(
                        f"LSP cleanup failed for target '{runtime}' at "
                        f"{spec.label(user_scope=user_scope)}: {exc}. "
                        "Review the path and permissions, then retry."
                    ) from exc

    # ------------------------------------------------------------------
    # Lockfile persistence
    # ------------------------------------------------------------------

    @staticmethod
    def update_lockfile(
        lsp_server_names: builtins.set,
        lock_path: Path | None = None,
        *,
        lsp_configs: builtins.dict | None = None,
        lsp_target_servers: dict[str, set[str]] | None = None,
        lsp_config_provenance: dict[str, str] | None = None,
        lockfile_state: LockFile | None = None,
        persist: bool = True,
        fail_on_write_error: bool = False,
    ) -> None:
        """Update the lockfile with the current set of APM-managed LSP servers."""
        if lock_path is None and persist:
            lock_path = get_lockfile_path(Path.cwd())
        try:
            lockfile = lockfile_state
            if lockfile is None and lock_path is not None:
                lockfile = LockFile.read(lock_path) if lock_path.exists() else LockFile()
            if lockfile is None:
                lockfile = LockFile()
            lockfile.lsp_servers = sorted(lsp_server_names)
            if lsp_configs is not None:
                lockfile.lsp_configs = lsp_configs
            if lsp_config_provenance is not None:
                lockfile.lsp_config_provenance = lsp_config_provenance
            if lsp_target_servers is not None:
                from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

                DeploymentLedgerCodec.replace_lsp_target_servers(
                    lockfile,
                    {
                        runtime: sorted(names)
                        for runtime, names in lsp_target_servers.items()
                        if names
                    },
                )
            if persist and lock_path is not None:
                lockfile.save(lock_path)
        except Exception as exc:
            _log.debug(
                "Failed to update LSP servers in lockfile at %s",
                lock_path,
                exc_info=True,
            )
            if fail_on_write_error:
                from apm_cli.install.errors import RequiredIntegrationError

                raise RequiredIntegrationError(
                    "LSP lockfile update failed. Check apm.lock.yaml permissions, then retry."
                ) from exc

    # ------------------------------------------------------------------
    # Target deployment
    # ------------------------------------------------------------------

    @staticmethod
    def install(
        lsp_deps: list,
        project_root=None,
        user_scope: bool = False,
        logger=None,
        diagnostics=None,
        target_runtimes: list[str] | None = None,
        fail_on_write_error: bool = False,
        managed_server_names: builtins.set | None = None,
        managed_target_servers: dict[str, set[str]] | None = None,
        force: bool = False,
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

        prepared_targets: list[tuple[str, _LSPTargetSpec, _PreparedTargetConfig]] = []
        for runtime in runtimes:
            spec = _LSP_TARGET_SPECS[runtime]
            servers = LSPIntegrator._servers_for_target(base_servers, spec)
            try:
                managed_names = managed_server_names
                if managed_target_servers is not None:
                    managed_names = managed_target_servers.get(runtime, set())
                prepared = LSPIntegrator._prepare_target_config(
                    spec,
                    servers,
                    project_root=project_root_path,
                    user_scope=user_scope,
                    managed_server_names=managed_names,
                    force=force,
                )
                prepared_targets.append((runtime, spec, prepared))
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
                if fail_on_write_error:
                    from apm_cli.install.errors import RequiredIntegrationError

                    raise RequiredIntegrationError(
                        f"LSP configuration failed for target '{runtime}' at "
                        f"{spec.label(user_scope=user_scope)}: {exc}. "
                        "Review the path and permissions, then retry; use --force "
                        "only for a reviewed ownership collision."
                    ) from exc

        changed_servers: builtins.set = builtins.set()
        for runtime, spec, prepared in prepared_targets:
            try:
                changed = LSPIntegrator._write_prepared_target_config(prepared)
                changed_servers.update(changed)
                if changed:
                    noun = "server" if len(changed) == 1 else "servers"
                    logger.progress(
                        f"Configured {len(changed)} LSP {noun} in "
                        f"{spec.label(user_scope=user_scope)}"
                    )
                    if runtime == "claude" and not user_scope:
                        logger.progress(
                            "  |-- run /reload-plugins or restart Claude Code to activate"
                        )
                if runtime == "claude" and not user_scope:
                    legacy_path = project_root_path / ".lsp.json"
                    if legacy_path.exists():
                        message = (
                            "Retained legacy .lsp.json. Claude Code does not discover "
                            "project LSP servers there; review and remove it after "
                            "migrating any user-owned entries."
                        )
                        if diagnostics is not None:
                            diagnostics.warn(message)
                        else:
                            logger.warning(message)
            except Exception as exc:
                _log.debug(
                    "Failed to write LSP config to %s",
                    spec.label(user_scope=user_scope),
                    exc_info=True,
                )
                if diagnostics:
                    diagnostics.warn(
                        f"Failed to write LSP config to {prepared.path}: "
                        f"{exc}. Check file permissions or run with --verbose for details."
                    )
                if fail_on_write_error:
                    from apm_cli.install.errors import RequiredIntegrationError

                    raise RequiredIntegrationError(
                        f"LSP configuration failed for target '{runtime}' at "
                        f"{spec.label(user_scope=user_scope)}: {exc}. "
                        "Review the path and permissions, then retry."
                    ) from exc

        return len(changed_servers)
