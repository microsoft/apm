"""``apm update`` -- refresh APM dependencies to the latest matching refs.

This is the package-manager convention popularised by ``cargo update``,
``poetry update``, ``bundle update``, and ``npm update`` -- the verb is
about the dependency graph, not about updating the CLI binary itself.
The CLI self-updater lives at ``apm self-update`` (see
:mod:`apm_cli.commands.self_update`); when this command runs outside an
``apm.yml`` project it forwards to the self-updater as a deprecated
back-compat shim for one release (see ``update()`` below).

What it does
------------
``apm update`` is conceptually equivalent to ``apm install --update``
**plus** an interactive plan-and-confirm gate:

1. Run resolve to discover which deps would change.
2. Render a structured plan (``[~]`` updated, ``[+]`` added,
   ``[-]`` removed) that names every dep, the ref/SHA transition, and
   the deployed files at risk.
3. Prompt ``Apply these changes? [y/N]`` -- default **No**, mirroring
   the security framing in the public response on issue #1203.
4. On ``y``: continue the install pipeline (download + integrate +
   lockfile rewrite).  On ``N`` / ``--dry-run``: exit cleanly with no
   on-disk mutations.  In no-TTY mode, ref changes fail closed; an
   unchanged locked graph may still restore a wholly absent package cache
   without prompting.

Flags
-----
* ``--yes``/``-y`` -- skip the prompt (CI / automation).
* ``--dry-run``    -- render the plan and exit without prompting.
* ``--verbose``/``-v`` -- show unchanged deps in the plan and pipeline
  diagnostics.
* ``--global``/``-g`` -- refresh user-scope dependencies under
  ``$APM_HOME`` (default ``~/.apm``) instead of the current project (mirrors
  ``apm install -g``).
* ``[PACKAGES]...`` -- positional names to refresh only those
  dependencies; omit to refresh everything.
* ``--force`` -- overwrite locally-authored files and deploy despite
  critical security findings; does not bypass upstream ref resolution.
* ``--parallel-downloads`` -- max concurrent package downloads
  (0 disables parallelism).
* ``--target``/``-t`` -- agent harness(es) to deploy to; comma-separated
  for multiple targets. The generated command help lists every accepted
  value. Overrides ``apm.yml targets:``, the saved config target, and
  auto-detection.
* ``--legacy-skill-paths`` -- keep per-client skill paths instead of
  ``.agents/skills/`` (env fallback: ``APM_LEGACY_SKILL_PATHS``).

These flags make ``apm update`` a strict superset of the deprecated
``apm deps update`` (issue #1525). ``apm install --update`` remains the
swiss-army-knife escape hatch for the rest of the install surface.
"""

from __future__ import annotations

import copy
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from apm_cli.core.remediation import (
    CLI_BINARY_OUTDATED,
    UPDATE_UNDECLARED_PACKAGE,
    USER_MANIFEST_MISSING,
)
from apm_cli.core.scope import manifest_file

from ..core.auth import AuthResolver
from ..core.command_logger import InstallLogger
from ..core.deprecations import UPDATE_REFRESHES_DEPENDENCIES, deprecation_message
from ..core.target_catalog import target_help_fragment
from ..core.target_detection import TargetParamType
from ..deps.github_downloader import GitHubPackageDownloader
from ..deps.lockfile_write_authority import LockfileWriter, lockfile_writer
from ..deps.revision_pins import (
    RemoteRefDownloader,
    RevisionPinResolutionError,
    RevisionPinResolutionResult,
    RevisionPinUpdate,
    apply_revision_pin_updates,
    render_revision_pin_update_plan,
    resolve_revision_pin_updates,
)
from ..install.errors import (
    AuthenticationError,
    DirectDependencyError,
    FrozenInstallError,
    PolicyViolationError,
    RequiredIntegrationError,
)
from ..install.locking import serialized_lifecycle
from ..install.lsp.integration import LspPreflight
from ..install.plan import UpdatePlan, render_plan_text
from ..utils.console import _rich_echo, _rich_error, _rich_info, _rich_success, _rich_warning
from ._helpers import (
    UnknownPackageError,
    _find_apm_yml,
    explicit_policy_option,
    resolve_requested_packages,
    trust_transitive_mcp_option,
)

if TYPE_CHECKING:
    from ..core.command_logger import CommandLogger
    from ..core.scope import InstallScope
    from ..core.target_detection import EffectiveTargetDecision
    from ..deps.lockfile import LockFile
    from ..models.dependency.reference import DependencyReference


@dataclass
class _UpdateRunState:
    """Mutable state shared with the install plan callback."""

    plan: UpdatePlan | None = None
    proceeded: bool = False
    revision_pins_applied: bool = False
    cache_rehydration_requested: bool = False
    gate_retirement: bool = False
    pin_repairs: tuple[Any, ...] = ()


def _render_update_completion_summary(
    *,
    installed: int,
    changed: int,
    revision_pin_updates: list[Any],
    plan_state: _UpdateRunState,
    exec_reconcile_required: bool,
    restored_provenance: bool,
    logger: CommandLogger,
) -> None:
    """Render the final update outcome summary after all writes commit."""
    applied = bool(installed) and changed > 0
    dep_noun = "dependency" if changed == 1 else "dependencies"
    if applied and revision_pin_updates:
        count = len(revision_pin_updates)
        pin_noun = "pin" if count == 1 else "pins"
        _rich_success(
            f"Updated {changed} APM {dep_noun} and {count} revision {pin_noun} in apm.yml."
        )
    elif applied:
        _rich_success(f"Updated {changed} APM {dep_noun}.")
    elif plan_state.cache_rehydration_requested and installed:
        logger.success(
            _no_ref_change_summary(
                exec_reconcile_required or plan_state.gate_retirement,
                restored_provenance,
            ),
            symbol="check",
        )
    elif revision_pin_updates:
        count = len(revision_pin_updates)
        noun = "pin" if count == 1 else "pins"
        _rich_success(f"Updated {count} revision {noun} in apm.yml.")
    elif plan_state.pin_repairs:
        _report_pin_repairs(plan_state.pin_repairs, " No dependency changes were applied.")
        return
    else:
        _rich_success("No dependency changes were applied.")

    # Every other outcome line still reports what the pin repairs did, so a
    # failed pins write is never hidden behind a revision-pin summary.
    if plan_state.pin_repairs:
        _report_pin_repairs(plan_state.pin_repairs, "")


