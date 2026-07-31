"""MCP ``install`` orchestration (strangler-fig extraction from ``MCPIntegrator``).

Keeps ``MCPIntegrator.install`` as a thin delegate so public API and test patch
paths stay stable while this module owns the full install flow.
"""

from __future__ import annotations

import builtins
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from yaml import YAMLError

from apm_cli.core.apm_yml import (
    ConflictingTargetsError,
    EmptyTargetsListError,
    UnknownTargetError,
    parse_targets_field,
)
from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.core.target_catalog import accepted_target_values
from apm_cli.integration._shared import _hermes_runtime_opted_in as _hermes_opted_in_impl
from apm_cli.integration._shared import _RegistryDepGroup, _runtime_opted_in
from apm_cli.integration.mcp_install_registry_ops import (
    _install_registry_group,
    _record_managed_server,
)
from apm_cli.runtime.utils import find_runtime_binary
from apm_cli.utils.console import STATUS_SYMBOLS
from apm_cli.utils.yaml_io import load_yaml

if TYPE_CHECKING:
    from apm_cli.core.scope import InstallScope

# Opt-in runtimes gated solely on the presence of a project marker directory.
# Data-driven so the parallel detection branches collapse into one loop.
_DIR_GATED_RUNTIMES: dict[str, str] = {
    "cursor": ".cursor",
    "opencode": ".opencode",
    "gemini": ".gemini",
    "windsurf": ".windsurf",
    "kiro": ".kiro",
}


def _hermes_runtime_opted_in() -> bool:
    """Wrapper that passes this module's find_runtime_binary so monkeypatch seams work.

    Tests that patch ``mcp_integrator_install.find_runtime_binary`` must call
    this wrapper -- not ``_shared._hermes_runtime_opted_in`` directly -- so
    the patched reference is picked up at call time.
    """
    return _hermes_opted_in_impl(_find_binary=find_runtime_binary)


class _TargetSelectionSource(StrEnum):
    """Source that supplied the MCP target set before compatibility gates."""

    RUNTIME = "runtime"
    TARGET = "target"
    MANIFEST = "manifest"
    DISCOVERY = "discovery"
    FALLBACK = "fallback"
    INVALID_MANIFEST = "invalid-manifest"


def _discover_installed_runtimes_fallback(
    project_root_path: Path, is_vscode_available, *, user_scope: bool = False
) -> list[str]:
    """Binary/marker-probe runtime detection when the ClientFactory stack is absent."""
    installed_runtimes = [rt for rt in ["copilot", "codex"] if find_runtime_binary(rt) is not None]
    if is_vscode_available(project_root=project_root_path):
        installed_runtimes.append("vscode")
    for name, marker in _DIR_GATED_RUNTIMES.items():
        if (name == "kiro" and user_scope) or (project_root_path / marker).is_dir():
            installed_runtimes.append(name)
    if (project_root_path / ".claude").is_dir() or (find_runtime_binary("claude") is not None):
        installed_runtimes.append("claude")
    try:
        from apm_cli.adapters.client.intellij import _intellij_config_dir

        if _intellij_config_dir().is_dir():
            installed_runtimes.append("intellij")
    except (ImportError, ValueError):
        # ValueError (PathTraversalError) when LOCALAPPDATA/XDG_DATA_HOME is
        # misconfigured -- treat as "not installed" rather than crash.
        pass
    if _hermes_runtime_opted_in():
        installed_runtimes.append("hermes")
    return installed_runtimes


def _discover_installed_runtimes(project_root_path: Path, *, user_scope: bool = False) -> list[str]:
    """Discover all MCP-capable runtimes installed for ``project_root_path``."""
    from apm_cli.integration.mcp_integrator import _is_vscode_available

    try:
        from apm_cli.factory import ClientFactory
        from apm_cli.runtime.manager import RuntimeManager

        manager = RuntimeManager()
    except ImportError:
        return _discover_installed_runtimes_fallback(
            project_root_path, _is_vscode_available, user_scope=user_scope
        )

    installed_runtimes: list[str] = []
    for runtime_name in [
        "copilot",
        "codex",
        "vscode",
        "cursor",
        "opencode",
        "gemini",
        "windsurf",
        "kiro",
        "claude",
        "intellij",
        "hermes",
    ]:
        try:
            if _runtime_opted_in(
                runtime_name,
                project_root_path,
                _is_vscode_available,
                manager,
                _DIR_GATED_RUNTIMES,
                user_scope=user_scope,
            ):
                ClientFactory.create_client(runtime_name, project_root=project_root_path)
                installed_runtimes.append(runtime_name)
        except (ValueError, ImportError):
            continue
    return installed_runtimes


