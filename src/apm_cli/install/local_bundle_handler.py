"""CLI handler for ``apm install <local-bundle-path>`` (issue #1098).

Extracted from :mod:`apm_cli.commands.install` to keep that module under the
architecture invariant LOC budget enforced by
``tests/unit/install/test_architecture_invariants.py``.

The handler owns the imperative deploy path for local bundles. It does not go
through the dependency resolver or registry, but it must admit the bundle
through :func:`apm_cli.install.bundle_admission.admit_bundle` (policy and
integrity) before any deployment.
Local bundles remain a separate path because they short-circuit package network
I/O (proven by the air-gap E2E test).

Legacy MCP wiring (#1207): Claude bundles MAY ship a ``.mcp.json`` describing
MCP servers. After the per-target deploy loop, the handler routes those entries
through :func:`MCPIntegrator.install`. Native Agent Plugins fail before this
legacy parser or writer. The bundle's ``.mcp.json`` itself is metadata and is
never deployed.
"""

from __future__ import annotations

import functools
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from ..agent_plugins.constants import COM_MICROSOFT_APM_NAMESPACE
from ..bundle.formats import BundleFormat
from ..core.remediation import BUNDLE_INSTRUCTIONS_STAGED
from .deployed_paths import log_bundle_deployed

if TYPE_CHECKING:
    from apm_cli.models.dependency.mcp import MCPDependency
    from apm_cli.policy.models import LocalBundleSubjects

    from .local_bundle_pending import BundleRecordStage