def _report_pin_repairs(staged: tuple[Any, ...], suffix: str) -> None:
    """Report the pin repairs the commit actually saved, and any it could not."""
    from apm_cli.marketplace.version_pins import committed_ref_pin_repairs

    saved = len(committed_ref_pin_repairs())
    failed = len(staged) - saved
    if saved:
        noun = "pin" if saved == 1 else "pins"
        _rich_success(f"Repaired {saved} marketplace ref {noun}.{suffix}")
    if failed > 0:
        noun = "repair" if failed == 1 else "repairs"
        _rich_warning(
            f"Could not save {failed} marketplace ref pin {noun}; the conflicting pins are "
            "unchanged, so the next resolution refuses them again."
        )
        if suffix:
            _rich_success(suffix.strip())


def _render_pin_repairs_plan(repairs: tuple[Any, ...]) -> str:
    """Render staged marketplace ref-pin repairs as an ASCII plan."""
    if not repairs:
        return ""
    lines = ["Marketplace ref pin repairs:"]
    for repair in repairs:
        label = f"{repair.plugin_name}@{repair.marketplace_name}"
        if repair.version:
            label = f"{label} ({repair.version})"
        if repair.conflicting_pins:
            conflicts = ", ".join(f"{alias}={ref}" for alias, ref in repair.conflicting_pins)
            lines.append(
                f"  [~] {label}: replace conflicting legacy pins ({conflicts}) -> {repair.new_ref}"
            )
        elif repair.previous_ref:
            lines.append(f"  [~] {label}: update ref pin {repair.previous_ref} -> {repair.new_ref}")
        else:
            lines.append(f"  [~] {label}: repair ref pin -> {repair.new_ref}")
    return "\n".join(lines)


def _stdin_is_tty() -> bool:
    """Return True only when stdin is connected to a real terminal.

    A non-TTY stdin (CI, piped, redirected) means we cannot safely
    prompt for confirmation -- ``apm update`` aborts with guidance to
    re-run with ``--yes``.
    """
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _module_cache_needs_rehydration(
    lockfile: LockFile | None,
    modules_dir: Path,
) -> bool:
    """Return whether a locked dependency tree has no materialized cache."""
    if lockfile is None:
        return False
    has_locked_dependency = False
    for key, dependency in lockfile.dependencies.items():
        if key == ".":
            continue
        has_locked_dependency = True
        install_path = dependency.to_dependency_ref().get_install_path(modules_dir)
        if install_path.exists():
            return False
    return has_locked_dependency


def _locked_copy_contradicts_lock(lockfile: LockFile | None, modules_dir: Path) -> bool:
    """Whether a materialized locked package no longer matches its ``content_hash``.

    An update with no ref change still runs install when a copy contradicts
    the lock, so install's locked-source verdict
    (:func:`~apm_cli.install.source_helpers.locked_content_mismatch`) re-fetches
    it or refuses, as ``apm install``, ``--frozen`` and ``audit --ci`` do (#4385).
    """
    if lockfile is None:
        return False
    from apm_cli.install.source_helpers import locked_content_mismatch

    for key, dependency in lockfile.dependencies.items():
        # Local packages re-copy from their source on every install, so their
        # copy carries no locked-source verdict (as audit's replay skips them).
        if key == "." or not dependency.content_hash or dependency.source == "local":
            continue
        install_path = dependency.to_dependency_ref().get_install_path(modules_dir)
        if install_path.is_dir() and locked_content_mismatch(
            key, dependency, install_path, lockfile=lockfile
        ):
            return True
    return False


def _executables_need_reconcile(
    lockfile: LockFile | None,
    modules_dir: Path,
    scope: InstallScope,
    apm_package: Any,
) -> bool:
    """Return whether a recorded executables decision no longer holds (#3958).

    Lets the no-change short-circuit still run the install gate after an
    ``apm approve`` / ``apm deny`` so it deploys or retires executables.
    """
    if lockfile is None:
        return False
    from apm_cli.core.scope import InstallScope as _Scope
    from apm_cli.core.scope import get_deploy_root, get_manifest_path, manifest_root
    from apm_cli.install.exec_gate import lock_exec_decisions_stale
    from apm_cli.integration.targets import resolve_audit_targets

    def _policy() -> Any:
        from apm_cli.install.exec_gate import ExecPolicyUnavailable
        from apm_cli.policy.outcome_routing import classify_project_policy

        route = classify_project_policy(
            manifest_root(get_deploy_root(scope), user_scope=scope is _Scope.USER)
        )
        # A blocking or unresolved outcome fails toward running the install
        # gate, which routes the same outcome and reports it.
        if route.blocks or route.unresolved:
            raise ExecPolicyUnavailable(route.outcome)
        return route.policy

    return lock_exec_decisions_stale(
        lockfile,
        modules_dir,
        get_manifest_path(scope),
        policy_provider=_policy,
        targets=resolve_audit_targets(
            get_deploy_root(scope), user_scope=scope is _Scope.USER, lockfile=lockfile
        ),
        fallback_allow_executables=getattr(apm_package, "allow_executables", None),
    )


def _gate_retirements_pending(
    lockfile: LockFile | None, modules_dir: Path, scope: InstallScope, apm_package: Any
) -> bool:
    """Whether the current gates no longer allow something deployed (#4444, #4445).

    Asked once the resolver restaged local packages, so a changed local
    definition is judged as install judges it: a policy-denied dependency or
    a server install's MCP admission now parks also runs the install gate,
    which refuses or retires it.
    """
    from apm_cli.core.scope import InstallScope as _Scope
    from apm_cli.core.scope import get_deploy_root
    from apm_cli.install.exec_gate import gate_retirements

    try:
        retirements = gate_retirements(
            lockfile,
            modules_dir,
            apm_package,
            get_deploy_root(scope),
            user_scope=scope is _Scope.USER,
        )
    except (OSError, ValueError):
        return False
    # An unresolved or blocking org policy is the policy preflight's outcome.
    return retirements is not None and any(retirements)