def _intersect_script_runtimes(
    installed_runtimes: list[str],
    apm_config: dict | None,
    verbose: bool,
    logger,
    console,
) -> list[str]:
    """Narrow installed runtimes to those referenced in apm.yml scripts.

    With no script references, all installed runtimes are targeted.
    """
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    script_runtimes = MCPIntegrator._detect_runtimes(
        apm_config.get("scripts", {}) if apm_config else {}
    )

    if not script_runtimes:
        target_runtimes = installed_runtimes
        if target_runtimes:
            if verbose:
                logger.verbose_detail(
                    f"No scripts detected, using all installed runtimes: "
                    f"{', '.join(target_runtimes)}"
                )
        else:
            logger.warning("No MCP-compatible runtimes installed")
            logger.progress("Install a runtime with: apm runtime setup copilot")
        return target_runtimes

    target_runtimes = [rt for rt in installed_runtimes if rt in script_runtimes]
    if verbose:
        if console:
            console.print(f"|  [cyan]{STATUS_SYMBOLS['info']}  Runtime Detection[/cyan]")
            console.print(f"|     +- Installed: {', '.join(installed_runtimes)}")
            console.print(f"|     +- Used in scripts: {', '.join(script_runtimes)}")
            if target_runtimes:
                console.print(
                    f"|     +- Target: {', '.join(target_runtimes)} (available + used in scripts)"
                )
            console.print("|")
        else:
            logger.verbose_detail(f"Installed runtimes: {', '.join(installed_runtimes)}")
            logger.verbose_detail(f"Script runtimes: {', '.join(script_runtimes)}")
            if target_runtimes:
                logger.verbose_detail(f"Target runtimes: {', '.join(target_runtimes)}")
    if not target_runtimes:
        logger.warning("Scripts reference runtimes that are not installed")
        logger.progress("Install missing runtimes with: apm runtime setup <runtime>")
    return target_runtimes


def _apply_user_scope_filter(target_runtimes: list[str], scope, logger) -> list[str] | None:
    """At USER scope, keep only runtimes that support global installation."""
    from apm_cli.core.scope import InstallScope as _IS

    if scope is not _IS.USER:
        return target_runtimes

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
    skipped = set(pre_filter) - set(filtered_runtimes)
    if skipped:
        msg = (
            f"Skipped workspace-only runtimes at user scope: "
            f"{', '.join(sorted(skipped))}"
            f" -- omit --global to install these"
        )
        logger.warning(msg)
    if not filtered_runtimes:
        logger.warning(
            "No runtimes support user-scope MCP installation (supported: Copilot CLI, Claude Code, Codex CLI, Gemini CLI, Kiro, Windsurf, JetBrains Copilot)"
        )
        return None
    return filtered_runtimes


def _declared_manifest_target_runtimes(
    apm_config: dict | None,
) -> tuple[list[str] | None, bool]:
    """Return canonical manifest targets and whether the declaration is valid.

    Delegates to :func:`apm_cli.core.apm_yml.parse_targets_field`, the same
    parser the v2 file-deployment target resolver and
    :meth:`MCPIntegrator._gate_project_scoped_runtimes` both use, so a
    manifest-declared target list is interpreted identically everywhere
    (including folding the legacy ``all`` value back to auto-detect).

    The first tuple item is ``None`` when the manifest is unrestricted (no
    ``targets:``/``target:`` key, or legacy ``all``), which permits
    local-machine runtime discovery. The second item is ``False`` for malformed
    declarations. That state blocks discovery while leaving
    ``_gate_project_scoped_runtimes`` to render the established fail-closed
    error.
    """
    if not apm_config:
        return None, True

    try:
        parsed = parse_targets_field(apm_config)
    except (ConflictingTargetsError, EmptyTargetsListError, UnknownTargetError):
        return None, False
    return parsed or None, True


