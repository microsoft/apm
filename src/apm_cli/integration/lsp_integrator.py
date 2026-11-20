"""Runtime target adapter for LSP configuration.

Owns target paths, shapes, file writes, and cleanup mechanics. The install LSP
pipeline owns collection, trust filtering, and lifecycle reconciliation.
"""

from __future__ import annotations

import builtins
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.deps.lockfile import LockFile, LockfileRefusal, get_lockfile_path
from apm_cli.deps.lockfile_write_authority import lockfile_exists
from apm_cli.integration._shared import (
    deduplicate_deps,
    resolve_locked_apm_yml_sources,
)
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.runtime.utils import find_runtime_binary
from apm_cli.utils.atomic_io import atomic_write_text, umask_file_mode
from apm_cli.utils.path_security import PathTraversalError

if TYPE_CHECKING:
    from apm_cli.core.target_detection import EffectiveTargetDecision

_log = logging.getLogger(__name__)

_LSP_SERVERS_KEY = "lspServers"
_CLAUDE_LANGUAGE_KEY = "extensionToLanguage"
_COPILOT_LANGUAGE_KEY = "fileExtensions"
_CLAUDE_STARTUP_TIMEOUT_KEY = "startupTimeout"
_COPILOT_STARTUP_TIMEOUT_KEY = "warmupTimeoutMs"
_LEGACY_DEFAULT_TARGETS = ("claude",)
_LSP_TARGET_ORDER = ("copilot", "claude", "opencode")
_OPENCODE_SCHEMA_URL = "https://opencode.ai/config.json"
_OPENCODE_LSP_KEY = "lsp"
_SNAKE_LANGUAGE_KEY = "extension_to_language"


class _SkipServer(Exception):
    """Raised by an entry translator when a server cannot be expressed for a target."""


def _language_map(entry: dict) -> dict | None:
    """Return the extension-to-language map under any accepted spelling."""
    return (
        entry.get(_CLAUDE_LANGUAGE_KEY)
        or entry.get(_COPILOT_LANGUAGE_KEY)
        or entry.get(_SNAKE_LANGUAGE_KEY)
    )


def _opencode_entry(entry: dict) -> dict:
    """Translate a neutral LSP entry to OpenCode's ``lsp`` schema.

    OpenCode shape: ``{command: [cmd, *args], extensions: [...], env, initialization}``.
    Unsupported fields (timeouts, restart policy, workspace folder, settings) are
    dropped. Raises ``_SkipServer`` for transports OpenCode cannot express and
    for name-only declarations, since an OpenCode server entry needs a command.
    """
    transport = entry.get("transport")
    if transport is not None and transport != "stdio":
        raise _SkipServer(f"transport '{transport}' is not supported by OpenCode")
    command = entry.get("command")
    if not command:
        raise _SkipServer(
            "it declares no 'command' (name-only form), which OpenCode requires; "
            "declare it in object form with 'command' and 'extensionToLanguage'"
        )
    out: dict = {"command": [command, *(entry.get("args") or [])]}
    language_map = _language_map(entry)
    if language_map:
        out["extensions"] = sorted(
            {ext if ext.startswith(".") else f".{ext}" for ext in language_map}
        )
    if entry.get("env"):
        out["env"] = entry["env"]
    if entry.get("initializationOptions") is not None:
        out["initialization"] = entry["initializationOptions"]
    return out


