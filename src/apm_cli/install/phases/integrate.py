"""Sequential integration phase -- per-package integration loop.

Reads all prior phase outputs from *ctx* (resolve, targets, download) and
processes each dependency sequentially.  Per-source acquisition is handled
by ``DependencySource`` Strategy implementations
(``apm_cli.install.sources``); the shared post-acquire flow (security gate
+ primitive integration + diagnostics) lives in the Template Method
``apm_cli.install.template.run_integration_template``.

After the dependency loop, root-project primitives (``<project_root>/.apm/``)
are integrated when present (#714) -- this path is structurally distinct
(no ``PackageInfo``, dedicated ``ctx.local_deployed_files`` tracking) so it
remains a sibling helper here rather than a fourth ``DependencySource``.
"""

from __future__ import annotations

import builtins
import copy
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.deps.egress_route import EgressSurface, is_proxy_only
from apm_cli.install.errors import DirectDependencyError
from apm_cli.install.outcome import integration_changed_files
from apm_cli.install.phases._skip_logic import _compute_skip_download
from apm_cli.install.phases.heal import run_heal_chain
from apm_cli.install.services import _targets_explicitly_requested, integrate_local_content
from apm_cli.install.sources import make_dependency_source
from apm_cli.install.template import (
    announce_package,
    preflight_agent_plugin_materializations,
    preflight_skill_materializations,
    prepare_integration_materialization,
    run_integration_template,
)

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.install.deployment_plan import PackageDeploymentPlan


# ======================================================================
# Private helpers -- each encapsulates one per-package integration path
# ======================================================================


def failed_acquisitions(diagnostics: Any, failed_keys: list[str]) -> list[tuple[str, str]]:
    """Name each package whose acquire failed, with its recorded cause (#4909).

    A failed acquire only records a diagnostic, rendered after the install;
    a frozen refusal raised before then would otherwise hide the cause.
    """
    from apm_cli.utils.diagnostics import CATEGORY_ERROR

    keys = list(dict.fromkeys(failed_keys))
    errors = diagnostics.by_category().get(CATEGORY_ERROR, []) if keys and diagnostics else []
    failures: list[tuple[str, str]] = []
    for key in keys:
        messages = [d.message for d in errors if d.package == key]
        failures.extend(
            (key, message) for message in messages or [f"{key}: package could not be acquired"]
        )
    return failures


def _announce_held_headers(prepared_integrations) -> None:
    """Emit every held package header (refusal paths before integration)."""
    for _dep_key, _install_path, source, _materialization, _deltas in prepared_integrations:
        announce_package(source)


