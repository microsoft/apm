"""LSP server integration for the APM install pipeline.

Mirrors the MCP integration pattern with runtime-neutral target selection.
"""

import builtins
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from apm_cli.core.scope import manifest_file

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.target_detection import EffectiveTargetDecision
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.context import InstallContext
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.models.dependency.lsp import LSPDependency
    from apm_cli.utils.diagnostics import DiagnosticCollector

_log = logging.getLogger(__name__)

_PROJECT_LSP_OWNER = "project:."
_PACKAGE_LSP_OWNER_PREFIX = "package:"
_BUNDLE_LSP_OWNER_PREFIX = "bundle:"
_RESERVED_LSP_OWNER_PREFIXES = (
    _PROJECT_LSP_OWNER.split(".", 1)[0],
    _PACKAGE_LSP_OWNER_PREFIX,
    _BUNDLE_LSP_OWNER_PREFIX,
)


def require_lsp_capable_target(target_runtimes: list[str] | None, *, bundle: bool = False) -> None:
    """Refuse declared LSP servers that no selected target can configure (#3727).

    The single authority for the LSP capability gap. Install and update call
    it from a preflight (:func:`preflight_lsp_targets`, or
    :meth:`PreparedLspIntegration.require_capable_target`) before any target
    write, so a failed install deploys no skills and changes no lockfile or
    ``.gitignore``. It gates; it never rolls back.
    """
    if target_runtimes:
        return
    from apm_cli.install.errors import RequiredIntegrationError
    from apm_cli.integration.lsp_integrator import LSPIntegrator

    hint = LSPIntegrator.target_choice_hint()
    if bundle:
        raise RequiredIntegrationError(
            "Bundle lsp.json cannot be configured because the resolved target set has "
            f"no LSP-compatible runtime. Select {hint}."
        )
    raise RequiredIntegrationError(
        "LSP dependencies are declared, but no effective target supports "
        f"LSP configuration. Choose {hint}, then retry."
    )


@dataclass(frozen=True)
class LspPreflight:
    """LSP target filters of a command that integrates LSP after the pipeline.

    The pipeline gate resolves LSP targets with the same ``--runtime`` and
    ``--exclude`` the later service phase uses, so both reach one answer.
    """

    runtime: str | None = None
    exclude: str | None = None

    @classmethod
    def for_install(cls, ctx) -> "LspPreflight | None":
        """Return the preflight for ``apm install``, or None when LSP is not installed."""
        from apm_cli.constants import InstallMode

        if ctx.install_mode is not InstallMode.ALL:
            return None
        return cls(runtime=ctx.runtime, exclude=ctx.exclude)


def lsp_target_config(apm_package: "APMPackage") -> dict:
    """Return the manifest target metadata that LSP target resolution reads."""
    from apm_cli.models.apm_package import canonical_package_target_config

    return {
        "scripts": apm_package.scripts or {},
        **canonical_package_target_config(apm_package),
    }


def _resolve_lsp_target_runtimes(
    *,
    project_root: Path,
    user_scope: bool,
    runtime: str | None,
    exclude: str | None,
    apm_config: dict | None,
    explicit_target: str | list[str] | None,
    target_decision: "EffectiveTargetDecision | None",
    scope: object,
    logger=None,
) -> list[str]:
    """Resolve LSP targets; the preflight and the service phase both call this."""
    from apm_cli.integration.lsp_integrator import LSPIntegrator

    return LSPIntegrator.resolve_target_runtimes(
        project_root=project_root,
        user_scope=user_scope,
        runtime=runtime,
        exclude=exclude,
        apm_config=apm_config,
        explicit_target=explicit_target,
        target_decision=target_decision,
        scope=scope,
        logger=logger,
    )