def install_local_bundle(
    *,
    bundle_info,
    bundle_arg: str,
    target,
    global_: bool,
    force: bool,
    dry_run: bool,
    verbose: bool,
    no_policy: bool,
    alias: str | None,
    logger,
    legacy_skill_paths: bool = False,
    rejected_flags: dict[str, object],
    allow_executables: dict | None = None,
    create_config: bool = True,
    policy_fetch_result=None,
) -> None:
    """Deploy a local bundle into project / user scope.

    Validates rejected flags, verifies bundle integrity, resolves install
    targets, deploys files, and persists ``local_deployed_files`` to the
    (project or user) lockfile.  Cleans up tarball extraction on exit.
    """
    from ..bundle.local_bundle import check_target_mismatch
    from ..core.scope import InstallScope, get_manifest_path, get_source_root, manifest_root
    from ..core.target_detection import (
        EffectiveTargetDecision,
        resolve_manifest_target_decision,
    )
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ..install.services import (
        enforce_agent_plugin_deployment_boundary,
        integrate_local_bundle,
    )
    from ..integration.targets import resolve_targets

    # Reject incompatible flags in one consolidated error, in dict insertion
    # order (the CLI declaration order), not alphabetised -- M-cli-3.
    bad = [name for name, value in rejected_flags.items() if value]
    if bad:
        raise click.UsageError(
            "The following flag(s) are not valid with a local bundle install "
            f"({bundle_arg}): {', '.join(bad)}.\n"
            "Local-bundle install is an imperative deploy and does not "
            "interact with the dependency resolver or registry machinery; "
            "bundle .mcp.json metadata is handled separately."
        )

    # ``verbose`` reaches the InstallLogger at construction (the CLI seam wires
    # it in); logger.verbose_detail self-gates, so the handler never checks it.
    del verbose

    scope = InstallScope.USER if global_ else InstallScope.PROJECT
    project_root = Path.home() if global_ else Path.cwd()
    source_root = get_source_root(scope)

    logger.start(f"Installing local bundle from {bundle_arg}")

    try:
        enforce_agent_plugin_deployment_boundary(bundle_info=bundle_info)

        # Resolve targets before policy preflight so target restrictions apply to
        # the actual deployment set, auto-detected targets included. The bundle never
        # edits apm.yml; its targets still restrict where files land (--target >
        # apm.yml > apm config > auto-detect).
        explicit = resolve_manifest_target_decision(
            project_root,
            manifest_path=get_manifest_path(scope),
            explicit_target=target or None,
            user_scope=global_,
            auto_detect=False,
            create_config=create_config,
        ).value
        targets = resolve_targets(
            project_root,
            user_scope=global_,
            explicit_target=explicit,
            create_config=create_config,
        )
        from .target_hints import emit_disabled_experimental_target_hint

        requested = [explicit] if isinstance(explicit, str) else list(explicit or [])
        disabled_requested = [
            requested_target
            for requested_target in requested
            if emit_disabled_experimental_target_hint(
                requested_target,
                targets,
                logger,
                create_config=create_config,
            )
        ]

        if not targets:
            if disabled_requested:
                logger.info("No files were installed. Enable the experimental flag and retry.")
                return
            logger.warning(
                "No active targets resolved -- nothing will be deployed. "
                "Pass --target to select one explicitly."
            )
            return

        # Apply --legacy-skill-paths before policy: target names stay canonical, paths change.
        if legacy_skill_paths:
            from ..integration.skill_layout import resolve_skill_layout

            targets = resolve_skill_layout(targets, legacy_skill_paths=True)

        from .bundle_admission import (
            admit_bundle,
            bundle_policy_subjects,
            plugin_bundle_integrity,
            refuse_unmanaged_bundle_files,
        )

        policy_subjects = bundle_policy_subjects(bundle_info)
        verify_integrity = plugin_bundle_integrity(bundle_info, logger)
        integrity_verified = False

        def integrity_check(require_hashes: bool) -> None:
            nonlocal integrity_verified
            if not integrity_verified:
                verify_integrity(require_hashes)
                integrity_verified = bundle_info.lockfile is not None

        if bundle_info.lockfile is not None:
            # A bundle with a manifest is verified whatever the policy says: refuse a
            # tampered one before the executables gate offers to approve it (RA-035).
            # admit_bundle reuses this verdict.
            integrity_check(False)

        from ..bundle.local_bundle import bundle_declares_mcp

        bundle_mcp_deps: list[MCPDependency] = (
            _parse_legacy_bundle_mcp_servers(
                bundle_info.source_dir,
                bundle_format=bundle_info.format,
            )
            if bundle_declares_mcp(bundle_info) and bundle_info.source_dir is not None
            else []
        )
        bundle_lsp_deps = (
            _parse_bundle_lsp_servers(bundle_info.source_dir)
            if bundle_info.source_dir is not None
            else []
        )
        bundle_approval_key = _bundle_approval_key(bundle_info, allow_executables)
        bundle_record = _stage_bundle_executables(
            bundle_info,
            str(alias or bundle_info.package_id),
            bundle_approval_key,
            (bundle_mcp_deps, bundle_lsp_deps),
            scope_dir=None if dry_run else _bundle_lock_root(global_),
            create_config=create_config,
            policy_subjects=policy_subjects,
        )
        bundle_mcp_deps = _filter_bundle_executables(
            bundle_mcp_deps,
            bundle_info=bundle_info,
            allow_executables=allow_executables,
            approval_key=bundle_approval_key,
            exec_type="mcp",
            logger=logger,
            user_scope=global_,
        )
        bundle_lsp_deps = _filter_bundle_executables(
            bundle_lsp_deps,
            bundle_info=bundle_info,
            allow_executables=allow_executables,
            approval_key=bundle_approval_key,
            exec_type="lsp",
            logger=logger,
            user_scope=global_,
        )
        deployment_decision = EffectiveTargetDecision(
            [resolved.name for resolved in targets], "bundle deployment targets"
        )
        bundle_policy = admit_bundle(
            policy_root=manifest_root(source_root, user_scope=global_),
            apm_deps=(),
            mcp_deps=bundle_mcp_deps,
            target_decision=deployment_decision,
            integrity_check=integrity_check,
            no_policy=no_policy,
            logger=logger,
            dry_run=dry_run,
            fetch_result=policy_fetch_result,
            bundled_deps=policy_subjects,
        )
        from .commit_gate import judge_bundle_manifest_scripts

        judge_bundle_manifest_scripts(
            bundle_policy, get_manifest_path(scope), dry_run=dry_run, logger=logger
        )

        # Warn on bundle/install target mismatch.
        warning = check_target_mismatch(
            bundle_targets=bundle_info.pack_targets,
            install_targets=[t.name for t in targets],
        )
        if warning:
            logger.warning(warning)

        if bundle_lsp_deps:
            from apm_cli.install.lsp.integration import require_lsp_capable_target
            from apm_cli.integration.lsp_integrator import LSPIntegrator

            target_names = [resolved.name for resolved in targets]
            require_lsp_capable_target(
                LSPIntegrator.supported_target_runtimes(target_names), bundle=True
            )

        deploy = functools.partial(
            integrate_local_bundle,
            bundle_info,
            project_root,
            targets=targets,
            force=force,
            diagnostics=None,
            scope=scope,
            alias=alias,
            allow_executables=allow_executables,
            approval_key=bundle_approval_key,
        )
        refuse_unmanaged_bundle_files(
            bundle_policy, deploy, user_scope=global_, logger=logger, dry_run=dry_run
        )
        result = deploy(dry_run=dry_run, logger=logger, create_config=create_config)

        deployed = result.get("deployed_files", [])
        deployed_hashes = result.get("deployed_file_hashes", {})
        if global_:
            # The user lock resolves bundle claims against $HOME, as audit and
            # --frozen replay read them; absolute claims never resolve there.
            deployed, deployed_hashes = _home_relative_claims(
                deployed, deployed_hashes, project_root
            )
        skipped = result.get("skipped", 0)

        # Issue #1207 D2.b: instructions staged into ``apm_modules/<slug>/.apm/instructions/``
        # for targets without native instructions (opencode, codex, gemini) are compile
        # input, never deployment claims (#3705); a compile hint says why AGENTS.md /
        # GEMINI.md do not show them.
        staged_instructions = result.get("staged_files", [])
        # Issue #1207 D2.c: legacy Claude bundle MCP metadata goes through
        # MCPIntegrator.install; native Agent Plugins fail at the deployment boundary.
        # A Claude bundle always reconciles its owner, so a reinstall shipping no
        # approved MCP retires the servers it wired earlier (#3956).
        bundle_mcp_present = (
            bool(bundle_mcp_deps) or bundle_info.format == BundleFormat.CLAUDE_PLUGIN.value
        )

        if dry_run:
            logger.dry_run_notice(
                f"Would deploy {len(deployed) + len(staged_instructions)} file(s) from local bundle"
            )
            # IM5: list the files in default mode too, so users see WHICH would deploy.
            for f in (*deployed, *staged_instructions):
                logger.tree_item(f)
            return

        if bundle_mcp_present and bundle_info.source_dir is not None:
            from apm_cli.install.mcp.write_receipts import journal_mcp_writes

            # The bundle's MCP writes are recorded like any install's (#4801).
            with journal_mcp_writes(project_root, user_scope=global_, logger=logger):
                _wire_legacy_bundle_mcp_servers(
                    bundle_format=bundle_info.format,
                    targets=targets,
                    project_root=project_root,
                    user_scope=global_,
                    verbose=getattr(logger, "verbose", False),
                    logger=logger,
                    deps=bundle_mcp_deps,
                    owner=_bundle_owner_key(bundle_info),
                )
        # A kept user LSP entry fails install only after files and receipts are recorded (#4628).
        lsp_conflict = (
            _wire_bundle_lsp_servers(
                bundle_dir=bundle_info.source_dir,
                targets=targets,
                project_root=project_root,
                user_scope=global_,
                verbose=getattr(logger, "verbose", False),
                logger=logger,
                deps=bundle_lsp_deps,
                owner=_bundle_owner_key(bundle_info),
                force=force,
            )
            if bundle_info.source_dir is not None
            else None
        )

        # Persist into project lockfile -- never edit an existing apm.yml.
        staged_fallbacks = result.get("staged_file_fallback_hashes", {})
        if deployed or staged_instructions or staged_fallbacks:
            from ..deps.lockfile import migrate_lockfile_if_needed

            migrate_lockfile_if_needed(_bundle_lock_root(global_))
            lockfile_path = get_lockfile_path(_bundle_lock_root(global_))
            lockfile = LockFile.read(lockfile_path) or LockFile()
            from ..core.deployment_ledger import DeploymentLedgerCodec, record_bundle_receipts
            from ..integration.cleanup import DeploymentEvidence
            from ..utils.diagnostics import DiagnosticCollector

            # Evidence for migration deletes is bound to the pre-install lock (#3701).
            migration_diagnostics = DiagnosticCollector()
            migration_evidence = DeploymentEvidence.collect(
                project_root,
                user_scope=global_,
                lockfile=lockfile,
                lockfile_root=lockfile_path.parent,
                diagnostics=migration_diagnostics,
                # Not an integrating install: nothing was kept this run.
                kept=(),
            )
            # The bytes just deployed from this bundle are its render (#4360).
            migration_evidence.add_rendered(
                {
                    Path(record) if Path(record).is_absolute() else project_root / record: value
                    for record, value in deployed_hashes.items()
                }
            )
            DeploymentLedgerCodec.record_local_bundle_files(
                lockfile,
                deployed,
                deployed_hashes,
                scope=scope,
                staged=staged_instructions,
                staged_hashes=result.get("staged_file_hashes", {}),
                staged_fallback_hashes=staged_fallbacks,
                targets=_external_claim_targets(deployed, targets),
            )
            # Receipts are a bundle file's only deletion evidence (#4223); a
            # dry run writes nothing, so it reports no written files.
            record_bundle_receipts(
                project_root,
                scope=scope,
                targets=targets,
                written=result.get("written_files", []),
                lockfile=lockfile,
            )

            _kept_legacy = (
                []
                if legacy_skill_paths
                else _migrate_legacy_skills(
                    lockfile, project_root, migration_evidence, migration_diagnostics, logger
                )
            )

            if not global_:
                _bootstrap_project_manifest(logger)
            lockfile.write(lockfile_path)
            # The lockfile write commits this install; the pending record joins it.
            if bundle_record is not None:
                bundle_record.commit()
            if _kept_legacy:
                _refuse_unproven_legacy(_kept_legacy, logger, user_scope=global_)

        if bundle_record is not None:
            bundle_record.commit()
        msg = f"Installed {len(deployed) + len(staged_instructions)} file(s) from local bundle"
        if skipped:
            msg += f" ({skipped} skipped)"
        log_bundle_deployed(logger, msg, deployed, targets)
        # Policy warnings this entry collected render as the pipeline's do (#4957).
        logger.render_summary()
        from apm_cli.install.mcp.write_receipts import announce_kept_mcp_writes

        lock_path = get_lockfile_path(_bundle_lock_root(global_))
        announce_kept_mcp_writes(
            lock_path, project_root=project_root, user_scope=global_, exclude=None, logger=logger
        )
        if lsp_conflict is not None:
            raise lsp_conflict

        # Issue #1207 D2.b: post-install compile hint for staged instructions.
        if staged_instructions and not dry_run:
            target_names = ", ".join(sorted({t.name for t in targets}))
            logger.warning(
                f"Bundle staged {len(staged_instructions)} instruction(s) "
                f"for compile (target: {target_names}). "
                f"{BUNDLE_INSTRUCTIONS_STAGED.sentence()} "
                "Reference: https://microsoft.github.io/apm/consumer/choose-harnesses/#compiled-outputs"
            )

        # Issue #1207 D2.c: route bundle ``mcp.json`` / ``.mcp.json`` (Anthropic plugin
        # format) through the MCP integrator, so each resolved target's native MCP config
        # gets the servers in its own format and location. ``MCPIntegrator.install`` does
        # the per-target dispatch (the same path as ``apm.yml mcp_dependencies``).
    finally:
        # Tarball cleanup (caller-owned per LocalBundleInfo contract).
        if bundle_info.temp_dir is not None and bundle_info.temp_dir.exists():
            shutil.rmtree(bundle_info.temp_dir, ignore_errors=True)


