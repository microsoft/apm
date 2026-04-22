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

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


class PolicyViolationError(RuntimeError):
    """Raised when block-severity policy violations halt the install."""


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

    outcome = fetch_result.outcome
    logger = ctx.logger
    source = fetch_result.source

    # ------------------------------------------------------------------
    # 2. Route on outcome
    # ------------------------------------------------------------------

    # disabled -- discovery itself returned disabled (shouldn't reach
    # here from the escape-hatch above, but defensive)
    if outcome == "disabled":
        return

    # absent -- no policy published
    if outcome == "absent":
        if logger:
            from apm_cli.utils.console import _rich_info
            host_org = source.removeprefix("org:").removeprefix("url:")
            _rich_info(
                f"No org policy found for {host_org}",
                symbol="info",
            )
        ctx.policy_enforcement_active = False
        return

    # no_git_remote -- cannot determine org
    if outcome == "no_git_remote":
        if logger:
            from apm_cli.utils.console import _rich_warning
            _rich_warning(
                "Could not determine org from git remote; "
                "policy auto-discovery skipped",
                symbol="warning",
            )
        ctx.policy_enforcement_active = False
        return

    # empty -- policy present but no actionable rules
    if outcome == "empty":
        if logger:
            from apm_cli.utils.console import _rich_warning
            _rich_warning(
                "Org policy is present but empty; no enforcement applied",
                symbol="warning",
            )
        ctx.policy_enforcement_active = False
        return

    # malformed -- fail-open with loud warning (CEO mandate: matches
    # cache_miss_fetch_fail/garbage_response posture; fail-closed is a
    # follow-up via #829 with an explicit schema knob).
    if outcome == "malformed":
        if logger:
            from apm_cli.core.command_logger import InstallLogger
            reason = InstallLogger._policy_reason_malformed(source)
            from apm_cli.utils.console import _rich_warning
            _rich_warning(reason, symbol="warning")
        ctx.policy_enforcement_active = False
        return

    # cache_miss_fetch_fail / garbage_response -- loud warn, no enforce
    if outcome in ("cache_miss_fetch_fail", "garbage_response"):
        if logger:
            from apm_cli.core.command_logger import InstallLogger
            reason = InstallLogger._policy_reason_unreachable(source)
            from apm_cli.utils.console import _rich_warning
            _rich_warning(reason, symbol="warning")
        ctx.policy_enforcement_active = False
        return

    # cached_stale -- warn but STILL enforce
    if outcome == "cached_stale":
        if logger:
            age = fetch_result.cache_age_seconds
            logger.policy_resolved(
                source=source,
                cached=True,
                enforcement=fetch_result.policy.enforcement,
                age_seconds=age,
            )
            from apm_cli.utils.console import _rich_warning
            _rich_warning(
                f"Using stale cached policy (fetch failed: "
                f"{fetch_result.fetch_error or 'unknown'})",
                symbol="warning",
            )

    # found -- normal path
    if outcome == "found":
        if logger:
            logger.policy_resolved(
                source=source,
                cached=fetch_result.cached,
                enforcement=fetch_result.policy.enforcement,
                age_seconds=fetch_result.cache_age_seconds,
            )

    # ------------------------------------------------------------------
    # 3. Enforcement gate (found / cached_stale paths)
    # ------------------------------------------------------------------
    if outcome not in ("found", "cached_stale"):
        # Defensive: unrecognised outcome -- do not enforce
        ctx.policy_enforcement_active = False
        return

    policy = fetch_result.policy
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

    audit_result = run_dependency_policy_checks(
        ctx.deps_to_install,
        lockfile=ctx.existing_lockfile,
        policy=policy,
        mcp_deps=mcp_deps,
        effective_target=None,  # target-aware checks after targets phase
        fetch_outcome=fetch_result.outcome,
        fail_fast=(enforcement == "block"),
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
                )

    if has_blocking:
        raise PolicyViolationError(
            "Install blocked by org policy -- see violations above"
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


def _discover_with_chain(ctx: "InstallContext"):
    """Run chain-aware discovery via the shared seam in ``discovery.py``.

    Delegates to :func:`~apm_cli.policy.discovery.discover_policy_with_chain`
    which walks the inheritance chain, merges effective policy, and persists
    the cache with real ``chain_refs``.
    """
    from apm_cli.policy.discovery import discover_policy_with_chain

    return discover_policy_with_chain(ctx.project_root)