def _graph_declares_trusted_lsp(ctx: "InstallContext") -> bool:
    """Return whether the root or a resolved package declares a trusted LSP server.

    Reads each downloaded package manifest; a package whose LSP executables
    are not approved is skipped, as transitive LSP collection skips it later.
    """
    from types import SimpleNamespace

    from apm_cli.install.exec_gate import check_executable_approval
    from apm_cli.install.template import effective_allow_map
    from apm_cli.models.apm_package import APMPackage

    if ctx.apm_package.get_lsp_dependencies():
        return True
    graph = ctx.dependency_graph
    dependencies = (
        graph.flattened_dependencies.get_installation_list()
        if graph is not None
        else ctx.deps_to_install
    )
    from apm_cli.security.executables import closed_exec_map

    # Resolved once, on the first package that declares LSP servers.
    allow_map = closed_exec_map(None)
    allow_resolved = False
    for dep_ref in dependencies:
        install_path = dep_ref.get_install_path(ctx.apm_modules_dir)
        manifest = manifest_file(install_path)
        try:
            declared = (
                manifest.is_file() and APMPackage.from_apm_yml(manifest).get_lsp_dependencies()
            )
        except Exception:
            _log.debug("Skipping unreadable package manifest %s", manifest, exc_info=True)
            continue
        if not declared:
            continue
        if not allow_resolved:
            allow_map, allow_resolved = effective_allow_map(ctx), True
        # Pinned-hook admission reads the package's files, so the approval owner
        # needs its install path as well as its identity (#4658).
        package_info = SimpleNamespace(dependency_ref=dep_ref, install_path=install_path)
        if check_executable_approval(dep_ref.get_unique_key(), package_info, allow_map)[4]:
            return True
    return False


def preflight_lsp_targets(ctx: "InstallContext") -> None:
    """Fail an install before its first target write when LSP has no capable target.

    The pipeline runs this after packages are downloaded (their manifests are
    readable) and before the integrate phase deploys anything (#3727).
    """
    preflight = getattr(ctx, "lsp_preflight", None)
    if not isinstance(preflight, LspPreflight) or ctx.lockfile_only:
        return
    if not _graph_declares_trusted_lsp(ctx):
        return
    from apm_cli.core.scope import InstallScope

    decision = ctx.target_decision
    require_lsp_capable_target(
        _resolve_lsp_target_runtimes(
            project_root=ctx.project_root,
            user_scope=ctx.scope is InstallScope.USER,
            runtime=preflight.runtime,
            exclude=preflight.exclude,
            apm_config=lsp_target_config(ctx.apm_package),
            explicit_target=decision.value if decision is not None else ctx.target_override,
            target_decision=decision,
            scope=ctx.scope,
        )
    )


def _target_server_sets(lockfile: "LockFile") -> dict[str, set[str]]:
    """Return a mutable target ownership view from one lockfile."""
    target_servers = getattr(lockfile, "lsp_target_servers", {})
    if not isinstance(target_servers, dict):
        return {}
    return {runtime: set(server_names) for runtime, server_names in target_servers.items()}


def recorded_lsp_targets(lockfile: "LockFile") -> tuple[str, ...]:
    """Return the LSP targets *lockfile* records APM writing servers to."""
    return tuple(sorted(name for name, servers in _target_server_sets(lockfile).items() if servers))


def package_lsp_owner(resolved_by: str) -> str:
    """Return the ``lsp_config_provenance`` token for a package's LSP servers."""
    return f"{_PACKAGE_LSP_OWNER_PREFIX}{resolved_by}"


def _dependency_provenance(dependencies: list["LSPDependency"]) -> dict[str, str]:
    """Map every regular install declaration to its stable owner token."""
    return {
        dependency.name: (
            package_lsp_owner(dependency.resolved_by)
            if dependency.resolved_by
            else _PROJECT_LSP_OWNER
        )
        for dependency in dependencies
    }


def _is_regular_owner(owner: str) -> bool:
    """Return whether an LSP owner belongs to the replayable install graph."""
    return owner == _PROJECT_LSP_OWNER or owner.startswith(_PACKAGE_LSP_OWNER_PREFIX)


def _bundle_owner_aliases(owner: str) -> frozenset[str]:
    """Return canonical and safe legacy provenance tokens for one bundle."""
    canonical = f"{_BUNDLE_LSP_OWNER_PREFIX}{owner}"
    if owner.startswith(_RESERVED_LSP_OWNER_PREFIXES):
        return frozenset({canonical})
    return frozenset({canonical, owner})