def _reconcile_note(exec_reconcile_required: bool, gate_retirement: bool) -> str | None:
    """The info line for an update that reconciles deployed state without moving refs."""
    if gate_retirement:
        return "Re-checking deployed items against the current gates, as install does."
    if exec_reconcile_required:
        return (
            "Executables approvals changed since the last install; "
            "reconciling deployed executables."
        )
    return None


def _enforce_policy_without_changes(
    apm_package: Any, scope: InstallScope, logger: InstallLogger, dry_run: bool
) -> None:
    """Apply org policy to the declared dependencies when an update changes nothing."""
    from apm_cli.core.scope import InstallScope as _Scope
    from apm_cli.core.scope import get_deploy_root, manifest_root
    from apm_cli.install.pipeline_entry import enforce_declared_policy

    from ..models.dependency.replacement import project_manifest_dependencies

    enforce_declared_policy(
        manifest_root(get_deploy_root(scope), user_scope=scope is _Scope.USER),
        list(project_manifest_dependencies(apm_package)),
        list(apm_package.get_all_mcp_dependencies()) or None,
        logger,
        dry_run=dry_run,
    )


def _build_revision_pin_downloader() -> RemoteRefDownloader:
    """Build the downloader used for authoritative revision-pin ref checks."""
    return GitHubPackageDownloader(auth_resolver=AuthResolver())


def _resolve_and_stage_revision_pin_updates(
    *,
    all_declared_deps: list[DependencyReference],
    only_packages: list[str] | None,
    logger: InstallLogger,
    downloader: RemoteRefDownloader | None = None,
    max_workers: int = 4,
) -> RevisionPinResolutionResult:
    """Resolve SHA pins and stage their in-memory references for the plan.

    The passed dependency references belong to a staged APMPackage copy, not to
    the object parsed from disk. Mutating them lets the install pipeline resolve
    against the new SHAs after the user's consent decision while dry-run and
    decline paths leave the original manifest model untouched.
    """
    from git.exc import GitCommandError

    only_set = set(only_packages) if only_packages is not None else None
    logger.progress("Checking upstream for revision-pin freshness...", symbol="running")

    try:
        # Authoritative round-trip (intentional, do NOT short-circuit): this
        # bounded ls-remote pass resolves the latest annotated-tag SHA only to
        # build the plan and rewrite apm.yml. The subsequent install pipeline
        # independently re-resolves the freshly-written pin against upstream
        # before downloading. Threading the SHA resolved here into install
        # would collapse the authoritative-upstream fence.
        resolution = resolve_revision_pin_updates(
            all_declared_deps,
            downloader or _build_revision_pin_downloader(),
            only_packages=only_set,
            max_workers=max_workers,
        )
    except RevisionPinResolutionError as e:
        logger.revision_pin_resolution_failed(e)
        sys.exit(1)
    except (GitCommandError, OSError) as e:
        logger.error(f"Failed to resolve revision pins: {e}")
        if not logger.verbose:
            logger.info("Run with --verbose for detailed diagnostics.")
        sys.exit(1)

    logger.revision_pins_retained(resolution.skips)

    updates_by_key = {update.dep_key: update for update in resolution.updates}
    for dep_ref in all_declared_deps:
        update = updates_by_key.get(dep_ref.get_unique_key())
        if update is not None:
            dep_ref.reference = update.new_sha
    return resolution


def _annotate_lockfile_revision_tags(project_root: Path, updates: list[RevisionPinUpdate]) -> None:
    """Record resolved annotated tag names for updated SHA pins in the lockfile."""
    if not updates:
        return
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    lockfile_path = get_lockfile_path(project_root)
    lockfile = LockFile.read(lockfile_path)
    if lockfile is None:
        raise RuntimeError("Could not record revision-pin tags: apm.lock.yaml was not written")

    changed = False
    for update in updates:
        locked = lockfile.get_dependency(update.dep_key)
        if locked is None:
            raise RuntimeError(
                f"Could not record revision-pin tag for {update.display_name}: missing lockfile entry"
            )
        if (locked.resolved_commit or "").lower() != update.new_sha.lower():
            raise RuntimeError(
                f"Could not record revision-pin tag for {update.display_name}: "
                "lockfile SHA does not match updated manifest"
            )
        if locked.resolved_tag != update.tag:
            locked.resolved_tag = update.tag
            changed = True
    if changed:
        lockfile.save(lockfile_path)


