"""Pre-install policy enforcement for non-pipeline command sites.

Shared helper used by:
- ``install --mcp`` branch (W2-mcp-preflight)
- ``install <pkg>`` rollback (W2-pkg-rollback) -- imports this helper
- ``install --dry-run`` preflight (W2-dry-run) -- same helper, read-only mode

When ``install/phases/policy_gate.py`` lands (W2-gate-phase), it should
delegate to :func:`run_policy_preflight` for discovery + outcome logic
rather than duplicate it.  The gate phase adds pipeline-specific wiring
(writing ``ctx.policy_fetch``, ``ctx.policy_enforcement_active``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# #832: Canonical exception type lives in ``apm_cli.install.errors``.
# ``PolicyBlockError`` remains as an alias re-exported below so external
# call sites that imported it from this module keep working.
from apm_cli.install.errors import PolicyViolationError

from .discovery import PolicyFetchResult, discover_policy_with_chain
from .outcome_routing import route_discovery_outcome
from .policy_checks import run_dependency_policy_checks

# Deprecated alias kept for backward compatibility (#832).  New code
# should ``raise``/``except`` :class:`PolicyViolationError` directly.
PolicyBlockError = PolicyViolationError


# Maximum lines to emit per severity bucket in dry-run preview.
# Overflow is collapsed into a single tail line pointing to ``apm audit``.
_DRY_RUN_PREVIEW_LIMIT = 5


@dataclass(frozen=True, slots=True)
class PreflightOpts:
    """Options for run_policy_preflight."""

    project_root: Path | None = None
    apm_deps: list | None = None
    mcp_deps: list | None = None
    no_policy: bool = False
    dry_run: bool = False


def run_policy_preflight(
    logger,
    opts: PreflightOpts | None = None,
    **kwargs,
) -> tuple[PolicyFetchResult | None, bool]:
    """Discover + enforce policy for a non-pipeline command site.

    Parameters
    ----------
    logger:
        An :class:`InstallLogger` (or any object exposing
        ``policy_disabled``, ``policy_resolved``, ``policy_violation``,
        ``warning``).
    opts:
        Optional dataclass with all parameters. When provided,
        kwargs are ignored.
    **kwargs:
        Backward-compatible parameters: project_root, apm_deps, mcp_deps,
        no_policy, dry_run.

    Returns
    -------
    (PolicyFetchResult | None, enforcement_active: bool)
        ``enforcement_active`` is ``True`` when a policy was found and
        its enforcement level is ``"warn"`` or ``"block"``.

    Raises
    ------
    PolicyViolationError
        When ``enforcement == "block"`` and at least one check fails
        **and** ``dry_run is False``.
    """
    # Resolve opts for backward compatibility
    if opts is not None:
        project_root = opts.project_root
        apm_deps = opts.apm_deps
        mcp_deps = opts.mcp_deps
        no_policy = opts.no_policy
        dry_run = opts.dry_run
    else:
        project_root = kwargs.get("project_root")
        apm_deps = kwargs.get("apm_deps")
        mcp_deps = kwargs.get("mcp_deps")
        no_policy = kwargs.get("no_policy", False)
        dry_run = kwargs.get("dry_run", False)

    if project_root is None:
        raise ValueError("project_root must be provided via opts or as keyword argument")

    # -- Escape hatches ------------------------------------------------
    if no_policy or os.environ.get("APM_POLICY_DISABLE") == "1":
        reason = "--no-policy" if no_policy else "APM_POLICY_DISABLE=1"
        logger.policy_disabled(reason)
        return None, False

    # -- Discovery (chain-aware: resolves extends: + merges) -----------
    fetch_result = discover_policy_with_chain(project_root)

    # -- Route the outcome through the shared 9-outcome table ---------
    from .project_config import read_project_fetch_failure_default

    fetch_failure_default = read_project_fetch_failure_default(project_root)

    policy = route_discovery_outcome(
        fetch_result,
        logger=logger,
        fetch_failure_default=fetch_failure_default,
        raise_blocking_errors=not dry_run,
    )

    if policy is None or policy.enforcement == "off":
        return fetch_result, False

    # -- Enforcement (warn or block) -----------------------------------
    audit_result = run_dependency_policy_checks(
        apm_deps if apm_deps is not None else [],
        lockfile=None,
        policy=policy,
        mcp_deps=mcp_deps,
        fail_fast=(policy.enforcement == "block"),
    )

    if not audit_result.passed:
        _handle_policy_violations(audit_result, fetch_result, policy.enforcement, logger, dry_run)

    return fetch_result, True


def _handle_policy_violations(
    audit_result,
    fetch_result: PolicyFetchResult,
    enforcement: str,
    logger,
    dry_run: bool,
) -> None:
    """Emit diagnostics or raise for policy violations."""
    if dry_run:
        _emit_dry_run_preview(audit_result, enforcement, logger)
    else:
        _emit_live_violations(audit_result, fetch_result, enforcement, logger)


def _emit_dry_run_preview(audit_result, enforcement: str, logger) -> None:
    """Emit capped preview per severity bucket."""
    block_lines: list[tuple[str, str]] = []
    warn_lines: list[tuple[str, str]] = []

    for check in audit_result.failed_checks:
        items = check.details or [check.name]
        for detail in items:
            dep_ref = _extract_dep_ref(detail, check.name)
            if enforcement == "block":
                block_lines.append((dep_ref, detail))
            else:
                warn_lines.append((dep_ref, detail))

    # Emit block bucket (capped)
    for dep_ref, detail in block_lines[:_DRY_RUN_PREVIEW_LIMIT]:
        logger.warning(f"Would be blocked by policy: {dep_ref} -- {detail}")
    overflow = len(block_lines) - _DRY_RUN_PREVIEW_LIMIT
    if overflow > 0:
        logger.warning(
            f"... and {overflow} more would be blocked by policy. Run `apm audit` for full report."
        )

    # Emit warn bucket (capped)
    for dep_ref, detail in warn_lines[:_DRY_RUN_PREVIEW_LIMIT]:
        logger.warning(f"Policy warning: {dep_ref} -- {detail}")
    overflow = len(warn_lines) - _DRY_RUN_PREVIEW_LIMIT
    if overflow > 0:
        logger.warning(f"... and {overflow} more policy warnings. Run `apm audit` for full report.")


def _emit_live_violations(
    audit_result,
    fetch_result: PolicyFetchResult,
    enforcement: str,
    logger,
) -> None:
    """Push each violation to DiagnosticCollector and optionally raise."""
    for check in audit_result.failed_checks:
        items = check.details or [check.name]
        for detail in items:
            dep_ref = _extract_dep_ref(detail, check.name)
            logger.policy_violation(
                dep_ref=dep_ref,
                reason=detail,
                severity="block" if enforcement == "block" else "warn",
                source=fetch_result.source,
            )

    if enforcement == "block":
        raise PolicyViolationError(
            f"Install blocked by org policy: {len(audit_result.failed_checks)} check(s) failed",
            audit_result=audit_result,
            policy_source=fetch_result.source,
        )


def _extract_dep_ref(detail: str, check_name: str) -> str:
    """Extract a dep ref from a ``CheckResult.details`` line."""
    if not detail:
        return check_name
    if ":" in detail:
        head = detail.split(":", 1)[0].strip()
        if head:
            return head
        return check_name
    return detail.strip() or check_name
