"""CLI handler for ``apm install <local-bundle-path>`` (issue #1098).

Extracted from :mod:`apm_cli.commands.install` to keep that module under the
architecture invariant LOC budget enforced by
``tests/unit/install/test_architecture_invariants.py``.

The handler owns the imperative deploy path for local bundles -- it does NOT
go through the dependency resolver, MCP machinery, registry, or org-policy
gate.  Local bundles are intentionally a separate code path because they
short-circuit network I/O (proven by the air-gap E2E test).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click


def install_local_bundle(
    *,
    bundle_info,
    bundle_arg: str,
    target,
    global_: bool,
    force: bool,
    dry_run: bool,
    verbose: bool,
    alias: str | None,
    logger,
    legacy_skill_paths: bool = False,
    rejected_flags: dict[str, object],
) -> None:
    """Deploy a local bundle into project / user scope.

    Validates rejected flags, verifies bundle integrity, resolves install
    targets, deploys files, and persists ``local_deployed_files`` to the
    (project or user) lockfile.  Cleans up tarball extraction on exit.
    """
    from ..bundle.local_bundle import (
        check_target_mismatch,
        verify_bundle_integrity,
    )
    from ..core.scope import InstallScope
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ..install.services import integrate_local_bundle
    from ..integration.targets import resolve_targets

    # Reject incompatible flags with a single consolidated error.  Preserve
    # dict insertion order (matches the order options are declared on the
    # CLI command) rather than alphabetising -- M-cli-3.
    bad = [name for name, value in rejected_flags.items() if value]
    if bad:
        raise click.UsageError(
            "The following flag(s) are not valid with a local bundle install "
            f"({bundle_arg}): {', '.join(bad)}.\n"
            "Local-bundle install is an imperative deploy and does not "
            "interact with the dependency resolver, MCP, registry, or "
            "policy machinery."
        )

    # ``verbose`` is consumed by the InstallLogger on construction (the
    # CLI seam wires it in) -- the handler doesn't need to gate calls on
    # it because logger.verbose_detail self-gates.
    del verbose

    scope = InstallScope.USER if global_ else InstallScope.PROJECT
    project_root = Path.home() if global_ else Path.cwd()

    logger.start(f"Installing local bundle from {bundle_arg}")

    try:
        # Integrity verification (skipped when bundle has no lockfile).
        if bundle_info.lockfile is None:
            logger.warning(
                "Bundle has no apm.lock.yaml -- skipping integrity check. "
                "This bundle was produced by an older APM version."
            )
        else:
            errors = verify_bundle_integrity(bundle_info.source_dir, bundle_info.lockfile)
            if errors:
                logger.error("Bundle integrity check failed:")
                for err in errors:
                    # Plain detail lines -- no [x] symbol prefix per IM3.
                    click.echo(f"  - {err}", err=True)
                raise click.Abort()
            logger.verbose_detail("Bundle integrity verified")

        # Resolve targets and warn on bundle/install target mismatch.
        explicit = target if target else None
        targets = resolve_targets(
            project_root,
            user_scope=global_,
            explicit_target=explicit,
        )
        if not targets:
            logger.warning(
                "No active targets resolved -- nothing will be deployed. "
                "Pass --target to select one explicitly."
            )
            return

        # Apply --legacy-skill-paths override to resolved targets.
        if legacy_skill_paths:
            from ..integration.targets import apply_legacy_skill_paths

            targets = apply_legacy_skill_paths(targets)

        warning = check_target_mismatch(
            bundle_targets=bundle_info.pack_targets,
            install_targets=[t.name for t in targets],
        )
        if warning:
            logger.warning(warning)

        result = integrate_local_bundle(
            bundle_info,
            project_root,
            targets=targets,
            force=force,
            dry_run=dry_run,
            diagnostics=None,
            logger=logger,
            scope=scope,
            alias=alias,
        )

        deployed = result.get("deployed_files", [])
        deployed_hashes = result.get("deployed_file_hashes", {})
        skipped = result.get("skipped", 0)

        if dry_run:
            logger.dry_run_notice(f"Would deploy {len(deployed)} file(s) from local bundle")
            # IM5: surface the file list in default mode (not just verbose)
            # so users see WHICH files would deploy.
            for f in deployed:
                logger.tree_item(f)
            return

        # Persist into project lockfile -- never mutate apm.yml (per design).
        if deployed:
            from ..deps.lockfile import migrate_lockfile_if_needed

            migrate_lockfile_if_needed(project_root)
            lockfile_path = get_lockfile_path(project_root)
            lockfile = LockFile.read(lockfile_path) or LockFile()
            existing = set(lockfile.local_deployed_files)
            existing.update(deployed)
            lockfile.local_deployed_files = sorted(existing)
            existing_hashes = dict(lockfile.local_deployed_file_hashes)
            existing_hashes.update(deployed_hashes)
            lockfile.local_deployed_file_hashes = existing_hashes

            # Auto-migrate legacy per-client skill paths (#737).
            # After deploying new .agents/skills/ files, detect and clean up
            # any legacy paths (e.g. .github/skills/) still recorded in the
            # lockfile from a previous --legacy-skill-paths install.
            if not legacy_skill_paths:
                from ..utils.console import _rich_error, _rich_info
                from .skill_path_migration import (
                    COLLISION_DETAIL_TEMPLATE,
                    COLLISION_HEADER_TEMPLATE,
                    COLLISION_HINT,
                    MIGRATION_SUMMARY_TEMPLATE,
                )
                from .skill_path_migration import (
                    check_collisions as _check_coll,
                )
                from .skill_path_migration import (
                    detect_legacy_skill_deployments as _detect_legacy,
                )
                from .skill_path_migration import (
                    execute_migration as _exec_mig,
                )

                _plans = _detect_legacy(lockfile, project_root)
                if _plans:
                    _colls = _check_coll(_plans, project_root)
                    if _colls:
                        # H2: collision is an error.
                        _rich_error(
                            COLLISION_HEADER_TEMPLATE.format(count=len(_colls)),
                            symbol="error",
                        )
                        # M2: enumerate each collision (parity with pipeline).
                        for _plan in _plans:
                            for _cd in _colls:
                                if _plan.dst_path in _cd:
                                    _rich_error(
                                        COLLISION_DETAIL_TEMPLATE.format(
                                            dst_path=_plan.dst_path,
                                            src_path=_plan.src_path,
                                            dep_name=_plan.dep_name,
                                        ),
                                        symbol="error",
                                    )
                                    break
                        _rich_info(COLLISION_HINT, symbol="info")
                    else:
                        _mig_result = _exec_mig(_plans, lockfile, project_root)
                        _total = len(_mig_result.deleted) + len(_mig_result.skipped_no_file)
                        if _total:
                            _rich_info(
                                MIGRATION_SUMMARY_TEMPLATE.format(count=_total),
                                symbol="info",
                            )
                        if getattr(logger, "verbose", False) and _mig_result.deleted:
                            for _dp in _mig_result.deleted:
                                _rich_info(f"  removed {_dp}", symbol="info")

            lockfile.write(lockfile_path)

        msg = f"Installed {len(deployed)} file(s) from local bundle"
        if skipped:
            msg += f" ({skipped} skipped)"
        logger.success(msg)

    finally:
        # Tarball cleanup (caller-owned per LocalBundleInfo contract).
        if bundle_info.temp_dir is not None and bundle_info.temp_dir.exists():
            shutil.rmtree(bundle_info.temp_dir, ignore_errors=True)