def _migrate_legacy_skills(
    lockfile, project_root: Path, migration_evidence, migration_diagnostics, logger
) -> list[str]:
    """Auto-migrate legacy per-client skill paths (#737); return the kept ones (#4360).

    After deploying new .agents/skills/ files, detect and clean up any legacy
    paths (e.g. .github/skills/) still recorded in the lockfile from a
    previous --legacy-skill-paths install.
    """
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
            # A legacy copy unlike this bundle's render may hold edits (#4360).
            return [
                _plan.src_path
                for _plan in _plans
                if any(_plan.dst_path in _cd for _cd in _colls)
                and not migration_evidence.authorizes(_plan.src_path, project_root / _plan.src_path)
            ]
        else:
            _mig_result = _exec_mig(_plans, lockfile, project_root, evidence=migration_evidence)
            migration_diagnostics.render_summary()
            _total = len(_mig_result.deleted) + len(_mig_result.skipped_no_file)
            if _total:
                _rich_info(
                    MIGRATION_SUMMARY_TEMPLATE.format(count=_total),
                    symbol="info",
                )
            if getattr(logger, "verbose", False) and _mig_result.deleted:
                for _dp in _mig_result.deleted:
                    _rich_info(f"  removed {_dp}", symbol="info")
            return _mig_result.failed

    return []


