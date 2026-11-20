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
deploy; while it can block, the transaction snapshots the deploy paths before
integration and restores them if the install fails before the audit passes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from pathlib import Path

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
    from apm_cli.deps.lockfile import LockFile

    acquired = LockFile.from_installed_packages(
        _resolved_rows(ctx) if ctx.dry_run else ctx.installed_packages, ctx.dependency_graph
    )
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


def _check_merged_mcp_policy(ctx: InstallContext, prospective: LockFile) -> None:
    """Collect the merged MCP set from acquired packages and enforce policy."""
    from apm_cli.core.scope import is_user_scope
    from apm_cli.deps.lockfile import get_lockfile_path
    from apm_cli.install.mcp.integration import prepare_mcp_integration
    from apm_cli.install.template import effective_allow_map

    user_scope = is_user_scope(ctx.scope)
    prepared = prepare_mcp_integration(
        apm_package=ctx.apm_package,
        mcp_deps=list(ctx.apm_package.get_all_mcp_dependencies()),
        apm_modules_path=ctx.apm_modules_dir,
        lock_path=get_lockfile_path(ctx.apm_dir),
        should_install=True,
        trust_transitive_mcp=ctx.trust_transitive_mcp,
        logger=_SilentLogger(),
        active_package_keys=frozenset(ctx.update_plan_complete_dep_keys),
        user_scope=user_scope,
        project_root=ctx.project_root,
        executable_context=(effective_allow_map(ctx), True),
        no_policy=ctx.no_policy,
        claude_native_keys=frozenset(ctx.claude_native_keys),
        prospective_lock=prospective,
    )
    view = prepared.current_view
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


def check_before_deploy(ctx: InstallContext) -> None:
    """Run every check that must pass before the first deployed byte."""
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


def check_before_lockfile(ctx: InstallContext, lockfile: LockFile | None) -> None:
    """Run every check on the built lockfile before it is written."""
    from apm_cli.install.integrity import enforce_installed_hash_policy

    enforce_installed_hash_policy(ctx, lockfile)