@dataclass(frozen=True)
class WithdrawnBundleLspServer:
    """A deployed bundle LSP server whose bundle's LSP grant was withdrawn (RA-041)."""

    owner: str
    server: str

    def describe(self) -> str:
        return (
            f"{self.owner}: bundle LSP server '{self.server}' is deployed, but the "
            "bundle's approval no longer grants lsp"
        )


def withdrawn_bundle_lsp_servers(
    lockfile: "LockFile | None", scope_dir: Path, trust_context: Callable[[], Any]
) -> list[WithdrawnBundleLspServer]:
    """Bundle-owned LSP servers in *lockfile* that a deny or revoke withdrew (RA-041).

    The one rule ``apm install`` retires them by and ``apm install --frozen``
    and ``apm audit --ci`` refuse them by. *trust_context* is called once, and
    only when a bundle owning a recorded LSP server has an install record.
    """
    from apm_cli.install.local_bundle_pending import bundle_grant_withdrawn, recorded_bundle_keys
    from apm_cli.security.executables import EXEC_TYPE_LSP

    raw = getattr(lockfile, "lsp_config_provenance", None)
    provenance = dict(raw) if isinstance(raw, dict) else {}
    owners = {
        name: owner.removeprefix(_BUNDLE_LSP_OWNER_PREFIX)
        for name, owner in sorted(provenance.items())
        if not _is_regular_owner(owner)
    }
    if not any(recorded_bundle_keys(scope_dir, owner) for owner in owners.values()):
        return []
    context = trust_context()
    if context is None:
        return []
    return [
        WithdrawnBundleLspServer(owner, name)
        for name, owner in owners.items()
        if bundle_grant_withdrawn(scope_dir, owner, context, EXEC_TYPE_LSP)
    ]


def withdrawn_bundle_lsp_for_scope(
    lockfile: "LockFile | None",
    scope_dir: Path,
    project_root: Path,
    *,
    user_scope: bool,
    no_policy: bool,
) -> list[WithdrawnBundleLspServer]:
    """:func:`withdrawn_bundle_lsp_servers` under the scope's grants and org policy.

    ``apm install``, ``apm install --frozen`` and ``apm audit --ci`` all ask
    here, through the trust owner's admission, so they reach one verdict.
    """
    from apm_cli.security.trust import McpAdmission

    def _context() -> Any:
        return McpAdmission.for_project(
            project_root, user_scope=user_scope, no_policy=no_policy
        ).context

    return withdrawn_bundle_lsp_servers(lockfile, scope_dir, _context)


def _clean_target_differences(
    *,
    old_targets: dict[str, set[str]],
    new_targets: dict[str, set[str]],
    project_root: Path,
    user_scope: bool,
    logger,
    fail_on_write_error: bool,
) -> None:
    """Remove only target-scoped LSP entries whose recorded ownership was dropped."""
    from apm_cli.integration.lsp_integrator import LSPIntegrator

    for runtime, old_names in old_targets.items():
        stale = old_names - new_targets.get(runtime, set())
        if stale:
            LSPIntegrator.remove_stale(
                stale,
                project_root=project_root,
                user_scope=user_scope,
                logger=logger,
                target_runtimes=[runtime],
                fail_on_write_error=fail_on_write_error,
            )