def _resolve_download_strategy(
    ctx: InstallContext,
    dep_ref: Any,
    install_path: Path,
) -> tuple[Any, bool, Any, bool]:
    """Determine whether *dep_ref* can be served from cache.

    Returns ``(resolved_ref, skip_download, dep_locked_chk, ref_changed)``
    where *skip_download* is ``True`` when the package at *install_path*
    is already up-to-date.
    """
    from apm_cli.drift import (
        LOCKED_COPY_CONTENT_HASH,
        LOCKED_COPY_GIT_HEAD,
        detect_ref_change,
        locked_copy_proof,
    )
    from apm_cli.install.transaction import stage_existing_copy
    from apm_cli.models.apm_package import GitReferenceType

    existing_lockfile = ctx.existing_lockfile
    update_refs = ctx.update_refs
    diagnostics = ctx.diagnostics
    logger = ctx.logger
    dep_key = dep_ref.get_unique_key()

    # A semver range already in apm_modules skips the resolve callback, so
    # replay its locked tag here; never resolve the raw range as a ref (#3382).
    # Drift is still judged against the declared constraint: a bare ``1.1.0``
    # tag is itself a semver string and would not equal the locked range.
    drift_ref = dep_ref
    if not update_refs and dep_key not in ctx.git_semver_resolutions:
        from apm_cli.install.helpers.ref_reuse import replay_locked_git_semver

        _locked_semver = replay_locked_git_semver(dep_ref, existing_lockfile)
        if _locked_semver is not None:
            ctx.git_semver_resolutions[dep_key] = _locked_semver
            drift_ref = copy.copy(dep_ref)
            dep_ref.reference = _locked_semver.resolved_tag

    # npm-like behavior: Branches always fetch latest, only tags/commits use cache
    # Resolve git reference to determine type
    resolved_ref = None
    # Registry-sourced deps don't have a git reference to resolve — calling
    # ``resolve_git_reference`` on them would issue ``git ls-remote`` against
    # the dep's notional host (default github.com), which can trigger an SSH
    # key-acceptance prompt or a wasted network call. Skip the git probe
    # entirely for non-git sources.
    _source = getattr(dep_ref, "source", None)
    is_git_source = not isinstance(_source, str) or _source in (None, "git")
    if is_git_source and dep_key not in ctx.pre_downloaded_keys:
        # Resolve when there is an explicit ref, OR when update_refs
        # is True AND we have a non-cached lockfile entry to compare
        # against (otherwise resolution is wasted work -- the package
        # will be downloaded regardless).
        _has_lockfile_sha = False
        if update_refs and existing_lockfile:
            _lck = existing_lockfile.get_dependency(dep_key)
            _has_lockfile_sha = bool(
                _lck and _lck.resolved_commit and _lck.resolved_commit != "cached"
            )
        if dep_ref.reference or (update_refs and _has_lockfile_sha):
            try:  # noqa: SIM105
                resolved_ref = ctx.downloader.resolve_git_reference(dep_ref)
            except Exception:
                pass  # If resolution fails, skip cache (fetch latest)

    # Use cache only for tags and commits (not branches)
    is_cacheable = resolved_ref and resolved_ref.ref_type in [
        GitReferenceType.TAG,
        GitReferenceType.COMMIT,
    ]
    # Skip download if: already fetched by resolver callback, or cached tag/commit
    already_resolved = dep_key in ctx.callback_downloaded
    # Detect if manifest ref changed vs what the lockfile recorded.
    # detect_ref_change() handles all transitions including None->ref.
    _dep_locked_chk = existing_lockfile.get_dependency(dep_key) if existing_lockfile else None
    ref_changed = detect_ref_change(drift_ref, _dep_locked_chk, update_refs=update_refs)
    # When the manifest ref drifted from the lockfile, the content hash
    # will legitimately change after re-download.  Mark the dep so the
    # supply-chain check in sources.py doesn't treat it as an attack.
    if ref_changed:
        # resolve.py's BFS callback may have already added this;
        # set semantics make double-add safe.
        ctx.expected_hash_change_deps.add(dep_key)
    # Phase 5 (#171): Also skip when lockfile SHA matches local HEAD
    # -- but not when the manifest ref has changed (user wants different version).
    lockfile_match = False
    content_hash_already_verified = dep_key in ctx.content_hash_verified_deps
    # Track whether lockfile_match was satisfied without git HEAD verification
    # (the .apm-pin marker or content hash -- typical for virtual packages, where
    # install_path is a carved-out subdirectory rather than a git repo).
    # The self-heal logic below uses this to recover from the v<=0.12.2
    # branch-ref drift bug for upgrading users.
    lockfile_match_via_content_hash_only = False
    if install_path.exists() and existing_lockfile:
        locked_dep = existing_lockfile.get_dependency(dep_key)
        is_registry = getattr(locked_dep, "source", None) == "registry"
        if locked_dep and locked_dep.resolved_commit and locked_dep.resolved_commit != "cached":
            # Update mode: reuse the copy only while the remote ref still
            # resolves to the locked commit. Normal mode: only while the
            # manifest ref has not changed (user wants a different version).
            copy_check = (
                bool(resolved_ref) and resolved_ref.resolved_commit == locked_dep.resolved_commit
                if update_refs
                else not ref_changed
            )
        else:
            # Registry deps and unpinned git/virtual deps (#1548) have no
            # commit anchor; their content_hash is the only trust signal, so
            # any divergence must reach the fresh-download path and its
            # supply-chain mismatch check.
            copy_check = (
                bool(locked_dep)
                and (is_registry or bool(locked_dep.content_hash))
                and not ref_changed
                and not update_refs
            )
        if copy_check:
            # The canonical copy-validity owner decides (#3884): the resolver
            # gate and the parallel pre-download skip ask the same question.
            proof = (
                LOCKED_COPY_CONTENT_HASH
                if content_hash_already_verified
                else locked_copy_proof(locked_dep, install_path)
            )
            if proof is not None:
                lockfile_match = True
                lockfile_match_via_content_hash_only = (
                    proof != LOCKED_COPY_GIT_HEAD and not is_registry
                )
                if proof == LOCKED_COPY_CONTENT_HASH:
                    content_hash_already_verified = True
                    ctx.content_hash_verified_deps.add(dep_key)

    # Self-heal pipeline (PR #1158).
    #
    # All install-time heals (branch-ref drift detection, v<=0.12.2
    # buggy-lockfile recovery, future heals) live in
    # ``apm_cli.install.heals`` and are dispatched by ``run_heal_chain``.
    # Each heal is an isolated, individually-testable Chain-of-
    # Responsibility handler that may turn ``lockfile_match`` False,
    # set ``ref_changed`` True, and add a bypass key telling
    # ``FreshDependencySource`` that an upcoming content_hash change is
    # legitimate recovery, not a supply-chain attack.
    #
    # The dispatcher (not individual heals) renders user-facing
    # diagnostics + log messages, so heals stay pure and testable.
    lockfile_match, ref_changed = run_heal_chain(
        ctx,
        dep_ref,
        resolved_ref=resolved_ref,
        existing_lockfile=existing_lockfile,
        lockfile_match=lockfile_match,
        lockfile_match_via_content_hash_only=lockfile_match_via_content_hash_only,
        update_refs=update_refs,
        ref_changed=ref_changed,
    )

    # Issue #551: skip re-download when the BFS callback already fetched this
    # dep during resolution AND the remote SHA still matches what was captured.
    # This eliminates redundant network I/O in update/refresh when apm_modules/
    # is empty but the lockfile SHA is stale: the callback downloads the latest
    # content (recording its SHA), and the sequential loop would otherwise
    # re-download identical bytes because lockfile_match=False (stale SHA).
    _callback_sha = ctx.callback_downloaded.get(dep_key)
    _already_resolved_sha_match = (
        already_resolved
        and (update_refs or ctx.refresh)
        and bool(resolved_ref)
        and bool(_callback_sha)
        and getattr(resolved_ref, "resolved_commit", None) not in (None, "cached")
        and _callback_sha == resolved_ref.resolved_commit
    )

    if _already_resolved_sha_match and logger:
        logger.verbose_detail(f"  {dep_key}: callback SHA matches remote -- skipping re-download")

    skip_download = _already_resolved_sha_match or _compute_skip_download(
        install_path_exists=install_path.exists(),
        is_cacheable=is_cacheable,
        update_refs=update_refs,
        already_resolved=already_resolved,
        lockfile_match=lockfile_match,
        ref_changed=ref_changed,
    )

    # A same-SHA callback receipt binds validated bytes to their published path.
    # Recheck those bytes rather than comparing them with the previous revision.
    # A commit that did not move keeps its locked content hash as the baseline.
    from apm_cli.install.source_helpers import locked_commit_unchanged

    receipt = ctx.callback_content_hashes.get(dep_key)
    _locked_commit_unchanged = bool(
        _dep_locked_chk
        and _dep_locked_chk.content_hash
        and locked_commit_unchanged(_dep_locked_chk, _callback_sha)
    )
    if (
        _already_resolved_sha_match
        and not _locked_commit_unchanged
        and receipt
        and receipt[0] == install_path.resolve()
    ):
        from apm_cli.utils.content_hash import verify_package_hash

        if verify_package_hash(install_path, receipt[1], framed=True):
            content_hash_already_verified = True
            ctx.content_hash_verified_deps.add(dep_key)

    # Missing, stale or tampered callback evidence retains the lock hash check.
    if (
        skip_download
        and _dep_locked_chk
        and _dep_locked_chk.content_hash
        and not content_hash_already_verified
    ):
        from apm_cli.deps.carried_license import verify_locked_package
        from apm_cli.install.source_helpers import carried_licenses_for

        # The lock's own carried records are the baseline; a pre-#3556 entry
        # has none, so only exclude the copies this run carried (#3556).
        if not verify_locked_package(
            install_path,
            _dep_locked_chk.content_hash,
            _dep_locked_chk.carried_licenses or carried_licenses_for(ctx, dep_key, _dep_locked_chk),
            framed=_dep_locked_chk.content_hash_framed,
        ):
            from apm_cli.install.source_helpers import (
                locked_content_mismatch_line,
                observed_package_hash,
            )

            # The download phase owns this notice; report here only when it did not (#4351).
            if dep_ref.get_unique_key() not in getattr(ctx, "cache_mismatch_reported", ()):
                _hash_msg = (
                    locked_content_mismatch_line(
                        dep_ref.get_unique_key(),
                        _dep_locked_chk.content_hash,
                        observed_package_hash(_dep_locked_chk, install_path),
                    )
                    + " Re-downloading from the locked source."
                )
                diagnostics.warn(_hash_msg, package=dep_ref.get_unique_key())
                if logger:
                    logger.progress(_hash_msg)
            stage_existing_copy(ctx, install_path, _dep_locked_chk)
            skip_download = False

    # When registry-only mode is active, bypass cache if the
    # cached artifact was NOT previously downloaded via the
    # registry (no registry_prefix in lockfile). This handles
    # the transition from direct-VCS installs to proxy installs
    # for packages not yet in the lockfile.
    if (
        skip_download
        and ctx.registry_config
        and is_proxy_only(EgressSurface.SOURCE_ARCHIVE)
        and not dep_ref.is_local
    ):
        if not _dep_locked_chk or _dep_locked_chk.registry_prefix is None:
            skip_download = False

    return resolved_ref, skip_download, _dep_locked_chk, ref_changed


