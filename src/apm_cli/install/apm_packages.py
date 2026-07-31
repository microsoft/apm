"""APM package install pipeline and post-install summary helpers."""

import builtins

from apm_cli.constants import APM_YML_FILENAME, InstallMode
from apm_cli.install.errors import (
    AuthenticationError,
    FrozenInstallError,
    InstallFailureAlreadyRendered,
    PolicyViolationError,
)
from apm_cli.install.insecure_policy import InsecureDependencyPolicyError


def _install_apm_packages(ctx, outcome):
    """Execute the APM + transitive MCP installation pipeline.

    Parses ``apm.yml``, installs APM dependencies, collects and installs
    transitive MCP servers, and handles lockfile updates.

    Args:
        ctx: :class:`InstallContext` with configuration and environment.
        outcome: ``_ValidationOutcome`` from package validation (may be
            ``None`` when no explicit packages were passed).

    Returns:
        Tuple of ``(apm_count, mcp_count, lsp_count, apm_diagnostics)``.
    """
    # RULE B: late import so @patch("apm_cli.commands.install.<x>") intercepts
    # calls to APMPackage, LockFile, _install_apm_dependencies, _maybe_rollback_manifest,
    # _rich_*, _check_insecure_dependencies, migrate_lockfile_if_needed,
    # _frozen_install_tip,
    # get_lockfile_path, _project_has_root_primitives, APM_DEPS_AVAILABLE.
    import apm_cli.commands.install as _m

    logger = ctx.logger

    logger.resolution_start(
        to_install_count=len(ctx.only_packages or []) if ctx.packages else 0,
        lockfile_count=0,  # Refined later inside _install_apm_dependencies
    )

    # Parse apm.yml to get both APM and MCP dependencies
    try:
        apm_package = _m.APMPackage.from_apm_yml(ctx.manifest_path)
    except Exception as e:
        logger.error(f"Failed to parse {ctx.manifest_display}: {e}")
        raise InstallFailureAlreadyRendered from e

    apm_deps, dev_apm_deps = (
        apm_package.get_apm_dependencies(),
        apm_package.get_dev_apm_dependencies(),
    )
    prod_mcp_deps, dev_mcp_deps, mcp_deps = (
        apm_package.get_mcp_dependencies(),
        apm_package.get_dev_mcp_dependencies(),
        apm_package.get_all_mcp_dependencies(),
    )

    logger.verbose_detail(
        f"Parsed {APM_YML_FILENAME}: {len(apm_deps)} APM deps, "
        f"{len(prod_mcp_deps)} MCP deps"
        + (f", {len(dev_apm_deps)} dev APM deps" if dev_apm_deps else "")
        + (f", {len(dev_mcp_deps)} dev MCP deps" if dev_mcp_deps else "")
    )

    has_any_apm_deps = bool(apm_deps) or bool(dev_apm_deps)

    all_apm_deps = builtins.list(apm_deps) + builtins.list(dev_apm_deps)
    _m._check_insecure_dependencies(all_apm_deps, ctx.allow_insecure, logger)

    if ctx.frozen is True:
        from apm_cli.install.request import InstallRequest
        from apm_cli.install.service import InstallService

        InstallService.enforce_frozen(
            InstallRequest(
                apm_package=apm_package,
                frozen=True,
                scope=ctx.scope,
                trust_transitive_mcp=ctx.trust_transitive_mcp,
            )
        )

    # Determine what to install based on install mode
    should_install_apm = ctx.install_mode != InstallMode.MCP
    should_install_mcp = ctx.install_mode != InstallMode.APM
    should_install_lsp = should_install_mcp

    # Show what will be installed if dry run
    if ctx.dry_run:
        # -- W2-dry-run (#827): policy preflight in preview mode --
        # Runs discovery + checks against direct manifest deps (not
        # resolved/transitive -- dry-run does not run the resolver).
        # Block-severity violations render as "Would be blocked by
        # policy" without raising.  Documented limitation: transitive
        # deps are NOT evaluated since the resolver does not run.
        from apm_cli.policy.install_preflight import run_policy_preflight as _dr_preflight

        _dr_preflight(
            project_root=ctx.project_root,
            apm_deps=builtins.list(apm_deps) + builtins.list(dev_apm_deps),
            mcp_deps=mcp_deps if should_install_mcp else None,
            no_policy=ctx.no_policy,
            logger=logger,
            dry_run=True,
        )

        from apm_cli.install.presentation.dry_run import render_and_exit

        render_and_exit(
            logger=logger,
            should_install_apm=should_install_apm,
            apm_deps=apm_deps,
            mcp_deps=mcp_deps,
            dev_apm_deps=dev_apm_deps,
            should_install_mcp=should_install_mcp,
            update=ctx.update,
            only_packages=ctx.only_packages,
            apm_dir=ctx.apm_dir,
        )
        return 0, 0, 0, None  # render_and_exit exits; this line is defensive

    # Install APM dependencies first (if requested)
    apm_count = 0

    # Migrate legacy apm.lock -> apm.lock.yaml if needed (one-time, transparent)
    _m.migrate_lockfile_if_needed(ctx.apm_dir)

    old_mcp_servers: builtins.set = builtins.set()
    old_mcp_configs: builtins.dict = {}
    old_mcp_provenance: builtins.dict = {}
    old_mcp_target_servers: builtins.dict | None = None
    old_mcp_target_servers_present: bool = True
    _lock_path = _m.get_lockfile_path(ctx.apm_dir)
    _existing_lock = _m.LockFile.read(_lock_path)
    if _existing_lock:
        old_mcp_servers = builtins.set(_existing_lock.mcp_servers)
        old_mcp_configs = builtins.dict(_existing_lock.mcp_configs)
        old_mcp_provenance = builtins.dict(_existing_lock.mcp_config_provenance)
        old_mcp_target_servers = builtins.dict(_existing_lock.mcp_target_servers)
        old_mcp_target_servers_present = _existing_lock._mcp_target_servers_present

    # Enter the APM install path when there are deps, local .apm/ primitives
    # (#714), OR orphan deps in the lockfile to clean up (manifest emptied).
    from apm_cli.core.scope import InstallScope
    from apm_cli.core.scope import get_deploy_root as _get_deploy_root
    from apm_cli.deps.lockfile import _SELF_KEY as _LOCK_SELF_KEY

    _cli_project_root = _get_deploy_root(ctx.scope)
    _has_orphan_deps_in_lock = bool(
        _existing_lock
        and not has_any_apm_deps
        and any(k != _LOCK_SELF_KEY for k in _existing_lock.dependencies)
    )
    apm_diagnostics = None
    if should_install_apm and (
        has_any_apm_deps
        or _m._project_has_root_primitives(_cli_project_root)
        or _has_orphan_deps_in_lock
    ):
        if not _m.APM_DEPS_AVAILABLE:
            logger.error("APM dependency system not available")
            logger.progress(f"Import error: {_m._APM_IMPORT_ERROR}")
            raise InstallFailureAlreadyRendered

        try:
            # If specific packages were requested, only install those
            # Otherwise install all from apm.yml.
            # `only_packages` was computed above so the dry-run preview
            # and the actual install share one canonical list.
            install_result = _m._install_apm_dependencies(
                apm_package,
                ctx.update,
                ctx.verbose,
                ctx.only_packages,
                force=ctx.force,
                parallel_downloads=ctx.parallel_downloads,
                logger=logger,
                scope=ctx.scope,
                auth_resolver=ctx.auth_resolver,
                target=ctx.target,
                allow_insecure=ctx.allow_insecure,
                allow_insecure_hosts=ctx.allow_insecure_hosts,
                marketplace_provenance=(
                    outcome.marketplace_provenance if ctx.packages and outcome else None
                ),
                protocol_pref=ctx.protocol_pref,
                allow_protocol_fallback=ctx.allow_protocol_fallback,
                no_policy=ctx.no_policy,
                audit_override=ctx.audit_override,
                legacy_skill_paths=ctx.legacy_skill_paths,
                trust_transitive_mcp=ctx.trust_transitive_mcp,
                frozen=ctx.frozen,
                plan_callback=ctx.plan_callback,
                skill_subset=ctx.skill_subset,
                skill_subset_from_cli=ctx.skill_subset_from_cli,
                refresh=ctx.refresh,
                transaction=ctx.transaction,
            )
            from apm_cli.models.results import InstallDisposition, InstallResult

            if not isinstance(install_result, InstallResult):
                install_result = InstallResult(
                    installed_count=int(getattr(install_result, "installed_count", 0)),
                    diagnostics=getattr(install_result, "diagnostics", None),
                )
            apm_count = install_result.installed_count
            apm_diagnostics = install_result.diagnostics
            # Early-exit: if the install was CANCELLED (or otherwise not a
            # success), skip MCP/LSP integration entirely -- there is nothing
            # to wire up and run_mcp_integration() would unpack an empty tuple.
            if install_result.disposition not in {
                InstallDisposition.SUCCESS,
                InstallDisposition.PARTIAL_SUCCESS,
            }:
                ctx.install_result = install_result
                return apm_count, 0, 0, apm_diagnostics
        except InsecureDependencyPolicyError:
            raise
        except AuthenticationError as e:
            # #1015: render auth diagnostics on the DEFAULT path (not --verbose).
            logger.error(str(e))
            if e.diagnostic_context:
                logger.error_detail(e.diagnostic_context)
            logger.info("Tip: run 'apm doctor' to diagnose auth and connectivity.")
            raise InstallFailureAlreadyRendered(str(e)) from e
        except FrozenInstallError as e:
            logger.error(str(e))
            for reason in e.reasons:
                logger.error_detail(reason)
            logger.info(_m._frozen_install_tip(e))
            raise InstallFailureAlreadyRendered(str(e)) from e
        except InstallFailureAlreadyRendered:
            raise
        except Exception as e:
            # #832: surface PolicyViolationError verbatim (no double-nesting).
            msg = (
                str(e)
                if isinstance(e, PolicyViolationError)
                else f"Failed to install APM dependencies: {e}"
            )
            logger.error(msg)
            if not ctx.verbose:
                logger.progress("Run with --verbose for detailed diagnostics")
            raise InstallFailureAlreadyRendered(msg) from e
    elif should_install_apm and not has_any_apm_deps:
        logger.verbose_detail("No APM dependencies found in apm.yml")

    # When --update is used, package files on disk may have changed.
    # Clear the parse cache so transitive MCP collection reads fresh data.
    if ctx.update:
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()

    from apm_cli.core.scope import get_modules_dir
    from apm_cli.install.mcp import run_mcp_integration
    from apm_cli.policy.install_preflight import PolicyBlockError as _MCPPolicyBlockError

    apm_modules_path = get_modules_dir(ctx.scope)
    try:
        mcp_count, mcp_apm_config = run_mcp_integration(
            apm_package=apm_package,
            mcp_deps=mcp_deps,
            apm_modules_path=apm_modules_path,
            lock_path=_lock_path,
            old_mcp_servers=old_mcp_servers,
            old_mcp_configs=old_mcp_configs,
            old_mcp_provenance=old_mcp_provenance,
            old_mcp_target_servers=old_mcp_target_servers,
            old_mcp_target_servers_present=old_mcp_target_servers_present,
            project_root=ctx.project_root,
            user_scope=(ctx.scope is InstallScope.USER),
            should_install=should_install_mcp,
            logger=logger,
            diagnostics=apm_diagnostics,
            runtime=ctx.runtime,
            exclude=ctx.exclude,
            trust_transitive_mcp=ctx.trust_transitive_mcp,
            no_policy=ctx.no_policy,
            verbose=ctx.verbose,
            explicit_target=ctx.target,
            scope=ctx.scope,
        )
    except _MCPPolicyBlockError:
        logger.error(
            "MCP server(s) blocked by org policy. "
            "APM packages remain installed; MCP configs were NOT written."
        )
        logger.render_summary()
        raise InstallFailureAlreadyRendered() from None

    # -------------------------------------------------------------------------
    # LSP integration (extracted to install/lsp/integration.py)
    # -------------------------------------------------------------------------
    from apm_cli.install.lsp import LSPIntegrationContext, run_lsp_integration

    lsp_count = run_lsp_integration(
        apm_package=apm_package,
        apm_modules_path=apm_modules_path,
        lock_path=_lock_path,
        existing_lock=_existing_lock,
        project_root=ctx.project_root,
        logger=logger,
        diagnostics=apm_diagnostics,
        ctx=LSPIntegrationContext(
            user_scope=(ctx.scope is InstallScope.USER),
            should_install=should_install_lsp,
            target_context=(mcp_apm_config, ctx.target, ctx.scope),
        ),
    )

    # Local .apm/ content integration is now handled inside the
    # install pipeline (phases/integrate.py + phases/post_deps_local.py,
    # refactor F3).  The duplicate target resolution, integrator
    # initialization, and inline stale-cleanup block that lived here
    # have been removed.

    return apm_count, mcp_count, lsp_count, apm_diagnostics