def _refuse_unproven_legacy(kept: list[str], logger, *, user_scope: bool) -> None:
    """Fail on legacy skill files no evidence proves APM deployed (#4360).

    The lockfile keeps their rows; the files stay on disk for the user.
    """
    from ..integration.base_integrator import kept_edit_message
    from .errors import InstallFailureAlreadyRendered

    for path in kept:
        logger.error(kept_edit_message(path, user_scope=user_scope, proven_edit=False))
    raise InstallFailureAlreadyRendered(f"{len(kept)} legacy skill file(s) kept")


def _parse_legacy_bundle_mcp_servers(
    bundle_dir: Path,
    *,
    bundle_format: str,
) -> list[MCPDependency]:
    """Parse legacy bundle MCP metadata into self-defined dependencies.

    Returns an empty list when the file is missing, malformed, or has no
    ``mcpServers`` map.  Per-server parsing errors are logged at debug
    level and the offending entry is dropped so a single bad entry does
    not block the rest of the bundle's MCP wiring.
    """
    _require_legacy_bundle_mcp_format(bundle_format)
    from urllib.parse import urlparse

    from apm_cli.models.dependency.mcp import MCPDependency

    # Case-insensitive lookup, like the rest of bundle metadata filtering (HFS+/NTFS folding).
    mcp_path: Path | None = None
    for entry in bundle_dir.iterdir() if bundle_dir.is_dir() else []:
        if (
            entry.is_file()
            and not entry.is_symlink()
            and entry.name.lower() in {"mcp.json", ".mcp.json"}
        ):
            mcp_path = entry
            break
    if mcp_path is None:
        return []

    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # json.JSONDecodeError is a ValueError; the wider set also fails closed on deeply
        # nested JSON (RecursionError) or an oversized-integer literal (ValueError).
        return []
    if not isinstance(data, dict):
        return []
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return []

    out: list[MCPDependency] = []
    for name, cfg in servers.items():
        if not isinstance(name, str) or not isinstance(cfg, dict):
            continue
        spec = dict(cfg)
        transport = str(spec.get("type") or spec.get("transport") or "").strip().lower()
        if transport in {"http", "sse", "streamable-http"}:
            url = spec.get("url")
            if not isinstance(url, str) or not url:
                continue
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            if parsed.username or parsed.password or parsed.fragment:
                continue
        spec["name"] = name
        spec["registry"] = False
        try:
            out.append(MCPDependency.from_dict(spec))
        except (ValueError, TypeError):
            continue
    return out