def run_owned_lsp_integration(
    *,
    dependencies: list["LSPDependency"],
    owner: str,
    lock_path: Path,
    project_root: Path,
    user_scope: bool,
    target_runtimes: list[str],
    logger,
    fail_on_write_error: bool = True,
    force: bool = False,
) -> int:
    """Reconcile one bundle owner's LSP servers and persist ownership."""
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.lsp_integrator import LspInstallRecord, LSPIntegrator

    lockfile = LockFile.read(lock_path) or LockFile()
    owner_token = f"{_BUNDLE_LSP_OWNER_PREFIX}{owner}"
    owner_aliases = _bundle_owner_aliases(owner)
    old_owned = {
        name
        for name, recorded_owner in lockfile.lsp_config_provenance.items()
        if recorded_owner in owner_aliases
    }
    new_names = LSPIntegrator.get_server_names(dependencies)
    conflicts = {
        name
        for name in new_names
        if name in lockfile.lsp_servers
        and name not in old_owned
        and lockfile.lsp_config_provenance.get(name) not in owner_aliases
    }
    if conflicts:
        conflict_details = ", ".join(
            f"{name} (owned by {lockfile.lsp_config_provenance.get(name, 'legacy lock state')})"
            for name in sorted(conflicts)
        )
        raise ValueError(
            "Bundle LSP server name conflicts with another owner: "
            f"{conflict_details}. Rename the declaration or remove and reinstall "
            "the owning bundle; --force does not transfer ownership."
        )

    if not dependencies and not old_owned:
        return 0

    from apm_cli.install.errors import LSPUserEntryConflictError

    old_targets = _target_server_sets(lockfile)
    supported_target_runtimes = LSPIntegrator.supported_target_runtimes(target_runtimes)
    count = 0
    user_conflict = None
    record = LspInstallRecord()
    kept = record.kept_unowned
    if dependencies:
        require_lsp_capable_target(supported_target_runtimes, bundle=True)
        try:
            count = LSPIntegrator.install(
                dependencies,
                project_root=project_root,
                user_scope=user_scope,
                logger=logger,
                target_runtimes=supported_target_runtimes,
                fail_on_write_error=fail_on_write_error,
                managed_target_servers=old_targets,
                force=force,
                record=record,
            )
        except LSPUserEntryConflictError as exc:
            # The other servers were written; record them, then fail (#4628).
            user_conflict = exc
    new_targets = {runtime: set(names) for runtime, names in old_targets.items()}
    for names in new_targets.values():
        names.difference_update(old_owned)
    for runtime in supported_target_runtimes:
        new_targets.setdefault(runtime, set()).update(
            LSPIntegrator.expressible_server_names(dependencies, runtime) - kept.get(runtime, set())
        )
    _clean_target_differences(
        old_targets=old_targets,
        new_targets=new_targets,
        project_root=project_root,
        user_scope=user_scope,
        logger=logger,
        fail_on_write_error=fail_on_write_error,
    )

    recorded = new_names - _unowned_everywhere(kept, new_targets)
    for name in old_owned:
        lockfile.lsp_configs.pop(name, None)
        lockfile.lsp_config_provenance.pop(name, None)
    lockfile.lsp_servers = sorted((set(lockfile.lsp_servers) - old_owned) | recorded)
    lockfile.lsp_configs.update(
        {
            name: config
            for name, config in LSPIntegrator.get_server_configs(dependencies).items()
            if name in recorded
        }
    )
    lockfile.lsp_config_provenance.update(dict.fromkeys(recorded, owner_token))
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    DeploymentLedgerCodec.replace_lsp_target_servers(
        lockfile,
        {
            runtime: sorted(server_names)
            for runtime, server_names in new_targets.items()
            if server_names
        },
    )
    lockfile.write(lock_path)
    if user_conflict is not None:
        raise user_conflict
    return count


def has_recorded_lsp_state(lockfile: "LockFile | None") -> bool:
    """Return whether *lockfile* records LSP servers APM may need to reconcile."""
    return lockfile is not None and bool(
        lockfile.lsp_servers or lockfile.lsp_target_servers or lockfile.lsp_config_provenance
    )