def _run_mcp_lsp_integration(
    *,
    scope: InstallScope,
    project_root: Path,
    existing_lock: LockFile | None,
    lock_path: Path,
    target_decision: EffectiveTargetDecision | None,
    diagnostics: Any,
    logger: InstallLogger,
    verbose: bool,
    effective_allow_executables: dict[str, dict[str, bool]] | None = None,
    effective_allow_resolved: bool = False,
    force: bool = False,
    trust_transitive_mcp: bool = False,
) -> None:
    """Reconcile MCP and LSP servers against the current apm.yml.

    ``apm update`` calls ``_install_apm_dependencies`` directly rather than
    going through ``_install_apm_packages`` (see ``commands/install.py``),
    so it must separately run the same MCP/LSP integration that helper
    performs. Mirrors ``_install_apm_packages``'s ordering: clear the
    apm.yml parse cache, re-parse the on-disk manifest (revision pins are
    already applied by this point), then reconcile MCP and LSP servers.
    """
    from apm_cli.core.scope import InstallScope, get_modules_dir
    from apm_cli.install.lsp import run_lsp_integration
    from apm_cli.install.lsp.integration import lsp_target_config, prepare_lsp_integration
    from apm_cli.install.mcp import journal_mcp_writes, run_mcp_integration
    from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
    from apm_cli.policy.install_preflight import PolicyBlockError

    clear_apm_yml_cache()
    apm_package = APMPackage.from_apm_yml(manifest_file(Path(".")))
    effective_target = target_decision.value if target_decision is not None else None

    old_mcp_servers: set = set()
    old_mcp_configs: dict = {}
    old_mcp_provenance: dict = {}
    old_mcp_target_servers: dict = {}
    if existing_lock:
        old_mcp_servers = set(existing_lock.mcp_servers)
        old_mcp_configs = dict(existing_lock.mcp_configs)
        old_mcp_provenance = dict(existing_lock.mcp_config_provenance)
        old_mcp_target_servers = dict(existing_lock.mcp_target_servers)

    apm_modules_path = get_modules_dir(scope)
    user_scope = scope is InstallScope.USER
    # Declared LSP without a capable target fails before any MCP write (#3727).
    apm_config = lsp_target_config(apm_package)
    prepared_lsp = prepare_lsp_integration(
        apm_package=apm_package,
        apm_modules_path=apm_modules_path,
        lock_path=lock_path,
        project_root=project_root,
        should_install=True,
        logger=logger,
        existing_lock=existing_lock,
        user_scope=user_scope,
        target_context=(apm_config, effective_target, scope),
        target_decision=target_decision,
        diagnostics=diagnostics,
        executable_context=(effective_allow_executables, effective_allow_resolved),
    )
    prepared_lsp.require_capable_target()

    try:
        with journal_mcp_writes(project_root, user_scope=user_scope, logger=logger):
            _mcp_count, _mcp_apm_config = run_mcp_integration(
                apm_package=apm_package,
                mcp_deps=apm_package.get_all_mcp_dependencies(),
                apm_modules_path=apm_modules_path,
                lock_path=lock_path,
                old_mcp_servers=old_mcp_servers,
                old_mcp_configs=old_mcp_configs,
                old_mcp_provenance=old_mcp_provenance,
                old_mcp_target_servers=old_mcp_target_servers,
                trust_transitive_mcp=trust_transitive_mcp,
                project_root=project_root,
                user_scope=user_scope,
                should_install=True,
                logger=logger,
                diagnostics=diagnostics,
                verbose=verbose,
                explicit_target=effective_target,
                target_decision=target_decision,
                scope=scope,
                executable_context=(effective_allow_executables, effective_allow_resolved),
                pre_install_lock=existing_lock,
            )
    except PolicyBlockError:
        logger.refusal("MCP server(s) blocked by org policy. MCP configs were NOT written.")
        logger.render_summary()
        sys.exit(1)

    run_lsp_integration(
        apm_package=apm_package,
        apm_modules_path=apm_modules_path,
        lock_path=lock_path,
        existing_lock=existing_lock,
        project_root=project_root,
        user_scope=user_scope,
        should_install=True,
        logger=logger,
        diagnostics=diagnostics,
        target_context=(apm_config, effective_target, scope),
        target_decision=target_decision,
        fail_on_write_error=True,
        effective_allow_executables=effective_allow_executables,
        effective_allow_resolved=effective_allow_resolved,
        force=force,
        prepared=prepared_lsp,
    )


def _resolve_only_packages(packages: tuple[str, ...], declared: list) -> list[str] | None:
    """Resolve named packages to declared keys, or refuse with exit 1."""
    try:
        return resolve_requested_packages(packages, declared)
    except UnknownPackageError as e:
        _rich_error(f"Package '{e.token}' not found in apm.yml")
        if e.available:
            _rich_info(f"Available: {', '.join(e.available)}", symbol="info")
        else:
            _rich_info(
                "No APM dependencies are declared. "
                + UPDATE_UNDECLARED_PACKAGE.bind(package=shlex.quote(e.token)).sentence(),
                symbol="info",
            )
        sys.exit(1)


def _handle_service_only_update(
    *,
    apm_package: Any,
    scope: InstallScope,
    target: str | list[str] | None,
    dry_run: bool,
    logger: InstallLogger,
    verbose: bool,
    force: bool,
    trust_transitive_mcp: bool = False,
) -> bool:
    """Reconcile service-only manifests and return whether update is complete."""
    if apm_package.has_any_apm_dependencies():
        return False
    from apm_cli.policy.explicit_source import explicit_source_needs_standalone_check

    if explicit_source_needs_standalone_check(apm_package):
        # No dependency gate runs here; a named --policy must still load (#1925).
        _enforce_policy_without_changes(apm_package, scope, logger, dry_run)

    from apm_cli.core.scope import InstallScope, get_apm_dir, get_deploy_root
    from apm_cli.core.target_detection import resolve_package_target_decision
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    apm_dir = get_apm_dir(scope)
    lock_path = get_lockfile_path(apm_dir)
    existing_lock = LockFile.read(lock_path)
    has_services = bool(
        apm_package.get_all_mcp_dependencies()
        or apm_package.get_lsp_dependencies()
        or (existing_lock and (existing_lock.mcp_servers or existing_lock.lsp_servers))
    )
    if not has_services:
        logger.info("No APM dependencies declared in apm.yml -- nothing to update.")
        _explain_local_bundles(scope, apm_package, logger)
        return True
    if dry_run:
        logger.dry_run_notice("would reconcile MCP/LSP configuration; no files written")
        return True

    deploy_root = get_deploy_root(scope)
    target_decision = resolve_package_target_decision(
        deploy_root,
        package=apm_package,
        explicit_target=target,
        user_scope=scope is InstallScope.USER,
    )
    try:
        _run_mcp_lsp_integration(
            scope=scope,
            project_root=deploy_root,
            existing_lock=existing_lock,
            lock_path=lock_path,
            target_decision=target_decision,
            diagnostics=None,
            logger=logger,
            verbose=verbose,
            force=force,
            trust_transitive_mcp=trust_transitive_mcp,
        )
    except RequiredIntegrationError as exc:
        logger.error(str(exc))
        logger.render_summary()
        sys.exit(1)
    logger.success("MCP/LSP configuration reconciled. No APM dependencies to update.")
    _explain_local_bundles(scope, apm_package, logger)
    return True


def _explain_local_bundles(scope: Any, apm_package: Any, logger: InstallLogger) -> None:
    """Name the installed local bundles 'apm update' leaves untouched (#4318)."""
    from apm_cli.install.local_bundle_hook_trust import installed_local_bundles
    from apm_cli.install.outcome import (
        local_bundle_parked_next_step,
        local_bundle_update_next_step,
    )

    names, file_count = installed_local_bundles(scope, apm_package)
    if names or file_count:
        logger.info(local_bundle_update_next_step(names, file_count, scope))
    for slug, exec_types in _parked_local_bundles(scope, apm_package):
        logger.info(local_bundle_parked_next_step(slug, exec_types, scope))