def effective_bundle_allow_map(
    project_root: Path,
    *,
    user_scope: bool,
    no_policy: bool,
    logger: Any,
    migrate_user_legacy: bool = True,
    dry_run: bool = False,
    policy_fetch_result=None,
) -> dict[str, dict[str, bool]]:
    """Resolve local-bundle trust through the canonical trust owner.

    Org policy resolves from *project_root* as for a package install; the
    bundle policy preflight reports its outcome, so this read does not repeat
    it. *policy_fetch_result* is the install's one policy discovery, which the
    preflight reuses. The grants are read from the scope's manifest (``$APM_HOME/apm.yml``
    under ``-g``), never from the working directory's ``apm.yml`` at user
    scope.
    """
    from ..core.scope import manifest_root, scope_manifest_path
    from ..policy.outcome_routing import resolve_trust_policy
    from ..security.executables import (
        exec_trust_context_for_project,
        materialize_exec_map,
    )

    policy = resolve_trust_policy(
        manifest_root(project_root, user_scope=user_scope),
        no_policy=no_policy,
        announce=False,
        raise_blocking_errors=not dry_run,
        fetch_result=policy_fetch_result,
    )
    grants_manifest = scope_manifest_path(project_root, user_scope=user_scope)
    return materialize_exec_map(
        exec_trust_context_for_project(
            grants_manifest,
            policy=policy,
            logger=logger,
            migrate_user_legacy=migrate_user_legacy,
        )
    )


