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
import sys
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

    # malformed -- fail-closed always (config bug)
    if outcome == "malformed":
        if logger:
            from apm_cli.core.command_logger import InstallLogger
            reason = InstallLogger._policy_reason_malformed(source)
            from apm_cli.utils.console import _rich_error
            _rich_error(reason, symbol="error")
        sys.exit(1)

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

    mcp_deps = getattr(ctx, "mcp_deps_to_install", None)

    audit_result = run_dependency_policy_checks(
        ctx.deps_to_install,
        lockfile=ctx.existing_lockfile,
        policy=policy,
        mcp_deps=mcp_deps,
        effective_target=None,  # target-aware checks after targets phase
        fetch_outcome=fetch_result.outcome,
    )

    # ------------------------------------------------------------------
    # 5. Route violations through logger
    # ------------------------------------------------------------------
    has_blocking = False
    for check in audit_result.checks:
        if check.passed:
            continue
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
    """Run discovery and wire resolve_policy_chain + cache with real chain_refs.

    C1 amendment: today W1 callers pass ``[repo_ref]`` only to
    ``_write_cache``.  When the fetched policy has ``extends:``, we must
    resolve the full inheritance chain and re-write the cache with the
    merged effective policy AND the real chain refs so the fingerprint
    covers parent + leaf.
    """
    from apm_cli.policy import discovery as _discovery_mod
    from apm_cli.policy import inheritance as _inheritance_mod

    fetch_result = _discovery_mod.discover_policy(
        ctx.project_root,
        policy_override=None,
        no_cache=False,
    )

    # If discovery found a valid policy with extends, resolve the chain
    # and re-persist with real chain_refs so the fingerprint covers parent+leaf.
    if (
        fetch_result.policy is not None
        and fetch_result.outcome in ("found", "cached_stale")
        and fetch_result.policy.extends is not None
        and not fetch_result.cached  # Don't re-resolve if served from cache
    ):
        # Build the chain: the policy we got is the leaf.  Fetch the parent
        # via its extends reference, then merge.
        _resolve_and_cache_chain(ctx, fetch_result)

    return fetch_result


def _resolve_and_cache_chain(ctx: "InstallContext", fetch_result) -> None:
    """Resolve inheritance chain and update cache with merged policy + chain_refs.

    Mutates ``fetch_result.policy`` in-place with the merged effective policy.
    """
    from apm_cli.policy import discovery as _discovery_mod
    from apm_cli.policy import inheritance as _inheritance_mod

    leaf_policy = fetch_result.policy
    extends_ref = leaf_policy.extends

    # Fetch the parent policy (could itself have extends)
    parent_result = _discovery_mod.discover_policy(
        ctx.project_root,
        policy_override=extends_ref,
        no_cache=False,
    )

    if parent_result.policy is None:
        # Parent fetch failed -- use leaf as-is (already cached by discovery)
        return

    # Build chain [parent, leaf] and merge
    chain = [parent_result.policy, leaf_policy]
    try:
        merged = _inheritance_mod.resolve_policy_chain(chain)
    except Exception:
        # Chain resolution failed -- use leaf as-is
        return

    # Build chain_refs from sources
    chain_refs = []
    parent_source = parent_result.source
    if parent_source:
        chain_refs.append(
            parent_source.removeprefix("org:").removeprefix("url:").removeprefix("file:")
        )
    leaf_source = fetch_result.source
    if leaf_source:
        chain_refs.append(
            leaf_source.removeprefix("org:").removeprefix("url:").removeprefix("file:")
        )

    # Re-write cache with merged effective policy + real chain_refs
    # Use the leaf's source key as the cache key
    cache_key = leaf_source.removeprefix("org:").removeprefix("url:").removeprefix("file:")
    if cache_key:
        _discovery_mod._write_cache(
            cache_key, merged, ctx.project_root, chain_refs=chain_refs
        )

    # Update the fetch result with the merged policy
    fetch_result.policy = merged