def _integrate_root_project(
    ctx: InstallContext,
    *,
    deployment_plan: PackageDeploymentPlan | None = None,
) -> dict[str, int] | None:
    """Integrate root project's own .apm/ primitives (#714).

    Users should not need a dummy "./agent/apm.yml" stub to get their
    root-level .apm/ rules deployed alongside external dependencies.
    Treat the project root as an implicit local package: any primitives
    found in <project_root>/.apm/ are integrated after all declared
    dependency packages have been processed.

    Delegates to ``integrate_local_content`` which creates a
    synthetic ``_local`` APMPackage with ``PackageType.APM_PACKAGE`` so that
    a root-level ``SKILL.md`` is NOT deployed as a skill.  Deployed files
    are tracked on ``ctx.local_deployed_files`` for the downstream
    post-deps-local phase (stale cleanup + lockfile persistence).

    Returns a counter-delta dict, or ``None`` if root integration is
    not applicable or failed.
    """
    if not ctx.root_has_local_primitives or not ctx.targets:
        return None

    import builtins

    from apm_cli.integration.base_integrator import BaseIntegrator

    logger = ctx.logger
    diagnostics = ctx.diagnostics

    # Track error count before local integration so the post-deps-local
    # phase can decide whether stale cleanup is safe.
    ctx.local_content_errors_before = diagnostics.error_count if diagnostics else 0

    # Build managed_files that includes old local deployed files AND
    # freshly-deployed dep files so local content wins collisions with
    # both.  This matches the pre-refactor Click handler behavior where
    # managed_files was rebuilt from the post-install lockfile.
    _local_managed = builtins.set(ctx.managed_files)
    _local_managed.update(ctx.old_local_deployed)
    for _dep_files in ctx.package_deployed_files.values():
        _local_managed.update(_dep_files)
    _local_managed = BaseIntegrator.normalize_managed_files(_local_managed)

    if logger:
        from apm_cli.core.scope import is_user_scope

        # The user scope has no project root; never label it as one (RA-020).
        root_label = "<user scope>" if is_user_scope(ctx.scope) else "<project root>"
        logger.download_complete(root_label, ref_suffix="local")
        logger.verbose_detail("Integrating local .apm/ content...")
    try:
        _root_result = integrate_local_content(
            ctx.project_root,
            targets=ctx.targets,
            prompt_integrator=ctx.integrators["prompt"],
            agent_integrator=ctx.integrators["agent"],
            skill_integrator=ctx.integrators["skill"],
            instruction_integrator=ctx.integrators["instruction"],
            command_integrator=ctx.integrators["command"],
            hook_integrator=ctx.integrators["hook"],
            force=ctx.force,
            managed_files=_local_managed,
            diagnostics=diagnostics,
            logger=logger,
            scope=ctx.scope,
            source_root=ctx.source_root,
            ctx=ctx,
            deployment_plan=deployment_plan,
        )

        # Track deployed files for the post-deps-local phase (stale
        # cleanup + lockfile persistence of local_deployed_files).
        from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

        DeploymentLedgerCodec.replace_context_local_files(
            ctx, _root_result.get("deployed_files", [])
        )
        # Merged hook entries deploy no standalone file, yet audit replays them (#4033).
        ctx.local_hooks_merged = _root_result.get("hooks_contributed", 0) > 0

        _local_changed = integration_changed_files(_root_result)
        if _local_changed > 0 and logger:
            logger.verbose_detail(f"Deployed {_local_changed} local primitive(s) from .apm/")
            # Project-owned content is never an APM dependency (#4184): it is
            # reported separately and only when its deployed files changed.
            logger.record_local_content_updated()

        return {
            "prompts": _root_result["prompts"],
            "agents": _root_result["agents"],
            "skills": _root_result.get("skills", 0),
            "sub_skills": _root_result.get("sub_skills", 0),
            "instructions": _root_result["instructions"],
            "commands": _root_result["commands"],
            "hooks": _root_result["hooks"],
            "canvases": _root_result.get("canvases", 0),
            "links_resolved": _root_result["links_resolved"],
        }
    except Exception as e:
        import traceback as _tb

        diagnostics.error(
            f"Failed to integrate root project primitives: {e}",
            package="<root>",
            detail=_tb.format_exc(),
        )
        # When root integration is the *only* action (no external deps),
        # a failure means nothing was deployed -- surface it clearly.
        if not ctx.all_apm_deps and logger:
            logger.error(f"Root project primitives could not be integrated: {e}")
        return None