def _filter_bundle_executables(
    dependencies: list[Any],
    *,
    bundle_info,
    allow_executables: dict[str, dict[str, bool]] | None,
    approval_key: str | None,
    exec_type: str,
    logger,
    user_scope: bool,
) -> list[Any]:
    """Apply executable trust to the exact bundle artifact, never its claimed name."""
    from ..security.executables import (
        exec_map_authorizes_all,
        local_bundle_approval_hint,
        package_type_approved,
    )

    if not dependencies or exec_map_authorizes_all(allow_executables):
        return dependencies
    if approval_key is None:
        raise ValueError("Local bundle executable approval requires an artifact digest")
    if package_type_approved(allow_executables, (approval_key,), exec_type):
        return dependencies
    noun = "executable" if len(dependencies) == 1 else "executables"
    lead = f"Skipped {len(dependencies)} bundle {exec_type.upper()} {noun} from {bundle_info.package_id}"
    if bundle_type_denied(allow_executables, approval_key, exec_type):
        logger.warning(
            f"{lead}: {bundle_denied_hint(approval_key, exec_type, user_scope=user_scope)}"
        )
    else:
        logger.warning(
            f"{lead}. " + local_bundle_approval_hint(approval_key, exec_type, user_scope=user_scope)
        )
    return []


def bundle_type_denied(allow_executables: Any, approval_key: str, exec_type: str) -> bool:
    """Whether a deny of this exact bundle, not a missing approval, withholds *exec_type*."""
    from ..security.trust import package_denies_type

    context = getattr(allow_executables, "trust_context", None)
    return package_denies_type(context, (approval_key,), exec_type)


def bundle_denied_hint(approval_key: str, exec_type: str, *, user_scope: bool) -> str:
    """Name the deny and the user's own command that lifts it (RA-041).

    The one wording for a denied local bundle's hooks, MCP and LSP: a deny is
    never reported as a pending approval.
    """
    from ..core.remediation import LOCAL_BUNDLE_EXECUTABLES_DENIED
    from ..security.trust import approve_command

    install = "apm install -g <bundle-path>" if user_scope else "apm install <bundle-path>"
    remedy = LOCAL_BUNDLE_EXECUTABLES_DENIED.bind(
        key=approval_key, exec_type=exec_type, install=install
    ).with_command(approve_command(approval_key, user_scope=user_scope))
    return (
        f"{exec_type} is denied for this exact bundle ({approval_key}). "
        f"To {remedy.fixes_promise}, run:\n    {remedy.command}\nthen {remedy.then}."
    )


def _bundle_approval_key(bundle_info, allow_executables) -> str | None:
    """The exact-content approval key, or None when the gate admits everything."""
    from ..security.executables import exec_map_authorizes_all, local_bundle_approval_key

    if exec_map_authorizes_all(allow_executables):
        return None
    return local_bundle_approval_key(
        bundle_info.package_id,
        str(bundle_info.plugin_json.get("version") or ""),
        bundle_info.source_dir,
        bundle_info.lockfile,
    )


def _stage_bundle_executables(
    bundle_info,
    slug: str,
    approval_key: str | None,
    service_deps: tuple[list, list],
    *,
    scope_dir: Path | None,
    create_config: bool,
    policy_subjects: LocalBundleSubjects,
) -> BundleRecordStage | None:
    """Stage this install's exact-content key and executable types; not on dry runs.

    ``apm approve --pending`` lists the types trust still parks (#4221);
    ``apm approve <key>`` grants only a key an install recorded (#4437).
    The record is written only when the install commits (#4492).
    """
    from .local_bundle_pending import BundleRecordStage

    if scope_dir is None:
        return None
    from .local_bundle_paths import declared_bundle_exec_types

    declared = declared_bundle_exec_types(bundle_info, *service_deps, create_config=create_config)
    return BundleRecordStage(
        scope_dir,
        slug,
        approval_key,
        tuple(declared) if approval_key else (),
        policy_subjects if approval_key else None,
    )