def _resolve_target_runtimes(
    runtime: str | None,
    exclude: str | None,
    verbose: bool,
    apm_config: dict | None,
    project_root,
    user_scope: bool,
    explicit_target: str | list[str] | None,
    scope: InstallScope | None,
    logger,
    console,
) -> list[str] | None:
    """Detect, filter, and gate the target runtimes for MCP installation.

    Returns a list of runtime names, or ``None`` when nothing to configure.
    """
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    selection_source: _TargetSelectionSource
    if runtime:
        # Single runtime mode - skip auto-discovery entirely.
        target_runtimes: list[str] = [runtime]
        selection_source = _TargetSelectionSource.RUNTIME
    elif explicit_target is not None:
        target_runtimes = (
            [explicit_target] if isinstance(explicit_target, str) else list(explicit_target)
        )
        selection_source = _TargetSelectionSource.TARGET
    else:
        # Manifest loading/parsing (and the user-facing warnings
        # parse_targets_field can emit, e.g. legacy `targets: [all]`) is
        # deferred to this branch -- irrelevant, and wasted filesystem I/O,
        # whenever the caller already pinned a runtime/target explicitly.
        project_root_path = Path(project_root) if project_root is not None else Path.cwd()

        if apm_config is None:
            apm_yml = project_root_path / "apm.yml"
            try:
                if apm_yml.exists():
                    apm_config = load_yaml(apm_yml)
            except (OSError, UnicodeError, YAMLError) as exc:
                raise RuntimeError(
                    f"Unable to load MCP targets from {apm_yml}; fix the manifest before retrying"
                ) from exc

        declared_targets, manifest_valid = _declared_manifest_target_runtimes(apm_config)
        if not manifest_valid:
            # Do not inspect machine-local signals for an invalid declaration.
            # The shared gate below re-parses and renders the canonical error.
            target_runtimes = []
            selection_source = _TargetSelectionSource.INVALID_MANIFEST
            logger.verbose_detail(
                "Manifest target declaration is invalid; machine discovery skipped"
            )
        elif declared_targets is not None:
            # apm.yml declares `targets:` explicitly -- that is the deterministic,
            # committed source of truth for MCP ownership too. Using it instead of
            # local-machine runtime auto-discovery keeps `mcp_target_servers` (and
            # the deployment ledger `runtime` field) byte-identical across
            # developers with different harnesses installed, instead of each
            # `apm install` "stealing" MCP ownership toward whatever the current
            # machine happens to have (issue #2298).
            target_runtimes = declared_targets
            selection_source = _TargetSelectionSource.MANIFEST
            logger.verbose_detail(
                "Resolved MCP targets from apm.yml declaration: "
                f"{', '.join(target_runtimes)} (machine discovery skipped)"
            )
        else:
            installed_runtimes = _discover_installed_runtimes(
                project_root_path, user_scope=user_scope
            )
            target_runtimes = _intersect_script_runtimes(
                installed_runtimes, apm_config, verbose, logger, console
            )

            # Fall back to VS Code only if no runtimes are installed at all
            if not target_runtimes and not installed_runtimes:
                target_runtimes = ["vscode"]
                selection_source = _TargetSelectionSource.FALLBACK
            else:
                selection_source = _TargetSelectionSource.DISCOVERY

    # Exclusion narrows every selected source, including explicit CLI choices.
    # Apply it before progress output so the message names the narrowed set.
    if exclude:
        target_runtimes = [candidate for candidate in target_runtimes if candidate != exclude]
        # Invalid manifests continue to the shared gate for canonical rendering.
        if not target_runtimes and selection_source is not _TargetSelectionSource.INVALID_MANIFEST:
            logger.warning(
                f"All selected MCP runtimes excluded (--exclude {exclude}), "
                "skipping MCP configuration"
            )
            return None

    if selection_source is _TargetSelectionSource.RUNTIME:
        logger.progress(f"Targeting specific runtime: {', '.join(target_runtimes)}")
    elif selection_source is _TargetSelectionSource.TARGET:
        runtime_label = "runtime" if len(target_runtimes) == 1 else "runtimes"
        logger.progress(f"Targeting specific {runtime_label}: {', '.join(target_runtimes)}")
    elif selection_source is _TargetSelectionSource.MANIFEST:
        target_label = "target" if len(target_runtimes) == 1 else "targets"
        logger.progress(
            f"Targeting declared {target_label} from apm.yml: {', '.join(target_runtimes)}"
        )
    elif selection_source is _TargetSelectionSource.FALLBACK:
        logger.progress("No runtimes installed, using VS Code as fallback")
    elif (
        selection_source is _TargetSelectionSource.DISCOVERY
        and target_runtimes
        and not verbose
        and console is None
    ):
        # Machine discovery is intentionally the unrestricted-manifest fallback.
        logger.progress(
            f"Detected runtimes: {', '.join(target_runtimes)} "
            "(auto-detected; add targets: to apm.yml for consistent results across machines)"
        )

    # Codex MCP is project-scoped: only configure it when Codex is an
    # active project target (silent skip, same as Cursor/OpenCode/Gemini).
    # Claude Code is gated identically: a host-wide `claude` binary should
    # not opt every APM project into `.mcp.json` writes.
    # Preserve direct-call compatibility for unknown legacy runtime strings:
    # the adapter layer owns their warning/error behavior. Known runtime names
    # are equivalent to explicit --target and therefore outrank manifest/signals.
    apply_project_gate = runtime is None or runtime in accepted_target_values("install")
    if apply_project_gate:
        target_runtimes = MCPIntegrator._gate_project_scoped_runtimes(
            target_runtimes,
            user_scope=user_scope,
            project_root=project_root,
            apm_config=apm_config,
            explicit_target=runtime or explicit_target,
        )

    # Explicit runtime/exclusion/gating can leave nothing to configure.
    if not target_runtimes:
        return None

    # Scope filtering: at USER scope, keep only global-capable runtimes.
    # Applied after both explicit --runtime and auto-discovery paths.
    return _apply_user_scope_filter(target_runtimes, scope, logger)