# ======================================================================
# Cowork cap checks (Amendment 7)
# ======================================================================

_COWORK_MAX_SKILLS: int = 50
"""Warn when the cowork skills directory contains more than this many skills."""

_COWORK_MAX_SKILL_SIZE: int = 1_048_576  # 1 MB
"""Warn when any source SKILL.md exceeds this size in bytes."""


def _check_cowork_caps(ctx: InstallContext) -> None:
    """Emit warn-only diagnostics for cowork skill count and size caps.

    Walks ``<cowork_root>/skills/*/SKILL.md`` (existing + just-installed)
    and checks against ``_COWORK_MAX_SKILLS`` and ``_COWORK_MAX_SKILL_SIZE``.
    Install still succeeds regardless.
    """
    if not ctx.targets:
        return

    cowork_root = None
    for t in ctx.targets:
        if t.name == "copilot-cowork" and t.resolved_deploy_root is not None:
            cowork_root = t.resolved_deploy_root
            break
    if cowork_root is None:
        return
    if not cowork_root.is_dir():
        return

    skill_dirs = sorted(
        d for d in cowork_root.iterdir() if d.is_dir() and (d / "SKILL.md").exists()
    )

    # --- count cap ---
    if len(skill_dirs) > _COWORK_MAX_SKILLS:
        msg = (
            f"Cowork skills directory contains {len(skill_dirs)} skills "
            f"(cap: {_COWORK_MAX_SKILLS}). Consider removing unused skills."
        )
        if ctx.logger:
            ctx.logger.warning(msg, symbol="warning")
        if ctx.diagnostics:
            ctx.diagnostics.warn(msg, package="cowork")

    # --- per-file size cap ---
    for skill_dir in skill_dirs:
        skill_md = skill_dir / "SKILL.md"
        try:
            size = skill_md.stat().st_size
        except OSError:
            continue
        if size > _COWORK_MAX_SKILL_SIZE:
            size_mb = size / (1024 * 1024)
            msg = (
                f"Skill '{skill_dir.name}/SKILL.md' is {size_mb:.1f} MB "
                f"(cap: 1 MB). Large skills may degrade Copilot performance."
            )
            if ctx.logger:
                ctx.logger.warning(msg, symbol="warning")
            if ctx.diagnostics:
                ctx.diagnostics.warn(msg, package="cowork")