def _parked_local_bundles(scope: Any, apm_package: Any) -> list[tuple[str, tuple[str, ...]]]:
    """Installed local bundles whose executables the current trust still parks (#4441)."""
    from apm_cli.core.scope import get_apm_dir, get_deploy_root, is_user_scope
    from apm_cli.install.local_bundle_pending import pending_bundles
    from apm_cli.security.executables import (
        package_type_approved,
        resolve_project_exec_allow_map,
    )

    allow = resolve_project_exec_allow_map(
        get_deploy_root(scope), apm_package, user_scope=is_user_scope(scope), no_policy=True
    )
    return [
        (bundle.slug, bundle.exec_types)
        for bundle in pending_bundles(
            get_apm_dir(scope), lambda key, kind: package_type_approved(allow, (key,), kind)
        )
    ]


def _no_ref_change_summary(exec_reconciled: bool, provenance_restored: bool) -> str:
    """The success line of an update that reconciled state without moving a ref."""
    if exec_reconciled:
        return "Reconciled executables without changing refs."
    if provenance_restored:
        return "Recorded restored marketplace provenance without changing refs."
    return "Restored dependency cache without changing refs."


def _reverified_lock(lockfile, lock_path: Path, logger, *, dry_run: bool, marketplaces=None):
    """Re-verify locked marketplace provenance before planning; fail closed (#4228).

    *marketplaces* is the root project's ``marketplaces:`` map (``None`` at user
    scope): its names resolve through apm.yml, not a registration (#2903).

    Returns ``(lockfile, restored)``: the lockfile with restored rows applied
    in memory, and their provenance for the lockfile phase. The update's one
    lockfile write records it (``LockfileBuilder`` via
    ``marketplace_provenance``, #4430); a failed update writes nothing.
    """
    from apm_cli.marketplace.catalog_origin import locked_provenance, reverify_locked_provenance
    from apm_cli.marketplace.client import catalog_cache_writes_on_success
    from apm_cli.marketplace.errors import MarketplaceError
    from apm_cli.marketplace.project_marketplaces import bind_project_marketplaces

    rows = lockfile.dependencies if lockfile is not None else {}
    before = {key: locked_provenance(dep) for key, dep in rows.items()}
    try:
        # A refused re-verification leaves the catalog cache unchanged; a
        # completed one persists what it fetched, validators included, and a
        # dry run persists nothing (#4484).
        with (
            bind_project_marketplaces(marketplaces),
            catalog_cache_writes_on_success(commit=not dry_run),
        ):
            restored = reverify_locked_provenance(lockfile, manifest_file(lock_path.parent))
    except MarketplaceError as exc:
        _rich_error(str(exc), symbol="error")
        sys.exit(1)
    for row in restored:
        logger.info(f"Restored marketplace provenance for {row} from its catalog.")
    return lockfile, {
        key: current
        for key, dep in rows.items()
        if (current := locked_provenance(dep)) != before.get(key)
    }


@click.command(
    name="update",
    help="Refresh APM dependencies to the latest matching refs",
)
@click.argument("packages", nargs=-1)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt (for CI / automation)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Render the update plan and exit without changing anything",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Show unchanged deps and detailed pipeline diagnostics",
)
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Refresh dependencies in the APM user home instead of the current project",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Overwrite locally-authored files and deploy despite critical security "
        "findings; does not bypass upstream ref resolution"
    ),
)
@click.option(
    "--parallel-downloads",
    type=int,
    default=4,
    show_default=True,
    help="Max concurrent package downloads (0 to disable parallelism)",
)
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    default=False,
    help=(
        "(Deprecated) Forwarded to 'apm self-update --check' (ignored with --global). "
        "Use 'apm update --dry-run' to preview dependency changes."
    ),
    hidden=True,
)
@click.option(
    "--target",
    "-t",
    type=TargetParamType(),
    default=None,
    help=(
        f"Agent target(s) to update for. {target_help_fragment('update')} "  # noqa: S608
        "Comma-separated for multiple: --target claude,cursor. "
        "Highest-priority entry in the resolution chain "
        "(--target > apm.yml targets: > apm config set target ... > auto-detect)."
    ),
)
@click.option(
    "--legacy-skill-paths",
    "legacy_skill_paths",
    is_flag=True,
    default=False,
    help=(
        "Deploy skill files to per-client paths (e.g. .cursor/skills/) instead of "
        "the shared .agents/skills/ directory. Env: APM_LEGACY_SKILL_PATHS=1."
    ),
)
@trust_transitive_mcp_option
@explicit_policy_option
@click.pass_context
@serialized_lifecycle
def update(
    ctx: click.Context,
    packages: tuple[str, ...],
    assume_yes: bool,
    dry_run: bool,
    verbose: bool,
    global_: bool,
    force: bool,
    parallel_downloads: int,
    check_only: bool,
    target: str | list[str] | None,
    legacy_skill_paths: bool,
    trust_transitive_mcp: bool,
) -> None:
    """Refresh APM dependencies to the latest matching refs.

    Examples:
        apm update                      # Resolve, show plan, prompt, then install
        apm update --dry-run            # Show plan only, do not change anything
        apm update --yes                # Skip the prompt (CI-safe)
        apm update org/pkg-a org/pkg-b  # Refresh only the named packages
        apm update -g                   # Refresh user-scope deps (~/.apm/)
        apm update --policy ./org-policy.yml  # Also enforce a vendored policy
    """
    from apm_cli.core.scope import InstallScope, get_manifest_path
    from apm_cli.integration.targets import should_use_legacy_skill_paths

    if global_:
        # User scope: operate on $APM_HOME/apm.yml. The cwd manifest walk and
        # the self-update back-compat shim apply only to project scope.
        scope = InstallScope.USER
        manifest_path = get_manifest_path(scope)
        if not manifest_path.is_file():
            _rich_error(
                f"No apm.yml found in {manifest_path.parent}. {USER_MANIFEST_MISSING.sentence()}"
            )
            sys.exit(1)
        if check_only:
            _rich_warning(
                "--check applies only to the self-update shim and is ignored with --global.",
                symbol="warning",
            )
        project_root = manifest_path.parent
    else:
        scope = InstallScope.PROJECT
        manifest_path = _find_apm_yml()
        if manifest_path is None:
            # Back-compat shim (one-release): when run outside a project,
            # forward to the renamed self-updater so existing users keep
            # working while we publicise ``apm self-update``.  Removed in
            # the release after this one.
            from apm_cli.commands.self_update import self_update as _self_update_cmd

            if target is not None:
                _rich_warning(
                    "--target is ignored when forwarding to 'apm self-update' "
                    f"(no apm.yml found). {CLI_BINARY_OUTDATED.sentence()}",
                    symbol="warning",
                )
            _rich_warning(deprecation_message("update-self-updater"), symbol="warning")
            ctx.invoke(_self_update_cmd, check=check_only)
            return

        if check_only:
            from apm_cli.commands.self_update import self_update as _self_update_cmd

            if target is not None:
                _rich_warning(
                    "--target is ignored when forwarding to 'apm self-update --check'. "
                    "Use 'apm update --dry-run' to preview dependency changes.",
                    symbol="warning",
                )
            _rich_warning(deprecation_message("update-check"), symbol="warning")
            ctx.invoke(_self_update_cmd, check=True)
            return

        project_root = manifest_path.parent
        if project_root != Path.cwd().resolve():
            _rich_info(
                f"Using apm.yml at {manifest_path} (project root: {project_root})",
                symbol="info",
            )

    _run_dep_update(
        assume_yes=assume_yes,
        dry_run=dry_run,
        verbose=verbose,
        project_root=project_root,
        target=target,
        scope=scope,
        packages=packages,
        force=force,
        parallel_downloads=parallel_downloads,
        legacy_skill_paths=legacy_skill_paths or should_use_legacy_skill_paths(),
        trust_transitive_mcp=trust_transitive_mcp,
    )


