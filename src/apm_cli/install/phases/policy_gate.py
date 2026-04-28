"""Policy enforcement gate phase.

Runs AFTER ``resolve.run(ctx)`` (so ``ctx.deps_to_install`` is populated)
and BEFORE ``targets.run(ctx)`` (so denied deps never reach integration).

Discovery outcomes (plan section B, 9-outcome matrix):
  found, absent, cached_stale, cache_miss_fetch_fail, malformed,
  disabled, garbage_response, no_git_remote, empty

Target-aware compilation checks are NOT performed here -- they run
AFTER the targets phase when the effective target is known
(W2-target-aware).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from apm_cli.install.errors import PolicyViolationError

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


# Re-export for backward compatibility: prior to #832 this class was
# defined here, and external test/integration code imports it via
# ``from apm_cli.install.phases.policy_gate import PolicyViolationError``.
__all__ = ["PolicyViolationError", "run"]


def run(ctx: "InstallContext") -> None:
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
    # 2. Route outcome through the shared 9-outcome table.
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
        raise_blocking_errors=True,
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
            logger.verbose_detail(
                "Policy enforcement is off; dependency checks skipped"
            )
        ctx.policy_enforcement_active = False
        return

    ctx.policy_enforcement_active = True

    # ------------------------------------------------------------------
    # 4. Run dependency policy checks
    # ------------------------------------------------------------------
    from apm_cli.policy.policy_checks import run_dependency_policy_checks

    mcp_deps = getattr(ctx, "direct_mcp_deps", None)

    # Pass manifest.includes only when we actually have an APMPackage --
    # leaving the kwarg unset preserves the legacy behaviour for callers
    # that have no manifest context (the seam treats "unset" as "skip
    # explicit-includes check").
    extra_kwargs = {}
    apm_package = getattr(ctx, "apm_package", None)
    if apm_package is not None:
        extra_kwargs["manifest_includes"] = getattr(
            apm_package, "includes", None
        )

    audit_result = run_dependency_policy_checks(
        ctx.deps_to_install,
        lockfile=ctx.existing_lockfile,
        policy=policy,
        mcp_deps=mcp_deps,
        effective_target=None,  # target-aware checks after targets phase
        fetch_outcome=fetch_result.outcome,
        fail_fast=(enforcement == "block"),
        **extra_kwargs,
    )

    # ------------------------------------------------------------------
    # 5. Route violations through logger
    # ------------------------------------------------------------------
    has_blocking = False
    for check in audit_result.checks:
        if not check.passed:
            severity = "block" if enforcement == "block" else "warn"
            reason = check.message
            # Include detail lines for richer diagnostics
            if check.details:
                reason = f"{check.message}: {', '.join(check.details[:5])}"
            if logger:
                logger.policy_violation(
                    dep_ref=check.name,
                    reason=reason,
                    severity=severity,
                    source=source,
                )
            if severity == "block":
                has_blocking = True
        elif check.details:
            # project-wins version-pin mismatches are passed=True with
            # warning details (policy_checks.py:228-235).  Emit them so
            # warn-mode surfaces all diagnostics.
            if logger:
                reason = check.message
                if check.details:
                    reason = f"{check.message}: {', '.join(check.details[:5])}"
                logger.policy_violation(
                    dep_ref=check.name,
                    reason=reason,
                    severity="warn",
                    source=source,
                )

    if has_blocking:
        raise PolicyViolationError(
            "Install blocked by org policy -- see violations above",
            audit_result=audit_result,
            policy_source=source or "unknown",
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _is_policy_disabled(ctx: "InstallContext") -> bool:
    """Check escape hatches: ctx.no_policy flag and APM_POLICY_DISABLE env."""
    logger = ctx.logger

    if getattr(ctx, "no_policy", False):
        if logger:
            logger.policy_disabled("--no-policy")
        return True

    if os.environ.get("APM_POLICY_DISABLE") == "1":
        if logger:
            logger.policy_disabled("APM_POLICY_DISABLE=1")
        return True

    return False


def _read_project_fetch_failure_default(ctx: "InstallContext") -> str:
    """Resolve project-side ``policy.fetch_failure_default`` (closes #829).

    Reads from ctx attribute first (test-friendly override) then falls
    back to parsing ``<project_root>/apm.yml``. Default is ``"warn"``.
    """
    explicit = getattr(ctx, "policy_fetch_failure_default", None)
    if isinstance(explicit, str) and explicit in {"warn", "block"}:
        return explicit
    from apm_cli.policy.project_config import read_project_fetch_failure_default

    return read_project_fetch_failure_default(ctx.project_root)


def _discover_with_chain(ctx: "InstallContext"):
    """Run chain-aware discovery via the shared seam in ``discovery.py``.

    Delegates to :func:`~apm_cli.policy.discovery.discover_policy_with_chain`
    which walks the inheritance chain, merges effective policy, and persists
    the cache with real ``chain_refs``.
    """
    from apm_cli.policy.discovery import discover_policy_with_chain

    return discover_policy_with_chain(ctx.project_root)
