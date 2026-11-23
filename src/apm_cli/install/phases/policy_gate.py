"""Policy enforcement gate phase.

Runs AFTER ``resolve.run(ctx)`` (so ``ctx.deps_to_install`` is populated)
and BEFORE ``targets.run(ctx)`` (so denied deps never reach integration).

Discovery outcomes are the closed set
``policy.outcome_routing.POLICY_DISCOVERY_OUTCOMES``; the routing owner
decides how each is handled.

Target-aware compilation checks are NOT performed here -- they run
AFTER the targets phase when the effective target is known
(W2-target-aware).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.install.errors import PolicyViolationError

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


# Re-export for backward compatibility: prior to #832 this class was
# defined here, and external test/integration code imports it via
# ``from apm_cli.install.phases.policy_gate import PolicyViolationError``.
__all__ = ["PolicyViolationError", "run"]


def run(ctx: InstallContext) -> None:
    """Execute the policy-gate phase.

    On return ``ctx.policy_fetch`` holds the full
    :class:`~apm_cli.policy.discovery.PolicyFetchResult` and
    ``ctx.policy_enforcement_active`` indicates whether dep checks ran.
    """
    # ------------------------------------------------------------------
    # 0. Escape-hatch: --no-policy / APM_POLICY_DISABLE=1
    # ------------------------------------------------------------------
    if _is_policy_disabled(ctx):
        return

    # ------------------------------------------------------------------
    # 1. Discovery
    # ------------------------------------------------------------------
    fetch_result = _discover_with_chain(ctx)
    ctx.policy_fetch = fetch_result

    logger = ctx.logger
    source = fetch_result.source

    # ------------------------------------------------------------------
    # 2. Route outcome through the policy-outcome routing owner.
    # Logging + fail-closed gating live in one place
    # (``policy/outcome_routing.py``) so this phase and the
    # ``install --mcp`` / ``install --dry-run`` preflight stay aligned.
    # ------------------------------------------------------------------
    from apm_cli.policy.outcome_routing import route_discovery_outcome

    fetch_failure_default = _read_project_fetch_failure_default(ctx)

    policy = route_discovery_outcome(
        fetch_result,
        logger=logger,
        fetch_failure_default=fetch_failure_default,
        raise_blocking_errors=getattr(ctx, "dry_run", False) is not True,
    )

    # ------------------------------------------------------------------
    # 3. Enforcement gate (found / cached_stale paths only)
    # ------------------------------------------------------------------
    if policy is None:
        ctx.policy_enforcement_active = False
        return

    enforcement = policy.enforcement

    # enforcement: off -- nothing to do
    if enforcement == "off":
        if logger:
            logger.verbose_detail("Policy enforcement is off; dependency checks skipped")
        ctx.policy_enforcement_active = False
        return

    ctx.policy_enforcement_active = True

    # ------------------------------------------------------------------
    # 4. Run dependency policy checks
    # ------------------------------------------------------------------
    from apm_cli.policy.policy_checks import (
        MarketplaceOrigins,
        marketplace_resolution_origins,
        run_dependency_policy_checks,
    )

    mcp_deps = getattr(ctx, "direct_mcp_deps", None)

    # Pass manifest.includes only when we actually have an APMPackage --
    # leaving the kwarg unset preserves the legacy behaviour for callers
    # that have no manifest context (the seam treats "unset" as "skip
    # explicit-includes check").
    extra_kwargs = {}
    apm_package = getattr(ctx, "apm_package", None)
    if apm_package is not None:
        extra_kwargs["manifest_includes"] = getattr(apm_package, "includes", None)
        # Plumb the manifest registries: block so _check_registry_source
        # can distinguish "configured" from "unreachable". Without this,
        # any policy.registry_source.require name is falsely flagged as
        # unconfigured even when the user wired the registry correctly.
        registries_map = getattr(apm_package, "registries", None)
        if registries_map is not None:
            extra_kwargs["registries"] = registries_map

    audit_result = run_dependency_policy_checks(
        _with_prefetch_denials(ctx),
        lockfile=ctx.existing_lockfile,
        policy=policy,
        mcp_deps=mcp_deps,
        effective_target=None,  # target-aware checks after targets phase
        fetch_outcome=fetch_result.outcome,
        fail_fast=(enforcement == "block"),
        direct_dep_keys={d.get_unique_key() for d in getattr(ctx, "all_apm_deps", []) or []},
        declared_refs=getattr(ctx, "declared_direct_refs", None) or None,
        # This install locks deps_to_install; a required one it adds is not absent.
        installing=True,
        # Root ``marketplaces:`` origins, and the catalog origins of locked
        # and freshly resolved marketplace deps, meet marketplace.allow (#2903).
        marketplace_origins=MarketplaceOrigins(
            getattr(apm_package, "marketplaces", None),
            marketplace_resolution_origins(
                ctx.existing_lockfile,
                keys=[d.get_unique_key() for d in ctx.deps_to_install],
                provenance=getattr(ctx, "marketplace_provenance", None),
            ),
        ),
        **extra_kwargs,
    )

    # ------------------------------------------------------------------
    # 5. Route violations through logger
    # ------------------------------------------------------------------
    # One violation per offending dependency / MCP server; the check
    # name is only the category (#3835).
    from apm_cli.policy.install_preflight import emit_check_violations, render_dry_run_verdicts

    if getattr(ctx, "dry_run", False) is True:
        # --dry-run previews the same verdicts and never stops the preview.
        if not audit_result.passed and logger:
            render_dry_run_verdicts(logger, audit_result, enforcement)
        return

    has_blocking = False
    for check in audit_result.checks:
        if not check.passed:
            severity = "block" if enforcement == "block" else "warn"
            if logger:
                emit_check_violations(logger, check, severity=severity, source=source)
            if severity == "block":
                has_blocking = True
        elif check.details and logger:
            # project-wins version-pin mismatches and self-defined MCP
            # warnings are passed=True with warning details.  Emit them so
            # warn-mode surfaces all diagnostics.
            emit_check_violations(logger, check, severity="warn", source=source)

    if has_blocking:
        raise PolicyViolationError(
            "Install blocked by org policy -- see violations above. Remove or replace "
            "the blocked dependencies in apm.yml (run 'apm policy status' to see the "
            "active policy), then re-run the install.",
            audit_result=audit_result,
            policy_source=source or "unknown",
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _with_prefetch_denials(ctx: InstallContext) -> list:
    """The resolved dependencies plus any refused by identity before fetching.

    A positional install keeps only the denials inside its selection, the
    same filter its resolved dependencies got.
    """
    denied = getattr(ctx, "prefetch_policy_denials", None) or ()
    graph = getattr(ctx, "dependency_graph", None)
    if denied and getattr(ctx, "only_packages", None) and graph is not None:
        from apm_cli.install.package_selection import selected_subtree_dependencies

        denied = selected_subtree_dependencies(
            ctx.only_packages, graph.dependency_tree, list(denied)
        )
    if not denied:
        return ctx.deps_to_install
    deps = list(ctx.deps_to_install)
    seen = {dep.get_unique_key() for dep in deps}
    for dep in denied:
        if dep.get_unique_key() not in seen:
            seen.add(dep.get_unique_key())
            deps.append(dep)
    return deps


def _is_policy_disabled(ctx: InstallContext) -> bool:
    """Check escape hatches: ctx.no_policy flag and APM_POLICY_DISABLE env."""
    logger = ctx.logger

    if getattr(ctx, "no_policy", False):
        if logger:
            logger.policy_disabled("--no-policy")
        return True

    from apm_cli.policy.discovery import policy_disabled_by_env

    if policy_disabled_by_env():
        if logger:
            logger.policy_disabled("APM_POLICY_DISABLE=1")
        return True

    return False


def _read_project_fetch_failure_default(ctx: InstallContext) -> str:
    """Resolve project-side ``policy.fetch_failure_default`` (closes #829).

    Reads from ctx attribute first (test-friendly override) then falls
    back to parsing the scope manifest. Default is ``"warn"``.
    """
    explicit = getattr(ctx, "policy_fetch_failure_default", None)
    if isinstance(explicit, str) and explicit in {"warn", "block"}:
        return explicit
    from apm_cli.policy.project_config import read_project_fetch_failure_default

    return read_project_fetch_failure_default(_policy_root(ctx))


def _policy_root(ctx: InstallContext):
    """Return the scope manifest root that policy settings and discovery read.

    At user scope this is ``$APM_HOME``, not the deploy root (HOME).
    """
    from apm_cli.core.scope import is_user_scope, manifest_root

    return manifest_root(ctx.project_root, user_scope=is_user_scope(getattr(ctx, "scope", None)))


def _discover_with_chain(ctx: InstallContext):
    """Run chain-aware discovery via the shared seam in ``discovery.py``.

    Delegates to :func:`~apm_cli.policy.discovery.discover_policy_with_chain`
    which walks the inheritance chain, merges effective policy, and persists
    the cache with real ``chain_refs``.
    """
    from apm_cli.policy.discovery import discover_policy_with_chain

    return discover_policy_with_chain(_policy_root(ctx))


def _prefetch_policy_refusal(ctx: InstallContext):
    """Return ``refuse(dep_ref) -> bool``: a blocking org policy refuses by identity.

    The policy is read once, lazily, at the scope manifest root. A refused
    dependency is never contacted, cloned or cached; it is recorded on
    ``ctx.prefetch_policy_denials`` and the policy gate reports it.
    """
    from apm_cli.policy.install_preflight import identity_policy_denial, prefetch_identity_policy

    state: dict = {}

    def refuse(dep_ref) -> bool:
        if "policy" not in state:
            state["policy"] = prefetch_identity_policy(_policy_root(ctx), no_policy=ctx.no_policy)
        if identity_policy_denial(dep_ref, state["policy"]) is None:
            return False
        ctx.prefetch_policy_denials.append(dep_ref)
        return True

    return refuse


def _drop_policy_refused_replacement_errors(ctx: InstallContext, dependency_graph) -> None:
    """A replacement whose selected source policy refused before fetch is no load failure.

    The policy gate reports the refusal with the violation wording.
    """
    for dep_ref in ctx.prefetch_policy_denials:
        errors = dependency_graph.dependency_tree.replacement_load_errors
        error = errors.get(dep_ref.get_unique_key())
        if error in dependency_graph.resolution_errors:
            dependency_graph.resolution_errors.remove(error)


def enforce_content_types(ctx: InstallContext, materialized) -> None:
    """Judge ``manifest.content_types`` on the package images integrate will deploy.

    Runs once every package is acquired and normalized, before any deploy
    write, so a blocking policy refuses the install with nothing deployed and
    the lockfile untouched (``PolicyViolationError`` refuses the commit);
    ``warn`` reports and continues.
    """
    fetch = getattr(ctx, "policy_fetch", None)
    policy = getattr(fetch, "policy", None)
    if (
        not getattr(ctx, "policy_enforcement_active", False)
        or policy is None
        or policy.manifest.content_types is None
        or getattr(ctx, "dry_run", False) is True
    ):
        return
    from apm_cli.policy.content_types_check import check_content_types
    from apm_cli.policy.install_preflight import emit_check_violations
    from apm_cli.policy.models import CIAuditResult

    # A package with no targets materializes without package_info and deploys nothing.
    roots = {
        source.dep_key: (Path(materialization.package_info.install_path),)
        for source, materialization in materialized
        if materialization.package_info is not None
    }
    check = check_content_types(roots, policy.manifest)
    if check.passed:
        return
    severity = "block" if policy.enforcement == "block" else "warn"
    if ctx.logger:
        emit_check_violations(ctx.logger, check, severity=severity, source=fetch.source)
    if severity == "block":
        raise PolicyViolationError(
            "Install blocked by org policy manifest.content_types -- see violations above. "
            "Remove the packages that ship those content types from apm.yml (run "
            "'apm policy status' to see the active policy), then re-run the install.",
            audit_result=CIAuditResult(checks=[check]),
            policy_source=fetch.source or "unknown",
        )