@lockfile_writer(LockfileWriter.UPDATE)
def _run_dep_update(
    *,
    assume_yes: bool,
    dry_run: bool,
    verbose: bool,
    project_root: Path | None = None,
    target: str | list[str] | None = None,
    scope=None,
    packages: tuple[str, ...] = (),
    force: bool = False,
    parallel_downloads: int = 4,
    legacy_skill_paths: bool = False,
    trust_transitive_mcp: bool = False,
) -> None:
    """Serialize update with every other mutation of the same workspace."""
    from apm_cli.core.scope import InstallScope
    from apm_cli.deps.lockfile_generator import require_current_cli
    from apm_cli.install.service import InstallService

    effective_scope = scope or InstallScope.PROJECT
    require_current_cli(project_root or Path.cwd(), command="apm update")
    with InstallService.catalog_freshness(update_refs=True, refresh=False):
        _run_dep_update_locked(
            assume_yes=assume_yes,
            dry_run=dry_run,
            verbose=verbose,
            project_root=project_root,
            target=target,
            scope=effective_scope,
            packages=packages,
            force=force,
            parallel_downloads=parallel_downloads,
            legacy_skill_paths=legacy_skill_paths,
            trust_transitive_mcp=trust_transitive_mcp,
        )


def _handle_unchanged_plan_no_op(
    retained_count: int,
    apm_package: Any,
    scope: Any,
    logger: Any,
    dry_run: bool,
) -> bool:
    if retained_count:
        noun = "pin" if retained_count == 1 else "pins"
        logger.info(
            f"No dependencies updated; retained {retained_count} revision "
            f"{noun} at the current SHA."
        )
    else:
        _rich_success(
            "All dependencies already at their latest matching refs.",
            symbol="check",
        )
    _enforce_policy_without_changes(apm_package, scope, logger, dry_run)
    return False