def _run_executable_approval_prompt(ctx: InstallContext) -> None:
    """Prompt for approval of packages whose executables were blocked.

    After the integration loop, any package that had hooks or bin/
    blocked is collected in ``ctx.blocked_executables``.  This function
    runs the interactive approval flow (or hard-errors in CI) and
    persists approved entries to the personal ``~/.apm/config.json``
    ``executables`` block (lowest authority, never committed to source
    control) so the next install deploys them (#1873).
    """
    if not ctx.blocked_executables:
        return
    from apm_cli.core.scope import is_user_scope
    from apm_cli.security.executables import trust_remedy
    from apm_cli.security.trust import approve_command, explain_command

    policy = getattr(getattr(ctx, "policy_fetch", None), "policy", None)
    recommend = tuple(getattr(getattr(policy, "executables", None), "recommend", None) or ())
    user_scope = is_user_scope(getattr(ctx, "scope", None))
    if os.environ.get("APM_NON_INTERACTIVE") or os.environ.get("CI") or not sys.stdin.isatty():
        first = ctx.blocked_executables[0].package_name
        msg = (
            f"{len(ctx.blocked_executables)} package(s) have executable primitives "
            "parked pending approval; install completed without deploying them."
        )
        remedy = trust_remedy(first, recommend, user_scope=user_scope)
        if ctx.logger:
            ctx.logger.warning(msg, symbol="warning")
            ctx.logger.info(remedy, symbol="info")
        else:
            from apm_cli.core.command_logger import CommandLogger

            logger = CommandLogger("install")
            logger.warning(msg, symbol="warning")
            logger.info(remedy, symbol="info")
        return

    from apm_cli.security.executables import (
        load_user_executables,
        prompt_executable_approval,
        save_user_executables,
    )

    # Seed the prompt with existing personal consent (not project entries,
    # which are read-only from the install pipeline's perspective).
    allow_exec, deny_exec = load_user_executables()

    updated = prompt_executable_approval(
        ctx.blocked_executables,
        allow_executables=allow_exec,
        recommend=recommend,
    )

    # Persist new approvals to the personal config if the user approved
    # anything new.
    if updated and updated != allow_exec:
        save_user_executables(updated, deny_exec)
        if ctx.logger:
            from apm_cli import config
            from apm_cli.core.remediation import EXECUTABLES_APPROVED

            ctx.logger.info(
                f"Updated {config.CONFIG_FILE}. {EXECUTABLES_APPROVED.sentence()}",
                symbol="info",
            )

    # Any package the user declined stays parked -- always surface a remedy
    # so the present-but-parked path is actionable on the decline branch too.
    parked = [d for d in ctx.blocked_executables if not _decl_fully_trusted(updated, d)]
    if parked:
        first = parked[0].package_name
        logger = ctx.logger
        if logger is None:
            from apm_cli.core.command_logger import CommandLogger

            logger = CommandLogger("install")
        logger.info(
            f"{len(parked)} package(s) left parked. Their executables will not run until trusted.",
            symbol="info",
        )
        from apm_cli.core.remediation import EXECUTABLES_PARKED

        deploy = EXECUTABLES_PARKED.bind(package=first).with_command(
            approve_command(first, user_scope=user_scope)
        )
        logger.info(
            f"Deploy later: {deploy.clause()}  |  "
            f"Why parked?: {explain_command(first, user_scope=user_scope)}",
            symbol="info",
        )


def _decl_fully_trusted(allow: dict, decl) -> bool:
    """Return True when *allow* grants every exec type *decl* declares."""
    identity = decl.package_key.split("#", 1)[0]
    grant = allow.get(decl.package_key) or allow.get(identity) or allow.get(decl.package_name) or {}
    # A pinned item map was just recorded from the current definitions.
    return all(
        grant.get(t, False) is True or isinstance(grant.get(t), dict) for t in decl.exec_types
    )


# ======================================================================
# Public phase entry point
# ======================================================================