def reconcile_lsp_after_uninstall(
    *,
    apm_package: "APMPackage",
    lockfile: "LockFile | None",
    lock_path: Path,
    modules_dir: Path,
    project_root: Path,
    user_scope: bool,
    logger,
    excluded_package_keys: Mapping[str, str] = MappingProxyType({}),
) -> bool:
    """Recompute trusted LSP state from every surviving declaration.

    Servers declared by *excluded_package_keys* (survivors the current gates
    no longer allow, from ``install/exec_gate.py::gate_retirements``) are
    left out, so the reconcile removes them (#4446).
    """
    if not has_recorded_lsp_state(lockfile):
        return False
    if apm_package is None:
        raise ValueError("Cannot reconcile existing LSP state without a valid project manifest")
    before = (
        list(lockfile.lsp_servers),
        dict(lockfile.lsp_configs),
        dict(lockfile.lsp_target_servers),
        dict(lockfile.lsp_config_provenance),
    )
    from apm_cli.core.scope import InstallScope
    from apm_cli.core.target_detection import (
        resolve_deployment_target_decision,
        resolve_package_target_decision,
    )
    from apm_cli.models.apm_package import (
        canonical_package_target_config,
        package_target_selection,
    )

    scope = InstallScope.USER if user_scope else InstallScope.PROJECT
    # Survivors stay where APM recorded writing them (#4059): a saved default
    # or harness detection decides only when the lock records no LSP target.
    target_decision = resolve_deployment_target_decision(
        manifest_target=package_target_selection(apm_package),
        recorded_targets=recorded_lsp_targets(lockfile),
        fallback=lambda: resolve_package_target_decision(
            project_root,
            package=apm_package,
            explicit_target=None,
            user_scope=user_scope,
        ),
    )
    apm_config = {"scripts": apm_package.scripts or {}}
    apm_config.update(canonical_package_target_config(apm_package))
    target_context = (apm_config, target_decision.value, scope)
    prepared = None
    if excluded_package_keys:
        prepared = prepare_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=modules_dir,
            lock_path=lock_path,
            project_root=project_root,
            should_install=True,
            logger=logger,
            existing_lock=lockfile,
            user_scope=user_scope,
            target_context=target_context,
            target_decision=target_decision,
        )
        kept, dropped = [], set()
        for dependency in prepared.dependencies:
            owner = getattr(dependency, "resolved_by", None)
            if owner in excluded_package_keys:
                dropped.add(owner)
            else:
                kept.append(dependency)
        for owner in sorted(dropped):
            logger.warning(
                f"{owner} is no longer allowed ({excluded_package_keys[owner]}); "
                "removing its LSP servers"
            )
        prepared = replace(prepared, dependencies=kept)
    run_lsp_integration(
        apm_package=apm_package,
        apm_modules_path=modules_dir,
        lock_path=lock_path,
        existing_lock=lockfile,
        project_root=project_root,
        user_scope=user_scope,
        should_install=True,
        logger=logger,
        target_context=target_context,
        target_decision=target_decision,
        fail_on_write_error=True,
        persist=False,
        prepared=prepared,
    )
    after = (
        list(lockfile.lsp_servers),
        dict(lockfile.lsp_configs),
        dict(lockfile.lsp_target_servers),
        dict(lockfile.lsp_config_provenance),
    )
    return before != after


@dataclass(frozen=True)
class PreparedLspIntegration:
    """Trusted declarations and canonical targets prepared before native writes."""

    dependencies: list["LSPDependency"]
    target_runtimes: list[str] | None

    def require_capable_target(self) -> None:
        """Fail before the caller writes MCP or LSP config when no target fits.

        ``target_runtimes`` is ``None`` when LSP is not being installed.
        """
        if self.dependencies and self.target_runtimes is not None:
            require_lsp_capable_target(self.target_runtimes)