def _bundle_owner_key(bundle_info) -> str:
    """Return the version-independent service owner for a legacy local bundle."""
    return bundle_info.package_id


def _require_legacy_bundle_mcp_format(bundle_format: str) -> None:
    """Reject native Agent Plugins before any legacy MCP interpretation."""
    if bundle_format == BundleFormat.AGENT_PLUGIN.value:
        from apm_cli.agent_plugins.errors import AgentPluginLegacyBoundaryError

        raise AgentPluginLegacyBoundaryError(
            "Native Agent Plugin MCP must not enter legacy MCP integration"
        )
    if bundle_format != BundleFormat.CLAUDE_PLUGIN.value:
        raise ValueError("Legacy MCP helpers require a Claude plugin bundle format")


def _home_relative_claims(
    deployed: list[str], hashes: dict[str, str], home: Path
) -> tuple[list[str], dict[str, str]]:
    """Encode user-scope bundle claims relative to the home deploy root."""
    from ..utils.paths import portable_relpath

    def _relative(path: str) -> str:
        candidate = Path(path)
        if not candidate.is_absolute():
            return path
        try:
            candidate.resolve().relative_to(home.resolve())
        except ValueError:
            return path
        return portable_relpath(candidate.resolve(), home.resolve())

    return [_relative(path) for path in deployed], {
        _relative(path): digest for path, digest in hashes.items()
    }


def _external_claim_targets(deployed: list[str], targets: Any) -> dict[str, str]:
    """Name the target whose absolute static root holds each absolute claim.

    A claim under an external root (``$CODEX_HOME`` outside ``$HOME``) stays
    absolute and its path prefix names no target; without this its ledger
    row is unattributed and audit cannot resolve it (#3714).
    """
    from ..utils.path_security import PathTraversalError, ensure_path_within

    attributed: dict[str, str] = {}
    for claim in deployed:
        if not Path(claim).is_absolute():
            continue
        for target in targets:
            root = target.managed_deploy_root
            if root is None or target.resolved_deploy_root is not None:
                continue
            try:
                ensure_path_within(Path(claim), root)
            except PathTraversalError:
                continue
            attributed[claim] = target.name
            break
    return attributed


def _bootstrap_project_manifest(logger) -> None:
    """Create the bootstrap apm.yml a bundle install needs to be auditable (RA-017).

    A project lockfile without apm.yml fails ``apm audit --ci``
    (manifest-missing), so a bundle installed into a directory with no
    manifest gets the same minimal one ``apm install <pkg>`` creates. An
    existing apm.yml is never edited.
    """
    from ..constants import APM_YML_FILENAME
    from ..core.project_name import resolve_bootstrap_project_name
    from ..core.scope import InstallScope, get_manifest_path
    from ..models.manifest_bootstrap import bootstrap_config, write_minimal_manifest

    manifest_path = get_manifest_path(InstallScope.PROJECT)
    if manifest_path.exists():
        return
    project_name = resolve_bootstrap_project_name(manifest_path.parent.name)
    write_minimal_manifest(bootstrap_config(project_name), target_path=manifest_path)
    logger.success(f"Created {APM_YML_FILENAME}")


def _bundle_lock_root(user_scope: bool) -> Path:
    """The scope's lockfile directory, apart from where files deploy.

    At user scope files deploy under ``$HOME`` but the lock lives in
    ``$APM_HOME`` with the user manifest, where ``install -g --frozen`` and
    audit read it.
    """
    from ..core.scope import InstallScope, get_apm_dir

    return get_apm_dir(InstallScope.USER if user_scope else InstallScope.PROJECT)