@dataclass(frozen=True)
class _LSPTargetSpec:
    """On-disk LSP config contract for one runtime target."""

    runtime: str
    project_relative_path: tuple[str, ...]
    user_relative_path: tuple[str, ...]
    """Config path relative to the runtime's user root (``resolve_user_root``)."""
    project_servers_key: str | None
    user_servers_key: str | None
    project_label: str
    project_config_defaults: tuple[tuple[str, str], ...] = ()
    user_config_defaults: tuple[tuple[str, str], ...] = ()
    cleanup_empty_relative_dirs: tuple[tuple[str, ...], ...] = ()
    user_cleanup_empty_relative_dirs: tuple[tuple[str, ...], ...] = ()
    # Keys of the default rename-only translation (unused with entry_translator).
    language_key: str | None = None
    startup_timeout_key: str | None = None
    # Optional per-target entry translator (replaces the rename-only default).
    # It receives the neutral entry and returns the on-disk entry, or raises
    # ``_SkipServer`` to omit that server for this target only.
    entry_translator: Callable[[dict], dict] | None = None
    # Top-level keys written only when APM creates the file (never overwritten).
    # They do not count as user content when cleanup decides to delete the file.
    created_defaults: tuple[tuple[str, str], ...] = ()
    # The file also holds settings APM does not own (for example OpenCode's
    # ``mcp`` key). A file that is not a JSON object, or whose servers key is
    # not an object, is refused instead of being rewritten from scratch,
    # even with --force. A same-name server APM does not manage is a
    # collision that only --force overrides.
    shared_config: bool = False
    # Shown once when APM creates the servers key, for a harness where that
    # key has a side effect beyond the servers APM writes.
    servers_key_created_note: str | None = None
    # Native project-root reference for an authored ${CLAUDE_PLUGIN_ROOT} at
    # project scope (#3825); None writes the absolute package path.
    plugin_root_project_reference: str | None = None

    def user_root(self) -> Path:
        """Return the runtime's user root through the harness user-root owner."""
        from apm_cli.integration.targets import resolve_user_root

        return resolve_user_root(self.runtime)

    def path(self, project_root: Path, *, user_scope: bool) -> Path:
        """Return the config path for this target and scope."""
        if user_scope:
            return self.user_root().joinpath(*self.user_relative_path)
        return project_root.joinpath(*self.project_relative_path)

    def servers_key(self, *, user_scope: bool) -> str | None:
        """Return the wrapper key for this scope, or None for top-level maps."""
        return self.user_servers_key if user_scope else self.project_servers_key

    def label(self, *, user_scope: bool) -> str:
        """Return a human-readable config path label."""
        if user_scope:
            from apm_cli.integration.targets import user_root_display

            return "/".join((user_root_display(self.runtime), *self.user_relative_path))
        return self.project_label

    def config_defaults(self, *, user_scope: bool) -> tuple[tuple[str, str], ...]:
        """Return required top-level defaults for this target and scope."""
        return self.user_config_defaults if user_scope else self.project_config_defaults

    def cleanup_empty_dirs(self, project_root: Path, *, user_scope: bool) -> tuple[Path, ...]:
        """Return APM-owned directories this target may remove when empty."""
        if user_scope:
            root = self.user_root()
            return tuple(root.joinpath(*parts) for parts in self.user_cleanup_empty_relative_dirs)
        return tuple(project_root.joinpath(*parts) for parts in self.cleanup_empty_relative_dirs)