def prepare_lsp_integration(  # noqa: PLR0913
    *,
    apm_package: "APMPackage",
    apm_modules_path: Path,
    lock_path: Path,
    project_root: Path,
    should_install: bool,
    logger: "InstallLogger",
    existing_lock: "LockFile | None",
    user_scope: bool,
    runtime: str | None = None,
    exclude: str | None = None,
    target_context: tuple[dict | None, str | list[str] | None, object] = (None, None, None),
    target_decision: "EffectiveTargetDecision | None" = None,
    diagnostics: "DiagnosticCollector | None" = None,
    executable_context: tuple[dict[str, dict[str, bool]] | None, bool] = (None, False),
    no_policy: bool = False,
    claude_native_keys: frozenset[str] = frozenset(),
) -> PreparedLspIntegration:
    """Collect trusted declarations once before native-service reconciliation."""
    from apm_cli.integration.lsp_integrator import LSPIntegrator
    from apm_cli.security.executables import (
        filter_lsp_by_allow_executables,
        resolve_project_exec_allow_map,
    )

    apm_config, explicit_target, scope = target_context
    lsp_deps = apm_package.get_lsp_dependencies()
    if not isinstance(lsp_deps, list):
        logger.verbose_detail("LSP dependencies were not a list; defaulting to empty")
        lsp_deps = []
    if should_install and apm_modules_path.exists():
        transitive_lsp = LSPIntegrator.collect_transitive(
            apm_modules_path,
            lock_path,
            diagnostics=diagnostics,
            claude_native_keys=claude_native_keys,
        )
        if transitive_lsp:
            logger.verbose_detail(f"Collected {len(transitive_lsp)} transitive LSP dependency(ies)")
            effective_allow_executables = resolve_project_exec_allow_map(
                project_root,
                apm_package,
                user_scope=user_scope,
                executable_context=executable_context,
                no_policy=no_policy,
                logger=logger,
            )
            transitive_lsp = filter_lsp_by_allow_executables(
                transitive_lsp,
                effective_allow_executables,
                logger,
            )
            lsp_deps = LSPIntegrator.deduplicate(lsp_deps + transitive_lsp)
    target_runtimes = None
    if should_install and (lsp_deps or (existing_lock and existing_lock.lsp_servers)):
        target_runtimes = _resolve_lsp_target_runtimes(
            project_root=project_root,
            user_scope=user_scope,
            runtime=runtime,
            exclude=exclude,
            apm_config=apm_config,
            explicit_target=explicit_target,
            target_decision=target_decision,
            scope=scope,
            logger=logger,
        )
    return PreparedLspIntegration(lsp_deps, target_runtimes)


def _legacy_lsp_owner_names(names: set[str], targets_recorded: bool) -> dict:
    """Server names a lockfile without per-target ownership says APM wrote (#4628).

    They only keep install from preserving an entry APM itself wrote earlier
    as if it were the user's; they never authorize a protected-plugin overwrite.
    """
    return {} if targets_recorded or not names else {"legacy_server_names": set(names)}


def _refusal(conflict):
    """*conflict* as a refusal: it wrote no server, so the install commits nothing (#4697).

    Like an MCP native conflict that wrote no connection, nothing is recorded;
    the transaction discards the staged lockfile, so a failed install leaves
    ``apm.lock.yaml`` byte-identical.
    """
    from apm_cli.install.errors import LSPUserEntryRefusal

    return LSPUserEntryRefusal(str(conflict), preserved=conflict.preserved, wrote_servers=False)


def _unowned_everywhere(kept: dict[str, set[str]], new_targets: dict[str, set[str]]) -> set[str]:
    """User entries APM owns on no target, so the lock never claims them (#4628)."""
    return set().union(*kept.values(), set()) - set().union(*new_targets.values(), set())


