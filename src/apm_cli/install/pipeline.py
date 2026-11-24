"""Install pipeline orchestrator.

Extracted from ``apm_cli.commands.install._install_apm_dependencies``
(refactor F2) to keep the Click command module under ~1 000 LOC and
concentrate the phase-call sequence in one import-safe module.

The function ``run_install_pipeline(...)`` is the public entry point.
``commands/install.py`` re-exports it as ``_install_apm_dependencies``
so that every existing ``@patch("apm_cli.commands.install._install_apm_dependencies")``
keeps working without test changes.

Design notes
------------
* Each phase is called via its ``run(ctx)`` entry point.
* Diagnostics, registry config, and managed_files are set up here and
  attached to :class:`InstallContext` *before* the phases that need them.
* Symbols on the ``commands/install`` module that phases access via
  ``_install_mod.X`` stay as re-exports there -- this module does NOT
  duplicate those re-exports.

Source-vs-deploy root convention
--------------------------------
:class:`InstallContext` carries two roots; phases must pick the
correct one or ``apm install --root`` silently produces wrong paths
(the bug surfaces only when ``project_root != source_root``).

* ``ctx.source_root`` -- read sources here (``apm.yml``, ``.apm/``
  primitives, local-path packages).  Equal to ``$PWD`` regardless of
  ``--root``.
* ``ctx.project_root`` / ``ctx.apm_dir`` -- write deploy artifacts
  here (``apm_modules/``, ``apm.lock.yaml``, ``.claude/``, ``.codex/``,
  etc.).  Becomes the ``--root`` target when set.

Convention: a phase that *reads* an existing project file uses
``source_root``; a phase that *writes* anything uses ``project_root``
(or the helper that already does -- e.g. :func:`get_apm_dir`).  When
a new field is added to :class:`InstallContext`, the source-vs-write
side must be an explicit, documented choice -- not implicit.
"""

from __future__ import annotations

import builtins
import contextlib
import time
from typing import TYPE_CHECKING

from apm_cli.claude_plugins.admission import ClaudeNativePluginError
from apm_cli.install.helpers.plan_inputs import confirm_update_plan

from ..agent_plugins.errors import AgentPluginError
from ..codex_plugins.client import CodexNativeError
from ..copilot_plugins.settings import CopilotSettingsCollisionError
from ..deps.egress_route import EgressSurface, is_proxy_only, is_refused
from ..deps.lockfile_write_authority import after_lockfile_commit, commit_point_seam_for_tests
from ..models.dependency.materialization import MaterializationPathCollisionError
from ..models.results import InstallDisposition, InstallResult
from ..utils.console import _rich_error
from ..utils.diagnostics import DiagnosticCollector
from ..utils.path_security import PathTraversalError
from .commit_gate import check_before_lockfile, check_before_release
from .errors import (
    AuthenticationError,
    DirectDependencyError,
    FrozenInstallError,
    InstallFailureAlreadyRendered,
    PreviewIncomplete,
    RequiredIntegrationError,
)
from .failure_residue import record_install_outcome
from .finalization import close_pipeline_attempt
from .outcome import (
    direct_dependency_failed,
    raise_direct_dependency_failure,
    result_from_install_context,
    stop_before_integration,
)
from .pipeline_entry import preview_install as _preview_install
from .source_helpers import (
    lockfile_refresh_command,
    proxy_only_lock_entries,
    ref_update_operation_label,
)
from .transaction import InstallTransaction, transactional_pipeline

if TYPE_CHECKING:
    from ..core.auth import AuthResolver
    from ..core.command_logger import InstallLogger
    from ..core.deployment_ledger import DeploymentMaterializationSession
    from ..core.target_detection import EffectiveTargetDecision
    from .context import InstallContext


# CRITICAL: Shadow Python builtins that share names with Click commands.
# The parent ``commands/install`` module does this; we must do the same
# to avoid NameError when using ``set()``, ``list()``, ``dict()`` below.
set = builtins.set
list = builtins.list
dict = builtins.dict


def _run_phase(name: str, phase, ctx):
    """Invoke ``phase.run(ctx)`` with verbose-only timing (F6, #1116).

    Return the phase result. Timing output is best-effort and must not
    mask the phase's own exception.

    Verbose mode shows ``[i] Phase: <name> -> 1.234s`` so users (and
    CI logs) can locate the phase responsible for a slow install
    without instrumenting individual sources.
    """
    logger = getattr(ctx, "logger", None)
    verbose = bool(getattr(ctx, "verbose", False))
    if not verbose or logger is None:
        return phase.run(ctx)
    started = time.perf_counter()
    try:
        return phase.run(ctx)
    finally:
        elapsed = time.perf_counter() - started
        with contextlib.suppress(Exception):
            logger.verbose_detail(f"Phase: {name} -> {elapsed:.3f}s")