def _install_self_defined_deps(
    self_defined_deps: list,
    target_runtimes: list[str],
    stored_mcp_configs: dict,
    servers_to_update: builtins.set,
    successful_updates: builtins.set,
    project_root,
    user_scope: bool,
    verbose: bool,
    console,
    logger,
    managed_target_servers: dict[str, builtins.set[str]] | None,
) -> int:
    """Install self-defined (``registry: false``) MCP deps for all target runtimes."""
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    configured_count = 0
    self_defined_names = [dep.name for dep in self_defined_deps]
    self_defined_to_install = MCPIntegrator._check_self_defined_servers_needing_installation(
        self_defined_names,
        target_runtimes,
        project_root=project_root,
        user_scope=user_scope,
    )
    already_configured_candidates_sd = [
        name for name in self_defined_names if name not in self_defined_to_install
    ]

    # Detect config drift for "already configured" self-defined servers
    if stored_mcp_configs and already_configured_candidates_sd:
        drifted_sd_deps = [
            dep for dep in self_defined_deps if dep.name in already_configured_candidates_sd
        ]
        drifted_sd = MCPIntegrator._detect_mcp_config_drift(
            drifted_sd_deps,
            stored_mcp_configs,
        )
        if drifted_sd:
            servers_to_update.update(drifted_sd)
            MCPIntegrator._append_drifted_to_install_list(self_defined_to_install, drifted_sd)
    already_configured_self_defined = [
        name for name in already_configured_candidates_sd if name not in servers_to_update
    ]

    if already_configured_self_defined:
        if console:
            for name in already_configured_self_defined:
                console.print(
                    f"|  [green]{STATUS_SYMBOLS['check']}[/green] {name} "
                    f"[dim](already configured)[/dim]"
                )
        else:
            count = len(already_configured_self_defined)
            logger.success(f"{count} self-defined server(s) already configured")
            for name in already_configured_self_defined:
                logger.verbose_detail(f"{name} already configured, skipping")

    for dep in self_defined_deps:
        if dep.name not in self_defined_to_install:
            continue

        is_update = dep.name in servers_to_update
        synthetic_info = MCPIntegrator._build_self_defined_info(dep)
        self_defined_cache = {dep.name: synthetic_info}
        transport_label = dep.transport or "stdio"
        self_defined_env = {} if transport_label == "stdio" else dep.env or {}
        action_text = "Updating" if is_update else "Configuring"
        if console:
            console.print(
                f"|  [cyan]{STATUS_SYMBOLS['running']}[/cyan]  {dep.name} "
                f"[dim](self-defined, {transport_label})[/dim]"
            )
            console.print(
                f"|     +- {action_text} for {', '.join([rt.title() for rt in target_runtimes])}..."
            )
        else:
            logger.progress(
                f"{dep.name}: {action_text.lower()} for {', '.join(target_runtimes)}..."
            )

        any_ok = False
        for rt in target_runtimes:
            if verbose:
                logger.verbose_detail(f"Configuring {dep.name} for {rt}...")
            if MCPIntegrator._install_for_runtime(
                rt,
                [dep.name],
                self_defined_env,
                self_defined_cache,
                project_root=project_root,
                user_scope=user_scope,
                logger=logger,
            ):
                any_ok = True
                _record_managed_server(managed_target_servers, rt, dep.name)

        if any_ok:
            if console:
                label = "updated" if is_update else "configured"
                console.print(
                    f"|  [green]{STATUS_SYMBOLS['check']}[/green]  {dep.name} -> "
                    f"{', '.join([rt.title() for rt in target_runtimes])}"
                    f" [dim]({label})[/dim]"
                )
            configured_count += 1
            if is_update:
                successful_updates.add(dep.name)
        elif console:
            console.print(
                f"|  [red]{STATUS_SYMBOLS['cross']}[/red]  {dep.name}  -- failed for all runtimes"
            )
        else:
            logger.error(f"{dep.name} -- failed for all runtimes")

    return configured_count