def run_lsp_integration(  # noqa: PLR0913
    *,
    apm_package: "APMPackage",
    apm_modules_path: Path,
    lock_path: Path,
    existing_lock: "LockFile | None",
    project_root: Path,
    user_scope: bool,
    should_install: bool,
    logger,
    diagnostics=None,
    runtime: str | None = None,
    exclude: str | None = None,
    apm_config: dict | None = None,
    explicit_target: str | list[str] | None = None,
    scope=None,
    target_context: tuple[dict | None, str | list[str] | None, object] | None = None,
    target_decision: "EffectiveTargetDecision | None" = None,
    fail_on_write_error: bool = False,
    effective_allow_executables: dict[str, dict[str, bool]] | None = None,
    effective_allow_resolved: bool = False,
    force: bool = False,
    no_policy: bool = False,
    persist: bool = True,
    refuse_unwritten_conflict: bool = True,
    prepared: PreparedLspIntegration | None = None,
) -> int:
    """Run LSP server integration after APM package installation.

    Mirrors the MCP integration pattern:
    1. Collect direct + transitive LSP deps
    2. Deduplicate (first occurrence wins)
    3. Resolve runtime targets
    4. Install to each target's LSP config
    5. Clean up stale servers
    6. Update lockfile

    Args:
        apm_package: Root APM package with LSP deps.
        apm_modules_path: Path to apm_modules directory.
        lock_path: Path to apm.lock.yaml.
        existing_lock: Previously loaded lockfile (for old LSP state).
        project_root: Project root directory.
        user_scope: If True, write to user-scope runtime config paths.
        should_install: Whether LSP integration should run (same gate as MCP).
        logger: Install logger instance.
        diagnostics: Optional DiagnosticCollector.
        runtime: Optional runtime override.
        exclude: Optional runtime exclusion.
        apm_config: Parsed apm.yml target metadata for project-scope gating.
        explicit_target: Explicit target selected by CLI or manifest.
        scope: Optional InstallScope for user/project filtering.
        target_context: Compact `(apm_config, explicit_target, scope)` tuple
            used by the install command to keep entry-point glue small.
        persist: False when an uninstall reconciles survivors in memory; a
            user-entry conflict then finishes reconciliation before failing.
        refuse_unwritten_conflict: Whether a user-entry conflict that wrote no
            server refuses the commit (#4697). False when this command already
            staged other service writes (an MCP connection) that must be recorded.

    Returns:
        Number of LSP servers configured.
    """
    from apm_cli.install.errors import LSPUserEntryConflictError
    from apm_cli.integration.lsp_integrator import LspInstallRecord, LSPIntegrator

    if target_context is not None:
        apm_config, explicit_target, scope = target_context
    prepared = prepared or prepare_lsp_integration(
        apm_package=apm_package,
        apm_modules_path=apm_modules_path,
        lock_path=lock_path,
        project_root=project_root,
        should_install=should_install,
        logger=logger,
        existing_lock=existing_lock,
        user_scope=user_scope,
        runtime=runtime,
        exclude=exclude,
        target_context=(apm_config, explicit_target, scope),
        target_decision=target_decision,
        diagnostics=diagnostics,
        executable_context=(effective_allow_executables, effective_allow_resolved),
        no_policy=no_policy,
    )
    lsp_deps, target_runtimes = prepared.dependencies, prepared.target_runtimes

    # Capture old LSP servers from lockfile
    old_lsp_servers: builtins.set = builtins.set()
    old_lsp_configs: builtins.dict = {}
    old_lsp_provenance: dict[str, str] = {}
    old_lsp_targets: dict[str, set[str]] = {}
    old_lsp_targets_present = False
    if existing_lock:
        old_lsp_servers = builtins.set(existing_lock.lsp_servers)
        old_lsp_configs = builtins.dict(existing_lock.lsp_configs)
        raw_provenance = getattr(existing_lock, "lsp_config_provenance", {})
        old_lsp_provenance = dict(raw_provenance) if isinstance(raw_provenance, dict) else {}
        old_lsp_targets = _target_server_sets(existing_lock)
        old_lsp_targets_present = (
            getattr(existing_lock, "_lsp_target_servers_present", False) is True
        )

    if should_install and not (
        lsp_deps or old_lsp_servers or old_lsp_provenance or old_lsp_targets
    ):
        logger.verbose_detail("No LSP dependencies found in apm.yml")
        return 0

    lsp_count = 0

    if should_install:
        bundle_names = {
            name for name, owner in old_lsp_provenance.items() if not _is_regular_owner(owner)
        }
        withdrawn = bundle_names & {
            item.server
            for item in withdrawn_bundle_lsp_for_scope(
                existing_lock,
                apm_modules_path.parent,
                project_root,
                user_scope=user_scope,
                no_policy=no_policy,
            )
        }
        bundle_names -= withdrawn
        old_regular_names = {
            name for name, owner in old_lsp_provenance.items() if _is_regular_owner(owner)
        }
        if old_lsp_targets_present:
            old_regular_names.update(set().union(*old_lsp_targets.values(), set()) - bundle_names)
        new_regular_names = LSPIntegrator.get_server_names(lsp_deps) if lsp_deps else builtins.set()
        conflicts = new_regular_names & bundle_names
        if conflicts:
            conflict_details = ", ".join(
                f"{name} (owned by {old_lsp_provenance[name]})" for name in sorted(conflicts)
            )
            raise ValueError(
                "Manifest LSP server name conflicts with an installed bundle owner: "
                f"{conflict_details}. Rename the declaration or remove and reinstall "
                "the owning bundle; --force does not transfer ownership."
            )

        conflict = None
        record = LspInstallRecord(locked_configs=old_lsp_configs)
        kept, kept_servers = record.kept_unowned, record.kept_edits
        if lsp_deps:
            if fail_on_write_error:
                require_lsp_capable_target(target_runtimes)
            try:
                lsp_count = LSPIntegrator.install(
                    lsp_deps,
                    project_root=project_root,
                    user_scope=user_scope,
                    logger=logger,
                    diagnostics=diagnostics,
                    target_runtimes=target_runtimes,
                    fail_on_write_error=fail_on_write_error,
                    managed_target_servers=old_lsp_targets,
                    force=force,
                    record=record,
                    **_legacy_lsp_owner_names(old_lsp_servers, old_lsp_targets_present),
                )
            except LSPUserEntryConflictError as exc:
                if not exc.wrote_servers and persist and refuse_unwritten_conflict:
                    raise _refusal(exc) from None
                # The other servers were written; record them, then fail (#4628).
                conflict = exc

        new_targets = {
            runtime_name: set(server_names)
            for runtime_name, server_names in old_lsp_targets.items()
        }
        for server_names in new_targets.values():
            server_names.difference_update(old_regular_names | withdrawn)
        for target_runtime in target_runtimes or []:
            new_targets.setdefault(target_runtime, set()).update(
                LSPIntegrator.expressible_server_names(lsp_deps, target_runtime)
                - kept.get(target_runtime, set())
            )
        if old_lsp_targets_present:
            _clean_target_differences(
                old_targets=old_lsp_targets,
                new_targets=new_targets,
                project_root=project_root,
                user_scope=user_scope,
                logger=logger,
                fail_on_write_error=fail_on_write_error,
            )

        # A server kept as the user's on every target is not APM's: record nothing for it.
        unwritten = _unowned_everywhere(kept, new_targets)
        new_regular_configs = LSPIntegrator.get_server_configs(lsp_deps)
        # A kept entry keeps its locked config: an edit never becomes APM's baseline (#4626).
        for name in kept_servers:
            if name in old_lsp_configs:
                new_regular_configs[name] = old_lsp_configs[name]
            else:
                new_regular_configs.pop(name, None)
        new_configs = {
            name: config for name, config in old_lsp_configs.items() if name in bundle_names
        }
        new_configs.update(new_regular_configs)
        new_provenance = {
            name: owner for name, owner in old_lsp_provenance.items() if name in bundle_names
        }
        new_provenance.update(_dependency_provenance(lsp_deps))
        for name in unwritten:
            new_configs.pop(name, None)
            new_provenance.pop(name, None)
        all_names = (bundle_names | new_regular_names) - unwritten
        LSPIntegrator.update_lockfile(
            all_names,
            lock_path,
            lsp_configs=new_configs,
            lsp_target_servers=new_targets,
            lsp_config_provenance=new_provenance,
            lockfile_state=existing_lock if not persist else None,
            persist=persist,
            fail_on_write_error=fail_on_write_error,
        )
        if conflict is not None:
            raise conflict
        if not lsp_deps:
            logger.verbose_detail("No LSP dependencies found in apm.yml")

    elif old_lsp_servers:
        # Selective APM or MCP installs preserve every LSP ownership view.
        LSPIntegrator.update_lockfile(
            old_lsp_servers,
            lock_path,
            lsp_configs=old_lsp_configs,
            lsp_target_servers=old_lsp_targets,
            lsp_config_provenance=old_lsp_provenance,
            lockfile_state=existing_lock if not persist else None,
            persist=persist,
            fail_on_write_error=fail_on_write_error,
        )

    return lsp_count