def _run_integration_phase(ctx: InstallContext) -> DeploymentMaterializationSession | None:
    """Bound durable ownership reads to integration, before lockfile persistence."""
    from apm_cli.core.deployment_ledger import (
        DeploymentMaterializationSession,
        retire_receipts_of_missing_projects,
    )
    from apm_cli.integration.skill_adoption import locked_skill_sources

    from .phases import integrate as _integrate_phase
    from .service import InstallService

    session = (
        None
        if ctx.lockfile_only or ctx.dry_run
        else DeploymentMaterializationSession(ctx.project_root, ctx.scope, ctx.existing_lockfile)
    )
    locked = locked_skill_sources(ctx) if session is not None else None
    if session is not None:
        session.locked_sources = locked
        session.verify_only = InstallService.frozen_disposition(ctx).replay
        ctx.old_local_deployed = session.adopt_root_receipts(ctx.old_local_deployed, ctx.targets)
        retire_receipts_of_missing_projects()
    try:
        with (
            contextlib.nullcontext()
            if ctx.lockfile_only
            else ctx.integrators["skill"].ownership_snapshot(
                replay_lockfile=InstallService.frozen_replay_lockfile(ctx),
                materialization_session=session,
            )
        ):
            _run_phase("integrate", _integrate_phase, ctx)
    finally:
        if locked is not None:
            locked.close()
    return session