def _post_install_summary(
    *,
    logger,
    apm_count,
    mcp_count,
    lsp_count=0,
    apm_diagnostics,
    force,
    elapsed_seconds=None,
    result=None,
):
    """Thin shim forwarding to :func:`apm_cli.install.summary.render_post_install_summary`.

    Kept as a module-level alias so existing tests that
    ``@patch("apm_cli.commands.install._post_install_summary")`` continue
    to work after the extraction (microsoft/apm#1116, F5).
    """
    from apm_cli.install.summary import render_post_install_summary

    return render_post_install_summary(
        logger=logger,
        apm_count=apm_count,
        mcp_count=mcp_count,
        lsp_count=lsp_count,
        apm_diagnostics=apm_diagnostics,
        force=force,
        elapsed_seconds=elapsed_seconds,
        result=result,
    )


def _install_apm_dependencies(  # noqa: PLR0913
    apm_package,
    update_refs: bool = False,
    verbose: bool = False,
    only_packages=None,
    force: bool = False,
    parallel_downloads: int = 4,
    logger=None,
    scope=None,
    auth_resolver=None,
    target=None,
    allow_insecure: bool = False,
    allow_insecure_hosts=(),
    marketplace_provenance=None,
    protocol_pref=None,
    allow_protocol_fallback=None,
    no_policy: bool = False,
    audit_override=None,
    skill_subset=None,
    skill_subset_from_cli: bool = False,
    legacy_skill_paths: bool = False,
    trust_transitive_mcp: bool = False,
    frozen: bool = False,
    plan_callback=None,
    refresh: bool = False,
    lockfile_only: bool = False,
    transaction=None,
):
    """Thin wrapper -- builds an :class:`InstallRequest` and delegates to
    :class:`apm_cli.install.service.InstallService`.

    Kept here so that ``@patch("apm_cli.commands.install._install_apm_dependencies")``
    continues to intercept calls from the Click handler.  The service
    itself is the typed Application Service entry point for any future
    programmatic callers.
    """
    # RULE B: access APM_DEPS_AVAILABLE via install module so patches are honoured.
    import apm_cli.commands.install as _m

    if not _m.APM_DEPS_AVAILABLE:
        raise RuntimeError("APM dependency system not available")

    from apm_cli.install.request import InstallRequest
    from apm_cli.install.service import InstallService

    request = InstallRequest(
        apm_package=apm_package,
        update_refs=update_refs,
        verbose=verbose,
        only_packages=only_packages,
        force=force,
        parallel_downloads=parallel_downloads,
        logger=logger,
        scope=scope,
        auth_resolver=auth_resolver,
        target=target,
        allow_insecure=allow_insecure,
        allow_insecure_hosts=allow_insecure_hosts,
        marketplace_provenance=marketplace_provenance,
        protocol_pref=protocol_pref,
        allow_protocol_fallback=allow_protocol_fallback,
        no_policy=no_policy,
        audit_override=audit_override,
        skill_subset=skill_subset,
        skill_subset_from_cli=skill_subset_from_cli,
        legacy_skill_paths=legacy_skill_paths,
        trust_transitive_mcp=trust_transitive_mcp,
        frozen=frozen,
        plan_callback=plan_callback,
        refresh=refresh,
        lockfile_only=lockfile_only,
        transaction=transaction,
    )
    return InstallService().run(request)
