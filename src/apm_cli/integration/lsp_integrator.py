"""Runtime target adapter for LSP configuration.

Owns target paths, shapes, file writes, and cleanup mechanics. The install LSP
pipeline owns collection, trust filtering, and lifecycle reconciliation.
"""

from __future__ import annotations

import builtins
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.deps.lockfile import LockFile, LockfileRefusal, get_lockfile_path
from apm_cli.deps.lockfile_write_authority import lockfile_exists
from apm_cli.integration import write_seam
from apm_cli.integration._shared import (
    deduplicate_deps,
    resolve_locked_apm_yml_sources,
)
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.write_seam import atomic_write_text
from apm_cli.runtime.utils import find_runtime_binary
from apm_cli.utils.atomic_io import umask_file_mode
from apm_cli.utils.path_security import PathTraversalError
from apm_cli.utils.paths import portable_relpath

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
    # None leaves the file as it is: a target that joined a hold and changes no server (#4871).
    content: str | None
    changed: builtins.set
    created_servers_key: bool = False
    owner_only: bool = False
    # Same-name entries APM did not write, left as the user wrote them (#4628):
    # *preserved* differ from the package's entry, *matching* already equal it.
    preserved: tuple[str, ...] = ()
    matching: tuple[str, ...] = ()
    # Managed server name -> "drift" | "blocked" | "unproven" for an entry kept (#4626).
    kept: dict[str, str] = field(default_factory=dict)
    # Servers held at a locked render this target cannot express: not written (#4626).
    withheld: tuple[str, ...] = ()
    # Managed servers the new config cannot express here, held at the locked
    # render on this target so its claim survives the blocked upgrade (#4871).
    joined: tuple[str, ...] = ()


@dataclass
class LspInstallRecord:
    """What one ``LSPIntegrator.install`` call is told and what it kept.

    *locked_configs* (in) are the server configs of the existing lock, the
    evidence of which entry APM wrote (#4626); ``None`` judges no entry.
    *kept_unowned* (out) maps runtime -> server names left as the user wrote
    them, never to be recorded as APM-owned (#4628). *kept_edits* (out) maps a
    managed server whose edit install kept to ``"drift"``, ``"blocked"`` or
    ``"unproven"``; the caller keeps its locked config (#4626). *held_claims*
    (out) maps runtime -> managed server names held at their locked render on
    a target the new config cannot reach; the caller keeps that target claim
    so stale cleanup leaves the entry (#4871). *locked_roots* (in) maps a
    locked server to its locked declaring package's root: an entry rendered
    there is APM's, so a package move is an update, not an edit (#4872).
    """

    locked_configs: dict[str, dict] | None = None
    locked_roots: dict[str, str] = field(default_factory=dict)
    kept_unowned: dict[str, builtins.set] = field(default_factory=dict)
    kept_edits: dict[str, str] = field(default_factory=dict)
    held_claims: dict[str, builtins.set] = field(default_factory=dict)


@dataclass
class _CleanupPlan:
    """One target config read for stale cleanup, and the names it will drop or keep."""

    config_path: Path
    config: dict
    servers_key: str | None
    servers: dict
    removed: list[str]
    edited: list[str]


def _written_by_apm(entry: object, expected: dict) -> bool:
    """Whether an on-disk entry is exactly what APM rendered from its locked config.

    A render that still holds ``${CLAUDE_PLUGIN_ROOT}`` had no proven package
    root, so it proves nothing: the entry is kept and reported (#5263).
    """
    from apm_cli.deps.plugin_root_render import contains_plugin_root

    return entry == expected and not contains_plugin_root(expected)


def _cleanup_failed(
    runtime: str, spec: _LSPTargetSpec, user_scope: bool, exc: Exception, fail: bool
) -> None:
    """Log a failed target cleanup; raise the actionable refusal when it must not pass."""
    label = spec.label(user_scope=user_scope)
    _log.debug("Failed to clean stale LSP servers from %s", label, exc_info=True)
    if fail:
        from apm_cli.install.errors import RequiredIntegrationError

        raise RequiredIntegrationError(
            f"LSP cleanup failed for target '{runtime}' at {label}: {exc}. "
            "Review the path and permissions, then retry."
        ) from exc