def _preflight_auth_check(ctx, auth_resolver, verbose: bool) -> None:
    """Verify auth for every distinct (host, org) before write phases.

    Called only when ``update_refs`` is set, so we know the pipeline is
    about to overwrite ``apm.yml``, ``apm.lock.yaml``, and
    ``apm_modules/``.  A single ``git ls-remote`` per cluster catches
    stale tokens before any file is touched.

    For ADO clusters the probe runs through
    :meth:`AuthResolver.try_with_fallback` (``ADO_APM_PAT`` -> ``az`` bearer
    -> path-scoped ``git credential fill``) -- the same chain as the clone path. Without this,
    ``apm install -g`` (which skipped preflight) would succeed but
    ``apm install -g --update`` would fail on the same machine with the
    same creds. See #1212.

    For generic hosts, the probe uses the same transport the real clone
    would use, mirroring :meth:`TransportSelector.select`: SSH only when
    the dep carries an explicit ``ssh://`` scheme; otherwise HTTPS (token
    embedded when available, plain HTTPS for anonymous public deps).
    SSH failures are detected via :func:`is_ssh_auth_failure_signal`;
    HTTPS failures via :func:`apm_cli.core.auth_outcome.classify_auth_outcome`.

    Raises :class:`AuthenticationError` (with ``build_error_context``
    payload) on the first auth failure that survives the fallback.
    """
    import subprocess as _sp

    from ..core.auth_chain import AdoAuthChainExhaustedError, AuthAttemptLog
    from ..core.auth_outcome import AuthAttemptFailed, classify_auth_outcome
    from ..models.dependency.host_virtual import dependency_repository_owner
    from ..utils.git_env import redact_git_diagnostic
    from ..utils.github_host import (
        is_azure_devops_hostname,
        is_github_hostname,
        is_ssh_auth_failure_signal,
    )

    logger = getattr(ctx, "logger", None)

    def _trace(line: str) -> None:
        """Emit a verbose tracing line; best-effort, never raises."""
        if not verbose or logger is None:
            return
        with contextlib.suppress(Exception):
            logger.verbose_detail(line)

    if is_refused(EgressSurface.SOURCE_PROBE):
        return  # Registry-only mode sends no source-host probe; the proxy serves downloads.
    seen: builtins.set = builtins.set()
    for dep in ctx.deps_to_install:
        dep = dep.source_reference
        host = dep.host
        if not host or is_github_hostname(host):
            continue  # github.com uses API probe with unauth fallback
        org = dependency_repository_owner(dep)
        key = (host, dep.port, org)
        if key in seen:
            continue
        seen.add(key)

        dep_ctx = auth_resolver.resolve_for_dep(dep)

        from ..deps.github_downloader import GitHubPackageDownloader

        _dl = GitHubPackageDownloader(auth_resolver=auth_resolver)
        _dl.github_host = host
        is_generic = not is_github_hostname(host) and not is_azure_devops_hostname(host)

        # For generic hosts, mirror TransportSelector.select() when picking
        # the probe transport: SSH only when the dep carries an explicit
        # ssh:// scheme. Shorthand deps (no explicit scheme) default to
        # HTTPS regardless of token presence -- TransportSelector's default
        # is plain HTTPS without a token and authenticated HTTPS with one.
        # Forcing SSH on tokenless generic hosts would break anonymous
        # access to public Gitea/Forgejo deps that have neither an HTTPS
        # token nor a configured SSH key.
        _explicit_scheme = (getattr(dep, "explicit_scheme", None) or "").lower()
        _use_ssh = is_generic and _explicit_scheme == "ssh"

        probe_url = _dl._build_repo_url(
            dep.repo_url,
            use_ssh=_use_ssh,
            dep_ref=dep,
        )
        probe_env = auth_resolver.git_env_for_remote(
            dep_ctx,
            probe_url,
        )

        endpoint = dep_ctx.host_info.display_name
        host_display = endpoint if not org else f"{endpoint}/{org}"

        def _run_ls_remote(url, env):
            # auth-delegated: ADO probes run inside auth_resolver.try_with_fallback;
            # other hosts use the AuthResolver-built probe_env.
            try:
                from ..utils.git_env import git_remote_refs

                return git_remote_refs(
                    url,
                    timeout=30,
                    env=env,
                    options=("--heads", "--exit-code"),
                )
            except _sp.TimeoutExpired:
                return None  # network timeout sentinel; treated as non-auth

        chain_attempts = AuthAttemptLog()
        if dep.is_azure_devops() and not _use_ssh:

            def _ado_probe(_token, git_env, url=probe_url):
                run = _run_ls_remote(url, git_env)
                if run is None:
                    # A timeout is an outcome, not a success: the chain must
                    # not treat it as an accepted credential.
                    raise _sp.TimeoutExpired("git ls-remote", 30)
                if run.returncode != 0:
                    raise AuthAttemptFailed(result=run)
                return run

            try:
                result = auth_resolver.try_with_fallback(
                    host,
                    _ado_probe,
                    org=org,
                    port=dep.port,
                    path=dep.repo_url,
                    host_type=dep.host_type,
                    remote_url=probe_url,
                    attempts=chain_attempts,
                    verbose_callback=_trace if verbose else None,
                )
            except AuthAttemptFailed as failed:
                result = failed.result
            except _sp.TimeoutExpired:
                result = None
            except AdoAuthChainExhaustedError as exhausted:
                _trace(f"Preflight: {host_display} -- auth rejected")
                raise AuthenticationError(
                    f"Authentication failed for {host}",
                    diagnostic_context=(
                        f"\n    {exhausted}\n"
                        + auth_resolver.build_error_context(
                            host,
                            ref_update_operation_label(),
                            org=org,
                            dep_url=dep.repo_url,
                            attempts=exhausted.attempts,
                        )
                        + "\n\n    No files were modified."
                        + "\n    apm.yml, apm.lock.yaml, and apm_modules/ are unchanged."
                    ),
                ) from exhausted
        else:
            result = _run_ls_remote(probe_url, probe_env)

        if result is None:
            continue  # timeout fallthrough -- handled by the real phase

        if result.returncode != 0:
            stderr_text = result.stderr or ""
            if _use_ssh:
                # Generic SSH transport: check SSH-specific failure signals.
                if not is_ssh_auth_failure_signal(stderr_text):
                    continue  # non-auth SSH failure (network, unknown host key) -- defer
                _trace(f"Preflight: {host_display} -- SSH auth rejected")
                raise AuthenticationError(
                    f"SSH authentication failed for {host}",
                    diagnostic_context=(
                        f"    SSH authentication was rejected by {host_display}.\n"
                        f"    Ensure your SSH key is loaded in ssh-agent "
                        f"(ssh-add -l) and that the\n"
                        f"    public key is authorised on the server.\n\n"
                        f"    git output: {redact_git_diagnostic(stderr_text.strip())}\n\n"
                        f"    No files were modified.\n"
                        f"    apm.yml, apm.lock.yaml, and apm_modules/ are unchanged."
                    ),
                )
            else:
                if not classify_auth_outcome(
                    host_info=dep_ctx.host_info,
                    error=stderr_text,
                    credential_presented=bool(dep_ctx.token),
                ).is_auth_failure:
                    continue  # non-auth git failure (network, ref-not-found) -- defer
                _trace(f"Preflight: {host_display} -- auth rejected")
                _diag = auth_resolver.build_error_context(
                    host,
                    ref_update_operation_label(),
                    org=org,
                    dep_url=dep.repo_url,
                    attempts=chain_attempts if dep.is_azure_devops() else None,
                )
                raise AuthenticationError(
                    f"Authentication failed for {host}",
                    diagnostic_context=(
                        _diag
                        + "\n\n    No files were modified."
                        + "\n    apm.yml, apm.lock.yaml, and apm_modules/ are unchanged."
                    ),
                )
        else:
            _trace(f"Preflight: {host_display} -- accepted")