def _run_dep_update_locked(
    *,
    assume_yes: bool,
    dry_run: bool,
    verbose: bool,
    project_root: Path | None = None,
    target: str | list[str] | None = None,
    scope=None,
    packages: tuple[str, ...] = (),
    force: bool = False,
    parallel_downloads: int = 4,
    legacy_skill_paths: bool = False,
    trust_transitive_mcp: bool = False,
) -> None:
    """Core ``apm update`` flow: resolve, plan, prompt, install.

    When ``project_root`` is provided, the working directory is
    switched to it before running so install pipeline paths
    (``apm.yml``, ``apm.lock.yaml``, deployed primitives) resolve
    against the discovered project root, not the caller's cwd.

    ``scope`` selects project vs user deployment (defaults to project).
    ``packages`` narrows the refresh to the named dependencies; ``force``,
    ``parallel_downloads`` and ``legacy_skill_paths`` mirror the
    install-pipeline flags.
    """
    import os

    if project_root is not None and project_root != Path.cwd().resolve():
        os.chdir(project_root)

    # Surface the new semantics to CI users on every invocation: the
    # interactive prompt aborts non-TTY runs anyway, but a banner up
    # front prevents "why did our pipeline break overnight?" tickets
    # from teams whose CI calls 'apm update' assuming it self-updates
    # the CLI binary.
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        _rich_info(
            f"{UPDATE_REFRESHES_DEPENDENCIES} {CLI_BINARY_OUTDATED.sentence()}",
            symbol="info",
        )

    try:
        from apm_cli.commands.install import _install_apm_dependencies  # local import: heavy module
        from apm_cli.core.scope import InstallScope
        from apm_cli.models.apm_package import APMPackage
    except ImportError as e:  # pragma: no cover -- defensive
        _rich_error(f"APM dependency system not available: {e}")
        sys.exit(1)

    if scope is None:
        scope = InstallScope.PROJECT

    try:
        apm_package = APMPackage.from_apm_yml(manifest_file(Path(".")))
    except (FileNotFoundError, ValueError) as e:
        _rich_error(f"Failed to parse apm.yml: {e}")
        sys.exit(1)

    logger = InstallLogger(
        verbose=verbose, dry_run=dry_run, partial=bool(packages), command="update"
    )
    from ..models.dependency.replacement import project_manifest_dependencies

    # Map any positional [PACKAGES] to canonical dependency keys for the
    # engine's only_packages filter; None means "refresh everything". A named
    # package must be declared even when apm.yml declares no APM dependencies
    # (#4263), so this runs before any reconcile or service-only path.
    only_packages = _resolve_only_packages(packages, project_manifest_dependencies(apm_package))
    from apm_cli.install.local_bundle_hook_trust import reconcile_scope_bundle_hooks

    reconcile_scope_bundle_hooks(scope, apm_package, logger=logger, dry_run=dry_run)
    if _handle_service_only_update(
        apm_package=apm_package,
        scope=scope,
        target=target,
        dry_run=dry_run,
        logger=logger,
        verbose=verbose,
        force=force,
        trust_transitive_mcp=trust_transitive_mcp,
    ):
        return

    # Stage revision-pin rewrites on an owned package copy. The install
    # pipeline must resolve against the new SHAs, but declined/dry-run paths
    # should not mutate the APMPackage instance parsed from the on-disk manifest.
    staged_apm_package = copy.deepcopy(apm_package)
    all_declared_deps = project_manifest_dependencies(staged_apm_package)

    revision_pin_resolution = _resolve_and_stage_revision_pin_updates(
        all_declared_deps=all_declared_deps,
        only_packages=only_packages,
        logger=logger,
        max_workers=parallel_downloads if parallel_downloads > 0 else 1,
    )
    revision_pin_updates = revision_pin_resolution.updates

    plan_state = _UpdateRunState()

    def _apply_revision_pin_manifest_updates() -> None:
        """Persist staged revision-pin updates exactly once after consent."""
        if not revision_pin_updates or plan_state.revision_pins_applied:
            return
        try:
            apply_revision_pin_updates(manifest_file(Path(".")), revision_pin_updates)
        except Exception as e:
            _rich_error(f"Failed to update apm.yml revision pins: {e}")
            sys.exit(1)
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()
        plan_state.revision_pins_applied = True

    def _confirm_plan_application() -> bool:
        """Run the single update consent gate."""
        if assume_yes:
            _apply_revision_pin_manifest_updates()
            plan_state.proceeded = True
            return True

        if not _stdin_is_tty():
            _rich_error(
                "Cannot prompt for confirmation in non-interactive shell. "
                "Re-run with --yes to apply, or --dry-run to preview."
            )
            sys.exit(1)

        proceed = click.confirm("Apply these changes?", default=False, show_default=True)
        plan_state.proceeded = proceed
        if not proceed:
            _rich_info("No changes applied.", symbol="info")
            return False
        _apply_revision_pin_manifest_updates()
        return True

    def _plan_callback(plan: UpdatePlan) -> bool:
        """Render plan, prompt, and decide whether to proceed."""
        plan_state.plan = plan

        revision_plan = render_revision_pin_update_plan(revision_pin_updates)
        if revision_plan:
            _rich_echo(revision_plan)
            _rich_echo("")

        # A moved revision pin is already listed above; list each package once (#4208).
        unpinned_plan = plan.excluding(update.dep_key for update in revision_pin_updates)
        from apm_cli.marketplace.version_pins import (
            has_pending_pin_repairs,
            staged_ref_pin_repair_details,
        )

        pin_repairs = staged_ref_pin_repair_details()
        plan_state.pin_repairs = pin_repairs
        has_pin_repairs = bool(pin_repairs) or has_pending_pin_repairs()
        if unpinned_plan.has_changes:
            rendered = render_plan_text(unpinned_plan, verbose=verbose)
            if rendered:
                _rich_echo(rendered)
                _rich_echo("")
        elif not revision_pin_updates:
            plan_state.gate_retirement = not _exec_reconcile_required and (
                _gate_retirements_pending(_existing_lock, _modules_dir, scope, staged_apm_package)
            )
            reconcile_note = _reconcile_note(_exec_reconcile_required, plan_state.gate_retirement)
            if reconcile_note:
                logger.info(reconcile_note)
            elif (
                not _cache_rehydration_required and not _restored_provenance and not has_pin_repairs
            ):
                return _handle_unchanged_plan_no_op(
                    len(revision_pin_resolution.skips),
                    apm_package,
                    scope,
                    logger,
                    dry_run,
                )
            if not has_pin_repairs:
                plan_state.cache_rehydration_requested = True

        rendered_pin_repairs = _render_pin_repairs_plan(pin_repairs)
        if rendered_pin_repairs:
            _rich_echo(rendered_pin_repairs)
            _rich_echo("")

        if revision_pin_updates and unpinned_plan.has_changes:
            pin_count = len(revision_pin_updates)
            dep_count = len(unpinned_plan.changed_entries)
            pin_noun = "pin rewrite" if pin_count == 1 else "pin rewrites"
            dep_noun = "dependency change" if dep_count == 1 else "dependency changes"
            logger.info(f"Total: {pin_count} revision {pin_noun} + {dep_count} {dep_noun}.")
            _rich_echo("")

        if dry_run:
            _rich_info(
                "Dry run: no changes applied. Re-run without --dry-run to update.",
                symbol="info",
            )
            return False

        if plan_state.cache_rehydration_requested:
            plan_state.proceeded = True
            return True

        return _confirm_plan_application()

    # Snapshot the pre-update lockfile's MCP/LSP state before
    # ``_install_apm_dependencies`` regenerates it. The lockfile phase
    # carries ``mcp_servers``/``mcp_configs`` forward unreconciled (see
    # ``install/phases/lockfile.py::_preserve_existing_mcp_state``), so this
    # is the correct "old" baseline for stale-server detection below. LSP
    # fields have no such carry-forward, so they must be captured here too.
    from apm_cli.core.scope import get_apm_dir, get_deploy_root, get_modules_dir
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    _apm_dir = get_apm_dir(scope)
    _modules_dir = get_modules_dir(scope)
    _mcp_lsp_project_root = get_deploy_root(scope)
    _lock_path = get_lockfile_path(_apm_dir)
    _existing_lock, _restored_provenance = _reverified_lock(
        LockFile.read(_lock_path),
        _lock_path,
        logger,
        dry_run=dry_run,
        marketplaces=None
        if scope is InstallScope.USER
        else getattr(apm_package, "marketplaces", None),
    )
    _cache_rehydration_required = _module_cache_needs_rehydration(
        _existing_lock,
        _modules_dir,
    ) or _locked_copy_contradicts_lock(_existing_lock, _modules_dir)
    _exec_reconcile_required = not _cache_rehydration_required and _executables_need_reconcile(
        _existing_lock, _modules_dir, scope, apm_package
    )

    try:
        # Fire pre-update lifecycle scripts
        _fire_update_scripts(
            "pre-update",
            apm_package=staged_apm_package,
            scope=scope,
            logger=logger,
            verbose=verbose,
        )

        result = _install_apm_dependencies(
            staged_apm_package,
            update_refs=True,
            verbose=verbose,
            scope=scope,
            only_packages=only_packages,
            force=force,
            parallel_downloads=parallel_downloads,
            logger=logger,
            plan_callback=_plan_callback,
            marketplace_provenance=_restored_provenance or None,
            target=target,
            legacy_skill_paths=legacy_skill_paths,
            lsp_preflight=LspPreflight(),
            trust_transitive_mcp=trust_transitive_mcp,
            post_install_after_services=True,
        )
    except FrozenInstallError as e:
        _rich_error(str(e))
        for reason in e.reasons:
            _rich_echo(reason)
        _rich_info(
            "Tip: run 'apm outdated' to see what changed, then 'apm update'.",
            symbol="info",
        )
        sys.exit(1)
    except AuthenticationError as e:
        _rich_error(str(e))
        if e.diagnostic_context:
            _rich_echo(e.diagnostic_context)
        _rich_info("Tip: run 'apm doctor' to diagnose auth and connectivity.", symbol="info")
        sys.exit(1)
    except PolicyViolationError as e:
        logger.render_summary()
        _rich_error(f"{e} Nothing was written.", symbol="error", err=True)
        sys.exit(1)
    except (DirectDependencyError, RequiredIntegrationError) as e:
        logger.error(str(e))
        sys.exit(1)
    except click.UsageError:
        raise
    except Exception as e:
        _rich_error(f"Error updating dependencies: {e}")
        if not verbose:
            _rich_info("Run with --verbose for detailed diagnostics.")
        sys.exit(1)

    from apm_cli.install.summary import exit_unless_install_result_allows_success

    exit_unless_install_result_allows_success(
        logger=logger,
        result=result,
        allow_neutral_outcome=True,
    )

    plan = plan_state.plan
    if plan is None or not isinstance(plan, UpdatePlan):
        return

    target_decision = getattr(result, "target_decision", None)
    # A declined pin repair is a refusal, never a no-op that may still write.
    reconcile_noop = (
        not dry_run
        and not plan.has_changes
        and not revision_pin_updates
        and not plan_state.pin_repairs
    )
    if plan_state.proceeded or reconcile_noop:
        from apm_cli.install.manifest_reconcile import reconcile_project_deployed_state

        reconcile_project_deployed_state(
            Path.cwd(),
            explicit_target=target_decision.value if target_decision else target,
            deploy_root=_mcp_lsp_project_root,
            lock_root=_apm_dir,
            user_scope=scope is InstallScope.USER,
            verbose=verbose,
        )

    if plan_state.proceeded:
        if revision_pin_updates:
            try:
                _annotate_lockfile_revision_tags(Path.cwd(), revision_pin_updates)
            except Exception as e:
                _rich_error(f"Failed to record revision-pin tags in apm.lock.yaml: {e}")
                sys.exit(1)

    if plan_state.proceeded or reconcile_noop:
        try:
            _run_mcp_lsp_integration(
                scope=scope,
                project_root=_mcp_lsp_project_root,
                existing_lock=_existing_lock,
                lock_path=_lock_path,
                target_decision=target_decision,
                diagnostics=getattr(result, "diagnostics", None),
                logger=logger,
                verbose=verbose,
                effective_allow_executables=getattr(result, "exec_allow_map", None),
                effective_allow_resolved=getattr(result, "exec_allow_resolved", False),
                force=force,
                trust_transitive_mcp=trust_transitive_mcp,
            )
        except RequiredIntegrationError as e:
            logger.error(str(e))
            logger.render_summary()
            sys.exit(1)
        except Exception as e:
            logger.error(f"Error reconciling MCP/LSP servers: {e}")
            logger.render_summary()
            if not verbose:
                logger.info("Run with --verbose for detailed diagnostics.")
            sys.exit(1)

    post_install = getattr(result, "post_install", None)
    if post_install is not None:
        post_install()

    if plan_state.proceeded:
        # Name files cleanup kept (#3701) and other install warnings once.
        update_diagnostics = getattr(result, "diagnostics", None)
        if update_diagnostics is not None and update_diagnostics.has_diagnostics:
            update_diagnostics.render_summary()
        # Report the number of dependencies that actually changed (per the
        # plan), not the total tree re-materialized (result.installed_count).
        # The latter counts unchanged deps that were re-integrated, which
        # contradicts the "N updated" line the plan just printed.
        # installed_count is still the guard for whether anything materialized:
        # a proceeded run that installed nothing reports the no-op outcome even
        # if the plan predicted changes.
        installed = getattr(result, "installed_count", 0)
        changed = len(plan.changed_entries)
        _render_update_completion_summary(
            installed=installed,
            changed=changed,
            revision_pin_updates=revision_pin_updates,
            plan_state=plan_state,
            exec_reconcile_required=_exec_reconcile_required,
            restored_provenance=bool(_restored_provenance),
            logger=logger,
        )

        # Fire post-update lifecycle scripts
        _fire_update_scripts(
            "post-update",
            apm_package=staged_apm_package,
            scope=scope,
            logger=logger,
            verbose=verbose,
        )


def _fire_update_scripts(
    event_name: str,
    *,
    apm_package: Any,
    scope: Any,
    logger: CommandLogger | None,
    verbose: bool,
) -> None:
    """Build a script runner and fire an update lifecycle event.

    Best-effort: all exceptions are swallowed so scripts never block
    the update flow.
    """
    import contextlib

    with contextlib.suppress(Exception):
        from apm_cli.core.lifecycle_scripts import (
            build_runner_from_context,
            event_for_dependencies,
        )

        project_root = None
        pkg_path = getattr(apm_package, "package_path", None)
        if pkg_path is not None:
            project_root = str(pkg_path)

        runner = build_runner_from_context(
            logger=logger,
            verbose=verbose,
            project_root=project_root,
        )

        event = event_for_dependencies(
            event_name,
            apm_package.get_apm_dependencies(),
            scope=scope.value if hasattr(scope, "value") else str(scope),
            working_directory=project_root,
        )

        runner.fire(event_name, event)


__all__ = ["update"]