def _fail_explicit_target_without_deploy(ctx: InstallContext) -> None:
    """Abort before any write when an explicit ``--target`` would deploy nothing."""
    tree = getattr(ctx.dependency_graph, "dependency_tree", None)
    if not _targets_explicitly_requested(ctx) or tree is None:
        return
    if ctx.root_has_local_primitives and not ctx.only_packages:
        return
    from apm_cli.install.target_filter import explicit_target_failure
    from apm_cli.models.apm_package import DependencyReference

    skipped = ctx.callback_failures
    nodes = [n for n in tree.nodes.values() if n.dependency_ref.get_unique_key() not in skipped]
    roots: list = []
    if ctx.only_packages:
        identities = builtins.set()
        for spec in ctx.only_packages:
            try:
                identities.add(DependencyReference.parse(spec).get_identity())
            except Exception:
                identities.add(spec)
        roots = [n for n in nodes if n.dependency_ref.get_identity() in identities]
    message = explicit_target_failure(list(ctx.targets), nodes, roots)
    if message:
        raise DirectDependencyError(message)


def _keep_prior_entries(
    ctx: InstallContext, kept: list[tuple[str, Path]], session_kinds: dict[str, str]
) -> None:
    """Keep each unproven managed file's prior lock entry, as cleanup keeps its files (#4360)."""
    from apm_cli.install.deployed_paths import deployed_path_entry

    lockfile = ctx.existing_lockfile
    for owner, path in kept:
        entry = deployed_path_entry(path, ctx.project_root, ctx.targets)
        if lockfile is None:
            prior = None
        elif owner == ".":
            # The project's own outputs keep their local lock hash (#4873).
            prior = lockfile.local_deployed_file_hashes.get(entry)
        else:
            previous = lockfile.get_dependency(owner)
            prior = previous.deployed_file_hashes.get(entry) if previous is not None else None
        ctx.package_cleanup_retained.setdefault(owner, {})[entry] = prior
        ctx.kept_overwrites.append((owner, entry))
        kind = session_kinds.get(path.resolve().as_posix())
        if kind is not None:
            ctx.kept_overwrite_kinds[entry] = kind


def _write_plan(ctx: InstallContext, materialized, deployment_plans, local_plan) -> list:
    """Every package this phase will deploy, as the commit gate plans its writes (#4381)."""
    plan = [
        (
            materialization.package_info,
            deployment_plans.get(materialization.dep_key),
            ctx.managed_files,
        )
        for _source, materialization in materialized
        if materialization.package_info is not None
    ]
    if local_plan is not None and ctx.root_has_local_primitives and ctx.targets:
        from apm_cli.install.project_package import build_project_local_package_info
        from apm_cli.integration.base_integrator import BaseIntegrator

        managed = BaseIntegrator.normalize_managed_files(
            builtins.set(ctx.managed_files) | builtins.set(ctx.old_local_deployed)
        )
        local = build_project_local_package_info(ctx.source_root or ctx.project_root)
        plan.append((local, local_plan, managed))
    return plan


def _kept_skill_values(ctx: InstallContext) -> frozenset[str]:
    """Return declared skill members this batch left on disk; cleanup retains them."""
    from apm_cli.integration.skill_integrator import SkillIntegrator

    skill = (getattr(ctx, "integrators", None) or {}).get("skill")
    return skill.kept_values() if isinstance(skill, SkillIntegrator) else frozenset()