def _print_mcp_summary(
    console,
    configured_count: int,
    successful_updates: builtins.set,
) -> None:
    """Print the MCP install summary footer panel."""
    if not console:
        return
    if configured_count > 0:
        # Use successful_updates (not servers_to_update) for accurate counts.
        # servers_to_update = all drift-detected servers (some may have failed).
        # successful_updates = servers that were re-applied AND succeeded.
        update_count = builtins.len(successful_updates)
        new_count = configured_count - update_count
        parts = []
        if new_count > 0:
            parts.append(f"configured {new_count} server{'s' if new_count != 1 else ''}")
        if update_count > 0:
            parts.append(f"updated {update_count} server{'s' if update_count != 1 else ''}")
        console.print(f"[green]{STATUS_SYMBOLS['success']} {', '.join(parts).capitalize()}[/green]")
    else:
        console.print(f"[green]{STATUS_SYMBOLS['success']} All servers up to date[/green]")


def run_mcp_install(  # noqa: PLR0913
    mcp_deps: list,
    runtime: str | None = None,
    exclude: str | None = None,
    verbose: bool = False,
    apm_config: dict | None = None,
    stored_mcp_configs: dict | None = None,
    project_root=None,
    user_scope: bool = False,
    explicit_target: str | list[str] | None = None,
    logger=None,
    diagnostics=None,
    scope: InstallScope | None = None,
    managed_target_servers: dict[str, builtins.set[str]] | None = None,
) -> int:
    """Install MCP dependencies.

    Args:
        mcp_deps: MCP dependency entries (registry strings or MCPDependency objects).
        runtime: Target specific runtime only.
        exclude: Exclude specific runtime.
        verbose: Show detailed information.
        apm_config: Parsed apm.yml config dict (loaded from disk when None).
        stored_mcp_configs: Lockfile MCP configs for diff-aware install.
        project_root: Project root for repo-local runtime configs.
        user_scope: Whether resolving at user scope.
        explicit_target: Explicit target selected by CLI or manifest.
        scope: InstallScope (PROJECT or USER).
        managed_target_servers: Mutable per-target APM ownership state.

    Returns:
        Number of MCP servers newly configured or updated.
    """
    # Local import: ``mcp_integrator`` must finish loading before this module
    # is first imported (``MCPIntegrator.install`` delegates here lazily).
    from apm_cli.integration.mcp_integrator import _get_console

    if logger is None:
        logger = NullCommandLogger()
    if not mcp_deps:
        logger.warning("No MCP dependencies found in apm.yml")
        return 0

    from apm_cli.core.scope import InstallScope

    # The explicit scope enum takes precedence over the raw user_scope bool
    # so callers cannot accidentally mix user-scope runtime filtering with
    # project-scope config writes (or the inverse).
    if scope is InstallScope.USER:
        user_scope = True
    elif scope is InstallScope.PROJECT:
        user_scope = False

    # Split into registry-resolved and self-defined deps
    # Backward compat: plain strings are treated as registry deps
    registry_deps = [
        dep
        for dep in mcp_deps
        if isinstance(dep, str)
        or (hasattr(dep, "is_registry_resolved") and dep.is_registry_resolved)
    ]
    self_defined_deps = [
        dep for dep in mcp_deps if hasattr(dep, "is_self_defined") and dep.is_self_defined
    ]
    registry_dep_names = [dep.name if hasattr(dep, "name") else dep for dep in registry_deps]

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
            logger.progress(f"Installing MCP dependencies ({len(mcp_deps)})...")
    else:
        logger.progress(f"Installing MCP dependencies ({len(mcp_deps)})...")

    # Runtime detection, gating, and scope filtering
    target_runtimes = _resolve_target_runtimes(
        runtime=runtime,
        exclude=exclude,
        verbose=verbose,
        apm_config=apm_config,
        project_root=project_root,
        user_scope=user_scope,
        explicit_target=explicit_target,
        scope=scope,
        logger=logger,
        console=console,
    )
    if target_runtimes is None:
        return 0

    if managed_target_servers is not None:
        active_targets = set(target_runtimes)
        current_names = {
            dep.name if hasattr(dep, "name") else dep
            for dep in mcp_deps
            if isinstance(dep, str) or hasattr(dep, "name")
        }
        for target in list(managed_target_servers):
            if target not in active_targets:
                del managed_target_servers[target]
            else:
                managed_target_servers[target].intersection_update(current_names)

    # Use the new registry operations module for better server detection
    configured_count = 0

    # --- Registry-based deps ---
    if registry_dep_names:
        try:
            from apm_cli.registry.operations import MCPServerOperations

            # Group deps by their per-dep registry URL so each group is
            # resolved against the correct registry endpoint.
            # Plain strings (backward-compat) and deps with registry=None go to
            # the default group (key=None).  Only str values trigger routing.
            registry_groups: builtins.dict[str | None, list] = {}
            for dep in registry_deps:
                dep_registry = getattr(dep, "registry", None)
                key = dep_registry if isinstance(dep_registry, str) else None
                if key not in registry_groups:
                    registry_groups[key] = []
                registry_groups[key].append(dep)

            for group_registry_url, group_deps_list in registry_groups.items():
                group_dep_names = [
                    dep.name if hasattr(dep, "name") else dep for dep in group_deps_list
                ]
                group_dep_map = {dep.name: dep for dep in group_deps_list if hasattr(dep, "name")}
                operations = MCPServerOperations(registry_url=group_registry_url)
                configured_count += _install_registry_group(
                    operations=operations,
                    group=_RegistryDepGroup(
                        deps=group_deps_list,
                        names=group_dep_names,
                        dep_map=group_dep_map,
                    ),
                    target_runtimes=target_runtimes,
                    stored_mcp_configs=stored_mcp_configs,
                    servers_to_update=servers_to_update,
                    successful_updates=successful_updates,
                    project_root=project_root,
                    user_scope=user_scope,
                    verbose=verbose,
                    console=console,
                    logger=logger,
                    managed_target_servers=managed_target_servers,
                )

        except ImportError:
            logger.warning("Registry operations not available")
            logger.error("Cannot validate MCP servers without registry operations")
            raise RuntimeError("Registry operations module required for MCP installation")  # noqa: B904

    # --- Self-defined deps (registry: false) ---
    if self_defined_deps:
        configured_count += _install_self_defined_deps(
            self_defined_deps=self_defined_deps,
            target_runtimes=target_runtimes,
            stored_mcp_configs=stored_mcp_configs,
            servers_to_update=servers_to_update,
            successful_updates=successful_updates,
            project_root=project_root,
            user_scope=user_scope,
            verbose=verbose,
            console=console,
            logger=logger,
            managed_target_servers=managed_target_servers,
        )

    # Close the panel
    _print_mcp_summary(console, configured_count, successful_updates)

    return configured_count
