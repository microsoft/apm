"""Install commit order: every check passes before anything is committed.

An install commits two things: the files integrators deploy to targets and
``apm.lock.yaml``. Spec req-pl-002 requires a blocking check to stop the
install before any of those bytes is written, so this module is the single
owner of the checks that guard both commit points:

* :func:`check_before_deploy` runs once inside the integrate phase, after
  every package is acquired and before the first integrator write.
* :func:`check_before_lockfile` runs on the fully built lockfile before it
  is written.

A failing check raises; :class:`~apm_cli.install.transaction.InstallTransaction`
then restores ``apm.yml`` and ``apm_modules`` staging, so nothing is committed.
The install-time content audit must scan deployed bytes, so it runs after
deploy; the transaction journals every path integrators write (#4282) and
restores them if the install fails before its lockfile commits.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from collections.abc import Sequence

    from apm_cli.core.target_detection import EffectiveTargetDecision
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.context import InstallContext


class _AnnouncedPolicyLogger:
    """Forward to the install logger; the policy gate already announced the policy.

    That includes the notices that enforcement is disabled or that no
    usable org policy was found.
    """

    def __init__(self, logger: Any) -> None:
        self._logger = logger

    def policy_resolved(self, **_kwargs: Any) -> None:
        return None

    def policy_disabled(self, _reason: str) -> None:
        return None

    def policy_discovery_miss(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._logger, name)


class _SilentLogger:
    """Discard collection output; the service stage reports admissions once."""

    verbose = False

    def __getattr__(self, _name: str) -> Any:
        return lambda *_args, **_kwargs: None


def policy_subject_mcp_deps(
    mcp_deps: list[Any],
    *,
    runtime: str | None,
    target_decision: EffectiveTargetDecision | None,
    user_scope: bool,
) -> list[Any]:
    """Return the MCP servers APM itself would write for the active runtimes.

    A server every active runtime loads natively from its plugin is written
    nowhere by APM; native admission already applied org MCP policy to it.
    """
    from apm_cli.install.mcp.native_runtimes import native_runtimes_of

    active_runtimes = (
        [runtime]
        if runtime is not None
        else target_decision.runtime_targets_for_scope(user_scope=user_scope)
        if target_decision is not None
        else None
    )
    return [
        dep
        for dep in mcp_deps
        if not active_runtimes or not set(active_runtimes) <= native_runtimes_of(dep)
    ]


def enforce_mcp_policy(
    mcp_deps: list[Any],
    *,
    brought_in_by: dict[str, str] | None,
    runtime: str | None,
    target_decision: EffectiveTargetDecision | None,
    user_scope: bool,
    project_root: Path,
    no_policy: bool,
    logger: Any,
    dry_run: bool = False,
    effective_target: tuple[str, ...] | None = None,
    effective_target_source: str = "effective target",
) -> None:
    """Enforce org MCP policy on the merged direct and transitive server set.

    An entry without a package pipeline (``install --mcp``) also passes its
    *effective_target* projection so target policy is judged in the same call.
    """
    from apm_cli.policy.install_preflight import run_policy_preflight

    subjects = policy_subject_mcp_deps(
        mcp_deps, runtime=runtime, target_decision=target_decision, user_scope=user_scope
    )
    if not subjects and effective_target is None:
        return
    from apm_cli.core.scope import manifest_root

    run_policy_preflight(
        # Org policy is discovered from the scope manifest ($APM_HOME under -g).
        project_root=manifest_root(project_root, user_scope=user_scope),
        mcp_deps=subjects,
        mcp_brought_in_by=brought_in_by,
        no_policy=no_policy,
        logger=logger,
        dry_run=dry_run,
        effective_target=effective_target,
        effective_target_source=effective_target_source,
    )


def refuse_blocked_mcp(logger: Any, exc: BaseException | None = None) -> NoReturn:
    """Render the MCP policy refusal every install entry shows, then fail the install.

    A discovery-level block (no ``audit_result``, e.g. ``fetch_failure_default:
    block`` with no reachable policy) prints its own reason (#4179); a check
    refusal already reported its violations, so it prints the summary line.
    """
    from apm_cli.install.errors import InstallFailureAlreadyRendered

    if exc is not None and getattr(exc, "audit_result", None) is None and str(exc):
        logger.refusal(str(exc))
    else:
        logger.refusal("MCP server(s) blocked by org policy. MCP configs were NOT written.")
    logger.render_summary()
    raise InstallFailureAlreadyRendered("MCP server(s) blocked by org policy") from None


def _resolved_rows(ctx: InstallContext) -> list[tuple[Any, None, int, str | None, bool]]:
    """Return lockfile rows for the resolved packages, before acquisition."""
    from apm_cli.install.source_helpers import locked_parent_identity

    tree = ctx.dependency_graph.dependency_tree if ctx.dependency_graph is not None else None
    rows = []
    for dependency in ctx.deps_to_install:
        node = tree.get_node(dependency.get_unique_key()) if tree is not None else None
        depth = node.depth if node else 1
        is_dev = bool(node.is_dev) if node else False
        rows.append((dependency, None, depth, locked_parent_identity(ctx, node), is_dev))
    return rows


def prospective_lockfile(ctx: InstallContext) -> LockFile:
    """Return the package set this install is about to commit.

    Every package is acquired before the first deploy, so its lock row is
    known: rows acquired this run replace the existing lockfile's rows, and
    only packages in the resolved graph are kept. ``--dry-run`` stops before
    acquisition, so its rows come from the resolved packages.
    """
    return _lockfile_from_rows(ctx, _resolved_rows(ctx) if ctx.dry_run else ctx.installed_packages)


def resolved_lockfile(ctx: InstallContext) -> LockFile:
    """The package set of the resolved graph, before anything is downloaded (#4826)."""
    return _lockfile_from_rows(ctx, _resolved_rows(ctx))


def _lockfile_from_rows(ctx: InstallContext, rows: Any) -> LockFile:
    from apm_cli.deps.lockfile import LockFile

    acquired = LockFile.from_installed_packages(rows, ctx.dependency_graph)
    for key, dependency in acquired.dependencies.items():
        if key in ctx.package_hashes:
            dependency.content_hash = ctx.package_hashes[key]
        # The acquired type (e.g. a Claude plugin without apm.yml) decides
        # which MCP declarations the policy check collects.
        if key in ctx.package_types:
            dependency.package_type = ctx.package_types[key]
    prospective = LockFile(apm_version=acquired.apm_version)
    active = ctx.update_plan_complete_dep_keys
    existing = ctx.existing_lockfile
    if existing is not None:
        for key, dependency in existing.dependencies.items():
            if key in active and key not in acquired.dependencies:
                prospective.dependencies[key] = dependency
    prospective.dependencies.update(acquired.dependencies)
    return prospective


def _prepare_merged_mcp(
    ctx: InstallContext, prospective: LockFile, native_keys: frozenset[str] = frozenset()
) -> Any:
    """The merged MCP set the acquired packages declare, prepared silently."""
    from apm_cli.core.scope import is_user_scope
    from apm_cli.deps.lockfile import get_lockfile_path
    from apm_cli.install.mcp.integration import prepare_mcp_integration
    from apm_cli.install.template import effective_allow_map

    return prepare_mcp_integration(
        apm_package=ctx.apm_package,
        mcp_deps=list(ctx.apm_package.get_all_mcp_dependencies()),
        apm_modules_path=ctx.apm_modules_dir,
        lock_path=get_lockfile_path(ctx.apm_dir),
        should_install=True,
        trust_transitive_mcp=ctx.trust_transitive_mcp,
        logger=_SilentLogger(),
        active_package_keys=frozenset(ctx.update_plan_complete_dep_keys),
        user_scope=is_user_scope(ctx.scope),
        project_root=ctx.project_root,
        executable_context=(effective_allow_map(ctx), True),
        no_policy=ctx.no_policy,
        claude_native_keys=frozenset(ctx.claude_native_keys) | native_keys,
        prospective_lock=prospective,
        dry_run=bool(ctx.dry_run),
    )


def _check_merged_mcp_policy(ctx: InstallContext, prospective: LockFile) -> None:
    """Collect the merged MCP set from acquired packages and enforce policy."""
    from apm_cli.core.scope import is_user_scope

    user_scope = is_user_scope(ctx.scope)
    prepared = _prepare_merged_mcp(ctx, prospective)
    view = prepared.current_view
    if ctx.dry_run and view is not None and ctx.logger is not None:
        from apm_cli.install.errors import policy_transitive_refusals

        # The real install stops here (refuse_policy_transitive); preview it.
        for name in policy_transitive_refusals(view):
            ctx.logger.warning(
                f"Would be blocked by policy: {name} -- transitive server not in "
                "mcp.allow (mcp.trust_transitive: false)"
            )
    ctx.preview_mcp_dependencies = tuple(prepared.dependencies)
    enforce_mcp_policy(
        list(prepared.dependencies),
        brought_in_by=view.brought_in_by if view is not None else None,
        runtime=ctx.mcp_runtime,
        target_decision=ctx.target_decision,
        user_scope=user_scope,
        project_root=ctx.project_root,
        no_policy=ctx.no_policy,
        logger=_AnnouncedPolicyLogger(ctx.logger),
        dry_run=ctx.dry_run,
    )
    ctx.mcp_policy_enforced = True


def check_before_resolve(ctx: InstallContext, committed: LockFile | None) -> None:
    """Refuse a frozen install the committed lockfile already fails, before any write.

    ``--frozen`` commits the existing lockfile, so its ``require_hashes``
    verdict is known before resolution. Judging it here keeps a refused
    frozen run from re-materializing ``apm_modules`` or reporting packages
    it will not install (I7). :func:`check_before_deploy` judges the same
    lockfile again for every other install.
    """
    from apm_cli.install.service import InstallService

    _refuse_denied_scripts_before_fetch(ctx)
    if ctx.dry_run or ctx.no_policy or committed is None:
        return
    if not InstallService.frozen_disposition(ctx).withhold_lockfile_writes:
        return
    from apm_cli.core.scope import is_user_scope, manifest_root
    from apm_cli.deps.lockfile import get_lockfile_path
    from apm_cli.install.integrity import enforce_policy_hashes
    from apm_cli.policy.outcome_routing import resolve_trust_policy

    policy = resolve_trust_policy(
        manifest_root(ctx.project_root, user_scope=is_user_scope(getattr(ctx, "scope", None))),
        announce=False,
        raise_blocking_errors=False,
    )
    enforce_policy_hashes(policy, committed, get_lockfile_path(ctx.apm_dir))


def check_before_deploy(ctx: InstallContext, write_plan: Sequence[PlannedPackage] = ()) -> None:
    """Run every check that must pass before the first deployed byte.

    *write_plan* holds every package the integrate phase is about to deploy;
    a managed file it would replace without deployment evidence refuses the
    whole install here, before any write (#4381).
    """
    from apm_cli.install.integrity import enforce_installed_hash_policy
    from apm_cli.install.service import InstallService

    prospective = prospective_lockfile(ctx)
    if not ctx.dry_run:
        # --dry-run acquires nothing, so no content hash exists to check yet;
        # --frozen commits the existing lockfile, so that is what is judged.
        frozen = InstallService.frozen_disposition(ctx).withhold_lockfile_writes
        enforce_installed_hash_policy(ctx, ctx.existing_lockfile if frozen else prospective)
    if ctx.install_mcp and not ctx.lockfile_only and ctx.apm_package is not None:
        _check_merged_mcp_policy(ctx, prospective)
        if not ctx.dry_run:
            _refuse_native_mcp_collisions(ctx, prospective, write_plan)
    if write_plan and not ctx.dry_run and not ctx.force:
        _refuse_unproven_overwrites(ctx, write_plan)
    _check_manifest_policy(ctx)


def blocking_install_policy(ctx: InstallContext) -> Any:
    """The blocking org policy, known before resolution fetches anything, or ``None``.

    The same policy the identity preflight uses to refuse a denied dependency
    unfetched: ``None`` under ``--no-policy``, ``--dry-run``, or a policy that
    does not block.
    """
    if ctx.dry_run:
        return None
    from apm_cli.core.scope import is_user_scope, manifest_root
    from apm_cli.policy.install_preflight import prefetch_identity_policy

    root = manifest_root(ctx.project_root, user_scope=is_user_scope(getattr(ctx, "scope", None)))
    return prefetch_identity_policy(root, no_policy=ctx.no_policy)


def _refuse_denied_scripts_before_fetch(ctx: InstallContext) -> None:
    """Refuse ``manifest.scripts: deny`` before resolution writes a byte (#4826)."""
    if not getattr(ctx.apm_package, "scripts", None):
        return
    from apm_cli.core.scope import is_user_scope, manifest_root

    refuse_denied_scripts(
        ctx.apm_package.scripts,
        manifest_root(ctx.project_root, user_scope=is_user_scope(getattr(ctx, "scope", None))),
        no_policy=ctx.no_policy,
        dry_run=ctx.dry_run,
        logger=ctx.logger,
    )


def refuse_denied_install_entry(scope: Any, *, no_policy: bool, dry_run: bool, logger: Any) -> None:
    """Refuse a scope manifest ``scripts:`` block before any install entry writes (#4826).

    Every ``apm install`` entry -- a local bundle, ``--mcp`` and the
    dependency pipeline -- passes here first. ``unmanaged_files.action:
    deny`` is judged on the claims each entry leaves: the pipeline and a
    local bundle at their final-claims gates, ``--mcp`` (which records no
    file claims) by :func:`judge_mcp_install_files`.
    """
    from apm_cli.core.scope import get_manifest_path

    refuse_denied_manifest_scripts(
        get_manifest_path(scope), no_policy=no_policy, dry_run=dry_run, logger=logger
    )


def judge_mcp_install_files(scope: Any, *, no_policy: bool, logger: Any, dry_run: bool) -> None:
    """Judge the files a direct ``--mcp`` install leaves, after its policy preflight (#4826).

    ``--mcp`` records no file claims, so the governed directories the
    policy names are judged against the scope's current lockfile. A
    blocking real install refused a denied ``scripts:`` block at entry;
    ``warn`` warns on both verdicts and ``--dry-run`` previews them (#4957).
    """
    from apm_cli.core.scope import (
        get_apm_dir,
        get_deploy_root,
        get_manifest_path,
        is_user_scope,
        manifest_root,
    )
    from apm_cli.deps.lockfile import LockFile, resolve_lockfile_path_for_read
    from apm_cli.policy.install_preflight import enforced_entry_policy
    from apm_cli.policy.policy_checks import _check_unmanaged_files

    # scripts: is judged with the source manifest's policy; unmanaged files with
    # the policy of the root the install writes to, as the MCP preflight reads it.
    manifest_path = get_manifest_path(scope)
    files_root = manifest_root(get_deploy_root(scope), user_scope=is_user_scope(scope))
    roots = {"scripts": manifest_path.parent, "files": files_root}
    policies = {
        root: enforced_entry_policy(root, no_policy=no_policy) for root in set(roots.values())
    }
    checks: dict[Path, list[Any]] = {root: [] for root in policies}
    scripts_policy = policies[roots["scripts"]]
    if scripts_policy is not None:
        checks[roots["scripts"]].extend(
            _entry_scripts_checks(scripts_policy, manifest_path, dry_run=dry_run)
        )
    files_policy = policies[roots["files"]]
    if files_policy is not None and files_policy.unmanaged_files.effective_action == "deny":
        path = resolve_lockfile_path_for_read(get_apm_dir(scope), read_only=True)
        root, lockfile = get_deploy_root(scope), LockFile.read(path)
        checks[roots["files"]].append(
            _check_unmanaged_files(root, lockfile, files_policy.unmanaged_files)
        )
    for policy_root, found in checks.items():
        if found:
            _report_install_policy_checks(
                logger, policies[policy_root], found, dry_run=dry_run, source=None
            )


def judge_bundle_manifest_scripts(
    policy: Any, manifest_path: Path, *, dry_run: bool, logger: Any
) -> None:
    """Warn on, or preview, the scope manifest's ``scripts:`` block for a local bundle (#4957).

    *policy* is the one the bundle's admission enforced. A blocking real
    install refused the block at entry.
    """
    if policy is None or policy.enforcement not in ("block", "warn"):
        return
    checks = _entry_scripts_checks(policy, Path(manifest_path), dry_run=dry_run)
    _report_install_policy_checks(logger, policy, checks, dry_run=dry_run, source=None)


def _entry_scripts_checks(policy: Any, manifest_path: Path, *, dry_run: bool) -> list[Any]:
    """The ``manifest.scripts`` check an entry still reports: none once refused at entry."""
    if policy.manifest.scripts != "deny" or not manifest_path.is_file():
        return []
    if policy.enforcement == "block" and not dry_run:
        return []
    from apm_cli.policy.policy_checks import _load_raw_apm_yml

    scripts = (_load_raw_apm_yml(manifest_path.parent) or {}).get("scripts")
    return [_scripts_policy_check(scripts, policy)]


def _scripts_policy_check(scripts: Any, policy: Any) -> Any:
    from apm_cli.policy.policy_checks import _check_scripts_policy

    return _check_scripts_policy({"scripts": scripts}, policy.manifest)


def refuse_denied_manifest_scripts(
    manifest_path: Path, *, no_policy: bool, dry_run: bool, logger: Any
) -> None:
    """Refuse the scope manifest's ``scripts:`` block before the install command writes.

    Judged on the manifest file itself, before marketplace reconciliation,
    package validation, hook reconciliation or lockfile migration, with the
    policy the identity preflight reads at the same root (#4826).
    """
    if dry_run:
        return
    from apm_cli.policy.policy_checks import _load_raw_apm_yml

    raw = _load_raw_apm_yml(Path(manifest_path).parent) if Path(manifest_path).is_file() else None
    refuse_denied_scripts(
        (raw or {}).get("scripts"),
        Path(manifest_path).parent,
        no_policy=no_policy,
        dry_run=dry_run,
        logger=logger,
    )


def refuse_denied_scripts(
    scripts: Any, policy_root: Path, *, no_policy: bool, dry_run: bool, logger: Any
) -> None:
    """Refuse a ``scripts:`` block a blocking ``manifest.scripts: deny`` forbids (#4826).

    The verdict reads only the manifest, so it runs before any fetch, cache
    or deploy write, also for an install with no APM dependencies. ``warn``
    and ``--dry-run`` are reported at deploy time.
    """
    if dry_run or not scripts:
        return
    from apm_cli.policy.install_preflight import prefetch_identity_policy

    policy = prefetch_identity_policy(policy_root, no_policy=no_policy)
    if policy is None or policy.manifest.scripts != "deny":
        return
    _report_install_policy_checks(
        logger, policy, [_scripts_policy_check(scripts, policy)], dry_run=False, source=None
    )


def policy_depth_limit(ctx: InstallContext) -> tuple[Any, int]:
    """The resolver depth limit, and the blocking policy that set it (#4826).

    A blocking ``dependencies.max_depth`` below the resolver's own cap stops
    resolution at the limit, so nothing deeper is fetched; otherwise the
    resolver keeps its default cap and the policy gate judges the resolved
    graph.
    """
    from apm_cli.policy.schema import DependencyPolicy

    cap = DependencyPolicy.max_depth
    policy = blocking_install_policy(ctx)
    if policy is None or policy.dependencies.max_depth >= cap:
        return None, cap
    return policy, policy.dependencies.max_depth


def refuse_policy_depth_limit(ctx: InstallContext, policy: Any, chains: Sequence[str]) -> None:
    """Refuse the chains resolution cut at the policy's ``max_depth``, before any deploy."""
    if not chains:
        return
    from apm_cli.policy.depth_check import depth_limit_check

    _enforce_install_policy_checks(
        ctx, policy, [depth_limit_check(chains, policy.dependencies.max_depth)]
    )


def _scripts_check(ctx: InstallContext, policy: Any) -> Any:
    """Judge the scripts of the manifest this install acts on, whatever its root or scope."""
    from apm_cli.policy.policy_checks import _check_scripts_policy

    scripts = getattr(ctx.apm_package, "scripts", None)
    return _check_scripts_policy({"scripts": scripts}, policy.manifest)


def _check_manifest_policy(ctx: InstallContext) -> None:
    """Report org policy on files this install would leave, before deploy (#4826).

    ``manifest.scripts: deny`` warns (or, under ``--dry-run``, previews the
    refusal; a blocking real install refused it before resolution).
    ``--dry-run`` also previews ``unmanaged_files.action: deny`` on the files
    no row of the current lockfile claims.
    """
    policy = _enforced_install_policy(ctx)
    if policy is None:
        return
    checks = []
    if policy.manifest.scripts == "deny":
        checks.append(_scripts_check(ctx, policy))
    if ctx.dry_run and policy.unmanaged_files.effective_action == "deny":
        from apm_cli.policy.policy_checks import _check_unmanaged_files

        checks.append(
            _check_unmanaged_files(ctx.project_root, ctx.existing_lockfile, policy.unmanaged_files)
        )
    _enforce_install_policy_checks(ctx, policy, checks)


def _refuse_native_mcp_collisions(
    ctx: InstallContext, prospective: LockFile, write_plan: Sequence[PlannedPackage]
) -> None:
    """Refuse an MCP server that collides with a native connection, before any write.

    The targets resolve as the later MCP stage resolves them, from the
    command's ``--runtime`` and ``--exclude`` (carried by ``lsp_preflight``);
    without them the MCP stage runs the same projection before its first write.
    A package the plan routes to Claude natively has its servers loaded by
    Claude, as the MCP stage will know once integration queued it (#4741).
    """
    from apm_cli.core.scope import is_user_scope
    from apm_cli.install.lsp.integration import lsp_target_config
    from apm_cli.install.mcp.collision_preflight import refuse_projected_mcp_collisions
    from apm_cli.install.service import InstallService

    filters = ctx.lsp_preflight
    if filters is None or ctx.target_decision is None or not ctx.preview_mcp_dependencies:
        return
    native_keys = frozenset(
        package_info.dependency_ref.get_unique_key()
        for package_info, plan, _managed in write_plan
        if getattr(plan, "claude_native", None) is not None
        and getattr(package_info, "dependency_ref", None) is not None
    )
    dependencies = (
        _prepare_merged_mcp(ctx, prospective, native_keys).dependencies
        if native_keys - set(ctx.claude_native_keys)
        else ctx.preview_mcp_dependencies
    )
    refuse_projected_mcp_collisions(
        dependencies,
        lockfile=ctx.existing_lockfile,
        project_root=ctx.project_root,
        user_scope=is_user_scope(ctx.scope),
        logger=ctx.logger,
        replay_claims=InstallService.frozen_disposition(ctx).withhold_lockfile_writes,
        runtime=filters.runtime,
        exclude=filters.exclude,
        apm_config=lsp_target_config(ctx.apm_package),
        explicit_target=ctx.target_decision.value,
        target_decision=ctx.target_decision,
        scope=ctx.scope,
    )


# (package_info, its PackageDeploymentPlan, the managed files it integrates with)
PlannedPackage = tuple[Any, Any, "set[str] | None"]
# The file primitives whose managed files OverwriteGate guards (#4360).
_GATED_PRIMITIVES = ("prompts", "agents", "commands", "instructions")
# Targets whose primitives deploy outside files (the Copilot App's SQLite
# workflows): no OverwriteGate applies and planning them would write.
_UNGATED_TARGETS = frozenset({"copilot-app"})


def _refuse_unproven_overwrites(ctx: InstallContext, write_plan: Sequence[PlannedPackage]) -> None:
    """Name every managed file the plan would keep without evidence, then refuse (#4381).

    The integrators run their own plan through the write seam in planning
    mode: ``BaseIntegrator._check_adopt_or_skip`` decides each file with the
    deployment session's ``overwrite_authorized``, as the real run does, and
    records the files ``OverwriteGate`` would keep instead of writing. The
    gate stays the per-file backstop for anything the plan cannot see.
    """
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.deployed_paths import deployed_path_entry
    from apm_cli.integration.base_integrator import kept_edit_message

    skill = ctx.integrators.get("skill")
    if getattr(skill, "_materialization_session", None) is None or ctx.diagnostics is None:
        return
    if _gated_dest_dirs_empty(ctx, write_plan):
        # W3d S1: no gated dest dir holds a file, so no planned write can meet an
        # existing file and the planning pass could only return nothing.
        ctx.overwrite_preflight_refused = False
        return
    kept = [
        (owner, path, edit)
        for package_info, plan, managed in write_plan
        for owner, path, edit in _planned_keeps(ctx, skill, package_info, plan, managed)
    ]
    user_scope = ctx.scope is InstallScope.USER
    for owner, path, edit in kept:
        entry = deployed_path_entry(path, ctx.project_root, ctx.targets)
        message = kept_edit_message(entry, user_scope=user_scope, proven_edit=edit is not None)
        ctx.diagnostics.error(message, package=owner)
    ctx.overwrite_preflight_refused = bool(kept)


def _gated_dest_dirs_empty(ctx: InstallContext, write_plan: Sequence[PlannedPackage]) -> bool:
    """Whether every directory a gated primitive deploys into is absent or empty.

    One scandir per (target, gated primitive) dest dir. The targets phase creates
    the target roots, so the proof is per dest dir. Any doubt (user scope, an
    external or absolute root, an unreadable dir) answers ``False``: plan as today.
    """
    import os

    from apm_cli.core.scope import InstallScope

    if ctx.scope is InstallScope.USER or ctx.project_root is None:
        return False
    # The targets the planning pass would visit: each plan's own selection.
    targets = {
        id(t): t
        for _info, plan, _managed in write_plan
        if plan is not None and getattr(plan, "target_selection", None) is not None
        for t in plan.target_selection.targets
    }
    base = str(ctx.project_root)
    dirs: set[str] = set()
    for target in targets.values():
        if getattr(target, "name", None) in _UNGATED_TARGETS:
            continue
        root_dir = getattr(target, "root_dir", None)
        primitives = getattr(target, "primitives", None)
        if (
            not root_dir
            or os.path.isabs(root_dir)
            or primitives is None
            or getattr(target, "managed_deploy_root", None) is not None
        ):
            return False
        for primitive in _GATED_PRIMITIVES:
            mapping = primitives.get(primitive)
            if mapping is None:
                continue
            root = getattr(mapping, "deploy_root", None) or root_dir
            if os.path.isabs(root):
                return False
            dirs.add(
                os.path.join(base, root, mapping.subdir)
                if mapping.subdir
                else os.path.join(base, root)
            )
    for path in dirs:
        try:
            with os.scandir(path) as entries:
                if next(entries, None) is not None:
                    return False
        except FileNotFoundError:
            continue
        except OSError:
            return False
    return True


def _planned_keeps(
    ctx: InstallContext,
    skill: Any,
    package_info: Any,
    plan: Any,
    managed: set[str] | None,
) -> list[tuple[str, Path, str | None]]:
    """Plan one package's gated file writes; a package the plan cannot render is left to its run."""
    from apm_cli.copilot_plugins.capability import admits_native_plugin
    from apm_cli.core.deployment_ledger import recorded_writes
    from apm_cli.integration import write_seam
    from apm_cli.integration.dispatch import get_dispatch_table
    from apm_cli.utils.diagnostics import DiagnosticCollector

    if plan is None or plan.source_plan is None or plan.codex_native is not None:
        return []
    if admits_native_plugin(package_info):
        return []
    targets = [t for t in plan.target_selection.targets if t in plan.projection_targets]
    table = get_dispatch_table()
    kept: list[tuple[str, Path, str | None]] = []
    for primitive in _GATED_PRIMITIVES:
        integrator = ctx.integrators.get(primitive.removesuffix("s"))
        if integrator is None:
            continue
        for target in targets:
            if target.primitives.get(primitive) is None or target.name in _UNGATED_TARGETS:
                continue
            planned: list[tuple[Path, str | None]] = []
            integrator._overwrite_plan = planned
            owner = ""
            try:
                with (
                    write_seam.planning(),
                    recorded_writes(skill, integrator, [target], package_info),
                ):
                    owner = integrator._overwrite_gate.owner
                    getattr(integrator, table[primitive].integrate_method)(
                        target,
                        package_info,
                        ctx.project_root,
                        force=ctx.force,
                        managed_files=managed,
                        diagnostics=DiagnosticCollector(),
                        scope=ctx.scope,
                        source_plan=plan.source_plan,
                    )
            except write_seam.PlannedWriteError:
                raise
            except Exception:
                # The real run reports this package's own failure; files the
                # plan already found unproven still refuse the install.
                return [*kept, *((owner, path, edit) for path, edit in planned)]
            finally:
                integrator._overwrite_plan = None
            kept.extend((owner, path, edit) for path, edit in planned)
    return kept


def check_before_lockfile(ctx: InstallContext, lockfile: LockFile | None) -> None:
    """Run every check on the built lockfile before it is written."""
    from apm_cli.install.integrity import enforce_installed_hash_policy

    enforce_installed_hash_policy(ctx, lockfile)


def check_before_release(ctx: InstallContext, lockfile: LockFile | None) -> None:
    """Judge ``unmanaged_files.action: deny`` while the deploy journal can still undo (#4826).

    *lockfile* is the staged lockfile after local ``.apm/`` content recorded
    its claims: a governed file no row claims is one this install would leave
    unmanaged. A refusal fails the install, and the transaction restores
    every file it deployed and discards the staged lockfile.
    """
    refuse_unmanaged_files(
        ctx.logger,
        _enforced_install_policy(ctx),
        ctx.project_root,
        lockfile,
        dry_run=ctx.dry_run,
        source=getattr(getattr(ctx, "policy_fetch", None), "source", None),
    )


def refuse_unmanaged_files(
    logger: Any,
    policy: Any,
    project_root: Path,
    lockfile: LockFile | None,
    *,
    dry_run: bool,
    source: str | None = None,
) -> None:
    """Refuse a governed file no row of *lockfile* claims under ``unmanaged_files: deny``."""
    if policy is None or policy.unmanaged_files.effective_action != "deny":
        return
    from apm_cli.policy.policy_checks import _check_unmanaged_files

    _report_install_policy_checks(
        logger,
        policy,
        [_check_unmanaged_files(project_root, lockfile, policy.unmanaged_files)],
        dry_run=dry_run,
        source=source,
    )


def _enforced_install_policy(ctx: InstallContext) -> Any:
    """The org policy the install's policy gate enforces, or ``None``."""
    if not getattr(ctx, "policy_enforcement_active", False):
        return None
    return getattr(getattr(ctx, "policy_fetch", None), "policy", None)


def _enforce_install_policy_checks(ctx: InstallContext, policy: Any, checks: list[Any]) -> None:
    """Refuse (``block``) or warn (``warn``) on failed org-policy *checks* of this install."""
    _report_install_policy_checks(
        ctx.logger,
        policy,
        checks,
        dry_run=ctx.dry_run,
        source=getattr(getattr(ctx, "policy_fetch", None), "source", None),
    )


def _report_install_policy_checks(
    logger: Any, policy: Any, checks: list[Any], *, dry_run: bool, source: str | None
) -> None:
    """Report failed *checks* through the install policy reporter; refuse under ``block``.

    The reporter names the policy field and the fix. A ``--dry-run``
    previews the verdict and never stops.
    """
    from apm_cli.policy.install_preflight import emit_check_violations, render_dry_run_verdicts
    from apm_cli.policy.models import CIAuditResult

    failed = [check for check in checks if not check.passed]
    if not failed:
        return
    if dry_run:
        if logger is not None:
            render_dry_run_verdicts(logger, CIAuditResult(checks=failed), policy.enforcement)
        return
    severity = "block" if policy.enforcement == "block" else "warn"
    if logger is not None:
        for check in failed:
            emit_check_violations(logger, check, severity=severity, source=source)
    if severity == "block":
        from apm_cli.install.errors import InstallFailureAlreadyRendered

        if logger is not None:
            logger.render_summary()
        raise InstallFailureAlreadyRendered("Install blocked by org policy")