@dataclass(frozen=True)
class _PreparedTargetConfig:
    """Validated target config ready for one atomic file write."""

    path: Path
    content: str
    changed: builtins.set
    created_servers_key: bool = False
    owner_only: bool = False


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
        user_relative_path=("skills", "apm-lsp", ".claude-plugin", "plugin.json"),
        language_key=_CLAUDE_LANGUAGE_KEY,
        startup_timeout_key=_CLAUDE_STARTUP_TIMEOUT_KEY,
        project_servers_key=_LSP_SERVERS_KEY,
        user_servers_key=_LSP_SERVERS_KEY,
        project_label=".claude/skills/apm-lsp/.claude-plugin/plugin.json",
        project_config_defaults=(("name", "apm-lsp"),),
        user_config_defaults=(("name", "apm-lsp"),),
        cleanup_empty_relative_dirs=(
            (".claude", "skills", "apm-lsp", ".claude-plugin"),
            (".claude", "skills", "apm-lsp"),
        ),
        user_cleanup_empty_relative_dirs=(
            ("skills", "apm-lsp", ".claude-plugin"),
            ("skills", "apm-lsp"),
        ),
        # code.claude.com/docs/en/plugins-reference "Where each variable
        # resolves": plugin LSP command, args, env and workspaceFolder
        # substitute ${CLAUDE_PROJECT_DIR}.
        plugin_root_project_reference="${CLAUDE_PROJECT_DIR}",
    ),
    "copilot": _LSPTargetSpec(
        runtime="copilot",
        project_relative_path=(".github", "lsp.json"),
        user_relative_path=("lsp-config.json",),
        language_key=_COPILOT_LANGUAGE_KEY,
        startup_timeout_key=_COPILOT_STARTUP_TIMEOUT_KEY,
        project_servers_key=_LSP_SERVERS_KEY,
        user_servers_key=_LSP_SERVERS_KEY,
        project_label=".github/lsp.json",
    ),
    "opencode": _LSPTargetSpec(
        runtime="opencode",
        project_relative_path=("opencode.json",),
        user_relative_path=("opencode.json",),
        project_servers_key=_OPENCODE_LSP_KEY,
        user_servers_key=_OPENCODE_LSP_KEY,
        project_label="opencode.json",
        entry_translator=_opencode_entry,
        created_defaults=(("$schema", _OPENCODE_SCHEMA_URL),),
        shared_config=True,
        # https://opencode.ai/docs/lsp/: "If `lsp` is omitted, all LSP servers
        # are disabled." "Use an object to keep built-ins enabled".
        servers_key_created_note=(
            "OpenCode enables its built-in language servers when the config has an "
            "'lsp' object; set \"disabled\": true on a built-in server to turn it off"
        ),
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
        claude_native_keys: frozenset[str] = frozenset(),
    ) -> list:
        """Collect LSP dependencies from resolved APM packages listed in apm.lock.

        Only scans apm.yml files for packages present in apm.lock to avoid
        picking up stale/orphaned packages from previous installs.
        Falls back to scanning all apm.yml files if no lock file is available.

        Declaring-package provenance is attached so the install pipeline can
        enforce executable approval before exposing transitive servers, with
        the runtimes that load each server natively from its plugin.
        """
        from apm_cli.claude_plugins.admission import native_service_runtimes

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
                    native_runtimes: frozenset[str] = frozenset()
                    if locked_dependency is None:
                        owner = apm_yml_path.parent.relative_to(apm_modules_dir).as_posix()
                        approval_keys: tuple[str, ...] = ()
                    else:
                        from apm_cli.security.executables import (
                            locked_dependency_approval_keys,
                        )

                        owner = locked_dependency.get_unique_key()
                        approval_keys = locked_dependency_approval_keys(locked_dependency)
                        native_runtimes = native_service_runtimes(
                            owner, locked_dependency, claude_native_keys
                        )
                    collected.extend(
                        replace(
                            dependency,
                            resolved_by=owner,
                            approval_keys=approval_keys,
                            native_runtimes=native_runtimes,
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
        if spec.entry_translator is not None:
            return spec.entry_translator(entry)
        language_map = _language_map(entry)
        out = {
            key: value
            for key, value in entry.items()
            if key not in (_SNAKE_LANGUAGE_KEY, _CLAUDE_LANGUAGE_KEY, _COPILOT_LANGUAGE_KEY)
        }
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
    def _servers_for_target(
        servers: dict[str, dict],
        spec: _LSPTargetSpec,
        warn: Callable[[str], object],
    ) -> dict[str, dict]:
        """Translate all server entries to one target's schema.

        Servers a target cannot express are skipped and reported through *warn*.
        """
        out: dict[str, dict] = {}
        for name, entry in servers.items():
            try:
                out[name] = LSPIntegrator._entry_for_target(entry, spec)
            except _SkipServer as skip:
                warn(f"Skipped LSP server '{name}' for {spec.runtime}: {skip}")
        return out

    @staticmethod
    def _render_plugin_roots(
        servers: dict[str, dict],
        lsp_deps: list,
        spec: _LSPTargetSpec,
        *,
        project_root: Path,
        user_scope: bool,
    ) -> dict[str, dict]:
        """Expand authored ``${CLAUDE_PLUGIN_ROOT}`` for one target and scope."""
        from apm_cli.deps.plugin_root_render import (
            finalize_project_root,
            plugin_root_of,
            render_plugin_root,
        )

        roots: dict[str, str] = {}
        for dep in lsp_deps:
            root = plugin_root_of(dep)
            if root and hasattr(dep, "name"):
                roots.setdefault(dep.name, root)
        reference = None if user_scope else spec.plugin_root_project_reference
        return {
            name: finalize_project_root(
                render_plugin_root(
                    entry,
                    plugin_root=roots.get(name),
                    project_root=project_root,
                    project_relative=reference is not None,
                ),
                reference,
            )
            for name, entry in servers.items()
        }

    @staticmethod
    def expressible_server_names(lsp_deps: list, runtime: str) -> builtins.set:
        """Return the names of *lsp_deps* that *runtime* can write.

        Lifecycle owners record target ownership from this set so a server a
        target skips (for example a socket server on OpenCode) is never
        claimed, and later cleanup cannot delete a same-name user entry.
        """
        servers = LSPIntegrator._servers_for_target(
            LSPIntegrator._base_server_entries(LSPIntegrator.deps_for_runtime(lsp_deps, runtime)),
            _LSP_TARGET_SPECS[runtime],
            warn=lambda _message: None,
        )
        return builtins.set(servers)

    @staticmethod
    def deps_for_runtime(lsp_deps: list, runtime: str) -> list:
        """Drop servers *runtime* loads natively from their plugin (never APM's copy)."""
        from apm_cli.install.mcp.native_runtimes import native_runtimes_of

        return [dep for dep in lsp_deps if runtime not in native_runtimes_of(dep)]

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
            from apm_cli.integration.targets import user_root_exists

            candidates = []
            if find_runtime_binary("copilot") is not None or (
                user_scope and user_root_exists("copilot")
            ):
                candidates.append("copilot")
            claude_root = (
                user_root_exists("claude")
                if user_scope
                else (project_root_path / ".claude").is_dir()
            )
            if claude_root or find_runtime_binary("claude") is not None:
                candidates.append("claude")
            opencode_root = (
                user_root_exists("opencode")
                if user_scope
                else (project_root_path / ".opencode").is_dir()
            )
            if opencode_root or find_runtime_binary("opencode") is not None:
                candidates.append("opencode")

        if exclude:
            from apm_cli.integration.mcp_user_scope_runtimes import filter_excluded_mcp_runtimes

            candidates = filter_excluded_mcp_runtimes(candidates, exclude)

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
    def target_choice_hint() -> str:
        """Return e.g. ``--target copilot, --target claude or --target opencode``."""
        flags = [f"--target {target}" for target in _LSP_TARGET_ORDER]
        return f"{', '.join(flags[:-1])} or {flags[-1]}"

    @staticmethod
    def supported_target_runtimes(target_runtimes: list[str]) -> list[str]:
        """Return requested runtimes that have an LSP target adapter."""
        requested = set(target_runtimes)
        return [target for target in _LSP_TARGET_ORDER if target in requested]

    # ------------------------------------------------------------------
    # JSON write helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _target_config_path(
        spec: _LSPTargetSpec,
        project_root: Path,
        *,
        user_scope: bool,
    ) -> Path:
        """Resolve a target config through the canonical deployment-path gate.

        The gate is pinned to the spec's exact path in both scopes, which also
        admits root-level files such as ``opencode.json`` without widening the
        harness directory prefixes.
        """
        if user_scope:
            root = spec.user_root()
            relative = Path(*spec.user_relative_path)
            # Validate from HOME when the root lives there, so a symlinked
            # ``~/.claude`` or ``~/.copilot`` stays rejected; a configured
            # root outside HOME must not be a symlink either.
            home = Path.home()
            if root.is_relative_to(home):
                relative = root.relative_to(home) / relative
                root = home
            elif root.is_symlink():
                raise PathTraversalError(
                    f"Refusing deployment through a symlinked path component: {root.as_posix()}"
                )
        else:
            root = project_root
            relative = Path(*spec.project_relative_path)
        relative_path = relative.as_posix()
        return BaseIntegrator.resolve_deploy_path(
            relative_path,
            root,
            allowed_prefixes=(relative_path,),
        )

    @staticmethod
    def _read_json_object(
        config_path: Path,
        *,
        fail_on_error: bool = False,
        allow_blank: bool = False,
    ) -> dict:
        """Read a JSON object from disk, returning an empty object on malformed input.

        With *allow_blank*, an empty or whitespace-only file reads as ``{}``
        even when *fail_on_error* is set.
        """
        if not config_path.exists():
            return {}
        try:
            raw = config_path.read_text(encoding="utf-8")
            if allow_blank and not raw.strip():
                return {}
            data = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError):
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
        try:
            config = LSPIntegrator._read_json_object(
                config_path,
                fail_on_error=spec.shared_config or (protected_plugin and not force),
                allow_blank=spec.shared_config,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            if not spec.shared_config:
                raise
            raise ValueError(
                f"{spec.label(user_scope=user_scope)} is not a plain JSON object "
                f"({exc}); APM will not rewrite it because that would drop other "
                "settings. Fix the file (comments and trailing commas are not "
                "supported) or add the LSP servers manually"
            ) from exc
        if not config_exists:
            for key, value in spec.created_defaults:
                config[key] = value
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
        created_servers_key = servers_key is not None and servers_key not in config

        if servers_key is None:
            existing = config
            if not isinstance(existing, dict):
                existing = {}
                config = existing
        else:
            existing = config.get(servers_key, {})
            if not isinstance(existing, dict):
                if spec.shared_config:
                    raise ValueError(
                        f"{spec.label(user_scope=user_scope)} has a non-object "
                        f"'{servers_key}' value; replace it with an object to let "
                        "APM add LSP servers"
                    )
                if protected_plugin and not force:
                    raise ValueError(
                        f"{spec.label(user_scope=user_scope)} has a non-object "
                        f"'{servers_key}' value; repair it or rerun with --force"
                    )
                existing = {}
            config[servers_key] = existing

        changed: builtins.set = builtins.set()
        for name, server_config in servers.items():
            # A same-name entry APM does not manage is user content: refuse to
            # overwrite it (and so never claim it) unless --force is given.
            if (
                (protected_plugin or spec.shared_config)
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
            created_servers_key=created_servers_key,
            owner_only=user_scope,
        )

    @staticmethod
    def _write_prepared_target_config(prepared: _PreparedTargetConfig) -> builtins.set:
        """Write one config after every target in the install has validated."""
        prepared.path.parent.mkdir(parents=True, exist_ok=True)
        LSPIntegrator._atomic_write_config(
            prepared.path, prepared.content, owner_only=prepared.owner_only
        )
        return prepared.changed

    @staticmethod
    def _atomic_write_config(config_path: Path, content: str, *, owner_only: bool) -> None:
        """Replace *config_path* atomically so an interrupted write never truncates it.

        User-scope configs can carry literal ``env`` values, so they stay
        owner-only (``0600``) like user-scope MCP configs. A project config
        keeps its existing mode, or gets the mode a plain ``open()`` would.
        """
        if not owner_only:
            atomic_write_text(config_path, content, new_file_mode=umask_file_mode(config_path))
            return
        if config_path.is_file():
            os.chmod(config_path, 0o600)
        atomic_write_text(config_path, content, new_file_mode=0o600)
        os.chmod(config_path, 0o600)

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
            # A created default counts as APM-owned only while it still holds
            # the value APM writes; a user-set value (for example a custom
            # ``$schema``) is user content that keeps the file alive.
            owned_keys.update(
                key for key, value in spec.created_defaults if config.get(key) == value
            )
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
                LSPIntegrator._atomic_write_config(
                    config_path, json.dumps(config, indent=2) + "\n", owner_only=user_scope
                )
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
                lockfile = LockFile.read(lock_path) if lockfile_exists(lock_path) else LockFile()
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

                if isinstance(exc, LockfileRefusal):
                    raise RequiredIntegrationError(str(exc)) from exc
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

        prepared_targets: list[tuple[str, _LSPTargetSpec, _PreparedTargetConfig]] = []
        for runtime in runtimes:
            spec = _LSP_TARGET_SPECS[runtime]
            runtime_deps = LSPIntegrator.deps_for_runtime(lsp_deps, runtime)
            servers = LSPIntegrator._servers_for_target(
                LSPIntegrator._base_server_entries(runtime_deps),
                spec,
                warn=diagnostics.warn if diagnostics is not None else logger.warning,
            )
            if not servers:
                continue
            servers = LSPIntegrator._render_plugin_roots(
                servers,
                runtime_deps,
                spec,
                project_root=project_root_path,
                user_scope=user_scope,
            )
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
                    if prepared.created_servers_key and spec.servers_key_created_note:
                        logger.progress(spec.servers_key_created_note)
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
