"""Post-targets target-aware policy check phase.

Runs AFTER ``targets.run(ctx)`` when the effective target is known.
Only checks target/compilation-related policy rules -- dependency
allow/deny/required and MCP checks already ran in the policy_gate
phase and must NOT be re-emitted here.

Design reference: plan.md section G, rubber-duck finding I6.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext

# Check IDs that are target/compilation-related in
# run_dependency_policy_checks.  Only these are processed; all other
# check IDs (dep allow/deny/required, MCP) already ran in the
# policy_gate phase and must not be double-emitted.
#
# Source: policy_checks.py _check_compilation_target -> name="compilation-target"
# This is the ONLY target-related check in the dep seam.  The other
# compilation checks (strategy, source-attribution) are disk-level
# concerns handled by the full run_policy_checks wrapper in audit.
TARGET_CHECK_IDS = frozenset({"compilation-target"})


def run(ctx: InstallContext) -> None:
    """Run target-aware policy checks after the targets phase.

    Skips entirely when:
    - ``policy_enforcement_active`` is ``False`` (gate phase already
      decided no enforcement -- no policy, fail-open, escape-hatched,
      enforcement=off, etc.)
    - no policy was fetched (``policy_fetch is None``)
    - no effective target is resolved (neither ``--target`` CLI override
      nor manifest ``target:`` field)
    """
    # ------------------------------------------------------------------
    # 1. Skip if gate phase already determined no enforcement
    # ------------------------------------------------------------------
    if not ctx.policy_enforcement_active:
        return

    # ------------------------------------------------------------------
    # 2. Skip if no policy fetched or policy object is missing
    # ------------------------------------------------------------------
    if ctx.policy_fetch is None or ctx.policy_fetch.policy is None:
        return

    # ------------------------------------------------------------------
    # 3. Resolve effective target: CLI --target wins, then manifest target
    #    (mirrors targets.py:38-39 logic exactly)
    # ------------------------------------------------------------------
    config_target = getattr(ctx.apm_package, "target", None) if ctx.apm_package else None
    effective_target = ctx.target_override or config_target or None

    if effective_target is None:
        return  # no target to check -- trivially passes

    # ------------------------------------------------------------------
    # 3b. Normalize MCP-only pseudo-targets to their canonical form so
    #     policy allow-lists match (e.g. intellij -> copilot).
    #     Only MCP_ONLY_TARGETS need this -- canonical targets like
    #     'vscode' must pass through unchanged.
    # ------------------------------------------------------------------
    # Keep these lazy: target_detection and integration.targets reference each
    # other at runtime, so module-level imports would create an import cycle.
    from apm_cli.core.target_detection import MCP_ONLY_TARGETS
    from apm_cli.integration.targets import RUNTIME_TO_CANONICAL_TARGET

    if effective_target in MCP_ONLY_TARGETS:
        canonical_target = RUNTIME_TO_CANONICAL_TARGET.get(effective_target)
        if canonical_target is None:
            raise RuntimeError(
                f"MCP-only target '{effective_target}' has no canonical policy mapping"
            )
        if ctx.logger:
            ctx.logger.verbose_detail(
                f"Normalizing MCP-only target '{effective_target}' -> "
                f"'{canonical_target}' for policy check"
            )
        effective_target = canonical_target

    # ------------------------------------------------------------------
    # 4. Run policy checks with effective_target populated
    # ------------------------------------------------------------------
    from apm_cli.policy.policy_checks import run_dependency_policy_checks

    policy = ctx.policy_fetch.policy

    registries_map = getattr(ctx.apm_package, "registries", None) if ctx.apm_package else None

    audit_result = run_dependency_policy_checks(
        ctx.deps_to_install,
        lockfile=ctx.existing_lockfile,
        policy=policy,
        effective_target=effective_target,
        fetch_outcome=ctx.policy_fetch.outcome,
        fail_fast=False,  # ensure target check runs even if dep checks re-pass
        registries=registries_map,
        direct_dep_keys={d.get_unique_key() for d in getattr(ctx, "all_apm_deps", []) or []},
    )

    # ------------------------------------------------------------------
    # 5. Filter to target-related checks only -- do NOT double-emit
    #    dep-policy violations that already surfaced in the gate phase.
    # ------------------------------------------------------------------
    from apm_cli.install.phases.policy_gate import PolicyViolationError

    enforcement = policy.enforcement
    has_blocking = False

    for check in audit_result.checks:
        if check.name not in TARGET_CHECK_IDS:
            continue  # already handled by policy_gate
        if check.passed:
            continue

        severity = "block" if enforcement == "block" else "warn"
        reason = check.message
        if check.details:
            reason = f"{check.message}: {', '.join(check.details[:5])}"

        if ctx.logger:
            ctx.logger.policy_violation(
                dep_ref=check.name,
                reason=reason,
                severity=severity,
                source=getattr(getattr(ctx, "policy_fetch", None), "source", None),
            )

        if severity == "block":
            has_blocking = True

    if has_blocking:
        raise PolicyViolationError(
            "Install blocked by org policy (compilation target) -- see violations above"
        )