def run(ctx: InstallContext) -> None:
    """Execute the sequential integration phase.

    On return the following *ctx* fields are populated / updated:
    ``installed_count``, ``unpinned_count``, ``installed_packages``,
    ``package_deployed_files``, ``package_types``, ``package_hashes``,
    ``total_prompts_integrated``, ``total_agents_integrated``,
    ``total_skills_integrated``, ``total_sub_skills_promoted``,
    ``total_instructions_integrated``, ``total_commands_integrated``,
    ``total_hooks_integrated``, ``total_links_resolved``.
    """
    # ------------------------------------------------------------------
    # Unpack loop-level aliases and int counters.
    # Mutable containers (lists, dicts, sets) share the reference so
    # in-place mutations by helpers are visible through ctx.  Int
    # counters are accumulated into locals and written back at the end.
    # ------------------------------------------------------------------
    deps_to_install = ctx.deps_to_install
    apm_modules_dir = ctx.apm_modules_dir
    if apm_modules_dir is None:
        raise RuntimeError("Resolution must set apm_modules_dir before integration")

    # LSP capability gate (#3727): declared LSP servers with no capable target
    # fail before this phase deploys anything to a target.
    from apm_cli.install.lsp.integration import preflight_lsp_targets

    preflight_lsp_targets(ctx)
    session = getattr(
        getattr(ctx, "integrators", {}).get("skill"), "_materialization_session", None
    )
    if session is not None:
        from apm_cli.install.phases.cleanup import deployment_evidence

        # Receipt-less managed files are replaced only when they equal the locked render (#4360).
        session.render_evidence = deployment_evidence(ctx)

    # Direct dep keys: used to distinguish direct vs transitive failures
    # so direct failures can be surfaced immediately.
    direct_dep_keys = builtins.set(dep.get_unique_key() for dep in ctx.all_apm_deps)

    # Int counters (written back to ctx at end of function)
    installed_count = ctx.installed_count
    unpinned_count = ctx.unpinned_count
    total_prompts_integrated = ctx.total_prompts_integrated
    total_agents_integrated = ctx.total_agents_integrated
    total_skills_integrated = ctx.total_skills_integrated
    total_sub_skills_promoted = ctx.total_sub_skills_promoted
    total_instructions_integrated = ctx.total_instructions_integrated
    total_commands_integrated = ctx.total_commands_integrated
    total_hooks_integrated = ctx.total_hooks_integrated
    total_links_resolved = ctx.total_links_resolved

    # ------------------------------------------------------------------
    # Main loop: iterate deps_to_install and dispatch to the appropriate
    # per-package helper based on package source.  Per-dep progress is
    # routed through ``ctx.tui`` (workstream B, #1116); when the TUI is
    # disabled every method is a no-op.
    # ------------------------------------------------------------------
    prepared_integrations = []
    for dep_ref in deps_to_install:
        # Determine installation directory using namespaced structure
        # e.g., microsoft/apm-sample-package -> apm_modules/microsoft/apm-sample-package/
        # For virtual packages: owner/repo/prompts/file.prompt.md -> apm_modules/owner/repo-file/
        # For subdirectory packages: owner/repo/subdir -> apm_modules/owner/repo/subdir/
        install_path = dep_ref.get_install_path(apm_modules_dir)

        # Skip deps that already failed during BFS resolution callback
        # to avoid a duplicate error entry in diagnostics.
        dep_key = dep_ref.get_unique_key()
        if dep_key in ctx.callback_failures:
            if ctx.logger:
                ctx.logger.verbose_detail(
                    f"  Skipping {dep_key} (already failed during resolution)"
                )
            continue

        # --- Build the right DependencySource and run the template ---
        if dep_ref.is_local and dep_ref.local_path:
            source = make_dependency_source(
                ctx,
                dep_ref,
                install_path,
                dep_key,
            )
        else:
            resolved_ref, skip_download, dep_locked_chk, ref_changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )
            # A callback or pre-download can materialize bytes before
            # this loop while still selecting CachedDependencySource.
            _fetched_now = dep_key in ctx.callback_downloaded or dep_key in ctx.pre_downloaded_keys
            source = make_dependency_source(
                ctx,
                dep_ref,
                install_path,
                dep_key,
                resolved_ref=resolved_ref,
                dep_locked_chk=dep_locked_chk,
                ref_changed=ref_changed,
                skip_download=skip_download,
                fetched_this_run=_fetched_now,
            )

        materialization, terminal_deltas = prepare_integration_materialization(source)
        prepared_integrations.append(
            (dep_key, install_path, source, materialization, terminal_deltas)
        )

    materialized = [
        (source, materialization)
        for _, _, source, materialization, _ in prepared_integrations
        if materialization is not None
    ]
    # A batch refusal must not swallow the acquired packages' held [+]
    # headers: print them before the refusal surfaces (#3994).
    try:
        from apm_cli.install.phases.policy_gate import enforce_content_types

        enforce_content_types(ctx, materialized)
        preflight_agent_plugin_materializations(materialized)
        _fail_explicit_target_without_deploy(ctx)
        deployment_plans, local_plan = preflight_skill_materializations(ctx, materialized)
        if ctx.skill_baseline_refused:
            _announce_held_headers(prepared_integrations)
            return

        from apm_cli.install.service import InstallService

        if InstallService.frozen_disposition(ctx).replay and ctx.existing_lockfile is not None:
            from apm_cli.core.scope import is_user_scope
            from apm_cli.install.template import effective_allow_map
            from apm_cli.security.trust import McpAdmission

            InstallService.enforce_frozen_restored(
                [
                    *ctx.transitive_failures,
                    *failed_acquisitions(
                        ctx.diagnostics,
                        [
                            dep_key
                            for dep_key, _, _, materialization, terminal in prepared_integrations
                            if materialization is None and terminal is None
                        ],
                    ),
                ]
            )
            InstallService.enforce_frozen_selections(ctx.deps_to_install, ctx.existing_lockfile)
            effective_allow_map(ctx)
            InstallService.enforce_hydrated_mcp(
                ctx.apm_package,
                ctx.existing_lockfile,
                apm_modules_dir,
                # The pipeline's routed, cached trust context (already resolved
                # for materialized packages) is the same one deployment uses.
                admission=McpAdmission.for_project(
                    ctx.project_root,
                    user_scope=is_user_scope(ctx.scope),
                    one_off=ctx.trust_transitive_mcp,
                    trust_context=getattr(effective_allow_map(ctx), "trust_context", None)
                    or getattr(ctx, "exec_trust_ctx", None),
                ),
                logger=ctx.logger,
                active_package_keys=frozenset(ctx.update_plan_complete_dep_keys),
            )
            # Hook approval pins are re-checked through the trust owner before
            # any hook deploys or retires, as audit --ci checks them (#4188).
            InstallService.enforce_frozen_hook_pins(
                ctx.existing_lockfile,
                apm_modules_dir,
                getattr(effective_allow_map(ctx), "trust_context", None)
                or getattr(ctx, "exec_trust_ctx", None),
            )
            # A deny, a revoked approval or a policy change since the lock
            # refuses after the specific checks above, before any executable
            # deploys or retires (RA-024).
            from apm_cli.install.frozen_retirement import refuse_stale_exec_decisions

            refuse_stale_exec_decisions(ctx, ctx.existing_lockfile, ctx.lockfile_path.parent)
        from apm_cli.install.commit_gate import check_before_deploy

        check_before_deploy(ctx, _write_plan(ctx, materialized, deployment_plans, local_plan))
        if ctx.overwrite_preflight_refused:
            _announce_held_headers(prepared_integrations)
            return
    except BaseException:
        _announce_held_headers(prepared_integrations)
        raise

    for dep_key, install_path, source, materialization, terminal_deltas in prepared_integrations:
        if terminal_deltas is not None:
            announce_package(source)
            deltas = terminal_deltas
        else:
            deltas = run_integration_template(
                source,
                materialization=materialization,
                deployment_plan=deployment_plans.get(dep_key),
            )

        if deltas is None:
            # Direct dependency failure: surface a single concise
            # inline marker so the user sees `[x] <pkg>: integration
            # failed` immediately (fixes "perceived hang" on HYBRID
            # validation failures). The full diagnostic detail --
            # resolved path and `--verbose` hint -- is rendered once
            # by `render_summary()` to avoid double-output.
            if dep_key in direct_dep_keys:
                if ctx.diagnostics:
                    ctx.diagnostics.error(
                        f"{dep_key}: integration failed",
                        package=dep_key,
                        detail=(f"Resolved at {install_path}. Run with --verbose for details."),
                    )
                elif ctx.logger:
                    ctx.logger.error(f"{dep_key}: integration failed")
                ctx.direct_dep_failed = True
            continue

        # Accumulate counter deltas from this package
        installed_count += deltas.get("installed", 0)
        unpinned_count += deltas.get("unpinned", 0)
        total_prompts_integrated += deltas.get("prompts", 0)
        total_agents_integrated += deltas.get("agents", 0)
        total_skills_integrated += deltas.get("skills", 0)
        total_sub_skills_promoted += deltas.get("sub_skills", 0)
        total_instructions_integrated += deltas.get("instructions", 0)
        total_commands_integrated += deltas.get("commands", 0)
        total_hooks_integrated += deltas.get("hooks", 0)
        total_links_resolved += deltas.get("links_resolved", 0)

    # ------------------------------------------------------------------
    # Integrate root project's own .apm/ primitives (#714).
    # ------------------------------------------------------------------
    root_deltas = _integrate_root_project(ctx, deployment_plan=local_plan)
    if root_deltas:
        total_prompts_integrated += root_deltas.get("prompts", 0)
        total_agents_integrated += root_deltas.get("agents", 0)
        total_skills_integrated += root_deltas.get("skills", 0)
        total_sub_skills_promoted += root_deltas.get("sub_skills", 0)
        total_instructions_integrated += root_deltas.get("instructions", 0)
        total_commands_integrated += root_deltas.get("commands", 0)
        total_hooks_integrated += root_deltas.get("hooks", 0)
        total_links_resolved += root_deltas.get("links_resolved", 0)

    # ------------------------------------------------------------------
    # Write int counters back to ctx (mutable containers already share
    # the reference and need no write-back).
    # ------------------------------------------------------------------
    ctx.installed_count = installed_count
    ctx.unpinned_count = unpinned_count
    ctx.total_prompts_integrated = total_prompts_integrated
    ctx.total_agents_integrated = total_agents_integrated
    ctx.total_skills_integrated = total_skills_integrated
    ctx.total_sub_skills_promoted = total_sub_skills_promoted
    ctx.total_instructions_integrated = total_instructions_integrated
    ctx.total_commands_integrated = total_commands_integrated
    ctx.total_hooks_integrated = total_hooks_integrated
    ctx.total_links_resolved = total_links_resolved
    ctx.kept_deployed_values = _kept_skill_values(ctx)

    # ------------------------------------------------------------------
    # Amendment 7: cowork 50-skill / 1 MB cap check (warn-only).
    # Runs once per install, after all packages integrate, only when
    # a cowork target with a resolved_deploy_root is active.
    # ------------------------------------------------------------------
    _check_cowork_caps(ctx)
    if session is not None:
        ctx.legacy_adopted = session.legacy_adopted
    if session is not None and session.kept_overwrites:
        _keep_prior_entries(ctx, session.kept_overwrites, session.kept_edit_kinds)
        ctx.kept_overwrite_written = session.written_entries()

    # ------------------------------------------------------------------
    # Executable approval prompt: if any packages had their hooks or
    # bin/ blocked, prompt the user to approve them (interactive) or
    # hard-error (CI).  Approved packages are persisted to apm.yml so
    # the next ``apm install`` deploys them automatically.
    # ------------------------------------------------------------------
    _run_executable_approval_prompt(ctx)