def _report_removed(logger, runtime: str, label: str, removed: list[str], *, user_scope: bool):
    """Report the stale servers removed from one target config."""
    noun = "server" if len(removed) == 1 else "servers"
    logger.progress(
        f"Removed {len(removed)} stale LSP {noun} ({', '.join(sorted(removed))}) from {label}"
    )
    for name in removed:
        logger.verbose_detail(f"Removed stale LSP server: {name}")
    if runtime == "claude" and not user_scope:
        logger.progress("  |-- run /reload-plugins or restart Claude Code to activate")


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
    def matching_entry_names(
        lsp_deps: list, runtime: str, *, project_root: Path, user_scope: bool
    ) -> builtins.set:
        """Names whose *runtime* config entry already equals what install would write.

        A frozen install needs no recorded claim for them: install leaves such
        an entry as it is whoever wrote it (#4628), as MCP does for a matching
        native connection.
        """
        spec = _LSP_TARGET_SPECS[runtime]
        runtime_deps = LSPIntegrator.deps_for_runtime(lsp_deps, runtime)
        servers = LSPIntegrator._servers_for_target(
            LSPIntegrator._base_server_entries(runtime_deps), spec, warn=lambda _message: None
        )
        if not servers:
            return builtins.set()
        try:
            servers = LSPIntegrator._render_plugin_roots(
                servers, runtime_deps, spec, project_root=project_root, user_scope=user_scope
            )
            config = LSPIntegrator._read_json_object(
                LSPIntegrator._target_config_path(spec, project_root, user_scope=user_scope)
            )
        except (OSError, ValueError):
            return builtins.set()
        servers_key = spec.servers_key(user_scope=user_scope)
        existing = config if servers_key is None else config.get(servers_key)
        if not isinstance(existing, dict):
            return builtins.set()
        return {name for name, entry in servers.items() if existing.get(name) == entry}

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
        legacy_server_names: builtins.set | None = None,
        locked_servers: dict[str, dict] | None = None,
        held: builtins.set | frozenset = frozenset(),
        locked_written: dict[str, dict] | None = None,
    ) -> _PreparedTargetConfig:
        """Validate and render one target config without writing it.

        *locked_servers* holds the entries APM renders for this target from
        the locked server configs: a managed entry that differs from both the
        new render and the locked one is kept, never rewritten (#4626).
        ``None`` (a caller without lock evidence) judges no entry. A *held*
        server, kept on another target, stays at its locked render here too.
        *locked_written* renders the locked configs at their locked package's
        root: an entry equal to it is APM's too (#4872), and a held server
        stays at it (#5437).
        """
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
        preserved: list[str] = []
        matching: list[str] = []
        kept: dict[str, str] = {}
        withheld: list[str] = []
        for name, server_config in servers.items():
            if name in held:
                # One recorded baseline per server: hold every target at it (#4626).
                # At the locked root: the render an untouched entry still holds (#5437).
                server_config = (locked_written or {}).get(name) or (locked_servers or {}).get(name)
                if server_config is None:
                    withheld.append(name)
                    continue
            # A same-name entry APM does not manage is user content: never
            # overwrite it (and so never claim it).
            foreign = name in existing and name not in managed_names
            unmanaged = foreign and existing[name] != server_config
            if unmanaged and (protected_plugin or spec.shared_config) and not force:
                raise FileExistsError(
                    f"LSP server '{name}' already exists in "
                    f"{spec.label(user_scope=user_scope)} and is not managed by APM; "
                    "rename it or rerun with --force"
                )
            if (
                foreign
                and not (protected_plugin or spec.shared_config)
                and name not in (legacy_server_names or ())
            ):
                # Kept like a native MCP connection: equality alone never
                # establishes ownership, and --force does not take it.
                (preserved if unmanaged else matching).append(name)
                continue
            if existing.get(name) == server_config:
                existing[name] = server_config
                continue
            kind = (
                None
                if force
                or locked_servers is None
                or name not in existing
                or (name not in managed_names and name not in (legacy_server_names or ()))
                else LSPIntegrator._kept_entry_kind(
                    existing[name],
                    locked_servers.get(name),
                    server_config,
                    written=(locked_written or {}).get(name),
                )
            )
            if kind is not None:
                kept[name] = kind
                continue
            changed.add(name)
            existing[name] = server_config

        return _PreparedTargetConfig(
            path=config_path,
            content=json.dumps(config, indent=2) + "\n",
            changed=changed,
            created_servers_key=created_servers_key,
            owner_only=user_scope,
            preserved=tuple(preserved),
            matching=tuple(matching),
            withheld=tuple(withheld),
            kept=kept,
        )

    @staticmethod
    def _locked_target_servers(
        locked_configs: dict[str, dict] | None,
        runtime_deps: list,
        spec: _LSPTargetSpec,
        *,
        project_root: Path,
        user_scope: bool,
        locked_roots: Mapping[str, str] | None = None,
    ) -> dict[str, dict] | None:
        """The entries APM renders for *spec* from the locked server configs (#4626).

        Only servers this install renders are judged; each locked config goes
        through the same rendering as a fresh declaration, with the declaring
        package's current root, or its locked root from *locked_roots*
        (#4872). ``None`` without locked configs.
        """
        if locked_configs is None:
            return None
        names = {dep.name for dep in runtime_deps if hasattr(dep, "name")}
        roots = [
            SimpleNamespace(name=name, plugin_root=root)
            for name, root in (locked_roots or {}).items()
            if name in names
        ]
        return LSPIntegrator._locked_entries(
            locked_configs,
            names,
            [*roots, *runtime_deps],
            spec,
            project_root=project_root,
            user_scope=user_scope,
        )

    @staticmethod
    def _locked_entries(
        locked_configs: dict[str, dict],
        names: builtins.set,
        runtime_deps: list,
        spec: _LSPTargetSpec,
        *,
        project_root: Path,
        user_scope: bool,
    ) -> dict[str, dict]:
        """Render the locked configs of *names* for *spec*, as a fresh install would."""
        locked = [
            {**config, "name": name}
            for name, config in locked_configs.items()
            if name in names and isinstance(config, dict)
        ]
        servers = LSPIntegrator._servers_for_target(
            LSPIntegrator._base_server_entries(locked), spec, warn=lambda _message: None
        )
        return LSPIntegrator._render_plugin_roots(
            servers, runtime_deps, spec, project_root=project_root, user_scope=user_scope
        )

    @staticmethod
    def _blocking_keeps(prepared_targets: list) -> builtins.set:
        """Servers kept as a blocked or unproven edit: they fail the install (#4626)."""
        return {
            name
            for _runtime, _spec, prepared in prepared_targets
            for name, kind in prepared.kept.items()
            if kind != "drift"
        }

    @staticmethod
    def _hold_kept_servers(prepared_targets: list, inputs: dict) -> list:
        """Re-prepare targets so a server kept on one target is held on all (#4626).

        The lock records one config per server: writing a new render to one
        target while another keeps an edit would leave that target past the
        recorded baseline. A previously managed target the new config cannot
        reach (a stdio server upgraded to socket on OpenCode) joins the hold at
        its locked render, so a blocked upgrade removes no entry (#4871).
        """
        kept = builtins.set().union(*(prepared.kept for _r, _s, prepared in prepared_targets))
        if not kept:
            return prepared_targets
        by_runtime = {runtime: prepared for runtime, _spec, prepared in prepared_targets}
        held = []
        for runtime, (servers, options) in inputs.items():
            spec = _LSP_TARGET_SPECS[runtime]
            prepared = by_runtime.get(runtime)
            own_kept = prepared.kept if prepared is not None else {}
            joined = LSPIntegrator._held_joiners(kept, servers, options)
            if kept & (builtins.set(servers) - builtins.set(own_kept)) or joined:
                # A target's own keeps are re-judged, never withheld: that drops their claim.
                # A joining target that cannot be prepared fails like any other (#4871).
                rebuilt = LSPIntegrator._prepare_target_config(
                    spec,
                    {**servers, **joined},
                    **options,
                    held=frozenset(kept - builtins.set(own_kept)),
                )
                # The held render is the locked one: keep the original kinds.
                prepared = replace(
                    rebuilt,
                    kept={**rebuilt.kept, **own_kept},
                    joined=tuple(sorted(joined)),
                    content=rebuilt.content if rebuilt.changed or not joined else None,
                )
            if prepared is not None:
                held.append((runtime, spec, prepared))
        return held

    @staticmethod
    def _held_joiners(kept: builtins.set, servers: dict, options: dict) -> dict[str, dict]:
        """Held servers this target managed but the new config cannot express (#4871)."""
        locked = {
            **(options.get("locked_servers") or {}),
            **(options.get("locked_written") or {}),
        }
        managed = options.get("managed_server_names") or builtins.set()
        return {
            name: locked[name]
            for name in sorted(kept & managed)
            if name not in servers and name in locked
        }

    @staticmethod
    def _report_kept_entries(
        prepared_targets: list,
        *,
        user_scope: bool,
        diagnostics,
        logger,
        kept_edits: dict[str, str] | None,
    ) -> None:
        """Name each kept entry: a drift edit warns, a blocked or unproven one fails (#4626)."""
        from apm_cli.integration.base_integrator import kept_entry_message

        for _runtime, spec, prepared in prepared_targets:
            for name, kind in sorted(prepared.kept.items()):
                if kept_edits is not None and (kind != "drift" or name not in kept_edits):
                    # A held target's drift never hides another target's block (#4871).
                    kept_edits[name] = kind
                message = kept_entry_message(
                    f"LSP server '{name}' in {spec.label(user_scope=user_scope)}",
                    user_scope=user_scope,
                    proven_edit=kind != "unproven",
                )
                if diagnostics is None:
                    (logger.warning if kind == "drift" else logger.error)(message)
                elif kind == "drift":
                    diagnostics.warn(message)
                else:
                    diagnostics.error(message)

    @staticmethod
    def _kept_entry_kind(
        current: object, locked: dict | None, rendered: dict, *, written: dict | None = None
    ) -> str | None:
        """Judge one managed entry that differs from the new render (#4626).

        ``None`` replaces it: it still holds the entry APM rendered from the
        locked config, at the current or the locked package root (*written*,
        #4872). Otherwise it is kept: ``"unproven"`` without a locked render,
        else the proven edit's ``"drift"`` or ``"blocked"``.
        """
        from apm_cli.integration.base_integrator import edit_kind

        if locked is None:
            return "unproven"
        if current == locked or (written is not None and current == written):
            return None
        return edit_kind(locked, rendered)

    @staticmethod
    def _write_prepared_target_config(prepared: _PreparedTargetConfig) -> builtins.set:
        """Write one config after every target in the install has validated."""
        if prepared.content is None:
            return prepared.changed
        write_seam.mkdir(prepared.path.parent, parents=True, exist_ok=True)
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
    def _plan_target_cleanup(
        spec: _LSPTargetSpec,
        stale_names: builtins.set,
        *,
        project_root: Path,
        user_scope: bool,
        fail_on_write_error: bool,
        locked_configs: dict[str, dict] | None,
        plugin_roots: Mapping[str, str] | None = None,
    ) -> _CleanupPlan | None:
        """Read one target config and decide which stale names to remove, without writing.

        An entry that no longer matches what APM recorded for it is the
        user's edit: it is kept and reported, never deleted (#5263).
        """
        config_path = LSPIntegrator._target_config_path(
            spec,
            project_root,
            user_scope=user_scope,
        )
        if not config_path.exists():
            return None
        config = LSPIntegrator._read_json_object(
            config_path,
            fail_on_error=fail_on_write_error,
        )
        for key, value in spec.config_defaults(user_scope=user_scope):
            if config.get(key) != value:
                if fail_on_write_error:
                    raise ValueError(f"{spec.label(user_scope=user_scope)} is not owned by APM")
                return None
        servers_key = spec.servers_key(user_scope=user_scope)

        servers = config if servers_key is None else config.get(servers_key, {})
        if not isinstance(servers, dict):
            return None

        present = sorted(name for name in stale_names if name in servers)
        # Each locked config renders at its own package's root, never any root (#5263).
        roots = [
            SimpleNamespace(name=name, plugin_root=root)
            for name, root in (plugin_roots or {}).items()
            if name in present
        ]
        expected = (
            LSPIntegrator._locked_entries(
                locked_configs,
                set(present),
                roots,
                spec,
                project_root=project_root,
                user_scope=user_scope,
            )
            if locked_configs is not None
            else {}
        )
        edited = [
            name
            for name in present
            if name in expected and not _written_by_apm(servers[name], expected[name])
        ]
        return _CleanupPlan(
            config_path=config_path,
            config=config,
            servers_key=servers_key,
            servers=servers,
            removed=[name for name in present if name not in edited],
            edited=edited,
        )

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
        plan = LSPIntegrator._plan_target_cleanup(
            spec,
            stale_names,
            project_root=project_root,
            user_scope=user_scope,
            fail_on_write_error=fail_on_write_error,
            locked_configs=None,
        )
        if plan is None:
            return []
        LSPIntegrator._apply_target_cleanup(
            spec, plan, project_root=project_root, user_scope=user_scope
        )
        return plan.removed

    @staticmethod
    def _apply_target_cleanup(
        spec: _LSPTargetSpec, plan: _CleanupPlan, *, project_root: Path, user_scope: bool
    ) -> None:
        """Remove the planned stale names from one target config."""
        config, servers, servers_key = plan.config, plan.servers, plan.servers_key
        for name in plan.removed:
            del servers[name]
        if not plan.removed:
            return
        if servers_key is not None:
            config[servers_key] = servers
        owned_keys = {key for key, _value in spec.config_defaults(user_scope=user_scope)}
        # A created default counts as APM-owned only while it still holds
        # the value APM writes; a user-set value (for example a custom
        # ``$schema``) is user content that keeps the file alive.
        owned_keys.update(key for key, value in spec.created_defaults if config.get(key) == value)
        if servers_key is not None:
            owned_keys.add(servers_key)
        if not servers and owned_keys and set(config) <= owned_keys:
            write_seam.unlink(plan.config_path)
            for directory in spec.cleanup_empty_dirs(project_root, user_scope=user_scope):
                try:
                    write_seam.rmdir(directory)
                except OSError:
                    break
        else:
            LSPIntegrator._atomic_write_config(
                plan.config_path, json.dumps(config, indent=2) + "\n", owner_only=user_scope
            )

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
        locked_configs: dict[str, dict] | None = None,
    ) -> None:
        """Remove LSP server entries no longer required by any dependency."""
        runtimes = target_runtimes if target_runtimes is not None else list(_LEGACY_DEFAULT_TARGETS)
        LSPIntegrator.remove_stale_targets(
            {runtime: stale_names for runtime in runtimes},
            project_root=project_root,
            user_scope=user_scope,
            logger=logger,
            fail_on_write_error=fail_on_write_error,
            locked_configs=locked_configs,
        )

    @staticmethod
    def remove_stale_targets(
        stale_by_runtime: Mapping[str, builtins.set],
        *,
        project_root=None,
        user_scope: bool = False,
        logger=None,
        fail_on_write_error: bool = False,
        locked_configs: dict[str, dict] | None = None,
        plugin_roots: Mapping[str, str] | None = None,
    ) -> None:
        """Remove stale LSP entries per target after reading every config first.

        Every config the cleanup touches is read and planned before the first
        change, so one unreadable config changes none of them (#5263).
        *locked_configs* are the server configs APM recorded; an entry that no
        longer matches its recorded render, at the declaring package's root
        from *plugin_roots*, is kept and reported.
        """
        if logger is None:
            logger = NullCommandLogger()
        project_root_path = Path(project_root) if project_root is not None else Path.cwd()
        plans: list[tuple[str, _LSPTargetSpec, _CleanupPlan]] = []
        for runtime, stale_names in stale_by_runtime.items():
            spec = _LSP_TARGET_SPECS.get(runtime)
            if spec is None or not stale_names:
                continue
            try:
                plan = LSPIntegrator._plan_target_cleanup(
                    spec,
                    stale_names,
                    project_root=project_root_path,
                    user_scope=user_scope,
                    fail_on_write_error=fail_on_write_error,
                    locked_configs=locked_configs,
                    plugin_roots=plugin_roots,
                )
            except Exception as exc:
                _cleanup_failed(runtime, spec, user_scope, exc, fail_on_write_error)
                continue
            if plan is not None:
                plans.append((runtime, spec, plan))
        from apm_cli.core.remediation import kept_note
        from apm_cli.utils.diagnostics import printable_ascii_text

        for runtime, spec, plan in plans:
            label = spec.label(user_scope=user_scope)
            for name in plan.edited:
                logger.warning(
                    kept_note(
                        f"LSP server {printable_ascii_text(name)} in {printable_ascii_text(label)}",
                        "it was edited after install",
                        "Remove it by hand if it is no longer needed.",
                    )
                )
            try:
                LSPIntegrator._apply_target_cleanup(
                    spec, plan, project_root=project_root_path, user_scope=user_scope
                )
            except Exception as exc:
                _cleanup_failed(runtime, spec, user_scope, exc, fail_on_write_error)
                continue
            if plan.removed:
                _report_removed(logger, runtime, label, plan.removed, user_scope=user_scope)

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
        legacy_server_names: builtins.set | None = None,
        record: LspInstallRecord | None = None,
    ) -> int:
        """Install LSP dependencies by writing target-specific runtime config.

        *record*, when given, carries the lock evidence in and the kept user
        entries and edits out (:class:`LspInstallRecord`).
        """
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
        inputs: dict[str, tuple[dict[str, dict], dict]] = {}
        for runtime in runtimes:
            spec = _LSP_TARGET_SPECS[runtime]
            runtime_deps = LSPIntegrator.deps_for_runtime(lsp_deps, runtime)
            servers = LSPIntegrator._servers_for_target(
                LSPIntegrator._base_server_entries(runtime_deps),
                spec,
                warn=diagnostics.warn if diagnostics is not None else logger.warning,
            )
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
                options = {
                    "project_root": project_root_path,
                    "user_scope": user_scope,
                    "managed_server_names": managed_names,
                    "force": force,
                    "legacy_server_names": legacy_server_names,
                    "locked_servers": LSPIntegrator._locked_target_servers(
                        record.locked_configs if record is not None else None,
                        runtime_deps,
                        spec,
                        project_root=project_root_path,
                        user_scope=user_scope,
                    ),
                    "locked_written": LSPIntegrator._locked_target_servers(
                        record.locked_configs if record is not None else None,
                        runtime_deps,
                        spec,
                        project_root=project_root_path,
                        user_scope=user_scope,
                        locked_roots=record.locked_roots if record is not None else None,
                    ),
                }
                if servers:
                    prepared = LSPIntegrator._prepare_target_config(spec, servers, **options)
                    prepared_targets.append((runtime, spec, prepared))
                # A target with nothing new to write can still join a hold (#4871).
                inputs[runtime] = (servers, options)
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

        prepared_targets = LSPIntegrator._hold_kept_servers(prepared_targets, inputs)
        LSPIntegrator._report_kept_entries(
            prepared_targets,
            user_scope=user_scope,
            diagnostics=diagnostics,
            logger=logger,
            kept_edits=record.kept_edits if record is not None else None,
        )
        changed_servers: builtins.set = builtins.set()
        preserved: dict[str, builtins.set] = {}
        written: list[str] = []
        for runtime, spec, prepared in prepared_targets:
            unwritten = {*prepared.preserved, *prepared.matching, *prepared.withheld}
            if record is not None and unwritten:
                record.kept_unowned.setdefault(runtime, builtins.set()).update(unwritten)
            if record is not None and prepared.joined:
                record.held_claims.setdefault(runtime, builtins.set()).update(prepared.joined)
            for name in prepared.matching:
                logger.verbose_detail(
                    f"LSP server '{name}' already matches the {runtime} entry in "
                    f"{spec.label(user_scope=user_scope)}; APM did not write it, so "
                    "it stays yours"
                )
            if prepared.preserved:
                preserved[runtime] = builtins.set(prepared.preserved)
                for name in prepared.preserved:
                    logger.error(
                        f"LSP server '{name}' conflicts with the existing {runtime} entry "
                        f"in {spec.label(user_scope=user_scope)}. Preserved that entry. "
                        "Rename or remove the conflicting entry yourself, then retry the "
                        "same install command. --force does not take ownership of user "
                        "entries."
                    )
            try:
                previous = prepared.path.read_bytes() if prepared.path.is_file() else None
                changed = LSPIntegrator._write_prepared_target_config(prepared)
                changed_servers.update(changed)
                if prepared.path.read_bytes() != previous:
                    written.append(portable_relpath(prepared.path, project_root_path))
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

        blocking = LSPIntegrator._blocking_keeps(prepared_targets)
        if blocking and written:
            # Name what the failed install still wrote, as file-level keeps do (#4626).
            logger.info(f"This install wrote {len(written)} file(s), recorded in apm.lock.yaml:")
            for label in written:
                getattr(logger, "error_detail", logger.info)(f"  {label}")
        if blocking and diagnostics is None and not preserved:
            # No collector counts the keep: fail the install here (#4626).
            from apm_cli.install.errors import LSPUserEntryConflictError

            raise LSPUserEntryConflictError(
                "LSP installation incomplete: kept " + ", ".join(sorted(blocking)),
                preserved={},
                wrote_servers=bool(changed_servers),
            )
        if preserved:
            from apm_cli.install.errors import LSPUserEntryConflictError

            message = "LSP installation incomplete: " + ", ".join(
                f"{name} ({runtime})"
                for runtime, names in sorted(preserved.items())
                for name in sorted(names)
            )
            logger.error(message)
            raise LSPUserEntryConflictError(
                message, preserved=preserved, wrote_servers=bool(changed_servers)
            )
        return len(changed_servers)