@transactional_pipeline
def run_install_pipeline(  # noqa: C901, PLR0913, PLR0915, RUF100
    apm_package: APMPackage,
    update_refs: bool = False,
    verbose: bool = False,
    only_packages: builtins.list = None,  # noqa: RUF013
    force: bool = False,
    parallel_downloads: int = 4,
    logger: InstallLogger = None,
    scope=None,
    auth_resolver: AuthResolver = None,
    target: str | list[str] | None = None,
    target_decision: EffectiveTargetDecision | None = None,
    allow_insecure: bool = False,
    allow_insecure_hosts=(),
    marketplace_provenance: dict = None,
    protocol_pref=None,
    allow_protocol_fallback: bool | None = None,
    no_policy: bool = False,
    audit_override: str | None = None,
    skill_subset: tuple | None = None,
    skill_subset_from_cli: bool = False,
    legacy_skill_paths: bool = False,
    plan_callback=None,
    refresh: bool = False,
    lockfile_only: bool = False,
    trust_bin: bool | None = None,
    transaction: InstallTransaction | None = None,
    frozen: bool = False,
    trust_transitive_mcp: bool = False,
    lsp_preflight=None,
    install_mcp: bool = True,
    mcp_runtime: str | None = None,
    dry_run: bool = False,
    reconcile_services=None,
):
    """Install APM package dependencies.

    This is the main orchestrator for the install pipeline.  It builds an
    :class:`InstallContext`, then calls each phase module in order:

    1. **resolve** -- dependency resolution + lockfile check
    2. **targets** -- target detection + integrator initialization
    3. **download** -- parallel package pre-download
    4. **integrate** -- sequential integration loop + root primitives
    5. **cleanup** -- orphan cleanup + intra-package stale-file removal
    6. **lockfile** -- generate ``apm.lock``
    7. **finalize** -- emit stats, return :class:`InstallResult`

    Args:
        apm_package: Parsed APM package with dependencies
        update_refs: Whether to update existing packages to latest refs
        verbose: Show detailed installation information
        only_packages: If provided, only install these specific packages
        force: Whether to overwrite locally-authored files on collision
        parallel_downloads: Max concurrent downloads (0 disables parallelism)
        logger: InstallLogger for structured output
        scope: InstallScope controlling project vs user deployment
        auth_resolver: Shared auth resolver for caching credentials
        target: Explicit target override from --target CLI flag
        target_decision: Canonical effective target decision when already resolved
        allow_insecure: Whether direct HTTP dependencies are approved
        allow_insecure_hosts: Extra approved hosts for transitive HTTP dependencies
        marketplace_provenance: Marketplace provenance data for packages
    """
    # Late import: the ``APM_DEPS_AVAILABLE`` guard in commands/install.py
    # already prevents callers from reaching here when deps are missing, but
    # keep the check as a defensive belt-and-suspenders measure.
    try:
        from ..deps.lockfile import LockFile, get_lockfile_path
    except ImportError:
        raise RuntimeError("APM dependency system not available")  # noqa: B904

    # Reset process-scoped perf counters and discovery memo so that
    # numbers / cache hits from earlier pipeline runs (tests, REPL,
    # long-lived processes) do not bleed into this install. See #1533.
    from ..primitives.discovery import clear_discovery_cache
    from ..utils import perf_stats as _perf_stats

    _perf_stats.reset()
    clear_discovery_cache()

    from ..core.scope import InstallScope, get_apm_dir, get_deploy_root, get_source_root

    if scope is None:
        scope = InstallScope.PROJECT

    from .phases.targets import reject_undeployable_manifest_targets

    reject_undeployable_manifest_targets(apm_package, logger)  # before any write

    apm_deps = apm_package.get_apm_dependencies()
    dev_apm_deps = apm_package.get_dev_apm_dependencies()
    all_apm_deps = apm_deps + dev_apm_deps

    project_root = get_deploy_root(scope)  # write target
    source_root = get_source_root(scope)  # source reads (apm.yml, .apm/)
    apm_dir = get_apm_dir(scope)

    # Check whether the source root has local .apm/ primitives (#714).
    # Sources resolve from $PWD even when --root redirects writes, so the
    # check uses source_root rather than project_root.
    from apm_cli.install.phases.local_content import _project_has_root_primitives

    _root_has_local_primitives = _project_has_root_primitives(source_root)

    # Read old local deployed files from the existing lockfile so the
    # post-deps-local phase can run stale cleanup even when no current
    # local content exists (e.g. .apm/ was deleted but old files remain).
    _old_local_deployed: builtins.list = []
    _early_lockfile = LockFile.read(get_lockfile_path(apm_dir)) if apm_dir else None
    if _early_lockfile:
        _old_local_deployed = builtins.list(_early_lockfile.local_deployed_files)

    # Detect orphan APM dependencies in the previous lockfile so we don't
    # short-circuit cleanup when the user removed every dep from apm.yml.
    # Without this check, deleting all deps would leave their deployed files
    # behind because the cleanup phase never runs.
    from apm_cli.deps.lockfile import _SELF_KEY

    _has_orphan_deps = bool(
        _early_lockfile and any(k != _SELF_KEY for k in _early_lockfile.dependencies)
    )

    from .helpers.no_work import (
        is_no_work_install,
        local_rows_need_reconciling,
        retained_local_work,
    )

    if not dry_run and is_no_work_install(
        all_apm_deps=all_apm_deps,
        root_has_local_primitives=_root_has_local_primitives,
        local_cleanup_pending=(
            not lockfile_only
            and local_rows_need_reconciling(
                _early_lockfile,
                project_root,
                apm_package,
                target=target,
                scope=scope,
                target_decision=target_decision,
            )
        ),
        has_orphan_deps=_has_orphan_deps,
        lockfile_only=lockfile_only,
        apm_dir=apm_dir,
        apm_package=apm_package,
        logger=logger,
    ):
        from ..native_plugins.claude_marketplaces import release_undeclared_marketplaces

        # Nothing is declared: a registry entry a failed uninstall left converges (#5258);
        # `apm lock` writes only the lockfile.
        if not lockfile_only:
            release_undeclared_marketplaces(project_root, scope, logger)
        return InstallResult()

    if target_decision is None:
        from apm_cli.core.target_detection import resolve_effective_target_decision
        from apm_cli.models.apm_package import package_target_selection

        defer_auto_detection = (
            lockfile_only
            or dry_run
            or bool(plan_callback is not None and logger is not None and logger.dry_run)
        )
        target_decision = resolve_effective_target_decision(
            project_root,
            explicit_target=target,
            manifest_target=package_target_selection(apm_package),
            user_scope=scope is InstallScope.USER,
            auto_detect=not defer_auto_detection,
        )

    # ------------------------------------------------------------------
    # Build InstallContext from function args + computed state
    # ------------------------------------------------------------------
    from ..policy._constraint_pinning import collect_manifest_declarations
    from .context import InstallContext

    ctx = InstallContext(
        trust_transitive_mcp=trust_transitive_mcp,
        install_mcp=install_mcp,
        mcp_runtime=mcp_runtime,
        project_root=project_root,
        apm_dir=apm_dir,
        source_root=source_root,
        apm_package=apm_package,
        update_refs=update_refs,
        frozen=frozen,
        verbose=verbose,
        only_packages=only_packages,
        force=force,
        parallel_downloads=parallel_downloads,
        logger=logger,
        scope=scope,
        auth_resolver=auth_resolver,
        target_override=target_decision.value,
        target_decision=target_decision,
        target_override_source=target_decision.source,
        allow_insecure=allow_insecure,
        allow_insecure_hosts=allow_insecure_hosts,
        marketplace_provenance=marketplace_provenance,
        protocol_pref=protocol_pref,
        allow_protocol_fallback=allow_protocol_fallback,
        all_apm_deps=all_apm_deps,
        declared_direct_refs=collect_manifest_declarations(apm_package),
        root_has_local_primitives=_root_has_local_primitives,
        old_local_deployed=_old_local_deployed,
        no_policy=no_policy,
        audit_override=audit_override,
        skill_subset=skill_subset,
        skill_subset_from_cli=skill_subset_from_cli,
        early_lockfile=_early_lockfile,
        legacy_skill_paths=legacy_skill_paths,
        refresh=refresh,
        lockfile_only=lockfile_only,
        dry_run=dry_run,
        trust_bin=trust_bin,
        transaction=transaction,
        lsp_preflight=lsp_preflight,
        reconcile_services=reconcile_services,
    )
    transaction.track_created_target_roots(ctx.created_target_roots)

    # ------------------------------------------------------------------
    # Workstream B (#1116): one Live region per major phase boundary.
    # When the controller is disabled (CI, dumb terminal,
    # ``APM_PROGRESS=never``) every method is a no-op so the surrounding
    # phases stay valid without per-call gating.
    # ------------------------------------------------------------------
    from apm_cli.utils.install_tui import InstallTui

    ctx.tui = InstallTui()

    # ------------------------------------------------------------------
    # Phase 1: Resolve dependencies
    # ------------------------------------------------------------------
    from .commit_gate import check_before_resolve
    from .phases import resolve as _resolve_phase

    check_before_resolve(ctx, _early_lockfile)
    refused = False
    ctx.tui.__enter__()
    try:
        ctx.tui.start_phase("resolve", total=len(all_apm_deps) or 1)
        _run_phase("resolve", _resolve_phase, ctx)
    except PreviewIncomplete:
        refused = True  # --dry-run only: the preview reports ctx.preview_errors
    finally:
        ctx.tui.__exit__()

    if dry_run:
        return _preview_install(ctx, run_gates=not refused)

    if (
        not ctx.deps_to_install
        and not ctx.root_has_local_primitives
        and not _has_orphan_deps
        and not retained_local_work(ctx, _early_lockfile, lockfile_only)
    ):
        if logger:
            logger.nothing_to_install(
                lockfile_present=_early_lockfile is not None,
                update_mode=update_refs,
            )
        return InstallResult()

    # ------------------------------------------------------------------
    # Plan-gate checkpoint (#1203): show the user what install/update
    # is about to do and let them confirm.  Invoked AFTER resolve so we
    # have ``ctx.deps_to_install`` with resolved refs, BEFORE downloads
    # begin so a "no" answer cancels cleanly without touching the
    # cache.
    #
    # Only ``apm update`` passes a callback today; all other entry
    # points pass ``None`` and the checkpoint is a no-op.  The TUI is
    # already exited (see the ``finally`` above), so callbacks can
    # write directly to stdout / call ``click.confirm`` without
    # collision.
    # ------------------------------------------------------------------
    if not confirm_update_plan(ctx, _early_lockfile, plan_callback):
        transaction.rollback()
        return InstallResult(
            disposition=InstallDisposition.CANCELLED,
            target_decision=target_decision,
        )

    if target_decision.value is None and scope is InstallScope.PROJECT and not lockfile_only:
        from apm_cli.core.target_detection import resolve_effective_target_decision
        from apm_cli.models.apm_package import package_target_selection

        target_decision = resolve_effective_target_decision(
            project_root,
            explicit_target=target,
            manifest_target=package_target_selection(apm_package),
        )
        ctx.target_decision = target_decision
        ctx.target_override = target_decision.value
        ctx.target_override_source = target_decision.source

    ctx.tui.__enter__()
    try:
        # --------------------------------------------------------------
        # Phase 1.5: Policy enforcement gate (#827)
        # Runs after resolve (deps_to_install populated) and before
        # targets (denied deps never reach integration).
        # PolicyViolationError halts the pipeline cleanly.
        # --------------------------------------------------------------

        # Populate direct MCP deps from the manifest so the policy gate
        # can enforce MCP allow/deny rules on them (S2 fix).
        ctx.direct_mcp_deps = apm_package.get_all_mcp_dependencies()

        # Populate direct LSP deps from the manifest for LSP integration.
        ctx.direct_lsp_deps = apm_package.get_lsp_dependencies()

        from .phases import policy_gate as _policy_gate_phase
        from .phases.policy_gate import PolicyViolationError

        try:
            _run_phase("policy_gate", _policy_gate_phase, ctx)
        except PolicyViolationError:
            raise  # re-raise through the outer except -> RuntimeError wrapper

        # --------------------------------------------------------------
        # Phase 2: Target detection + integrator initialization.
        # Skipped in lockfile_only mode -- no primitives are deployed.
        #
        # Phase 2.1 (native Copilot plugin registration capability) shares
        # this guard: it publishes the capability so the Agent Plugin
        # deployment boundary admits a verified plugin (#2703); lockfile_only skips it.
        # --------------------------------------------------------------
        if not lockfile_only:
            from .phases import claude_plugins, codex_plugins, copilot_plugins, services
            from .phases import targets as _targets_phase

            _run_phase("targets", _targets_phase, ctx)
            _run_phase("copilot_activate", copilot_plugins.ActivatePhase, ctx)

        # --------------------------------------------------------------
        # Phase 2.5: Post-targets target-aware policy check (#827)
        # Runs even in lockfile_only mode so that --target policy
        # constraints are enforced during resolution-only runs.
        # --------------------------------------------------------------
        from .phases import policy_target_check as _policy_target_check_phase

        try:
            _run_phase("policy_target_check", _policy_target_check_phase, ctx)
        except PolicyViolationError:
            raise  # re-raise through the outer except -> RuntimeError wrapper

        # --------------------------------------------------------------
        # Phase 1.75: Auth pre-flight for --update mode (#1015)
        # Skipped in lockfile_only mode -- no writes to apm.yml occur.
        # --------------------------------------------------------------
        if update_refs and ctx.deps_to_install and not lockfile_only:
            # Use ctx.auth_resolver: resolve phase guarantees it is set
            # (resolve.py:91-92), whereas the local ``auth_resolver``
            # parameter can still be None for callers that omit it.
            _preflight_auth_check(ctx, ctx.auth_resolver, verbose)

        # --------------------------------------------------------------
        # Seam: read phase outputs into locals for remaining code.
        # This minimises diff below -- subsequent phases (download,
        # integrate, cleanup, lockfile) continue using bare-name locals.
        # Future S-phases will fold them into the context one by one.
        # --------------------------------------------------------------
        transitive_failures = ctx.transitive_failures

        # Reuse the logger's DiagnosticCollector when available so that
        # diagnostics recorded earlier in the pipeline (e.g. warn-mode
        # policy violations pushed by ``logger.policy_violation()`` from
        # the policy_gate phase, which runs BEFORE this point) surface
        # in the final install summary.  Block-mode violations also flow
        # through here, but the pipeline aborts via PolicyViolationError
        # before render_summary() runs, so the inline ``[x]`` print is
        # what users see -- no duplication.
        diagnostics = (
            logger.diagnostics if logger is not None else DiagnosticCollector(verbose=verbose)
        )

        # Drain transitive failures collected during resolution into diagnostics
        for dep_display, fail_msg in transitive_failures:
            diagnostics.error(fail_msg, package=dep_display)

        # Collect installed packages for lockfile generation
        from ..deps.installed_package import InstalledPackage
        from ..deps.lockfile import LockFile, get_lockfile_path
        from ..deps.registry_proxy import RegistryConfig

        installed_packages: builtins.list[InstalledPackage] = []

        registry_config = RegistryConfig.from_env()
        proxy_only = registry_config is not None and is_proxy_only(EgressSurface.SOURCE_ARCHIVE)

        # Build managed_files from existing lockfile for collision detection
        managed_files = builtins.set()
        existing_lockfile = LockFile.read(get_lockfile_path(apm_dir)) if apm_dir else None
        if existing_lockfile:
            for dep in existing_lockfile.dependencies.values():
                managed_files.update(dep.deployed_files)

            # Conflict: registry-only mode requires all locked deps to route
            # through the configured proxy. Deps locked to direct VCS sources
            # (github.com, GHE Cloud, GHES) are incompatible. A ref update
            # re-resolves its selection through the registry; only the
            # entries it keeps as-is are checked.
            if proxy_only:
                conflicts = registry_config.validate_lockfile_deps(
                    proxy_only_lock_entries(existing_lockfile, update_refs=update_refs, ctx=ctx)
                )
                if conflicts:
                    _rich_error(
                        "PROXY_REGISTRY_ONLY is set but the lockfile contains "
                        "dependencies locked to direct VCS sources:"
                    )
                    for dep in conflicts[:10]:
                        host = dep.host or "github.com"
                        name = dep.repo_url
                        if dep.virtual_path:
                            name = f"{name}/{dep.virtual_path}"
                        _rich_error(f"  - {name} (host: {host})")
                    _rich_error(
                        f"Run '{lockfile_refresh_command(scope)}' to re-resolve "
                        "through the registry."
                    )
                    raise InstallFailureAlreadyRendered(
                        "Proxy-only lockfile contains direct VCS dependencies"
                    )

            # Supply chain warning: registry-proxy entries without a
            # content_hash cannot be verified on re-install.
            if proxy_only and not update_refs:
                missing = registry_config.find_missing_hashes(
                    builtins.list(existing_lockfile.dependencies.values())
                )
                if missing:
                    diagnostics.warn(
                        "The following registry-proxy dependencies have no "
                        f"content_hash in the lockfile. Run '{lockfile_refresh_command(scope)}' "
                        "to populate hashes for tamper detection.",
                        package="lockfile",
                    )
                    for dep in missing[:10]:
                        name = dep.repo_url
                        if dep.virtual_path:
                            name = f"{name}/{dep.virtual_path}"
                        diagnostics.warn(
                            f"  - {name} (host: {dep.host})",
                            package="lockfile",
                        )

        from .local_edits import hint_local_edits_before_install

        hint_local_edits_before_install(ctx, existing_lockfile)

        # Normalize path separators once for O(1) lookups in check_collision
        from ..integration.base_integrator import BaseIntegrator

        managed_files = BaseIntegrator.normalize_managed_files(managed_files)

        # --------------------------------------------------------------
        # Phase 4 (#171): Parallel package pre-download
        # --------------------------------------------------------------
        from .phases import download as _download_phase

        ctx.tui.start_phase("download", total=len(ctx.deps_to_install) or 1)
        _run_phase("download", _download_phase, ctx)

        # --------------------------------------------------------------
        # Phase 5: Sequential integration loop + root primitives
        # --------------------------------------------------------------
        # Populate ctx with locals needed by the integrate phase.
        ctx.diagnostics = diagnostics
        ctx.registry_config = registry_config
        ctx.managed_files = managed_files
        ctx.installed_packages = installed_packages

        # Apply barrier (#3874): a failed direct dependency integrates nothing.
        if direct_dependency_failed(ctx):
            return stop_before_integration(ctx, diagnostics)

        ctx.tui.start_phase("integrate", total=len(ctx.deps_to_install) or 1)
        transaction.journal_deploy_writes(ctx)
        materialization_session = _run_integration_phase(ctx)

        # A direct dependency failing integration fails the install; transitive ones continue.
        if ctx.direct_dep_failed:
            raise_direct_dependency_failure(ctx)

        precommit_result = result_from_install_context(ctx)
        if precommit_result.disposition is InstallDisposition.FAILED:
            return precommit_result

        # Update .gitignore only for project-scoped installs, not in lockfile_only mode.
        if scope == InstallScope.PROJECT and not lockfile_only:
            from apm_cli.install.helpers.gitignore import update_gitignore_for_apm_modules

            update_gitignore_for_apm_modules(logger=logger)
        elif verbose and logger is not None and not lockfile_only:
            logger.verbose_detail("Skipping .gitignore update (global scope install).")

        # ------------------------------------------------------------------
        # Phase: Orphan cleanup + intra-package stale-file cleanup.
        # Skipped in lockfile_only mode -- no files were deployed.
        # ------------------------------------------------------------------
        if not lockfile_only:
            from .phases import cleanup as _cleanup_phase

            _run_phase("cleanup", _cleanup_phase, ctx)

        # Phase: Skill path auto-migration (#737). Skipped in lockfile_only mode.
        if not ctx.legacy_skill_paths and ctx.existing_lockfile and not lockfile_only:
            from .skill_path_migration import run_skill_path_migration

            run_skill_path_migration(ctx)

        # Generate apm.lock for reproducible installs (T4: lockfile generation)
        from .phases.lockfile import LockfileBuilder

        # The lockfile is staged in its writer scope (#4086): the commit gate
        # checks it before that scope commits it (install/commit_gate.py).
        LockfileBuilder(ctx).build_and_save()
        check_before_lockfile(ctx, LockFile.read(get_lockfile_path(ctx.apm_dir)))

        # ------------------------------------------------------------------
        # Phase: Post-deps local .apm/ content.
        # Skipped in lockfile_only mode -- no file deployment occurred.
        # ------------------------------------------------------------------
        if not lockfile_only:
            from .phases import post_deps_local as _post_deps_local_phase

            _run_phase("post_deps_local", _post_deps_local_phase, ctx)
            check_before_release(ctx, LockFile.read(get_lockfile_path(ctx.apm_dir)))
            if materialization_session is not None:
                after_lockfile_commit(
                    lambda: materialization_session.bind_committed_claims(
                        LockFile.read(get_lockfile_path(ctx.apm_dir))
                    )
                )

        # ------------------------------------------------------------------
        # Phase: Optional install-time content audit (external_scanners flag).
        # Skipped in lockfile_only mode -- no files were deployed.
        # ------------------------------------------------------------------
        if not lockfile_only:
            from .phases import audit as _audit_phase

            _run_phase("audit", _audit_phase, ctx)
        commit_point_seam_for_tests()
        transaction.release_deploy_targets()

        # ------------------------------------------------------------------
        # Phase: native plugin registration (#2703, #3921), LAST: after the
        # lockfile is canonical and the integrity and content-audit gates allowed
        # the install. MCP/LSP reconciliation runs before every native
        # registration, which a rolled-back reconciliation skips (#4594); native
        # registration's lockfile flush is then the install's single commit (#4329).
        # ------------------------------------------------------------------
        if not lockfile_only:
            _run_phase("services", services, ctx)
            if services.native_registration_allowed(ctx):
                _run_phase("codex_plugins", codex_plugins, ctx)
                _run_phase("claude_plugins", claude_plugins, ctx)

        # Emit verbose integration stats + bare-success fallback + return result
        from .phases import finalize as _finalize_phase

        _perf_stats.render_summary(logger, project_root=str(ctx.project_root))
        return record_install_outcome(ctx, _run_phase("finalize", _finalize_phase, ctx))

    except (AgentPluginError, CodexNativeError, ClaudeNativePluginError):
        # Preserve schema-routing and native-boundary diagnostics verbatim.
        raise
    except AuthenticationError:
        # #1015: surface auth failures cleanly to the user. Same
        # pattern as PolicyViolationError -- re-raise so the typed
        # exception reaches commands/install.py for rendering with
        # build_error_context diagnostics instead of being wrapped
        # into "Failed to resolve APM dependencies: ...".
        raise
    except PolicyViolationError:
        # #832: surface policy violations cleanly to the user.  The
        # outer ``except Exception`` below would otherwise wrap the
        # message into ``RuntimeError("Failed to resolve APM dependencies:
        # Install blocked by org policy ...")`` and the caller in
        # ``commands/install.py`` would wrap it AGAIN as
        # ``"Failed to install APM dependencies: Failed to resolve APM
        # dependencies: Install blocked by org policy ..."``.  Re-raising
        # the typed exception lets the caller render the policy message
        # as-is.
        raise
    except (DirectDependencyError, FrozenInstallError, RequiredIntegrationError):
        # #946: same pattern -- surface the message as-is instead of
        # double-wrapping it through the generic RuntimeError below.
        raise
    except InstallFailureAlreadyRendered:
        raise
    except (PathTraversalError, MaterializationPathCollisionError, CopilotSettingsCollisionError):
        # These actionable failures can occur after resolution and persistence.
        # Preserve their diagnostics rather than misreporting a resolution error.
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to resolve APM dependencies: {e}")  # noqa: B904
    finally:
        close_pipeline_attempt(ctx)