def _wire_legacy_bundle_mcp_servers(
    *,
    bundle_format: str,
    targets,
    project_root: Path,
    user_scope: bool,
    verbose: bool,
    logger,
    deps: list[MCPDependency],
    owner: str | None = None,
) -> int:
    """Wire legacy bundle MCP servers through ``MCPIntegrator.install``.

    Returns the count of newly configured/updated MCP servers.
    """
    _require_legacy_bundle_mcp_format(bundle_format)
    if not deps and owner is None:
        return 0

    target_names = [t.name for t in targets]
    if owner is None:
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        count = MCPIntegrator.install(
            deps,
            verbose=verbose,
            apm_config={"targets": target_names, "scripts": {}},
            project_root=project_root,
            user_scope=user_scope,
            explicit_target=target_names,
            logger=logger,
            fail_on_write_error=True,
        )
    else:
        from apm_cli.deps.lockfile import get_lockfile_path
        from apm_cli.install.mcp.integration import run_owned_mcp_integration

        count = run_owned_mcp_integration(
            dependencies=deps,
            owner=owner,
            lock_path=get_lockfile_path(_bundle_lock_root(user_scope)),
            target_runtimes=target_names,
            verbose=verbose,
            project_root=project_root,
            user_scope=user_scope,
            logger=logger,
        )

    if count:
        joined = ", ".join(target_names)
        logger.success(f"Wired {count} MCP server(s) from bundle .mcp.json (target(s): {joined})")
    elif deps:
        # Bundle declared servers but none applied (targets all gated out, or all
        # servers already configured): emit an info line as a paper trail.
        joined = ", ".join(target_names)
        logger.info(
            f"Bundle .mcp.json declared {len(deps)} server(s); "
            f"no new MCP config changes for target(s): {joined}"
        )
    return count


def _parse_bundle_lsp_servers(
    bundle_dir: Path,
):
    """Parse ``<bundle>/lsp.json`` or ``<bundle>/com.microsoft.apm/lsp.json`` into LSP deps."""
    lsp_path: Path | None = None
    for entry in bundle_dir.iterdir() if bundle_dir.is_dir() else []:
        if (
            entry.is_file()
            and not entry.is_symlink()
            and entry.name.lower() in {"lsp.json", ".lsp.json"}
        ):
            lsp_path = entry
            break
        if entry.is_dir() and not entry.is_symlink() and entry.name == COM_MICROSOFT_APM_NAMESPACE:
            candidate = entry / "lsp.json"
            if candidate.is_file() and not candidate.is_symlink():
                lsp_path = candidate
                break
    if lsp_path is None:
        return []

    try:
        data = json.loads(lsp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return []
    if not isinstance(data, dict):
        return []
    servers = data.get("lspServers")
    if not isinstance(servers, dict):
        servers = data
    if not isinstance(servers, dict):
        return []

    from apm_cli.deps.plugin_parser import lsp_servers_to_apm_deps
    from apm_cli.models.dependency.lsp import LSPDependency

    return [
        LSPDependency.from_dict(spec)
        for spec in lsp_servers_to_apm_deps(servers, lsp_path, warn_on_invalid=False)
    ]


def _wire_bundle_lsp_servers(
    *,
    bundle_dir: Path,
    targets,
    project_root: Path,
    user_scope: bool,
    verbose: bool,
    logger,
    deps,
    owner: str,
    force: bool = False,
):
    """Wire bundle LSP servers through the canonical owned lifecycle.

    Returns the ``LSPUserEntryConflictError`` the caller raises once the
    bundle's other writes are recorded, or ``None``.
    """
    from apm_cli.deps.lockfile import get_lockfile_path
    from apm_cli.install.errors import LSPUserEntryConflictError
    from apm_cli.install.lsp.integration import run_owned_lsp_integration
    from apm_cli.integration.lsp_integrator import LSPIntegrator

    target_names = [t.name for t in targets]
    lsp_target_names = LSPIntegrator.supported_target_runtimes(target_names)
    try:
        count = run_owned_lsp_integration(
            dependencies=deps,
            owner=owner,
            lock_path=get_lockfile_path(_bundle_lock_root(user_scope)),
            project_root=project_root,
            user_scope=user_scope,
            logger=logger,
            target_runtimes=lsp_target_names,
            fail_on_write_error=True,
            force=force,
        )
    except LSPUserEntryConflictError as exc:
        return exc

    if count:
        logger.success(
            f"Wired {count} LSP server(s) from bundle lsp.json (target(s): {', '.join(lsp_target_names)})"
        )
    elif deps:
        logger.info(
            f"Bundle lsp.json declared {len(deps)} server(s); no new LSP config changes for target(s): {', '.join(lsp_target_names)}"
        )
    return None
